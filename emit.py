#!/usr/bin/env python3
"""Emit the run's ontology in a shippable form. One flag, two targets.

  python3 emit.py --emit cassis --run <run> --workspace <ws>
  python3 emit.py --emit dbt    --run <run> --workspace <ws> --dbt-project <root>

Both write to `<run>/emit/`; the dbt target also writes into the dbt project.
Neither touches the working tree at `<run>/cassis/`, and neither touches
QUESTIONS.md — that file is a deliverable for a person, not for a parser.

`cassis` — the ontology tree, the standalone result. It is also the format
`cassis ontology upload` imports, for anyone who later wants that; nothing here
requires it. The working tree is NOT that format, measured against cassis-cli
1.5.0: four things differ.

  * a table file keys itself `name: SCHEMA.TABLE`; canonical splits it into
    `schema_name` + `table_name`
  * columns carry `description_source` / `description_source_detail`, which the
    import reports as fields it would drop
  * a metric has no `display_name`, which the import requires
  * a domain README has neither the `type: Domain` frontmatter nor the
    generated `cassis:nav` region, and a stale nav region fails the check

Emitted beside the working tree rather than over it, because `verify.py`,
`metrics_review.py` and `dispatch_prep.py` all read the provenance keys:
canonicalizing in place would silently restamp a metric on the next re-run.
The stripped provenance lands in `<run>/emit/provenance.json`, so it is moved,
never dropped.

`dbt` — merges the ontology back into the input dbt project's own yml files.
A projection, not a migration: the ontology tree stays beside it and is the
full-fidelity artifact. The merge is additive and idempotent — it never
overwrites a description the project already has, never reorders or removes
anything it does not own, and a second run produces no diff. What dbt has no
field for goes under one namespace, `meta.cassis.*`, so it stays greppable.
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dump_json, load_json  # noqa: E402
from verify import load_tree  # noqa: E402

KIT = Path(__file__).resolve().parent

PROVENANCE_KEYS = ("description_source", "description_source_detail")
CANON_COLUMN_KEYS = ("name", "description", "unit", "synonyms", "data_type",
                     "nullable", "ordinal")
CANON_TABLE_KEYS = ("schema_name", "table_name", "domain_path", "description",
                    "synonyms", "grain", "columns", "sql")
CANON_METRIC_KEYS = ("name", "display_name", "domain_path", "table_schema",
                     "table_name", "expression", "filters", "unit", "synonyms",
                     "description", "precomputed_in")
CARDINALITIES = ("one_to_one", "one_to_many", "many_to_one", "many_to_many")

NAV_BEGIN = "<!-- cassis:nav:begin (generated, do not edit) -->"
NAV_END = "<!-- cassis:nav:end -->"
NAV_RE = re.compile(re.escape(NAV_BEGIN) + r".*?" + re.escape(NAV_END),
                    re.DOTALL)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

TEMPORAL_TYPES = ("TIMESTAMP", "DATETIME", "DATE", "TIMESTAMP_NTZ",
                  "TIMESTAMP_TZ", "TIMESTAMP_LTZ")
TEMPORAL_NAME_RE = re.compile(r"_(AT|DATE|TS|TIME|DATETIME|ON|DAY|MONTH)$")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- shared reads

def read_working_tree(run: Path) -> dict:
    tree_dir = run / "cassis"
    if not tree_dir.exists():
        die(f"no tree at {tree_dir} — run the build phase first")
    t = load_tree(tree_dir)
    if t["unparseable"]:
        for u in t["unparseable"]:
            print(f"  YAML ERROR: {u['file']}: {u['detail']}")
        die(f"{len(t['unparseable'])} file(s) will not parse; emitting from a "
            f"partially-read tree would ship a partial ontology silently")
    return t


def schema_case(schema: dict, table: str, column: str) -> str:
    """The column's PHYSICAL casing, from the schema export.

    Join clauses arrive in whatever case the source used — a dbt relationships
    test says `order_id` where the warehouse says `ORDER_ID` — and a Cassis
    identifier is a physical identifier, not a display name.
    """
    col = str(column).split(".")[-1].strip().strip('"')
    for known in (schema.get(table) or {}):
        if known.upper() == col.upper():
            return known
    return col


# ------------------------------------------------------------- emit: canonical

def display_name_for(name: str) -> str:
    s = str(name).replace("_", " ").strip()
    return (s[0].upper() + s[1:]) if s else str(name)


def canonical_table(key: str, tab: dict, schema: dict, dropped: Counter,
                    provenance: dict) -> dict:
    sch, name = key.split(".", 1)
    out = {"schema_name": sch, "table_name": name,
           "domain_path": tab.get("domain_path")}
    for k in ("description", "synonyms", "grain", "sql"):
        if tab.get(k):
            out[k] = tab[k]
    if out.get("grain"):
        out["grain"] = [schema_case(schema, key, g) for g in out["grain"]]
    types = schema.get(key) or {}
    cols = []
    for c in tab.get("columns") or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        cname = schema_case(schema, key, c["name"])
        oc = {"name": cname}
        for k in ("description", "unit", "synonyms"):
            if c.get(k):
                oc[k] = c[k]
        if types.get(cname):
            oc["data_type"] = types[cname]
        src = {k: c[k] for k in PROVENANCE_KEYS if c.get(k)}
        if src:
            provenance.setdefault(key, {})[cname] = src
        for k in c:
            if k not in CANON_COLUMN_KEYS and k not in PROVENANCE_KEYS:
                dropped[f"columns[].{k}"] += 1
        cols.append(oc)
    out["columns"] = cols
    for k in tab:
        if k in ("path", "name"):
            continue
        if k not in CANON_TABLE_KEYS:
            dropped[k] += 1
    return out


def canonical_metric(name: str, m: dict, tables: dict, dropped: Counter) -> dict:
    out = {"name": name,
           "display_name": m.get("display_name") or display_name_for(name)}
    tkey = f"{m.get('table_schema')}.{m.get('table_name')}"
    dpath = m.get("domain_path") or (tables.get(tkey) or {}).get("domain_path")
    if dpath:
        out["domain_path"] = dpath
    for k in ("table_schema", "table_name", "expression", "filters", "unit",
              "synonyms", "description", "precomputed_in"):
        if m.get(k):
            out[k] = m[k]
    # `notes` is what some enrichment passes called the human half of a caveat.
    # Fold it into the description rather than drop it: description is the only
    # field every consumer reads.
    extra = " ".join(str(m[k]).strip() for k in ("notes", "caveats")
                     if m.get(k))
    if extra:
        out["description"] = " ".join(x for x in (out.get("description"),
                                                  extra) if x)
    for k in m:
        if k in ("path", "notes", "caveats"):
            continue
        if k not in CANON_METRIC_KEYS:
            dropped[f"metrics.{k}"] += 1
    return out


def canonical_join(j: dict, schema: dict, dropped: Counter):
    fk = f"{j.get('from_schema')}.{j.get('from_table')}"
    tk = f"{j.get('to_schema')}.{j.get('to_table')}"
    pairs = []
    for entry in (j.get("column_pairs") or []):
        if isinstance(entry, dict) and entry.get("from_column"):
            pairs.append({"from_column": schema_case(schema, fk,
                                                     entry["from_column"]),
                          "to_column": schema_case(schema, tk,
                                                   entry.get("to_column", ""))})
    for clause in (j.get("on") or []):
        if "=" not in str(clause):
            continue
        left, right = [c.strip() for c in str(clause).split("=", 1)]
        pairs.append({"from_column": schema_case(schema, fk, left),
                      "to_column": schema_case(schema, tk, right)})
    if not pairs:
        return None
    fs, ft = fk.split(".", 1)
    ts, tt = tk.split(".", 1)
    out = {"from_schema": fs, "from_table": ft, "to_schema": ts, "to_table": tt,
           "column_pairs": pairs,
           "condition_sql": " AND ".join(
               f'"{fs}"."{ft}"."{p["from_column"]}" = '
               f'"{ts}"."{tt}"."{p["to_column"]}"' for p in pairs)}
    card = j.get("cardinality") or j.get("relationship")
    if card in CARDINALITIES:
        out["cardinality"] = card
    elif card:
        dropped["joins.cardinality (not one of the four values)"] += 1
    if j.get("description"):
        out["description"] = j["description"]
    return out


def nav_block(depth: int, tables: list, metrics: list) -> str:
    up = "../" * (depth + 1)
    lines = [NAV_BEGIN, ""]
    if tables:
        lines.append("## Tables")
        for key in sorted(tables):
            sch, name = key.split(".", 1)
            lines.append(f"- [{key}]({up}tables/{sch}/{name}.yml)")
        lines.append("")
    if metrics:
        lines.append("## Metrics")
        # by metric NAME, which is the file name — not by the label, even though
        # the label is what the line shows. Measured against `cassis ontology
        # fmt` on three real trees: sorting by label put 4 of 31 domain READMEs
        # out of date, and the fixture could not see it because its two orders
        # happen to coincide.
        for fname, label in sorted(metrics):
            lines.append(f"- [{label}]({up}metrics/{fname}.yml)")
    while lines and not lines[-1]:
        lines.pop()
    lines.append(NAV_END)
    return "\n".join(lines)


def domain_readme(rel: str, text: str, tables: list, metrics: list) -> str:
    """Canonical domain README: frontmatter, the reviewed prose, then the
    generated nav region. Idempotent — an existing frontmatter or nav region is
    replaced, not stacked."""
    body = NAV_RE.sub("", text or "").strip()
    fm = {}
    m = FRONTMATTER_RE.match(body)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = body[m.end():].strip()
    dpath = str(Path(rel).parent)
    dpath = "" if dpath == "." else dpath
    h1 = re.match(r"#\s+(.+)", body)
    title = fm.get("title") or (h1.group(1).strip() if h1 else None) \
        or (display_name_for(Path(dpath).name) if dpath else "Ontology")
    desc = fm.get("description")
    if not desc:
        prose = NAV_RE.sub("", body)
        prose = re.sub(r"^#.*$", "", prose, flags=re.M).strip()
        first = re.split(r"(?<=[.!?])\s|\n\n", prose)[0] if prose else ""
        desc = " ".join(first.split())[:280] or f"{title} domain."
    front = yaml.safe_dump({"type": "Domain", "title": title,
                            "description": desc},
                           sort_keys=False, allow_unicode=True, width=4096)
    head = ["---", front.rstrip(), "---", ""]
    parts = ["\n".join(head)]
    if body:
        parts.append(body + "\n")
    if tables or metrics:
        depth = len(Path(dpath).parts) if dpath else 0
        parts.append(nav_block(depth, tables, metrics))
    return "\n".join(parts).rstrip() + "\n"


def emit_cassis(run: Path, tree: dict, schema: dict) -> int:
    out_root = run / "emit" / "cassis"
    dropped, provenance = Counter(), {}

    unassigned = [k for k, t in tree["tables"].items()
                  if not t.get("domain_path")
                  or t.get("domain_path") == "UNASSIGNED"]
    if unassigned:
        die(f"{len(unassigned)} table(s) still UNASSIGNED "
            f"({unassigned[:5]}) — the domain tree has to be applied before "
            f"the ontology can be emitted")

    n_t = 0
    for key, tab in sorted(tree["tables"].items()):
        rec = canonical_table(key, tab, schema, dropped, provenance)
        p = out_root / "tables" / rec["schema_name"] / f"{rec['table_name']}.yml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True,
                                    width=4096))
        n_t += 1

    metrics = {}
    for name, m in sorted(tree["metrics"].items()):
        rec = canonical_metric(name, m, tree["tables"], dropped)
        metrics[name] = rec
        p = out_root / "metrics" / f"{name}.yml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True,
                                    width=4096))

    joins = [cj for cj in (canonical_join(j, schema, dropped)
                           for j in (tree["joins"] or [])
                           if isinstance(j, dict)) if cj]
    (out_root).mkdir(parents=True, exist_ok=True)
    (out_root / "joins.yml").write_text(
        yaml.safe_dump(joins, sort_keys=False, allow_unicode=True, width=4096)
        if joins else "[]\n")

    # domain READMEs, with the nav region generated from where things landed
    by_domain_t, by_domain_m = {}, {}
    for key, tab in tree["tables"].items():
        by_domain_t.setdefault(tab["domain_path"], []).append(key)
    for name, rec in metrics.items():
        by_domain_m.setdefault(rec.get("domain_path", ""), []).append(
            (name, rec["display_name"]))
    if not tree["readmes"]:
        die("no domain README under cassis/domains/ — the domain tree is the "
            "one thing the ontology cannot be emitted without")
    known = set()
    for rel in tree["readmes"]:
        d = str(Path(rel).parent)
        known.add("" if d == "." else d)
    missing = sorted(d for d in by_domain_t if d not in known)
    if missing:
        die(f"{len(missing)} domain path(s) have tables but no README: "
            f"{missing[:5]} — write the domain prose before emitting")
    if "" not in known:
        die("cassis/domains/README.md (the root domain) is missing")

    n_d = 0
    for rel, text in sorted(tree["readmes"].items()):
        d = str(Path(rel).parent)
        d = "" if d == "." else d
        p = out_root / "domains" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(domain_readme(rel, text, by_domain_t.get(d, []),
                                   by_domain_m.get(d, [])))
        n_d += 1

    if provenance:
        dump_json(provenance, run / "emit" / "provenance.json")

    # The reader guide travels with the tree. Without cassis-cli there is no
    # AGENTS.md in the output, so an ontology handed to anyone else is a
    # directory of YAML nobody has been told how to read. Copied, not
    # generated: it describes the format, which does not vary by run.
    guide = KIT / "templates" / "output-CLAUDE.md"
    if guide.exists():
        (run / "emit" / "CLAUDE.md").write_text(guide.read_text())

    print(f"ontology tree: {n_t} tables, {len(metrics)} metrics, "
          f"{len(joins)} joins, {n_d} domain READMEs -> {out_root}")
    if provenance:
        print(f"  description provenance moved out of the shipped tree "
              f"(Cassis would reject the fields) -> emit/provenance.json")
    if dropped:
        print("  fields dropped as not part of the Cassis format — check none "
              "of these carried content:")
        for k, n in dropped.most_common():
            print(f"    {n:4}  {k}")
    if guide.exists():
        print(f"  how to read it, for whoever you hand it to -> "
              f"emit/CLAUDE.md")
    # No "now validate it with our CLI" line. That check needs an account and
    # a network call, and a standalone kit's happy path does not run through
    # our API. It is a test-time postcondition of THIS repo (tests/test_kit.py,
    # skipped without a key), never a step in someone's run: verify.py is what
    # checks the tree, and it needs nothing.
    return 0


# ------------------------------------------------------------------ emit: dbt

_AGG_RE = re.compile(
    r"^(SUM|AVG|AVERAGE|COUNT|COUNT_IF|COUNTIF|MEDIAN|MIN|MAX)"
    r"\(\s*(DISTINCT\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\)$", re.IGNORECASE)
_AGG_MAP = {"SUM": "sum", "AVG": "average", "AVERAGE": "average",
            "COUNT": "count", "MEDIAN": "median", "MIN": "min", "MAX": "max",
            "COUNT_IF": "sum_boolean", "COUNTIF": "sum_boolean"}


def _ruamel():
    try:
        from ruamel.yaml import YAML
    except ImportError:
        die("the dbt export needs ruamel.yaml (pip install -r "
            "requirements.txt). PyYAML cannot round-trip somebody else's "
            "schema.yml — it drops every comment in the file.")
    return YAML()


def _reader(text: str):
    """A round-trip YAML configured to match this file's own indentation, so a
    merge shows up as the lines we added and nothing else."""
    y = _ruamel()
    y.preserve_quotes = True
    y.width = 4096
    # [ \t] and not \s: \s matches newlines, so a blank line above the key ends
    # up captured as indentation and every sequence lands one column short.
    m = re.search(r"^([ \t]*)[A-Za-z_][\w-]*:[ \t]*\r?\n([ \t]*)-[ \t]",
                  text or "", re.M)
    if m and len(m.group(2)) > len(m.group(1)):
        off = len(m.group(2)) - len(m.group(1))
        y.indent(mapping=2, sequence=off + 2, offset=off)
    else:
        y.indent(mapping=2, sequence=2, offset=0)
    return y


def _dump(y, data, path: Path):
    import io
    buf = io.StringIO()
    y.dump(data, buf)
    text = buf.getvalue()
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def _project_yml_files(root: Path):
    skip = {"target", "dbt_packages", "logs", ".git", "venv", ".venv"}
    for p in sorted(list(root.rglob("*.yml")) + list(root.rglob("*.yaml"))):
        if set(p.relative_to(root).parts) & skip:
            continue
        if p.name in ("dbt_project.yml", "packages.yml", "profiles.yml",
                      "dependencies.yml", "selectors.yml"):
            continue
        yield p


def _cassis_meta(node):
    """`meta.cassis`, created if absent. Ours to overwrite; everything else
    under `meta` is theirs and is left exactly as it is."""
    meta = node.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        node["meta"] = meta
    block = meta.get("cassis")
    if not isinstance(block, dict):
        block = {}
        meta["cassis"] = block
    return block


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _tests_list(entry, fallback_key):
    """The column's OWN test list, whichever of the two spellings it uses.

    dbt errors on a column that has both `tests` and `data_tests`, so appending
    under the project's dominant key is wrong the moment one column disagrees
    with the rest of the project. Only a column with no list at all gets the
    fallback.
    """
    for k in ("data_tests", "tests"):
        if isinstance(entry.get(k), list):
            return entry[k]
    entry[fallback_key] = []
    return entry[fallback_key]


def _dbt_join_meta(j: dict, tree: dict, from_rec: dict, to_rec: dict,
                   model_case) -> dict:
    """What the `relationships` test beside it cannot carry: the cardinality,
    and the grain the join resolves to on the other side."""
    to_key = f"{j['to_schema']}.{j['to_table']}"

    def case_of(rec, col):
        return model_case(rec, col) if rec else col

    # `column_pairs`, the same key the Cassis join file uses — and never `on`,
    # which YAML 1.1 reads as the boolean True, so PyYAML (dbt's own loader)
    # would hand a reader `True:` where we wrote `on:`.
    out = {"to": f"{to_key} (ref('{to_rec['dbt_model']}'))" if to_rec
           else to_key,
           "column_pairs": [
               {"from_column": case_of(from_rec, p["from_column"]),
                "to_column": case_of(to_rec, p["to_column"])}
               for p in j["column_pairs"]]}
    if j.get("cardinality"):
        out["cardinality"] = j["cardinality"]
    grain = (tree["tables"].get(to_key) or {}).get("grain") or []
    if grain:
        out["to_grain"] = [case_of(to_rec, g) for g in grain]
    return out


def emit_dbt(run: Path, workspace: Path, tree: dict, schema: dict,
             project: Path) -> int:
    # resolve(): the adapter records paths through whatever /tmp or symlinked
    # root the project sits under, and a mismatch here reads as "that file is
    # not in the project" for a file that plainly is.
    project = Path(project).resolve()
    if not (project / "dbt_project.yml").exists():
        die(f"{project} is not a dbt project (no dbt_project.yml)")
    repo = load_json(workspace / "repo" / "tables.json")
    if repo.get("adapter") != "dbt":
        die(f"this run ingested with the {repo.get('adapter')!r} adapter, so "
            f"there is no dbt project to write back into. The dbt export "
            f"needs a run whose input WAS a dbt project.")
    # A dbt record names a MODEL, and the ontology names a warehouse table.
    # ingest.py rewrites the record's `table` to the schema key when it can, but
    # a record it could not map unambiguously keeps its bare model name — which
    # is the normal state of a real run (measured: 176 of 176 records still
    # bare on one of them).
    # Keying on the schema-qualified name alone therefore matched nothing at all
    # and exported an empty merge. Same key-format family as the `grains.json`
    # bug in skeleton.py, so the same conservative fallback: bare name, and only
    # when it is unique.
    records, bare_records = {}, {}
    for r in repo.get("records") or []:
        if not r.get("dbt_model"):
            continue
        key = str(r["table"]).upper()
        records[key] = r
        bare_records.setdefault(key.rpartition(".")[2], []).append(r)
    contested = []

    def record_for(table_key: str):
        hit = records.get(table_key.upper())
        if hit:
            return hit
        rivals = bare_records.get(table_key.rpartition(".")[2].upper(), [])
        if len(rivals) == 1:
            return rivals[0]
        if rivals:
            contested.append((table_key, [r["dbt_model"] for r in rivals]))
        return None
    y_plain = yaml.safe_load((project / "dbt_project.yml").read_text()) or {}
    model_paths = y_plain.get("model-paths") or ["models"]

    # Where each model is already patched. dbt refuses two patches for one
    # model, so this map decides "edit" vs "create" — guessing would be a
    # duplicate-patch parse error in their project, not ours.
    docs, entries, seq_used = {}, {}, Counter()
    for p in _project_yml_files(project):
        text = p.read_text()
        y = _reader(text)
        try:
            data = y.load(text)
        except Exception as e:                       # their file, their syntax
            print(f"  skipped (will not parse): {p.relative_to(project)}: "
                  f"{str(e).splitlines()[0][:90]}")
            continue
        if not isinstance(data, dict):
            continue
        docs[p] = (y, data)
        for key in ("models", "seeds", "snapshots"):
            for i, node in enumerate(data.get(key) or []):
                if isinstance(node, dict) and node.get("name"):
                    entries.setdefault(str(node["name"]), (p, key, i))
        for node in (data.get("models") or []):
            for c in (node.get("columns") or []) if isinstance(node, dict) else []:
                if not isinstance(c, dict):
                    continue
                for k in ("data_tests", "tests"):
                    for t in (c.get(k) or []):
                        seq_used[k] += 1
                        if isinstance(t, dict):
                            body = next(iter(t.values()), None)
                            if isinstance(body, dict):
                                seq_used["arguments" if "arguments" in body
                                         else "top_level_args"] += 1
    # Both of these mirror the project rather than pick a favorite. dbt 1.10
    # deprecates `tests:` and top-level generic-test arguments, but the modern
    # spelling of either is a hard error on an older dbt — and we do not know
    # which dbt they run, only what their own files already say.
    #
    # This is only the fallback for a column that has NO test list yet. Which
    # key an existing column uses is decided per column, by _tests_list: a real
    # project mixes both spellings, and dbt rejects a column carrying `tests`
    # and `data_tests` at once — measured on a 181-model project, where a
    # project-wide choice added `data_tests` beside their `tests` and broke
    # `dbt parse` outright.
    tests_key = "tests" if seq_used["tests"] and not seq_used["data_tests"] \
        else "data_tests"
    args_nested = bool(seq_used["arguments"]) and not seq_used["top_level_args"]

    joins_by_table = {}
    for j in (tree["joins"] or []):
        if not isinstance(j, dict):
            continue
        cj = canonical_join(j, schema, Counter())
        if cj:
            joins_by_table.setdefault(
                f"{cj['from_schema']}.{cj['from_table']}", []).append(cj)

    report = {"models": [], "descriptions_written": 0, "descriptions_kept": 0,
              "columns_added": 0, "relationships_tests_added": 0,
              "columns_declared_twice": [],
              "metrics_exported": [], "metrics_not_exported": [],
              "tables_without_a_dbt_model": [], "tests_key": tests_key}
    touched, created = set(), set()

    def model_case(rec, col):
        """The project's own casing for a column, so a merge does not introduce
        a second spelling of the same column."""
        sql = rec.get("sql") or ""
        m = re.search(r"\b" + re.escape(col) + r"\b", sql, re.IGNORECASE)
        return m.group(0) if m else col.lower()

    for key, tab in sorted(tree["tables"].items()):
        rec = record_for(key)
        if not rec:
            report["tables_without_a_dbt_model"].append(key)
            continue
        model = rec["dbt_model"]
        if model in entries:
            p, section, idx = entries[model]
            y, data = docs[p]
            node = data[section][idx]
        else:
            # Locate the model's own directory inside THIS project rather than
            # trusting the path the workspace recorded: a run is re-exportable
            # against a fresh checkout, and an absolute path from another one
            # would write the new yml outside the project entirely.
            hits = [q for q in project.rglob(f"{model}.sql")
                    if not (set(q.relative_to(project).parts)
                            & {"target", "dbt_packages"})]
            base = hits[0].parent if hits else (project / model_paths[0])
            p = base / "schema.yml"
            if p in docs:
                y, data = docs[p]
            else:
                y = _reader("models:\n  - name: x\n")
                data = y.load("version: 2\n\nmodels: []\n")
                docs[p] = (y, data)
                created.add(p)
            if not isinstance(data.get("models"), list):
                data["models"] = []
            node = {"name": model}
            data["models"].append(node)
            entries[model] = (p, "models", len(data["models"]) - 1)
        touched.add(p)

        if tab.get("description"):
            if _blank(node.get("description")):
                node["description"] = tab["description"]
                report["descriptions_written"] += 1
            else:
                report["descriptions_kept"] += 1
        block = _cassis_meta(node)
        block["domain"] = tab["domain_path"]
        if tab.get("synonyms"):
            block["synonyms"] = list(tab["synonyms"])
        if tab.get("grain"):
            block["grain"] = [model_case(rec, schema_case(schema, key, g))
                              for g in tab["grain"]]
        if tab.get("caveats"):
            block["caveats"] = tab["caveats"]

        existing = {}
        if not isinstance(node.get("columns"), list):
            if tab.get("columns"):
                node["columns"] = []
            else:
                report["models"].append(model)
                continue
        # A column can be declared MORE THAN ONCE in one model's yml — dbt
        # warns and carries on, and a real 2,900-model project does it. Keep
        # every declaration: writing to one of them while a sibling already
        # carries the same test produces two tests with one generated name,
        # which IS a hard dbt error (measured on a real project).
        for c in node["columns"]:
            if isinstance(c, dict) and c.get("name"):
                existing.setdefault(str(c["name"]).upper(), []).append(c)
        for col in (tab.get("columns") or []):
            cname = schema_case(schema, key, col.get("name", ""))
            if not cname:
                continue
            has_content = any(col.get(k) for k in
                              ("description", "synonyms", "unit", "caveats"))
            cjoins = [j for j in joins_by_table.get(key, [])
                      if any(p2["from_column"].upper() == cname.upper()
                             for p2 in j["column_pairs"])]
            if not has_content and not cjoins:
                continue
            declared = existing.get(cname.upper()) or []
            if len(declared) > 1:
                report["columns_declared_twice"].append(f"{model}.{cname}")
            entry = declared[0] if declared else None
            if entry is None:
                entry = {"name": model_case(rec, cname)}
                node["columns"].append(entry)
                existing[cname.upper()] = [entry]
                declared = [entry]
                report["columns_added"] += 1
            if col.get("description"):
                if _blank(entry.get("description")):
                    entry["description"] = col["description"]
                    report["descriptions_written"] += 1
                else:
                    report["descriptions_kept"] += 1
            cblock = None
            for k in ("synonyms", "unit", "caveats"):
                if col.get(k):
                    cblock = cblock if cblock is not None else \
                        _cassis_meta(entry)
                    cblock[k] = col[k]
            if cjoins:
                cblock = cblock if cblock is not None else \
                    _cassis_meta(entry)
                # A list: one column can carry more than one join, and a
                # singular key holding one of two is how content goes missing.
                # Column references use the PROJECT's casing throughout this
                # block — the reader here is a dbt user grepping their own repo.
                cblock["join"] = [
                    _dbt_join_meta(
                        j, tree, rec,
                        record_for(f"{j['to_schema']}.{j['to_table']}"),
                        model_case)
                    for j in cjoins]
                for j in cjoins:
                    to_key = f"{j['to_schema']}.{j['to_table']}"
                    to_rec = record_for(to_key)
                    if not to_rec:
                        continue
                    pair = next(p2 for p2 in j["column_pairs"]
                                if p2["from_column"].upper() == cname.upper())
                    field = model_case(to_rec, pair["to_column"])
                    tests = _tests_list(entry, tests_key)
                    already = any(
                        isinstance(t, dict) and "relationships" in t
                        and to_rec["dbt_model"] in str(t["relationships"])
                        for d in declared
                        for k in ("data_tests", "tests")
                        for t in (d.get(k) or []))
                    if already:
                        continue
                    body = {"to": f"ref('{to_rec['dbt_model']}')",
                            "field": field}
                    tests.append({"relationships":
                                  {"arguments": body} if args_nested else body})
                    report["relationships_tests_added"] += 1
        report["models"].append(model)

    # ---- metrics: semantic_models + metrics, in a file the kit owns ----
    #
    # Measured against dbt-core 1.10.20: a project with no MetricFlow time
    # spine fails to PARSE the moment any semantic model or metric exists —
    # "the semantic layer requires a time spine model with granularity DAY or
    # smaller". Not a warning, and not limited to metrics that ask for a time
    # grain. So the spine is a precondition: without one the metrics ship under
    # meta.cassis.metrics instead, because breaking `dbt parse` in somebody's
    # repo is a worse outcome than a metric they have to wire up by hand. The
    # kit does not write a spine itself — that is warehouse-dialect SQL and a
    # materialized table in their warehouse, neither of which an export should
    # decide for them.
    sem_path = project / model_paths[0] / "cassis_semantic_models.yml"
    taken, has_spine = set(), False
    for p, (y, data) in docs.items():
        if p == sem_path:
            continue
        for key in ("semantic_models", "metrics"):
            for node in (data.get(key) or []):
                if not isinstance(node, dict) or not node.get("name"):
                    continue
                taken.add(f"{key}:{node['name']}")
                # MetricFlow requires measure names unique across the WHOLE
                # project, so a collision here is a parse error in their repo.
                for meas in (node.get("measures") or []):
                    if isinstance(meas, dict) and meas.get("name"):
                        taken.add(f"measures:{meas['name']}")
        for node in (data.get("models") or []):
            if not isinstance(node, dict):
                continue
            if node.get("time_spine") or (node.get("config") or {}).get(
                    "time_spine") or node.get("name") in (
                    "metricflow_time_spine", "all_days"):
                has_spine = True
    report["time_spine_found"] = has_spine
    sem_models, sem_metrics = [], []
    by_table_metrics = {}
    for name, m in sorted(tree["metrics"].items()):
        tkey = f"{m.get('table_schema')}.{m.get('table_name')}"
        by_table_metrics.setdefault(tkey, []).append((name, m))
    for tkey, items in sorted(by_table_metrics.items()):
        rec, tab = record_for(tkey), tree["tables"].get(tkey)
        measures = []
        for name, m in items:
            why = None
            expr = str(m.get("expression") or "").strip()
            mm = _AGG_RE.match(expr)
            if not has_spine:
                why = ("the project has no MetricFlow time spine, and dbt "
                       "refuses to parse any semantic model or metric without "
                       "one — add a time spine model, then re-run the export")
            elif not rec:
                why = "its table has no dbt model"
            elif m.get("filters"):
                # MetricFlow's `filter` needs a Dimension() reference the kit
                # cannot synthesize, and a metric exported without its
                # mandatory WHERE is a wrong number that looks governed.
                why = f"mandatory filter ({m['filters']}) has no dbt equivalent"
            elif not mm:
                why = f"expression {expr!r} is not a single aggregate"
            elif mm.group(2):
                why = "COUNT(DISTINCT ...) needs a count_distinct measure the " \
                      "kit does not infer safely"
            if why:
                report["metrics_not_exported"].append({"metric": name,
                                                       "reason": why})
                continue
            measures.append((name, m, _AGG_MAP[mm.group(1).upper()],
                             schema_case(schema, tkey, mm.group(3))))
        if not measures or not rec or not tab:
            continue
        types = schema.get(tkey) or {}
        tcol = next((c for c in types
                     if any(str(types[c]).upper().startswith(t)
                            for t in TEMPORAL_TYPES)), None) \
            or next((c for c in types if TEMPORAL_NAME_RE.search(c.upper())),
                    None)
        if not tcol:
            for name, m, _, _ in measures:
                report["metrics_not_exported"].append({
                    "metric": name,
                    "reason": f"{tkey} has no time column, and a MetricFlow "
                              f"semantic model needs an agg_time_dimension"})
            continue
        if f"semantic_models:{rec['dbt_model']}" in taken:
            for name, m, _, _ in measures:
                report["metrics_not_exported"].append({
                    "metric": name,
                    "reason": f"the project already defines a semantic model "
                              f"named {rec['dbt_model']}"})
            continue
        sm = {"name": rec["dbt_model"],
              "description": f"Cassis metrics over {tkey}.",
              "model": f"ref('{rec['dbt_model']}')",
              "defaults": {"agg_time_dimension": model_case(rec, tcol)},
              "dimensions": [{"name": model_case(rec, tcol), "type": "time",
                              "type_params": {"time_granularity": "day"}}]}
        grain = [g for g in (tab.get("grain") or [])]
        if len(grain) == 1:
            sm["entities"] = [{"name": re.sub(r"_?ID$", "", grain[0].upper(),
                                              flags=re.I).lower()
                                       or rec["dbt_model"],
                               "type": "primary",
                               "expr": model_case(rec, schema_case(
                                   schema, tkey, grain[0]))}]
        sm["measures"] = []
        for name, m, agg, col in measures:
            clash = next((k for k in (f"metrics:{name}",
                                      f"measures:{name}_measure")
                          if k in taken), None)
            if clash:
                report["metrics_not_exported"].append({
                    "metric": name,
                    "reason": f"the project already defines a "
                              f"{clash.split(':')[0][:-1]} with that name"})
                continue
            measure = {"name": f"{name}_measure", "agg": agg,
                       "expr": model_case(rec, col)}
            desc = m.get("description") or ""
            caveats = " ".join(str(m[k]).strip() for k in ("caveats", "notes")
                               if m.get(k))
            if desc or caveats:
                measure["description"] = " ".join(x for x in (desc, caveats)
                                                  if x)
            sm["measures"].append(measure)
            met = {"name": name,
                   "label": m.get("display_name") or display_name_for(name),
                   "type": "simple",
                   "type_params": {"measure": measure["name"]},
                   "description": measure.get("description")
                   or f"{display_name_for(name)} over {tkey}."}
            cfg = {}
            if caveats:
                cfg["caveats"] = caveats
            if m.get("synonyms"):
                cfg["synonyms"] = list(m["synonyms"])
            cfg["expression"] = m.get("expression")
            met["config"] = {"meta": {"cassis": cfg}}
            sem_metrics.append(met)
            report["metrics_exported"].append(name)
        if sm["measures"]:
            sem_models.append(sm)

    # everything that did not become a dbt metric still ships, on its table
    unexported = {d["metric"] for d in report["metrics_not_exported"]}
    for tkey, items in sorted(by_table_metrics.items()):
        left = [(n, m) for n, m in items if n in unexported]
        rec = record_for(tkey)
        if not left or not rec or rec["dbt_model"] not in entries:
            continue
        p, section, idx = entries[rec["dbt_model"]]
        y, data = docs[p]
        block = _cassis_meta(data[section][idx])
        block["metrics"] = [
            {k: v for k, v in (("name", n),
                               ("expression", m.get("expression")),
                               ("filters", m.get("filters")),
                               ("unit", m.get("unit")),
                               ("synonyms", list(m.get("synonyms") or []) or None),
                               ("description", m.get("description")))
             if v} for n, m in left]
        touched.add(p)

    if sem_models or sem_metrics:
        y = _reader("models:\n  - name: x\n")
        payload = y.load("version: 2\n")
        if sem_models:
            payload["semantic_models"] = sem_models
        if sem_metrics:
            payload["metrics"] = sem_metrics
        if _dump(y, payload, sem_path):
            print(f"  wrote {sem_path.relative_to(project)} "
                  f"({len(sem_models)} semantic model(s), "
                  f"{len(sem_metrics)} metric(s)) — a file the kit owns")
        touched.add(sem_path)
    n_written = 0
    for p in sorted(touched):
        if p == sem_path:
            continue
        y, data = docs[p]
        if _dump(y, data, p):
            n_written += 1
    report["files_written"] = n_written
    report["files_created"] = sorted(str(p.relative_to(project))
                                     for p in created)
    dump_json(report, run / "emit" / "dbt-export-report.json")

    print(f"dbt export -> {project}")
    print(f"  {len(report['models'])} model(s) merged, {n_written} file(s) "
          f"changed, {len(report['files_created'])} created")
    print(f"  descriptions: {report['descriptions_written']} written, "
          f"{report['descriptions_kept']} left alone (the project already had "
          f"one)")
    print(f"  {report['columns_added']} column entries added, "
          f"{report['relationships_tests_added']} relationships test(s) "
          f"({tests_key}:)")
    print(f"  metrics: {len(report['metrics_exported'])} as dbt metrics, "
          f"{len(report['metrics_not_exported'])} kept under "
          f"meta.cassis.metrics instead")
    for d in report["metrics_not_exported"]:
        print(f"    {d['metric']}: {d['reason']}")
    if report["columns_declared_twice"]:
        dupes = sorted(set(report["columns_declared_twice"]))
        report["columns_declared_twice"] = dupes
        print(f"  {len(dupes)} column(s) are declared twice in their own "
              f"model's yml — a defect in the project, and dbt picks one of "
              f"the two. Enrichment went to the first: {dupes[:3]}")
    if contested:
        report["contested_model_names"] = sorted(
            {f"{k} <- {', '.join(m)}" for k, m in contested})
        print(f"  {len(report['contested_model_names'])} ontology table(s) "
              f"match more than one dbt model by bare name and were left alone "
              f"— re-run ingest so it maps them, or the wrong model gets the "
              f"description: {report['contested_model_names'][:3]}")
    if report["tables_without_a_dbt_model"]:
        print(f"  {len(report['tables_without_a_dbt_model'])} ontology "
              f"table(s) have no dbt model and stay Cassis-only: "
              f"{report['tables_without_a_dbt_model'][:5]}")
    if sem_models:
        print("  run `dbt parse` before you commit: the semantic models are the "
              "one half of this export that can fail a parse. Deleting "
              f"{sem_path.relative_to(project)} removes them and keeps every "
              "description.")
    print("  QUESTIONS.md untouched — it is a deliverable for a person")
    print(f"  report: {run / 'emit' / 'dbt-export-report.json'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--emit", required=True, choices=["cassis", "dbt"])
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--dbt-project", type=Path,
                    help="the dbt project the run ingested (--emit dbt)")
    args = ap.parse_args()

    tree = read_working_tree(args.run)
    schema = load_json(args.workspace / "schema.json")
    if args.emit == "cassis":
        return emit_cassis(args.run, tree, schema)
    if not args.dbt_project:
        die("--emit dbt needs --dbt-project <the dbt project you ingested>")
    return emit_dbt(args.run, args.workspace, tree, schema, args.dbt_project)


if __name__ == "__main__":
    sys.exit(main())
