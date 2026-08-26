#!/usr/bin/env python3
"""Build the ID'd evidence index from an ingested workspace.

Usage: python3 evidence.py --workspace <dir>

Outputs under <workspace>/evidence/:
  enums.json              {id, table, column, values, source}
  grains.json             {id, table, columns, source}
  join_candidates.json    {id, left, right, on, occurrences, provenance, sources[:5]}
  metric_candidates.json  {id, expression, tables, occurrences, sources[:5]}
  usage.json              {table: {questions, question_views, dashboard_cards, history}}
  nonsummable.json        {id, table, signal, evidence}
Everything is a CANDIDATE with provenance — the enrichment agent grounds its
claims in these IDs; nothing here is asserted as true.
"""
import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (die, dump_json, fact_id, load_json, load_scope,
                    read_csv_rows)

AGG_RE = re.compile(
    r"\b(SUM|AVG|COUNT|COUNT_IF|COUNTIF|MEDIAN|MIN|MAX|APPROX_COUNT_DISTINCT)\s*\("
    r"([^()]{1,120}(?:\([^()]*\)[^()]{0,120})?)\)",
    re.IGNORECASE,
)
FROM_JOIN_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+((?:"?[A-Za-z_][\w$]*"?\s*\.\s*){1,2}"?[A-Za-z_][\w$]*"?)'
    r"(?:\s+(?:AS\s+)?([A-Za-z_][\w$]*))?",
    re.IGNORECASE,
)
ON_EQ_RE = re.compile(
    r"\b([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)\s*=\s*"
    r"([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)"
)
# BigQuery style: `schema.table`.column = `schema.table`.column
ON_EQ_BQ_RE = re.compile(
    r"`([\w.]+)`\s*\.\s*([A-Za-z_][\w$]*)\s*=\s*`([\w.]+)`\s*\.\s*([A-Za-z_][\w$]*)"
)
KEYWORDS = {"SELECT", "WHERE", "GROUP", "ORDER", "LATERAL", "UNNEST", "TABLE", "VALUES"}


def _norm_table(raw: str) -> str:
    """Normalize, keeping only the last two dot-segments (drops db prefix)."""
    parts = re.sub(r'["\s]', "", raw).upper().split(".")
    return ".".join(parts[-2:])


def _alias_map(sql: str, known: set) -> dict:
    """alias -> SCHEMA.TABLE for one SQL text (last binding wins per alias)."""
    amap = {}
    for m in FROM_JOIN_RE.finditer(sql):
        tbl = _norm_table(m.group(1))
        if tbl not in known:
            continue
        alias = (m.group(2) or "").upper()
        if alias and alias not in KEYWORDS:
            amap[alias] = tbl
        # bare table name also usable as its own qualifier
        amap[tbl.split(".")[1]] = tbl
    return amap


def mine_joins(sql_sources, known: set):
    """sql_sources: iterable of (source_id, sql). Returns pair stats."""
    pairs = defaultdict(lambda: {"occurrences": 0, "sources": [], "on": Counter()})
    for src, sql in sql_sources:
        if not sql:
            continue
        amap = _alias_map(sql, known)
        eqs = [(m.group(1).upper(), m.group(2).upper(),
                m.group(3).upper(), m.group(4).upper())
               for m in ON_EQ_RE.finditer(sql)]
        for m in ON_EQ_BQ_RE.finditer(sql):
            lq = _norm_table(m.group(1))
            rq = _norm_table(m.group(3))
            eqs.append((lq, m.group(2).upper(), rq, m.group(4).upper()))
        for la, lc, ra, rc in eqs:
            lt = amap.get(la) or (la if la in known else None)
            rt = amap.get(ra) or (ra if ra in known else None)
            if not lt or not rt or lt == rt:
                continue
            key = tuple(sorted((lt, rt)))
            cond = f"{lt}.{lc} = {rt}.{rc}" if key[0] == lt else f"{rt}.{rc} = {lt}.{lc}"
            rec = pairs[key]
            rec["occurrences"] += 1
            rec["on"][cond] += 1
            if len(rec["sources"]) < 5 and src not in rec["sources"]:
                rec["sources"].append(src)
    return pairs


