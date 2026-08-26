#!/usr/bin/env python3
"""Optional data profiling — to SETTLE questions the SQL alone leaves open.

Everything else in this kit reasons about code. Code cannot tell you whether a
column called `..._ratio` holds 0-1 or 0-100, whether a STRING column really
carries timestamps, or whether a column is NULL in every row that exists. Those
were real open questions on a real run, and each is one aggregate away from
being answered.

So this is not "dump statistics next to every table". Each measurement here is
chosen because it resolves a specific class of question:

    measurement            settles
    -------------------    -----------------------------------------------------
    min / max (numeric)    ratio-vs-percent scale, unit sanity, negative sentinels
    null_rate              "is this column actually populated?"
    distinct_count         is this an identifier, a flag, or a category?
    top values            the real enum, including values the yml never declared
    pattern of samples     STRING columns that hold timestamps, numbers, or JSON
    count vs count(key)    VERIFIES the declared grain instead of trusting it

**Nothing leaves the machine.** Profiling runs in your own warehouse against your
own credentials, and its output stays in your workspace. `plan` writes SQL for you
to run and review; nothing here connects anywhere. Value samples are OFF by
default because they are the one output that can carry personal data — stats
alone answer most of the questions above.

Usage:
  python3 profile.py plan --workspace WS [--dialect bigquery] [--samples 0]
                          [--tables T ...] [--out DIR]
  python3 profile.py load --workspace WS --results FILE|DIR
  python3 profile.py report --workspace WS        # what the profile settles
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dump_json, load_json  # noqa: E402

NUMERIC = ("INT", "FLOAT", "NUMERIC", "DECIMAL", "BIGNUMERIC", "DOUBLE", "REAL",
           "NUMBER")
TEMPORAL = ("DATE", "TIME", "TIMESTAMP", "DATETIME")

DIALECTS = ("duckdb", "bigquery", "snowflake", "postgres", "redshift", "databricks",
            "trino", "mysql")

# We author ONE canonical statement and transpile it, rather than branching on the
# dialect by hand. Hand-branching is how you ship SQL that only ever ran against
# the engine you happened to have: it puts the dialect knowledge in our head
# instead of in a library that already has it, and every untested branch is a
# first-live-run failure. sqlglot both rewrites and, crucially, PARSES — so every
# statement can be validated for every dialect offline, before anyone runs it.
CANON = "duckdb"

try:
    import sqlglot
except ImportError:  # pragma: no cover
    sqlglot = None


def to_dialect(sql: str, dialect: str) -> str:
    if sqlglot is None or dialect == CANON:
        return sql
    return sqlglot.transpile(sql, read=CANON, write=dialect)[0]


def qualify(table: str) -> str:
    return ".".join(f'"{p}"' for p in table.split("."))


def column_sql(table: str, col: str, ctype: str, approx: bool = False) -> str:
    """One row per column: the measurements that settle a question.

    Deliberately plain: COUNT / MIN / MAX / AVG / COUNT(DISTINCT) and a TRY_CAST.
    Exact COUNT(DISTINCT) is the default because approximate counting is the only
    thing here that needs an engine-specific function, and it buys speed on tables
    big enough to care — an opt-in, not a portability tax.
    """
    c = f'"{col}"'
    ty = (ctype or "").upper()
    distinct = (f"APPROX_COUNT_DISTINCT({c})" if approx else f"COUNT(DISTINCT {c})")
    sel = [f"'{table}' AS tbl", f"'{col}' AS col", f"'{ty}' AS declared_type",
           "COUNT(*) AS n_rows", f"COUNT({c}) AS n_present",
           f"{distinct} AS n_distinct"]
    if any(k in ty for k in NUMERIC):
        sel += [f"CAST(MIN({c}) AS VARCHAR) AS min_v",
                f"CAST(MAX({c}) AS VARCHAR) AS max_v",
                f"CAST(AVG({c}) AS VARCHAR) AS avg_v"]
    else:
        # A STRING column that really holds a number or a timestamp is a top
        # failure mode. MIN/MAX of the text form is enough to expose it, and it
        # needs no cast that could fail.
        sel += [f"CAST(MIN({c}) AS VARCHAR) AS min_v",
                f"CAST(MAX({c}) AS VARCHAR) AS max_v", "NULL AS avg_v"]
    return "SELECT " + ", ".join(sel) + f" FROM {qualify(table)}"


def top_values_sql(table: str, col: str, k: int) -> str:
    """The real enum, including values the yml never declared.

    A separate GROUP BY statement rather than a string-aggregate inside the stats
    query: portable everywhere, and it returns frequencies, which is what tells a
    genuine category apart from a typo.

    Parenthesized because a bare `LIMIT` binds to the whole UNION rather than to
    its branch, so `SELECT ... LIMIT 5 UNION ALL SELECT ...` is a syntax error.
    sqlglot parses that happily; DuckDB does not — which is why this generator is
    executed against a real engine in the tests and not only parsed.
    """
    c = f'"{col}"'
    return (f"(SELECT '{table}' AS tbl, '{col}' AS col, "
            f"CAST({c} AS VARCHAR) AS value, COUNT(*) AS n "
            f"FROM {qualify(table)} GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT {k})")


def grain_sql(table: str, grain: list) -> str:
    """Verify a declared grain instead of trusting it."""
    cols = ", ".join(f'"{c}"' for c in grain)
    return (f"SELECT '{table}' AS tbl, '{'+'.join(grain)}' AS grain, "
            f"COUNT(*) AS n_rows, COUNT(DISTINCT {cols}) AS n_grain_values "
            f"FROM {qualify(table)}")


def cmd_plan(args):
    schema = load_json(args.workspace / "schema.json")
    ev = args.workspace / "evidence"
    grains = {}
    if (ev / "grains.json").exists():
        bare = {t.split(".", 1)[1].upper(): t for t in schema}
        for g in load_json(ev / "grains.json"):
            key = g["table"] if g["table"] in schema else bare.get(g["table"].upper())
            if key:
                grains[key] = g.get("columns") or []
    want = [t for t in sorted(schema)
            if not args.tables or t in set(x.upper() for x in args.tables)]
    out = args.out or (args.workspace / "profile" / "sql")
    out.mkdir(parents=True, exist_ok=True)
    n_col, unparseable = 0, []
    for t in want:
        stmts = [column_sql(t, c, ty, args.approx) for c, ty in schema[t].items()]
        n_col += len(stmts)
        blocks = ["\nUNION ALL\n".join(stmts) + ";"]
        if grains.get(t):
            blocks.append("-- grain check: n_rows vs n_grain_values proves or "
                          "disproves the grain the repo's tests declare\n"
                          + grain_sql(t, grains[t]) + ";")
        if args.top_values:
            blocks.append(f"-- the REAL enum per low-cardinality column, with "
                          f"frequencies (top {args.top_values})\n" +
                          "\nUNION ALL\n".join(
                              top_values_sql(t, c, args.top_values)
                              for c in schema[t]) + ";")
        head = (f"-- Profiling for {t}  (dialect: {args.dialect})\n"
                f"-- Run this in YOUR warehouse. The output stays on your machine;\n"
                f"-- the bootstrap never transmits it and never connects out.\n"
                f"-- Reads {len(stmts)} column(s), "
                + ("aggregates only — no row values leave the table.\n"
                   if not args.top_values else
                   f"plus up to {args.top_values} frequent VALUES per column, which "
                   f"can carry personal data — review before running.\n"))
        rendered = []
        for b in blocks:
            try:
                rendered.append("\n".join(
                    to_dialect(s, args.dialect) + ";" if not s.endswith(";")
                    else to_dialect(s.rstrip(";"), args.dialect) + ";"
                    for s in [b.rstrip(";")] ))
            except Exception as e:                      # noqa: BLE001
                unparseable.append((t, str(e).splitlines()[0]))
                rendered.append(b)
        (out / f"{t}.sql").write_text(head + "\n\n".join(rendered) + "\n")
    print(f"profiling plan: {len(want)} tables, {n_col} column measurements, "
          f"dialect {args.dialect}"
          + (" (transpiled from the canonical form by sqlglot)" if sqlglot and
             args.dialect != CANON else "")
          + f", values {'OFF (aggregates only)' if not args.top_values else args.top_values}")
    if unparseable:
        print(f"WARN: {len(unparseable)} statement(s) did not transpile cleanly; "
              f"emitted in canonical form: {unparseable[:2]}")
    print(f"-> {out}")
    print(f"Validate before running (free, offline, no warehouse):\n"
          f"  python3 profile.py check --workspace {args.workspace} "
          f"--dialect {args.dialect}\n"
          f"Then run them in your warehouse, save the rows as JSON or CSV, and:\n"
          f"  python3 profile.py load --workspace {args.workspace} --results <file>")
    return 0


def cmd_check(args):
    """Parse every generated statement in the target dialect, offline.

    This is the answer to "will this run against your warehouse?" that does not
    require your warehouse. A dialect error is a parse error, and sqlglot can
    parse all of these dialects, so the whole plan can be validated for free
    before anyone is asked to execute anything. What it cannot catch: permissions,
    a table that does not exist, and cost. Those are what a warehouse dry-run is
    for — BigQuery's is free, and `EXPLAIN` serves elsewhere.
    """
    sqldir = args.workspace / "profile" / "sql"
    # --all-dialects probes the GENERATOR, so it needs no emitted plan
    if not args.all_dialects and not sqldir.exists():
        print("no plan to check — run `profile.py plan` first")
        return 1
    if sqlglot is None:
        print("sqlglot is not installed; cannot validate offline. Install it, or "
              "dry-run the SQL in the warehouse instead.")
        return 1
    # Two different questions, and conflating them produces nonsense — the first
    # version of this check parsed BigQuery-rendered SQL as Postgres and "failed".
    #   default:        does the plan I emitted parse as the dialect it was emitted
    #                   FOR? (the question that matters before running it)
    #   --all-dialects: would the GENERATOR hold up for every engine we claim to
    #                   support? (a coverage test for the generator, not for a run)
    bad, n = [], 0
    if args.all_dialects:
        schema = load_json(args.workspace / "schema.json")
        probe = [("num", "FLOAT64"), ("txt", "STRING"), ("ts", "TIMESTAMP"),
                 ("flag", "BOOL")]
        for d in DIALECTS:
            stmts = [column_sql("S.T", c, ty) for c, ty in probe]
            stmts.append(grain_sql("S.T", ["num", "txt"]))
            stmts.append(top_values_sql("S.T", "txt", 10))
            for s in stmts:
                n += 1
                try:
                    sqlglot.parse_one(to_dialect(s, d), read=d)
                except Exception as e:                  # noqa: BLE001
                    bad.append((f"generator/{d}", d, str(e).splitlines()[0][:110]))
        print(f"generator coverage: {n} statement(s) transpiled and re-parsed across "
              f"{len(DIALECTS)} dialects")
    else:
        for f in sorted(sqldir.glob("*.sql")):
            text = f.read_text()
            m = re.search(r"dialect:\s*([a-z]+)", text)
            d = m.group(1) if m else args.dialect
            if d not in DIALECTS:
                d = CANON
            for stmt in text.split(";"):
                s = "\n".join(l for l in stmt.splitlines()
                              if not l.strip().startswith("--")).strip()
                if not s:
                    continue
                n += 1
                try:
                    sqlglot.parse_one(s, read=d)
                except Exception as e:                  # noqa: BLE001
                    bad.append((f.name, d, str(e).splitlines()[0][:110]))
        print(f"checked {n} emitted statement(s), each against the dialect it was "
              f"generated for")
    if bad:
        print(f"FAIL: {len(bad)} statement/dialect combination(s) do not parse")
        for name, d, err in bad[:10]:
            print(f"    {name} [{d}]: {err}")
        print("\nFix the generator rather than the emitted SQL. If this is an "
              "engine the kit does not model, hand the failing statement AND the parser "
              "error to the enrichment agent to repair, then feed the fix back here "
              "so the next run does not hit it.")
        return 1
    print("PASS: every statement parses in every requested dialect")
    return 0


def _rows_from(path: Path) -> list:
    if path.is_dir():
        rows = []
        for f in sorted(path.iterdir()):
            if f.suffix.lower() in (".json", ".csv"):
                rows += _rows_from(f)
        return rows
    text = path.read_text()
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("rows") or []
    import csv as _csv
    return list(_csv.DictReader(text.splitlines()))


def cmd_load(args):
    rows = _rows_from(args.results)
    per_table, top_values, grain_rows = {}, {}, []
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        t = str(low.get("tbl") or low.get("table") or "").upper()
        if not t:
            continue
        if low.get("grain"):
            grain_rows.append(low)
            continue
        c = str(low.get("col") or low.get("column") or "").upper()
        if not c:
            continue
        # Three row shapes share tbl/col: stats, top-values, grain. Keying them all
        # into one slot let the top-values rows silently overwrite the stats and
        # cost every finding except the grain check — found by executing the plan,
        # not by parsing it.
        if "value" in low:
            top_values.setdefault(t, {}).setdefault(c, []).append(
                {"value": low.get("value"), "n": low.get("n")})
        else:
            per_table.setdefault(t, {})[c] = low
    outdir = args.workspace / "profile"
    outdir.mkdir(parents=True, exist_ok=True)
    for t, cols in per_table.items():
        dump_json({"table": t, "columns": cols,
                   "top_values": top_values.get(t) or {}}, outdir / f"{t}.json")
    if grain_rows:
        dump_json(grain_rows, outdir / "_grain_checks.json")
    nv = sum(len(v) for c in top_values.values() for v in c.values())
    print(f"loaded profile for {len(per_table)} tables "
          f"({sum(len(c) for c in per_table.values())} columns), "
          f"{nv} observed value(s), {len(grain_rows)} grain check(s) -> {outdir}")
    return 0


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}|$)")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def cmd_report(args):
    pdir = args.workspace / "profile"
    if not pdir.exists():
        print("no profile loaded — run `plan`, execute the SQL, then `load`")
        return 0
    schema = load_json(args.workspace / "schema.json")
    findings = []
    for f in sorted(pdir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        d = load_json(f)
        t = d["table"]
        for c, m in (d.get("columns") or {}).items():
            ty = str(m.get("declared_type") or "").upper()
            rows, present = _num(m.get("n_rows")), _num(m.get("n_present"))
            distinct = _num(m.get("n_distinct"))
            lo, hi = _num(m.get("min_v")), _num(m.get("max_v"))
            if rows and present == 0:
                findings.append((t, c, "always_null",
                                 f"populated in 0 of {int(rows)} rows — describe it "
                                 f"as unused rather than guessing its meaning"))
                continue
            if rows and present is not None and rows > 0 and present / rows < 0.05:
                findings.append((t, c, "nearly_always_null",
                                 f"populated in {present:.0f} of {rows:.0f} rows "
                                 f"({100 * present / rows:.1f}%)"))
            # ratio vs percent: the classic 0-1 vs 0-100 ambiguity
            if any(k in ty for k in NUMERIC) and hi is not None:
                nm = c.lower()
                if any(k in nm for k in ("ratio", "rate", "pct", "percent", "share")):
                    if hi <= 1.0001:
                        findings.append((t, c, "scale_0_1",
                                         f"max {hi:g} — a fraction, not a percentage"))
                    elif hi <= 100.5:
                        findings.append((t, c, "scale_0_100",
                                         f"max {hi:g} — a percentage, not a fraction"))
                    else:
                        findings.append((t, c, "scale_unbounded",
                                         f"max {hi:g} — neither 0-1 nor 0-100; not a "
                                         f"share despite the name"))
                if lo is not None and lo < 0 and not any(
                        k in nm for k in ("delta", "diff", "adjust", "correction",
                                          "balance", "imbalance", "margin", "ecart")):
                    findings.append((t, c, "negative_values",
                                     f"min {lo:g} — negatives in a column whose name "
                                     f"does not suggest them; sentinel or real?"))
            # a STRING that is really a timestamp or a number
            if "CHAR" in ty or "STRING" in ty or "TEXT" in ty:
                probe = str(m.get("min_v") or "")
                if TS_RE.match(probe):
                    findings.append((t, c, "string_holds_timestamp",
                                     f"declared {ty} but values look temporal "
                                     f"(e.g. {probe[:19]!r}) — comparisons and "
                                     f"ordering will be lexical, not chronological"))
                elif NUM_RE.match(probe):
                    findings.append((t, c, "string_holds_number",
                                     f"declared {ty} but values are numeric "
                                     f"(e.g. {probe[:12]!r}) — SUM/AVG need a cast"))
            # identifier vs flag vs category
            if distinct is not None and rows:
                if distinct <= 1 and rows > 1:
                    findings.append((t, c, "single_value",
                                     "one distinct value across the table — carries "
                                     "no information for filtering or grouping"))
                elif distinct == 2 and "BOOL" not in ty:
                    findings.append((t, c, "two_valued",
                                     f"two distinct values in a {ty} column — likely "
                                     f"a flag; capture both values as an enum"))
    # grain verification
    gpath = pdir / "_grain_checks.json"
    if gpath.exists():
        for g in load_json(gpath):
            rows, uniq = _num(g.get("n_rows")), _num(g.get("n_grain_values"))
            if rows and uniq and rows > uniq:
                findings.append((g.get("tbl"), g.get("grain"), "grain_not_unique",
                                 f"{rows:.0f} rows but only {uniq:.0f} distinct "
                                 f"grain values — the declared grain does NOT "
                                 f"identify a row; joins on it will fan out"))
    dump_json([{"table": t, "column": c, "finding": k, "detail": v}
               for t, c, k, v in findings], args.workspace / "profile" / "_findings.json")
    from collections import Counter as _C
    kinds = _C(k for _, _, k, _ in findings)
    print(f"profile findings: {len(findings)}")
    for k, n in kinds.most_common():
        print(f"    {n:5d} {k}")
    for t, c, k, v in findings[:12]:
        print(f"  {t}.{c}: {v}")
    print(f"\n-> {args.workspace / 'profile' / '_findings.json'} "
          f"(fed into packets; each one is a question you no longer have to ask)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--dialect", default="bigquery", choices=sorted(DIALECTS))
    p.add_argument("--top-values", type=int, default=0, metavar="K",
                   help="also return the K most frequent VALUES per column with "
                        "counts (default 0 = aggregates only; values can carry "
                        "personal data, so this is opt-in)")
    p.add_argument("--approx", action="store_true",
                   help="APPROX_COUNT_DISTINCT instead of exact COUNT(DISTINCT) — "
                        "faster on very large tables, one engine-specific function")
    p.add_argument("--tables", nargs="*")
    p.add_argument("--out", type=Path)
    p.set_defaults(fn=cmd_plan)
    l = sub.add_parser("load")
    l.add_argument("--workspace", type=Path, required=True)
    l.add_argument("--results", type=Path, required=True)
    l.set_defaults(fn=cmd_load)
    ck = sub.add_parser("check")
    ck.add_argument("--workspace", type=Path, required=True)
    ck.add_argument("--dialect", default=CANON, choices=sorted(DIALECTS))
    ck.add_argument("--all-dialects", action="store_true",
                    help="coverage test for the GENERATOR: transpile a probe "
                         "statement to every supported engine and re-parse it")
    ck.set_defaults(fn=cmd_check)
    r = sub.add_parser("report")
    r.add_argument("--workspace", type=Path, required=True)
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
