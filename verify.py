#!/usr/bin/env python3
"""Verification gates for a generated ontology tree.

Usage:
  python3 verify.py --tree <cassis dir> --workspace <ingested workspace> \
      [--out report.json] [--judge-input judge_input.json] \
      [--only GLOB ...]

`--only` scopes the REPORT, not the gates: every gate still reads the whole
tree (identifiers need the full schema, population rules need every README),
but only findings whose file matches a given pattern are printed and written.
Patterns are fnmatch globs; a bare substring matches too. This is for an
agent checking its own files mid-run — the gates are the same ones the final
verification runs, so there is never a reason to write a proxy validator.

Gates (deterministic):
  identifiers        every table/column/grain/join/metric identifier resolves
  restatement        column descriptions that restate the column name
  hedges             hedge words in shipped prose
  prose_refs         SCHEMA.TABLE mentions in prose that don't exist
  metric_grounding   metrics carrying business synonyms whose expression has
                     no corroboration in the evidence index
  glossary_crossmatch  glossary-sourced text naming its source entity on a
                     table that is not that entity (report, not pass/fail)
  divergent_duplicates  the same source text on several columns, corrected on
                     one during the run and left verbatim on another
                     (report, not pass/fail)
  population_rules   global exclusion/filter rules stranded away from the
                     tables that need them (report, not pass/fail)

Plus: extracts (claim, source SQL) pairs for the LLM judge into
--judge-input; the judge itself is an agent step, not this script.
"""
import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (GLOSSARY_TIERS, dump_json, glossary_source_token, load_json,
                    metric_expr_cores, normalize_expr)

HEDGE_RE = re.compile(
    r"\b(likely|probably|appears to be|presumably|seems to|maybe|might be|possibly)\b",
    re.IGNORECASE)
STOP = {"the", "a", "an", "of", "for", "in", "on", "at", "is", "this", "that",
        "to", "id", "identifier", "number", "date", "time", "timestamp", "flag",
        "true", "when", "whether", "count", "total", "name", "value", "column",
        "day", "user", "member", "account", "company"}
EXCLUSION_RE = re.compile(
    r"\b(excluded?|exclude[sd]?|filter(?:ed)?\s+out|not\s+included|left\s+out)\b",
    re.IGNORECASE)
TICKET_KEYWORDS = re.compile(
    r"\b(custom week|iso week|date_trunc|utc|timezone|business\s+(?:seconds|hours|days)|"
    r"top-box|average|mean\b|sum of|excludes?\b|only counts?|derived from|"
    r"computed as|per session|per conversation|plus one|0-4|1-5|filter on)\b",
    re.IGNORECASE)
# a description that both states facts and gives usage advice can contradict
# itself (the APP_ID class: mapping says app_variant_a->fr, advice says filter on
# APP_ID). Pair these for an internal-consistency judgment.
ADVICE_RE = re.compile(
    r"\b(filter (?:on|to|by)|use\b|prefer\b|go to|instead|query\b|rely on|"
    r"when you need)\b", re.IGNORECASE)
FACT_RE = re.compile(
    r"\b(maps? to|derived from|rolls? up|corresponds? to|both\b|carries|"
    r"is computed|comes? from)\b", re.IGNORECASE)


def load_tree(tree: Path) -> dict:
    tables, metrics, unparseable = {}, {}, []

    def _read(p):
        """A file that will not parse is a gate finding, not a crash.

        Descriptions can carry French prose, and `Source : x` inside an
        unquoted scalar is valid-looking text that YAML rejects. One bad file
        must not abort the whole verification run — the parallel agents
        self-check against a tree their siblings are still editing, and would
        otherwise see a traceback about a file they do not own.
        """
        try:
            return yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            unparseable.append({"gate": "unparseable", "file": str(p),
                                "detail": str(e).split("\n")[0]})
            return None

    for p in sorted((tree / "tables").rglob("*.yml")):
        d = _read(p)
        if isinstance(d, dict) and d.get("name"):
            tables[d["name"]] = {"path": p, **d}
    for p in sorted((tree / "metrics").rglob("*.yml")):
        d = _read(p)
        if isinstance(d, dict):
            metrics[d.get("name", p.stem)] = {"path": p, **d}
    joins = []
    if (tree / "joins.yml").exists():
        joins = _read(tree / "joins.yml") or []
    readmes = {str(p.relative_to(tree / "domains")): p.read_text()
               for p in sorted((tree / "domains").rglob("README.md"))}
    return {"tables": tables, "metrics": metrics, "joins": joins,
            "readmes": readmes, "unparseable": unparseable}


