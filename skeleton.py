#!/usr/bin/env python3
"""Pre-generate the mechanical parts of an ontology tree so agents edit
instead of type. Default step after evidence.py.

Produces in the run workspace:
  cassis/tables/<SCHEMA>/<TABLE>.yml   # shells: name, domain_path: UNASSIGNED,
                                       # grain (from evidence), full column
                                       # name list (no descriptions)
  joins_draft.yml                      # resolved join candidates with
                                       # provenance, for promote-or-reject
  batches.json                         # usage-ranked table batches for the
                                       # parallel enrichment stage

The identifier gate doubles as the failsafe: any UNASSIGNED domain_path left
after the synthesis stage fails verify.py.

Usage: python3 skeleton.py --workspace <ingested ws> --run <run dir>
                           [--scope <scope.tsv>] [--batches N]
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import die, dump_json, load_json, load_scope


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--batches", type=int, default=6)
    ap.add_argument("--scope", type=Path,
                    help="the scope file whose `model` rows are the tables to "
                         "build shells for. Without it EVERY table in the "
                         "schema gets a shell, which is only what you want "
                         "when the schema export IS the agreed scope")
    args = ap.parse_args()

    schema = load_json(args.workspace / "schema.json")
    full = len(schema)
    # Checkpoint 1 is decorative unless the decision reaches here. Measured on
    # GitLab's public analytics project: a 2,183-table
    # schema and a 50-table agreed scope produced 2,183 shells, prefill against
    # 39,271 columns and batches carrying the whole warehouse — 30,623 columns
    # needing descriptions instead of 1,702, the scoped tables scattered across 11 of 12
    # batches, and every out-of-scope shell then failing verify.py's UNASSIGNED
    # gate. Invisible on a warehouse whose schema
    # export WAS the agreed scope.
    if args.scope:
        want = load_scope(args.scope)
        if not want:
            die(f"{args.scope} marks no table for modeling. A scope that "
                f"resolves to nothing would build zero shells and report it "
                f"as success — settle the decisions and re-run.")
        unknown = sorted(want - set(schema))
        schema = {t: c for t, c in schema.items() if t in want}
        if not schema:
            die(f"none of the {len(want)} scoped tables are in schema.json "
                f"(first few: {unknown[:5]}) — the scope file and the schema "
                f"export disagree on table keys.")
        if unknown:
            print(f"WARN: {len(unknown)} scoped tables are not in the schema "
                  f"export, so they get no shell: {unknown[:5]}")
    ev = args.workspace / "evidence"
    # evidence/grains.json keys tables by BARE name (the dbt adapter's model
    # table name); schema.json keys them SCHEMA.TABLE. Match on both or every
    # shell silently ships `grain: []` even when the repo's tests declare one.
    grains = {}
    if (ev / "grains.json").exists():
        bare = {t.split(".", 1)[1].upper(): t for t in schema}
        for g in load_json(ev / "grains.json"):
            key = g["table"] if g["table"] in schema else bare.get(g["table"].upper())
            if key:
                grains[key] = g["columns"]
    usage = load_json(ev / "usage.json") if (ev / "usage.json").exists() else {}

    # --- table shells ---
    n = 0
    for table, cols in sorted(schema.items()):
        sch, name = table.split(".", 1)
        rec = {
            "name": table,
            "domain_path": "UNASSIGNED",
            "description": "",
            "synonyms": [],
            "grain": grains.get(table, []),
            "columns": [{"name": c} for c in cols],
        }
        p = args.run / "cassis" / "tables" / sch / f"{name}.yml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
        n += 1

    # --- seeded joins draft ---
    drafts = []
    jc = ev / "join_candidates.json"
    in_scope = set(schema)
    crossing = 0
    if jc.exists():
        for c in load_json(jc):
            if not (c.get("left") and c.get("right")):
                continue
            l_in, r_in = c["left"] in in_scope, c["right"] in in_scope
            # Excluded from modeling is never excluded from evidence, so a
            # candidate with one foot in the scope stays — but it cannot be
            # promoted, because the counterpart has no table file and the
            # identifier gate would fail on it. Say so in the disposition
            # rather than dropping the edge silently.
            if args.scope and not (l_in or r_in):
                continue
            if args.scope and not (l_in and r_in):
                crossing += 1
                disp = ("TODO: reject with reason, or record as a boundary — "
                        "the counterpart is outside the modelled scope")
            else:
                disp = "TODO: promote to joins.yml or reject with reason"
            drafts.append({
                "left": c["left"], "right": c["right"],
                "on": c.get("on") or [],
                "provenance": c.get("provenance"),
                "occurrences": c.get("occurrences"),
                "disposition": disp,
            })
    (args.run / "joins_draft.yml").write_text(
        yaml.safe_dump(drafts, sort_keys=False, allow_unicode=True))

    # --- usage-ranked enrichment batches ---
    def score(t):
        u = usage.get(t) or {}
        return (u.get("questions", 0) + u.get("dashboard_cards", 0)
                + u.get("history", 0))

    if any(score(t) for t in schema):
        ranked = sorted(schema, key=lambda t: -score(t))
        order = "usage-ranked; batch 0 carries the highest-usage tables"
    else:
        # No BI corpus / query history (repo-only run): usage.json is empty and
        # alphabetical order would be arbitrary. Fall back to the repo's own
        # relationship graph — in-degree (how many models point AT this table)
        # is the structural stand-in for "analysts land here". Tie-breaks:
        # out-degree, then modeled-layer (core/reference before staging mirrors),
        # then name, so the order is total and reproducible.
        cands = load_json(jc) if jc.exists() else []
        indeg, outdeg = {}, {}
        for c in cands:
            if c.get("right"):
                indeg[c["right"]] = indeg.get(c["right"], 0) + 1
            if c.get("left"):
                outdeg[c["left"]] = outdeg.get(c["left"], 0) + 1
        layer_rank = {"DBT_CORE": 3, "DBT_REFERENCE": 3, "DBT_SEEDS": 1}
        ranked = sorted(schema, key=lambda t: (
            -indeg.get(t, 0), -outdeg.get(t, 0),
            -layer_rank.get(t.split(".", 1)[0], 2), t))
        order = ("dbt-relationship-ranked (no BI corpus): in-degree, then "
                 "out-degree, then layer; batch 0 carries the hub tables")
    # Sequential fill in rank order: batch 0 IS the high-value slice (the
    # progressive-publish v0.1), later batches carry the long tail.
    #
    # Balance on DESCRIPTION GAPS, not raw column count. A column that is already
    # documented costs an agent a glance; an undocumented one costs a written
    # description grounded in SQL. Balancing on column count puts 218 documented
    # columns and 3 undocumented ones in the same "large" batch as 162 blank ones,
    # so the stage waits on the wrong agent. A stage ends when its slowest agent
    # ends, so this is the number that sets wall-clock.
    documented = set()
    repo_path = args.workspace / "repo" / "tables.json"
    if repo_path.exists():
        bare = {}
        for k in schema:
            bare.setdefault(k.split(".", 1)[1].upper(), []).append(k)
        for r in load_json(repo_path)["records"]:
            nm = str(r["table"]).upper()
            hits = [nm] if nm in schema else bare.get(nm, [])
            if len(hits) != 1:
                continue
            for c, d in (r.get("column_descriptions") or {}).items():
                if d and str(d).strip():
                    documented.add((hits[0], c.upper()))

    def load_of(t):
        return sum(1 for c in schema[t] if (t, c.upper()) not in documented) or 1

    total_load = sum(load_of(t) for t in schema)
    per_batch = max(1, total_load // args.batches)
    batches, cur, cur_load = [], [], 0
    for t in ranked:
        cur.append(t)
        cur_load += load_of(t)
        if cur_load >= per_batch and len(batches) < args.batches - 1:
            batches.append(cur)
            cur, cur_load = [], 0
    if cur:
        batches.append(cur)
    dump_json({"order": order, "balanced_on": "columns needing descriptions",
               "total_description_gaps": total_load, "batches": batches},
              args.run / "batches.json")

    scope_note = (f" (scope: {len(schema)} of {full} schema tables)"
                  if args.scope else f" (no --scope: all {full} schema tables)")
    join_note = (f" ({crossing} crossing the scope boundary)" if crossing else "")
    print(f"skeletons: {n} table shells{scope_note} | joins draft: "
          f"{len(drafts)} candidates{join_note} | {args.batches} enrichment "
          f"batches (ranked; balanced on description gaps)")


if __name__ == "__main__":
    main()