def mine_metrics(sql_sources, known: set):
    exprs = defaultdict(lambda: {"occurrences": 0, "sources": [], "tables": set()})
    for src, sql in sql_sources:
        if not sql:
            continue
        tables = frozenset(t for t in known
                           if re.search(rf"\b{re.escape(t)}\b", sql.upper()
                                        .replace('"', "")))
        for m in AGG_RE.finditer(sql):
            fn = m.group(1).upper()
            arg = re.sub(r"\s+", " ", m.group(2)).strip().upper()
            if fn == "COUNT" and arg in ("*", "1"):
                continue
            expr = f"{fn}({arg})"
            rec = exprs[expr]
            rec["occurrences"] += 1
            rec["tables"] |= tables
            if len(rec["sources"]) < 5 and src not in rec["sources"]:
                rec["sources"].append(src)
    return exprs


# Column names too generic to be a business key, whatever the tests say.
_GENERIC_KEYS = {"ID", "NAME", "DAY", "DATE", "MONTH", "YEAR", "STATUS", "TYPE",
                 "CODE", "EMAIL", "LABEL", "VALUE", "SOURCE", "TITLE", "STATE"}


def mine_shared_key_joins(schema: dict, grains: list, already: set) -> list:
    """Hub-and-spoke candidates: every table carrying a key another table is
    grained on.

    The PK-test miner only proposes a join when the repo declared a relationship,
    so it misses the joins nobody bothered to write a test for. Measured on one
    real warehouse: `dim_devices.housing_id = dim_housings.housing_id`, among the
    most load-bearing joins there, appeared in no candidate at all.

    One hub per key, elected by layer and DIM_-ness. Electing exactly one matters:
    letting every grained table claim a key regenerates the artifact mesh that a
    synthesis agent then has to reject by hand (1,603 candidates instead of ~224
    on that same warehouse, most of them staging mirrors pointing at each other).
    """
    def coltype(table, col):
        cols = schema.get(table) or {}
        return str(cols.get(col) or cols.get(col.upper()) or "").upper()

    by_key = defaultdict(list)
    for g in grains:
        cols = g.get("columns") or []
        if len(cols) == 1 and g["table"] in schema:
            by_key[str(cols[0]).upper()].append(g["table"])

    def rank(t):
        sch, name = t.split(".", 1)
        return (0 if name.startswith("DIM_") and sch.endswith("CORE") else
                1 if sch.endswith("CORE") else
                2 if sch.endswith("REFERENCE") else 3, t)

    out = []

    # --- rule B: <entity>_ID on the fact -> ID on the dimension ---
    # The star-schema convention, and the one rule A cannot see: rule A matches
    # identical column names (the dbt house style, e.g. device_id on both sides),
    # but a classic warehouse names the dimension key `ID` and the foreign key
    # `<entity>_ID`. Measured on one real warehouse: 196 of 233 column pairs have
    # DIFFERENT names on each side, so rule A alone moved its ceiling by zero.
    def singular(tok):
        if tok.endswith("IES"):
            return tok[:-3] + "Y"
        if tok.endswith("SES"):
            return tok[:-2]
        if tok.endswith("S") and not tok.endswith("SS"):
            return tok[:-1]
        return tok

    fk_to_hub = {}
    for hub in sorted(t for t, cols in
                      ((g["table"], [c.upper() for c in (g.get("columns") or [])])
                       for g in grains if g["table"] in schema)
                      if cols == ["ID"]):
        name = hub.split(".", 1)[1]
        whole = singular(name)
        for tok in {singular(t) for t in name.split("_") if len(t) > 2} | {whole}:
            fk = f"{tok}_ID"
            # prefer the hub whose WHOLE name is the entity (COMPANIES -> COMPANY_ID)
            # over one that merely contains the token (ACCOUNTS)
            score = (0 if tok == whole else 1, hub)
            if fk not in fk_to_hub or score < fk_to_hub[fk][0]:
                fk_to_hub[fk] = (score, hub)
    for fk, (_score, hub) in sorted(fk_to_hub.items()):
        for table, cols in sorted(schema.items()):
            if table == hub or (table, hub) in already:
                continue
            if fk not in {c.upper() for c in cols}:
                continue
            out.append({
                "id": fact_id("fkhub", table, hub, fk),
                "left": table, "right": hub,
                "on": [f"{fk.lower()} = id"],
                "provenance": "fk_name_hub",
                "occurrences": None,
                "raw": {"key": fk, "hub_grain": "ID",
                        "reason": f"{hub} is grained on ID; {table}.{fk} names it"},
            })

    # --- rule A: identical key name on both sides ---
    for key, tables in sorted(by_key.items()):
        if key in _GENERIC_KEYS:
            continue
        hub = sorted(tables, key=rank)[0]
        ty = coltype(hub, key)
        # a date or timestamp is a conformed dimension at best, not an identifier
        if "DATE" in ty or "TIME" in ty:
            continue
        if not (key.endswith("_ID") or len(key) <= 6):
            continue
        for table, cols in sorted(schema.items()):
            if table == hub or (table, hub) in already:
                continue
            if key not in {c.upper() for c in cols}:
                continue
            out.append({
                "id": fact_id("sharedkey", table, hub, key),
                "left": table, "right": hub,
                "on": [f"{key.lower()} = {key.lower()}"],
                "provenance": "shared_key_hub",
                "occurrences": None,
                "raw": {"key": key, "hub_grain": key,
                        "reason": f"{hub} is grained on {key}; {table} carries it"},
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--scope", type=Path,
                    help="optional scope file (TAB-separated SCHEMA.TABLE[\tdomain]); "
                         "reports how much of the agreed scope the material covers")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when a postcondition warns (CI)")
    args = ap.parse_args()
    ws = args.workspace
    ev = ws / "evidence"

    schema = load_json(ws / "schema.json")
    known = set(schema)
    repo = load_json(ws / "repo" / "tables.json")
    records = repo["records"]

    # Adapter records key tables by BARE name (a dbt model carries no warehouse
    # schema); schema.json keys them SCHEMA.TABLE. Emit the QUALIFIED name so
    # every downstream lookup matches. Measured cost of not doing this: 0 of
    # 114 grains matched, so all 198 table shells shipped
    # `grain: []` while the repo's own tests declared one.
    _bare_to_qualified = defaultdict(list)
    for t in known:
        _bare_to_qualified[t.split(".", 1)[1].upper()].append(t)

    def qualify(bare_name):
        hits = _bare_to_qualified.get(str(bare_name).upper(), [])
        return hits[0] if len(hits) == 1 else None

    # enums + grains from the repo layer
    enums, grains = [], []
    unqualified = []
    for r in records:
        t = qualify(r["table"]) or r["table"]
        if t == r["table"] and t not in known:
            unqualified.append(r["table"])
        for col, values in (r.get("enums") or {}).items():
            enums.append({"id": fact_id("enum", t, col), "table": t, "column": col,
                          "values": values, "source": r.get("yml_path")})
        if r.get("granularity"):
            grains.append({"id": fact_id("grain", t), "table": t,
                           "columns": r["granularity"], "source": r.get("yml_path")})
    dump_json(enums, ev / "enums.json")
    dump_json(grains, ev / "grains.json")

    # SQL corpora
    corpora = {
        "repo": [(f"repo:{r['table']}", r.get("sql") or "") for r in records],
        "question": [(f"q:{row['id']}", row["sql"])
                     for row in read_csv_rows(ws / "questions.csv")],
        "dashboard": [(f"d:{row['id']}", row["sql"])
                      for row in read_csv_rows(ws / "dashboard_queries.csv")],
        "history": [(f"h:{row['id']}", row["sql"])
                    for row in read_csv_rows(ws / "query_history.csv")],
    }
    all_sources = [x for rows in corpora.values() for x in rows]

    # joins
    pair_stats = mine_joins(all_sources, known)
    joins = []
    for (lt, rt), rec in sorted(pair_stats.items()):
        top_on = [c for c, _ in rec["on"].most_common(3)]
        joins.append({"id": fact_id("join", lt, rt), "left": lt, "right": rt,
                      "on": top_on, "occurrences": rec["occurrences"],
                      "provenance": "sql_mined", "sources": rec["sources"]})
    # declared + implicit relationships from the dbt adapter, when present.
    # dbt models carry no warehouse schema; resolve names via the schema export.
    def _resolve(model_name):
        return qualify(model_name)

    rels = repo.get("relationships") or {}
    for prov in ("declared", "implicit"):
        for rel in rels.get(prov) or []:
            frm = _resolve(rel.get("from_model") or rel.get("model"))
            to = _resolve(rel.get("to_model") or rel.get("target"))
            joins.append({
                "id": fact_id("rel", prov, str(rel)), "provenance": f"dbt_{prov}",
                "left": frm, "right": to,
                "on": [f"{rel.get('from_column')} = {rel.get('to_column') or rel.get('field', '')}"],
                "raw": {k: rel.get(k) for k in
                        ("from_model", "to_model", "model", "target",
                         "from_column", "to_column", "reason", "source") if rel.get(k)},
            })
    joins += mine_shared_key_joins(schema, grains,
                                   {(j.get("left"), j.get("right")) for j in joins})
    dump_json(joins, ev / "join_candidates.json")

    # metrics (from BI + history only — repo SQL defines columns, not metrics)
    bi_sources = corpora["question"] + corpora["dashboard"] + corpora["history"]
    expr_stats = mine_metrics(bi_sources, known)
    col_to_tables = defaultdict(set)
    for t, cols in schema.items():
        for c in cols:
            col_to_tables[c].add(t)
    metrics = []
    for e, rec in expr_stats.items():
        if rec["occurrences"] < 3:
            continue
        tables = set(rec["tables"])
        if not tables:  # resolve via schema column lookup
            for ident in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", e):
                tables |= col_to_tables.get(ident, set())
        if not tables:  # not expressible on the in-scope schema
            continue
        metrics.append({"id": fact_id("metric", e), "expression": e,
                        "tables": sorted(tables),
                        "occurrences": rec["occurrences"],
                        "sources": rec["sources"]})
    metrics.sort(key=lambda m: -m["occurrences"])
    metrics = metrics[:200]
    dump_json(metrics, ev / "metric_candidates.json")

    # usage ranking (in-scope), plus out-of-scope demand as its own signal —
    # "questions your users ask that this scope cannot answer"
    usage = defaultdict(lambda: {"questions": 0, "question_views": 0,
                                 "dashboard_cards": 0, "history": 0})
    out_of_scope = Counter()
    def _tally(row, field):
        for t in filter(None, row["tables"].split(";")):
            if t in known:
                usage[t][field] += 1
                if field == "questions":
                    usage[t]["question_views"] += int(row.get("views") or 0)
            else:
                out_of_scope[t] += 1
    for row in read_csv_rows(ws / "questions.csv"):
        _tally(row, "questions")
    for row in read_csv_rows(ws / "dashboard_queries.csv"):
        _tally(row, "dashboard_cards")
    for row in read_csv_rows(ws / "query_history.csv"):
        _tally(row, "history")
    dump_json(dict(usage), ev / "usage.json")
    dump_json(dict(out_of_scope.most_common(100)), ev / "usage_out_of_scope.json")

    # non-summable candidates
    nonsummable = []
    for r in records:
        signals = []
        blob = (r.get("description", "") + " " + r.get("caveats", "")).lower()
        if "grouped_dimension" in blob:
            signals.append("yml mentions grouped_dimension")
        sql = r.get("sql") or ""
        if re.search(r"\bGROUPING\s+SETS\b", sql, re.IGNORECASE):
            signals.append("SQL uses GROUPING SETS")
        if len(re.findall(r"\bUNION\s+ALL\b", sql, re.IGNORECASE)) >= 3 and \
           re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE):
            signals.append("SQL stacks >=3 UNION ALL aggregate blocks")
        if signals:
            nonsummable.append({"id": fact_id("ns", qualify(r["table"]) or r["table"]),
                                "table": qualify(r["table"]) or r["table"],
                                "signals": signals})
    dump_json(nonsummable, ev / "nonsummable.json")

    # ---- postconditions: an empty class is a bug until proven otherwise ----
    # Every harness bug found on one long real run was silent: a key mismatch or an
    # unresolved indirection produced an EMPTY field, no error, and the run
    # continued for hours on material it had quietly dropped. So say so, loudly.
    n_desc = sum(1 for r in records if r.get("description"))
    n_coldesc = sum(len(r.get("column_descriptions") or {}) for r in records)
    warns = []
    if records and not n_desc and not n_coldesc:
        warns.append(f"0 of {len(records)} repo records carry ANY description — if the "
                     f"repo documents its models, the adapter dropped it (doc blocks "
                     f"unresolved, or no compiled manifest)")
    if records and not grains:
        warns.append(f"0 grains from {len(records)} records — table shells will ship "
                     f"`grain: []`; check the repo declares uniqueness tests")
    if not joins:
        warns.append("0 join candidates — the tree will have no joins to disposition")
    # Two distinct failures, worth distinguishing: a BARE model name we could not
    # qualify (a key-format mismatch we can repair), versus an
    # already-qualified name that simply is not in the agreed scope (a
    # model-name vs materialized-table mapping gap).
    bare_unresolved = [t for t in unqualified if "." not in t]
    off_schema = [r["table"] for r in records
                  if (qualify(r["table"]) or r["table"]) not in known]
    if bare_unresolved or off_schema:
        # A real run hit this on 131 of 254 records and the banner
        # named the class but not one member, so the operator could not tell
        # an alias/custom-schema config from an adapter bug without re-deriving
        # the list by hand. Name every one.
        dump_json({"off_schema": sorted(off_schema),
                   "bare_unresolved": sorted(bare_unresolved)},
                  ev / "unmapped_models.json")
    if bare_unresolved:
        warns.append(f"{len(bare_unresolved)} records carry a bare model name that "
                     f"matches no schema table: {bare_unresolved[:5]}")
    if off_schema and len(off_schema) > len(records) // 2:
        warns.append(f"{len(off_schema)} of {len(records)} repo records name a table "
                     f"outside schema.json — their SQL, descriptions and grains cannot "
                     f"reach the tree (check model-name vs materialized-table mapping; "
                     f"e.g. {off_schema[:3]}, full list in evidence/unmapped_models.json)")
    # Scope coverage: does the input material actually cover what the scope
    # agreed to model? Benchmark B shipped docs for 427 models while 70 of its 97 in-scope
    # tables had no model at all — a missing-input problem that looked like a bug
    # for two benchmark runs because nothing measured it. Say it before the agents
    # start, not after.
    if args.scope and args.scope.exists():
        want = load_scope(args.scope)
        grounded = {qualify(r["table"]) or r["table"] for r in records}
        described = {qualify(r["table"]) or r["table"] for r in records
                     if r.get("column_descriptions")}
        no_model = sorted(want - grounded)
        no_docs = sorted(want - described)
        print(f"scope coverage: {len(want) - len(no_model)}/{len(want)} in-scope tables "
              f"have a repo model | {len(want) - len(no_docs)}/{len(want)} have existing "
              f"column descriptions")
        if no_model and len(no_model) > len(want) // 4:
            warns.append(f"{len(no_model)} of {len(want)} IN-SCOPE tables have no model in "
                         f"the input material — the agent will have only a column list "
                         f"for them; check the input before spending agent "
                         f"tokens on them: {no_model[:4]}")

    for w in warns:
        print(f"WARN: {w}")
    if warns and args.strict:
        die(f"{len(warns)} postcondition warning(s) with --strict")

    print(f"evidence: {len(enums)} enums | {len(grains)} grains | "
          f"{len(joins)} join candidates | {len(metrics)} metric candidates "
          f"(>=3 occurrences, resolved) | {len(usage)} tables ranked | "
          f"{len(nonsummable)} non-summable candidates")


if __name__ == "__main__":
    main()
