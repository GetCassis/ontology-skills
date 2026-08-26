"""Shared helpers for the bootstrap kit.

Normalized corpus layout (produced by ingest.py inside a workspace dir):

  workspace/
    schema.json            # {"SCHEMA.TABLE": {"COL": "TYPE", ...}, ...}
    repo/tables.json       # per-table transformation-repo record (adapter output)
    questions.csv          # id,name,path,views,sql,tables  (saved BI questions)
    dashboard_queries.csv  # id,dashboard,card,sql,tables   (dashboard native SQL)
    query_history.csv      # id,sql,tables                  (arm B)
    docs/<id>.txt + docs/index.json
    evidence/*.json        # built by evidence.py
"""
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

# SCHEMA.TABLE references in SQL/prose. Quoted ("CORE"."USERS"), bare
# (core.users), and three-part database-qualified (TURING.core.users) — the
# last two segments are what we match against the schema.
_QUALIFIED3_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*(?P<s3>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*\.\s*(?P<t3>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_QUALIFIED_RE = re.compile(
    r'(?:"(?P<qs>[A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"(?P<qt>[A-Za-z_][A-Za-z0-9_]*)")'
    r"|(?:\b(?P<s>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*(?P<t>[A-Za-z_][A-Za-z0-9_]*)\b)"
)


DOC_BLOCK_RE = re.compile(r"{%\s*docs\s+([A-Za-z0-9_]+)\s*%}(.*?){%\s*enddocs\s*%}",
                          re.DOTALL)
DOC_REF_RE = re.compile(r"{{\s*doc\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)\s*}}")


def load_scope(path) -> set:
    """The tables a scope file marks for MODELING, upper-cased.

    Two shapes are in the wild: the TSV `scope.py propose` writes (a `table`
    header plus a `decision` column) and a bare list of `SCHEMA.TABLE`,
    optionally with a domain in column 2. Reading column 1 of every line
    handles the second and silently mis-reads the first — it counts the
    `table` header as a table and takes `read_only` and `review` rows as
    in scope, which is how the preflight coverage line reported 2183/2184
    against a 50-table scope. The header decides which shape it is, so a
    domain-annotated list is never mistaken for a decision.
    """
    lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    if not lines:
        return set()
    head = [c.strip().lower() for c in lines[0].split("\t")]
    if head[:1] == ["table"] and "decision" in head:
        di = head.index("decision")
        want = set()
        for line in lines[1:]:
            cells = [c.strip() for c in line.split("\t")]
            if len(cells) > di and cells[di].lower() != "model":
                continue
            if cells[0]:
                want.add(cells[0].upper())
        return want
    return {l.split("\t")[0].strip().upper() for l in lines
            if l.split("\t")[0].strip()}


def collect_doc_blocks(repo_root: Path) -> dict:
    """Every `{% docs name %}...{% enddocs %}` body in a dbt repo, by name.

    dbt states descriptions as indirections into these blocks. Resolving them is
    not optional polish: in source-only mode (no compiled manifest, the normal
    state of a repo read from source) they are the ONLY place the owner's own column
    documentation lives, and an unresolved ref reaches the enrichment agent as
    literal `{{ doc('x') }}` — or, worse, as nothing at all.
    """
    blocks = {}
    for md in Path(repo_root).rglob("*.md"):
        try:
            text = md.read_text(errors="replace")
        except OSError:
            continue
        for name, body in DOC_BLOCK_RE.findall(text):
            blocks[name] = body.strip()
    return blocks


def resolve_doc_refs(text, blocks: dict, missing: set = None):
    """Expand doc refs in one description. Plain text passes through unchanged."""
    if not isinstance(text, str):
        return None

    def sub(m):
        name = m.group(1)
        if name in blocks:
            return blocks[name]
        if missing is not None:
            missing.add(name)
        return ""
    return DOC_REF_RE.sub(sub, text).strip() or None


def descriptions_from_yml(repo_root: Path, blocks: dict = None) -> dict:
    """{model_name: {"description": str, "columns": {COL: str}}} read from source yml.

    Used when the adapter's inventory came back without descriptions, which is
    what happens whenever a compiled manifest is unavailable.
    """
    root = Path(repo_root)
    blocks = blocks if blocks is not None else collect_doc_blocks(root)
    out, missing = {}, set()
    for yml in list(root.rglob("*.yml")) + list(root.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(yml.read_text(errors="replace")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(doc, dict):
            continue
        # `models:` is a LIST of model specs in a schema yml, but a nested DICT of
        # per-path config in dbt_project.yml. Only the list shape carries
        # descriptions, and the dict shape is not an error worth reporting.
        entries = [e for key in ("models", "seeds")
                   for e in (doc.get(key) if isinstance(doc.get(key), list) else [])]
        for model in entries:
            if not isinstance(model, dict) or not model.get("name"):
                continue
            cols = {}
            for col in (model.get("columns") or []):
                if not isinstance(col, dict) or not col.get("name"):
                    continue
                cd = resolve_doc_refs(col.get("description"), blocks, missing)
                if cd:
                    cols[str(col["name"]).upper()] = cd
            out[model["name"]] = {
                "description": resolve_doc_refs(model.get("description"), blocks,
                                                missing) or "",
                "columns": cols,
            }
    out["__unresolved_refs__"] = sorted(missing)
    return out


# `**column_name**: description`, optionally as a list item. The shape a mkdocs
# or wiki column glossary uses.
_GLOSSARY_ENTRY_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\*\*([A-Za-z0-9_\\]+)\*\*\s*:\s*(.+?)\s*$", re.M)


def parse_column_glossary(paths, producer: str = "glossary_dir") -> dict:
    """{COLUMN_NAME: {"description", "source"}} from markdown column glossaries.

    A warehouse-wide glossary keys documentation by COLUMN NAME, not by table —
    a shape the model-keyed adapters cannot see at all. Benchmark B shipped one
    covering 1,672 of its 2,190 in-scope columns while the harness read only its
    per-model dbt export and concluded nothing had been documented at all.

    Name-keyed text is a strong default, not table-specific truth: the same column
    name can carry a different convention in another table. Callers must keep the
    provenance so an agent verifies it against that table's SQL.

    `producer` records WHO said this is a glossary, because the same parser reads
    two very different things: a file the user handed over as a column dictionary
    (`--glossary-dir`), and a file a classifier guessed was one because it had
    three `**Bold**:` lines (`--context-dir`). Only the first is a claim by a
    person, and `glossary_tier` is where that difference is spent.
    """
    out = {}
    for base in ([paths] if isinstance(paths, (str, Path)) else paths):
        base = Path(base)
        files = base.rglob("*.md") if base.is_dir() else [base]
        for f in sorted(files):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for m in _GLOSSARY_ENTRY_RE.finditer(text):
                col = m.group(1).replace("\\", "").upper()
                desc = m.group(2).replace("\\_", "_").strip()
                if col and desc and col not in out:
                    out[col] = {"description": desc, "source": str(f),
                                "producer": producer}
    return out


def fact_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def question_key(q: dict) -> str:
    """A stable id for a questions*.yml entry: its file and its title.

    Two scripts need to agree on which question a documentation candidate
    answers, and the entries carry no id of their own. The title is what a
    person reads and what a candidate is judged against, so keying on it is
    right: retitle an entry and it loses its candidate rather than inheriting
    an answer to a question it no longer asks.
    """
    return fact_id("dq", q.get("_source") or "", str(q.get("title") or ""))


def load_yaml(path: Path):
    with open(path) as f:
        return yaml.safe_load(f)


def dump_json(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def load_schema(workspace: Path) -> dict:
    """schema.json -> {"SCHEMA.TABLE": {COL: TYPE}}"""
    return load_json(workspace / "schema.json")


def sql_table_refs(sql: str, known_tables=None) -> set:
    """SCHEMA.TABLE references found in a SQL text, uppercased.

    When known_tables is given, only returns members of it — this kills the
    false positives from things like alias.column dotted pairs.
    """
    out = set()
    for m in _QUALIFIED3_RE.finditer(sql or ""):
        ref = f"{m.group('s3').upper()}.{m.group('t3').upper()}"
        if known_tables is None or ref in known_tables:
            out.add(ref)
    for m in _QUALIFIED_RE.finditer(sql or ""):
        s = (m.group("qs") or m.group("s") or "").upper()
        t = (m.group("qt") or m.group("t") or "").upper()
        ref = f"{s}.{t}"
        if known_tables is None or ref in known_tables:
            out.add(ref)
    return out


def read_csv_rows(path: Path):
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def glossary_source_token(source) -> str:
    """The entity a glossary file documents, from its filename
    ('.../column__geo_area.md' -> 'geo_area'). Empty string when unknown."""
    if not source:
        return ""
    stem = Path(str(source)).stem
    m = re.match(r"column__(.+)$", stem, re.IGNORECASE)
    return (m.group(1) if m else stem).lower()


# Both tiers a name-keyed glossary fill can land in. Anything that came from a
# glossary reads VERIFY in a packet and is re-checked by an agent, whichever tier
# it earned — the tier decides only whether it can corroborate a metric.
# Files in a repository that are instructions written FOR A MODEL, not
# documentation of anything. GitLab's public handbook ships its own CLAUDE.md and
# AGENTS.md, addressed to agents working on that repo, and ingest copied them in
# like any other page. Two reasons that is wrong and neither is tidiness: a file
# telling an agent how to work on a repo will never answer a question about a
# warehouse, and it is a prompt-injection surface — text from a client's tree,
# written to steer a model, read by ours. They stay indexed, because knowing they
# were there is worth something; they are just not documentation.
AGENT_INSTRUCTION_NAMES = frozenset({
    "claude.md", "agents.md", "gemini.md", "conventions.md",
    ".cursorrules", ".windsurfrules", ".clinerules", ".aiderrules",
    ".goosehints",
})
AGENT_INSTRUCTION_DIRS = frozenset({".cursor", ".claude", ".windsurf", ".aider",
                                    ".gemini", ".goose"})


def is_agent_instructions(rel_path) -> bool:
    """Instructions for a model, judged on POSITION as well as name.

    Position matters and the name alone is a trap: matching `agents.md` and
    `claude.md` anywhere in a tree flagged two real handbook pages — a support
    desk's page about Zendesk agents, and a page about how to use Claude. These
    files sit at the root of a repo, or inside a tool's dot-directory, by
    convention. Pass a path RELATIVE to the ingested root.
    """
    p = Path(str(rel_path))
    parts = [x.lower() for x in p.parts]
    if any(x in AGENT_INSTRUCTION_DIRS for x in parts[:-1]):
        return True
    if p.name.lower() == "copilot-instructions.md" and ".github" in parts:
        return True
    return len(parts) == 1 and p.name.lower() in AGENT_INSTRUCTION_NAMES


GLOSSARY_TIERS = ("warehouse_glossary", "inferred_glossary")


def glossary_declared_entity(source) -> str:
    """The entity a glossary file DECLARES it documents, by the `column__<entity>`
    filename convention — empty when the file makes no such claim.

    Distinct from `glossary_source_token`, which always returns something for
    display. A tier decision needs the difference: a fill can only be demoted for
    contradicting an entity claim if a claim was made. One file called
    `glossary.md` claims nothing, and demoting every fill from it would punish
    the commonest shape a real column dictionary arrives in.
    """
    if not source:
        return ""
    m = re.match(r"column__(.+)$", Path(str(source)).stem, re.IGNORECASE)
    return (m.group(1) if m else "").lower()


def glossary_tier(entry: dict, table: str, column: str) -> str:
    """Which tier a name-keyed glossary fill earns: was the match CHECKABLE.

    Two things make it not. The glossary was inferred from prose rather than
    handed over as a column dictionary — measured on a 4,726-page public
    handbook, that classifier produced 647 "column definitions", 8 reached a
    column and all 8 were wrong. Or the glossary declares an entity that is not
    this table: one warehouse's glossary defined REGION_NAME for a single
    entity and prefill sprayed that wording across 30+ tables that hold a
    different one.

    Both tiers still ship their text, and both still read `VERIFY` in the packet
    so an agent checks them. The only thing the demoted tier cannot do is
    corroborate a metric. That asymmetry is the whole point: a wrong description
    an agent then checks is a draft, and a wrong description counted as the
    warehouse owner's own words is a number nobody downstream can audit.
    """
    if (entry or {}).get("producer") != "glossary_dir":
        return "inferred_glossary"
    stem = glossary_declared_entity(entry.get("source")).rstrip("s")
    if stem and stem not in column.lower() and stem not in table.lower():
        return "inferred_glossary"
    return "warehouse_glossary"


# dbt-style layer prefixes: the part of a model name that names the LAYER,
# not the materialized table (mrt_sales__orders -> SALES_ORDERS).
_LAYER_PREFIX_RE = re.compile(r"^(STG|INT|MRT|FCT|DIM|MART|BASE)_")


def model_table_candidates(name: str) -> list:
    """Materialized-table name candidates for a transformation-repo model name,
    most-literal first. Conservative by design: strip one layer prefix and
    collapse dbt's double underscore — nothing fuzzier, because a wrong
    unambiguous-looking match is worse than an unmapped record."""
    n = str(name).upper()
    out, seen = [], set()
    stripped = _LAYER_PREFIX_RE.sub("", n)
    for cand in (n, stripped, stripped.replace("__", "_"), n.replace("__", "_")):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def map_records_to_schema(records: list, schema: dict) -> tuple:
    """Rewrite each record's `table` to the schema's key when the repo names
    models by convention rather than by materialized table name.

    Measured motivation: 35 of 427 dbtdocs records reached schema.json because
    the repo says MRT_SALES__ORDERS where the warehouse says SALES_ORDERS — so
    grains, enums, SQL and descriptions all silently missed their tables, and
    the driving agent hand-mapped ten of them before this existed.

    Mutates records in place; the original name survives as `model_table`.
    Only unambiguous mappings apply: a candidate that lands on a table already
    claimed directly, or that two records both want, maps nobody — those are
    returned as collisions for the caller to WARN about (the silent-data-loss
    doctrine: an unmapped record must be loud, never quietly dropped).
    Returns (mapped_count, collisions) where collisions is a list of
    (record_table, contested_schema_table).
    """
    keys_ci = {k.upper(): k for k in schema}
    bare = {}
    for k in schema:
        bare.setdefault(k.split(".", 1)[-1].upper(), []).append(k)
    claimed = set()
    for r in records:
        hit = keys_ci.get(str(r.get("table", "")).upper())
        if hit:
            r["table"] = hit  # normalize case on direct hits
            claimed.add(hit)

    # tier 0 = same-schema exact candidate, tier 1 = bare-name unique across
    # schemas. When two models contest one table (e.g. the int-layer
    # INT_SALES__ORDERS and the mart MRT_SALES__ORDERS both resolve to
    # SALES_ORDERS), the strictly better match wins — the mart sits in the
    # table's own schema, the int model only bare-matches from another one.
    # A tie is a real collision and maps nobody.
    proposals = {}  # record index -> (target schema key, tier)
    by_target = {}
    for i, r in enumerate(records):
        t = str(r.get("table", "")).upper()
        if t in keys_ci and keys_ci[t] in claimed:
            continue
        sch, _, name = t.rpartition(".")
        target = None
        for cand in model_table_candidates(name):
            if sch and f"{sch}.{cand}" in keys_ci:
                target = (keys_ci[f"{sch}.{cand}"], 0)
                break
            hits = bare.get(cand, [])
            if len(hits) == 1:
                target = (hits[0], 1)
                break
        if target:
            proposals[i] = target
            by_target.setdefault(target[0], []).append((target[1], i))

    mapped, collisions = 0, []
    for i, (target, tier) in proposals.items():
        rivals = by_target[target]
        best = min(t for t, _ in rivals)
        beaten = (tier > best) or sum(1 for t, _ in rivals if t == best) > 1
        if target in claimed or beaten:
            collisions.append((records[i]["table"], target))
            continue
        records[i]["model_table"] = records[i]["table"]
        records[i]["table"] = target
        mapped += 1
    return mapped, collisions


# Metric-expression matching, shared by verify.py (the grounding gate) and
# metrics_review.py (the review artifact). One definition, or the gate and the
# review file drift and stamp the same metric differently.
METRIC_CORE_RE = re.compile(
    r"(SUM|AVG|COUNT|COUNT_IF|MEDIAN|MIN|MAX)\(([A-Z0-9_.]+)\)")


def normalize_expr(e: str) -> str:
    return re.sub(r"\s+", "", (e or "").upper())


def metric_expr_cores(expression: str) -> list:
    """(fn, argument) pairs from a metric expression, normalized.

    Only simple aggregate cores are machine-matchable against the mined
    corpus; a CASE WHEN or arithmetic composite returns [] and the caller
    must say "not machine-checkable" rather than "uncorroborated".
    """
    return METRIC_CORE_RE.findall(normalize_expr(expression))