# ---------- gate: identifiers ----------

def gate_identifiers(t, schema) -> list:
    findings = []
    domains = {""}
    for rel in t["readmes"]:
        d = str(Path(rel).parent)
        domains.add("" if d == "." else d)
    modeled = set()
    for name, tab in t["tables"].items():
        f = str(tab["path"])
        if name not in schema:
            findings.append({"gate": "identifiers", "file": f,
                             "detail": f"table {name} not in schema"})
            continue
        modeled.add(name)
        cols = schema[name]
        if tab.get("domain_path") not in domains:
            findings.append({"gate": "identifiers", "file": f,
                             "detail": f"domain_path {tab.get('domain_path')!r} unresolved"})
        for g in tab.get("grain") or []:
            if g not in cols:
                findings.append({"gate": "identifiers", "file": f,
                                 "detail": f"grain column {g} not in schema"})
        seen = set()
        for c in tab.get("columns") or []:
            n = c.get("name")
            if n in seen:
                findings.append({"gate": "identifiers", "file": f,
                                 "detail": f"duplicate column {n}"})
            seen.add(n)
            if n not in cols:
                findings.append({"gate": "identifiers", "file": f,
                                 "detail": f"column {n} not in schema"})
    # Bare keywords only. Function NAMES are not listed here on purpose: a
    # hand-maintained list of them is a defect generator, and it generated one —
    # an independent run hit `identifiers: COALESCE not a column`, because the
    # list happened to carry NULLIF and NULLIFZERO and not COALESCE, and the
    # person running it had to patch this file to finish. A function is
    # structurally identifiable: it is the token immediately before a `(`. That
    # is now how they are skipped, so the next warehouse's SAFE_DIVIDE, ROUND or
    # GREATEST costs nobody an afternoon.
    kw = {"DISTINCT", "NOT", "LIKE", "AND", "OR", "IN", "IS",
          "NULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END",
          # cast targets, which follow AS rather than a paren
          "NUMERIC", "DECIMAL", "FLOAT", "INT", "INTEGER", "BIGINT", "SMALLINT",
          "STRING", "VARCHAR", "CHAR", "DATE", "DATETIME", "TIMESTAMP",
          "BOOLEAN", "BOOL"}
    schema_ci = {k.upper(): k for k in schema}
    for name, m in t["metrics"].items():
        f = str(m["path"])
        key = f"{m.get('table_schema')}.{m.get('table_name')}"
        if key not in schema:
            canonical = schema_ci.get(key.upper())
            if not canonical:
                findings.append({"gate": "identifiers", "file": f,
                                 "detail": f"metric base table {key} not in schema"})
                continue
            # Resolve the case so the column check below still runs — a casing
            # slip must not buy a metric a free pass on its column identifiers.
            findings.append({"gate": "identifiers", "file": f,
                             "detail": f"metric base table {key} differs in case "
                                       f"from the schema's {canonical}"})
            key = canonical
        cols = {c.upper() for c in schema[key]}
        blob = f"{m.get('expression') or ''} {m.get('filters') or ''}"
        # Strip quoted literals first: `status = 'SUCCESS'` names a VALUE, not a
        # column, and flagging it would make the gate cry wolf on every filter.
        blob = re.sub(r"'[^']*'|\"[^\"]*\"", " ", blob)
        # Bare UPPERCASE identifiers, plus `backticked` ones in any case. The
        # backtick branch matters: a metric written SUM(`net_revenue_eur`)
        # skips the bare-word pattern, which only matches uppercase — on one
        # real run, 3 such metrics summed columns that do not exist in the
        # warehouse and nothing caught them.
        # `(?!\s*\()` drops function calls: the token before an open paren is
        # never a column reference, whatever the function is called.
        idents = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b(?!\s*\()", blob))
        idents |= {i for i in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", blob)}
        for ident in idents:
            if ident.upper() in kw or ident.upper() in cols:
                continue
            findings.append({"gate": "identifiers", "file": f,
                             "detail": f"identifier {ident} not a column of {key}"})
    for i, j in enumerate(t["joins"]):
        for side in ("from", "to"):
            key = f"{j.get(f'{side}_schema')}.{j.get(f'{side}_table')}"
            if key not in schema:
                findings.append({"gate": "identifiers", "file": "joins.yml",
                                 "detail": f"join {i}: endpoint {key} not in schema"})
        fk = f"{j.get('from_schema')}.{j.get('from_table')}"
        tk = f"{j.get('to_schema')}.{j.get('to_table')}"
        for q in j.get("column_pairs") or []:
            if fk in schema and q.get("from_column") not in schema[fk]:
                findings.append({"gate": "identifiers", "file": "joins.yml",
                                 "detail": f"join {i}: {q.get('from_column')} not in {fk}"})
            if tk in schema and q.get("to_column") not in schema[tk]:
                findings.append({"gate": "identifiers", "file": "joins.yml",
                                 "detail": f"join {i}: {q.get('to_column')} not in {tk}"})
        # `on: ["a = b"]` is the other shape a join can take (what the synthesis
        # stage emits); validate its columns too rather than trusting the writer.
        for clause in (j.get("on") or []):
            if "=" not in str(clause):
                continue
            lc, rc = [c.strip().split(".")[-1] for c in str(clause).split("=", 1)]
            for tbl, col in ((fk, lc), (tk, rc)):
                if tbl in schema and col.upper() not in {c.upper() for c in schema[tbl]}:
                    findings.append({"gate": "identifiers", "file": "joins.yml",
                                     "detail": f"join {i}: {col} not in {tbl}"})
    return findings


# ---------- gate: restatement ----------

def is_restatement(col: str, desc: str) -> bool:
    toks = set(re.findall(r"[a-z]+", desc.lower())) - STOP
    name_toks = set(re.findall(r"[a-z]+", col.lower().replace("_", " ")))
    extra = {t for t in toks - name_toks
             if not any(t.startswith(n[:6]) and len(n) > 3 for n in name_toks)}
    return len(extra) <= 1


def gate_restatement(t) -> list:
    out = []
    for name, tab in t["tables"].items():
        for c in tab.get("columns") or []:
            d = (c.get("description") or "").strip()
            if d and is_restatement(c.get("name", ""), d):
                out.append({"gate": "restatement", "file": str(tab["path"]),
                            "detail": f"{name}.{c.get('name')}: {d[:80]}"})
    return out


# ---------- gates: hedges, prose refs ----------

def gate_hedges(t, tree: Path) -> list:
    out = []
    for p in list(tree.rglob("*.yml")) + [tree / "domains" / rel for rel in t["readmes"]]:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if HEDGE_RE.search(line):
                out.append({"gate": "hedges", "file": f"{p}:{i}",
                            "detail": line.strip()[:100]})
    return out


def gate_prose_refs(t, schema) -> list:
    out = []
    ref_re = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")
    known_schemas = {k.split(".")[0] for k in schema}
    for rel, text in t["readmes"].items():
        for m in ref_re.finditer(text):
            ref = m.group(1)
            if ref.split(".")[0] in known_schemas and ref not in schema:
                out.append({"gate": "prose_refs", "file": rel,
                            "detail": f"{ref} mentioned but not in schema"})
    for name, tab in t["tables"].items():
        blob = json.dumps(tab, default=str)
        for m in ref_re.finditer(blob):
            ref = m.group(1)
            if ref.split(".")[0] in known_schemas and ref not in schema:
                out.append({"gate": "prose_refs", "file": str(tab["path"]),
                            "detail": f"{ref} mentioned but not in schema"})
    return out


# ---------- gate: glossary cross-entity wording ----------

def gate_glossary_crossmatch(t, workspace: Path) -> list:
    """Glossary-sourced text that names its source ENTITY, sitting on a table
    that is not that entity and a column that does not carry it either.

    The failure this catches, on a real run: a name-keyed glossary defined
    REGION_NAME as "the region containing the geo area" for its own entity,
    and prefill's bare-name match sprayed that wording across 30+ tables
    holding other entities — the judge caught one instance by luck; this gate
    lists the class. Advisory (a review report, not pass/fail): a foreign
    attribute column legitimately carries its home entity's wording, so a
    finding means "read it", never "delete it". Measured on that run:
    79 findings over 379 glossary fills, the bulk genuinely cross-entity.
    """
    gp = Path(workspace) / "column_glossary.json"
    if not gp.exists():
        return []
    glos = load_json(gp)
    out = []
    for name, tab in t["tables"].items():
        tname = name.split(".")[-1].upper()
        for c in tab.get("columns") or []:
            # Both glossary tiers, or the gate would go quiet on exactly the
            # fills the tier split demoted for being cross-entity.
            if c.get("description_source") not in GLOSSARY_TIERS:
                continue
            cname = str(c.get("name", "")).upper()
            tok = (c.get("description_source_detail")
                   or glossary_source_token((glos.get(cname) or {}).get("source"))
                   ).upper()
            stem = tok.rstrip("S")
            if not stem or stem in cname or stem in tname:
                continue
            # entity mentioned as words in the text (underscores flex to spaces)
            pat = "[ _]".join(re.escape(w) for w in stem.split("_"))
            text = (c.get("description") or "").upper()
            if re.search(rf"\b{pat}S?\b", text):
                out.append({"gate": "glossary_crossmatch", "file": str(tab["path"]),
                            "detail": f"{cname}: wording from the '{tok.lower()}' "
                                      f"glossary names that entity, but this table "
                                      f"is not it — confirm the framing fits or "
                                      f"rewrite (provenance stays "
                                      f"`{c.get('description_source')}`)"})
    return out


# ---------- gate: divergent duplicates ----------

def gate_divergent_duplicates(t, workspace: Path) -> list:
    """The same owner text on several columns, corrected on one during the run
    and left verbatim on another (report, not pass/fail).

    The failure this catches, on a real run: the warehouse's own
    yml described the same timezone column on two tables with one shared
    sentence, naming a source field that exists in zero of the schema export's
    12,056 rows. The
    enrichment agent that owned one table read the text against the defining
    SQL and corrected it; the agent holding the other table had that column as
    check-only and passed it as matching. Per-packet isolation means a column
    corrected in one packet and checked in another is never compared — this
    gate is that comparison, run over the whole tree. A finding means "the
    same source text was corrected elsewhere; read this copy against its own
    SQL", never "apply the other packet's edit here".
    """
    repo_path = Path(workspace) / "repo" / "tables.json"
    if not repo_path.exists():
        return []

    def norm(s):
        return " ".join(str(s or "").split())

    instances = defaultdict(list)   # normalized source text -> [(table, col)]
    for r in load_json(repo_path).get("records") or []:
        for col, txt in (r.get("column_descriptions") or {}).items():
            n = norm(txt)
            if len(n) >= 40:   # short strings ("UTC timestamp") repeat honestly
                instances[n].append((r["table"], col))

    out = []
    for src, locs in instances.items():
        if len({tb for tb, _ in locs}) < 2:
            continue
        verbatim, changed = [], []
        for tb, col in locs:
            tab = t["tables"].get(tb)
            if not tab:
                continue
            c = next((c for c in tab.get("columns") or []
                      if str(c.get("name", "")).upper() == col.upper()), None)
            if not c or not c.get("description"):
                continue
            (verbatim if norm(c["description"]) == src else changed).append(
                (tb, col, tab))
        if not (verbatim and changed):
            continue
        edited_at = ", ".join(f"{tb}.{col}" for tb, col, _ in changed)
        for tb, col, tab in verbatim:
            out.append({"gate": "divergent_duplicates", "file": str(tab["path"]),
                        "detail": f"{tb}.{col}: carries the same source text "
                                  f"that was corrected on {edited_at} during "
                                  f"this run — read this copy against its own "
                                  f"defining SQL"})
    return out


# ---------- gate: metric grounding ----------

def gate_metric_grounding(t, evidence_dir: Path) -> list:
    out = []
    cand_path = evidence_dir / "metric_candidates.json"
    corpus = set()
    if cand_path.exists():
        for c in load_json(cand_path):
            corpus.add(normalize_expr(c["expression"]))
    if not corpus:
        # No query corpus exists for this run: expression corroboration is
        # unsatisfiable. Downgrade to one advisory instead of per-metric noise;
        # repo-level corroboration (model SQL + doc block) requires review.
        n_syn = sum(1 for m in t["metrics"].values() if m.get("synonyms"))
        return [{"gate": "metric_grounding", "file": "(advisory)",
                 "detail": f"no query corpus available — {n_syn} metrics carry "
                           f"business synonyms; verify each is corroborated by "
                           f"repo SQL + docs, and confirm with whoever owns "
                           f"the definition"}]
    for name, m in t["metrics"].items():
        syns = [s for s in (m.get("synonyms") or []) if isinstance(s, str)]
        if not syns:
            continue
        # corroborated when any corpus expression shares the aggregate+argument
        core = metric_expr_cores(m.get("expression"))
        corroborated = any(f"{fn}({arg.split('.')[-1]})" in corpus or
                           f"{fn}({arg})" in corpus for fn, arg in core) if core else True
        if core and not corroborated:
            out.append({"gate": "metric_grounding", "file": str(m["path"]),
                        "detail": f"{name}: synonyms {syns} but expression "
                                  f"{m.get('expression')!r} has no corroboration "
                                  f"in the query corpus"})
    return out


# ---------- gate: population rules ----------

def gate_population_rules(t, schema, evidence_dir: Path) -> list:
    usage = {}
    up = evidence_dir / "usage.json"
    if up.exists():
        usage = load_json(up)
    # 1. collect candidate global rules from domain READMEs.
    # A rule term only counts when it is SELECTIVE: it must match columns in at
    # most MAX_CARRIER_TABLES distinct tables, else it's a generic word
    # ("account", "employee") and produces noise, not a carrier.
    # Population rules ride on flag columns: prefer terms matching a
    # BOOLEAN-typed column; fall back to any sufficiently selective term.
    MAX_CARRIER_TABLES = 4
    term_tables = defaultdict(set)   # term -> tables whose columns contain it
    term_cols = defaultdict(set)     # term -> (table, col)
    term_bool_cols = defaultdict(set)
    for tab, tcols in schema.items():
        for c, ctype in tcols.items():
            toks = [t for t in c.lower().split("_") if len(t) >= 5]
            toks.append(c.lower())  # whole column name: catches IS_TEST, TEST_GROUP
            for tok in toks:
                term_tables[tok].add(tab)
                term_cols[tok].add((tab, c))
                if "BOOL" in str(ctype).upper():
                    term_bool_cols[tok].add((tab, c))
    rules = []
    for rel, text in t["readmes"].items():
        for line in text.splitlines():
            if not EXCLUSION_RE.search(line):
                continue
            terms = set(w.lower() for w in re.findall(r"[A-Za-z_]{5,}", line))
            cols, hit_terms = set(), set()
            for term in terms:
                if term in term_bool_cols and len(term_tables[term]) <= MAX_CARRIER_TABLES:
                    cols |= term_bool_cols[term]
                    hit_terms.add(term)
            if not cols:  # fallback: any selective term
                for term in terms:
                    if term in term_tables and len(term_tables[term]) <= MAX_CARRIER_TABLES:
                        cols |= term_cols[term]
                        hit_terms.add(term)
            if cols:
                rules.append({"readme": rel, "line": line.strip()[:160],
                              "terms": sorted(hit_terms),
                              "carrier_columns": sorted(f"{t0}.{c0}" for t0, c0 in cols)})
    # 2. adjacency from the generated joins
    adj = defaultdict(set)
    for j in t["joins"]:
        a = f"{j.get('from_schema')}.{j.get('from_table')}"
        b = f"{j.get('to_schema')}.{j.get('to_table')}"
        adj[a].add(b)
        adj[b].add(a)

    def hops(src, targets, cap=3):
        seen, q = {src}, deque([(src, 0)])
        while q:
            node, d = q.popleft()
            if node in targets:
                return d
            if d >= cap:
                continue
            for nxt in adj[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, d + 1))
        return None

    out = []
    for rule in rules:
        carrier_tables = {c.rsplit(".", 1)[0] for c in rule["carrier_columns"]}
        terms = rule.get("terms") or []
        # a rule stated below root only governs its own domain subtree
        rule_domain = str(Path(rule["readme"]).parent)
        rule_domain = "" if rule_domain == "." else rule_domain
        unbound = []
        for tab_name, tab in t["tables"].items():
            if tab_name in carrier_tables:
                continue
            if rule_domain and not str(tab.get("domain_path", "")).startswith(rule_domain):
                continue
            blob = json.dumps(tab, default=str).lower()
            if any(term in blob for term in terms):
                continue  # locally bound
            d = hops(tab_name, carrier_tables)
            u = usage.get(tab_name) or {}
            unbound.append({
                "table": tab_name,
                "hops_to_carrier": d,
                "usage": (u.get("questions", 0) + u.get("dashboard_cards", 0)
                          + u.get("history", 0)),
            })
        unbound.sort(key=lambda x: -x["usage"])
        out.append({"gate": "population_rules", "file": rule["readme"],
                    "detail": rule["line"], "terms": rule.get("terms"),
                    "carrier_columns": rule["carrier_columns"],
                    # Why this line was flagged, in the finding itself: a real
                    # run burned time on hits where a plain English
                    # word ("before", "matching") coincided with a column name,
                    # and the report never said the match WAS the trigger.
                    "why": (f"flagged because the word(s) {sorted(terms)} match "
                            f"column(s) {rule['carrier_columns'][:3]} in at most "
                            f"{MAX_CARRIER_TABLES} tables schema-wide, which reads "
                            f"as a population rule those columns carry. A plain "
                            f"English word colliding with a column name triggers "
                            f"this too — if that is what happened, reword the "
                            f"prose or ignore the finding."),
                    "unbound_tables_top": unbound[:10],
                    "unbound_count": len(unbound)})
    return out


# ---------- judge input: computation claims vs source SQL ----------

def build_judge_input(t, workspace: Path) -> list:
    repo = load_json(workspace / "repo" / "tables.json")
    # records may be keyed SCHEMA.TABLE (syncer) or bare model name (dbt);
    # index by both so tree names (always SCHEMA.TABLE) resolve either way.
    sql_by_table = {}
    for r in repo["records"]:
        sql = r.get("sql") or ""
        sql_by_table.setdefault(r["table"], sql)
        if "." not in r["table"]:
            continue
    for r in repo["records"]:
        sql = r.get("sql") or ""
        bare = r["table"].split(".")[-1]
        sql_by_table.setdefault(bare, sql)

    def _lookup(name):
        return sql_by_table.get(name) or sql_by_table.get(name.split(".")[-1], "")
    pairs = []
    for name, tab in t["tables"].items():
        sql = _lookup(name)
        for c in tab.get("columns") or []:
            desc = c.get("description") or ""
            if not desc:
                continue
            col = c.get("name", "")
            if TICKET_KEYWORDS.search(desc):
                snippet = ""
                if sql:
                    lines = sql.splitlines()
                    for i, line in enumerate(lines):
                        if re.search(rf"\bAS\s+{re.escape(col)}\b", line, re.IGNORECASE):
                            snippet = "\n".join(lines[max(0, i - 3):i + 2])
                            break
                pairs.append({"table": name, "column": col, "claim": desc.strip(),
                              "check": "sql",
                              "keyword": TICKET_KEYWORDS.search(desc).group(0),
                              "source_sql_snippet": snippet or "(no defining SQL line found)",
                              "sql_available": bool(sql)})
            if ADVICE_RE.search(desc) and FACT_RE.search(desc) and len(desc) > 120:
                pairs.append({"table": name, "column": col, "claim": desc.strip(),
                              "check": "internal",
                              "keyword": ADVICE_RE.search(desc).group(0),
                              "source_sql_snippet":
                                  "(internal-consistency: judge whether the guidance in "
                                  "this claim contradicts facts stated in the same claim)",
                              "sql_available": False})
    for name, m in t["metrics"].items():
        desc = f"{m.get('description') or ''} {m.get('notes') or ''}"
        if TICKET_KEYWORDS.search(desc):
            key = f"{m.get('table_schema')}.{m.get('table_name')}"
            pairs.append({"table": key, "column": f"metric:{name}",
                          "claim": desc.strip()[:500],
                          "keyword": TICKET_KEYWORDS.search(desc).group(0),
                          "source_sql_snippet": f"expression: {m.get('expression')} | "
                                                f"filters: {m.get('filters')}",
                          "sql_available": True})
    return pairs


def _only_match(file_str: str, patterns) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(file_str, p) or fnmatch(file_str, f"*{p}*")
               for p in patterns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--judge-input", type=Path)
    ap.add_argument("--only", action="append", metavar="GLOB", default=None,
                    help="report only findings whose file matches (fnmatch "
                         "glob; a bare substring matches too); repeatable. "
                         "The gates still read the whole tree — this scopes "
                         "the report, for an agent checking its own files "
                         "while siblings are still editing theirs.")
    args = ap.parse_args()

    schema = load_json(args.workspace / "schema.json")
    t = load_tree(args.tree)
    ev = args.workspace / "evidence"

    report = {}
    report["identifiers"] = gate_identifiers(t, schema)
    report["restatement"] = gate_restatement(t)
    report["hedges"] = gate_hedges(t, args.tree)
    report["prose_refs"] = gate_prose_refs(t, schema)
    report["metric_grounding"] = gate_metric_grounding(t, ev)
    report["glossary_crossmatch"] = gate_glossary_crossmatch(t, args.workspace)
    report["divergent_duplicates"] = gate_divergent_duplicates(t, args.workspace)
    report["population_rules"] = gate_population_rules(t, schema, ev)

    if args.only:
        report = {g: [f for f in findings
                      if _only_match(str(f.get("file", "")), args.only)]
                  for g, findings in report.items()}

    for gate, findings in report.items():
        print(f"{gate}: {len(findings)}")
    if args.out:
        dump_json(report, args.out)
        print(f"report -> {args.out}")
    if args.judge_input:
        pairs = build_judge_input(t, args.workspace)
        if args.only:
            def _pair_file(p):
                tab = t["tables"].get(p["table"])
                return str(tab["path"]) if tab else ""
            pairs = [p for p in pairs
                     if _only_match(p["table"], args.only)
                     or _only_match(_pair_file(p), args.only)]
        dump_json(pairs, args.judge_input)
        print(f"judge input: {len(pairs)} claim/SQL pairs -> {args.judge_input}")


if __name__ == "__main__":
    main()
