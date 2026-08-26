#!/usr/bin/env python3
"""Materialize a synthesis agent's decisions into the tree.

Stage B (addendum §16) owns judgment: which domain each table belongs to, which
join candidates are real, what each domain's rules are. Applying those decisions
to 198 shell files and a joins.yml is pure mechanics, and mechanics belong in a
script (design rule) — an agent doing 198 Edit calls is slower, costlier, and
free to typo a table name that no gate would catch until verify.py.

So Stage B writes two small decision files and this script applies them:

  decisions/domains.yml   {table: domain_path}  — every table, no UNASSIGNED left
  decisions/joins.yml     list of {left, right, on, verdict: promote|reject,
                                   reason}     — every joins_draft entry

Both are validated against the real schema and the real draft before anything is
written: an unknown table name, an unknown column in an `on` clause, or a missing
disposition is an error, not a silent pass.

Usage:
  python3 apply_synthesis.py --run <run dir> --workspace <ws>
                             [--scope <scope.tsv>] [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

import yaml


def load_yaml(p: Path):
    return yaml.safe_load(Path(p).read_text()) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--scope", type=Path,
                    help="the scope file whose `model` rows are the tables that "
                         "must all carry a domain. Without it every table in "
                         "the schema export has to be assigned, which is only "
                         "right when the export IS the agreed scope")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    schema = json.loads((args.workspace / "schema.json").read_text())
    # Identifier checks stay against the whole export (a boundary join's
    # counterpart is a real table), but the "every table needs a domain" bar
    # applies to the MODELLED set — otherwise a 2,183-table export demands
    # 2,183 domain assignments for a 50-table ontology.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import load_scope
    must_assign = set(schema)
    if args.scope:
        want = load_scope(args.scope)
        must_assign = {t for t in schema if t in want}
    dec = args.run / "decisions"
    errors, warnings = [], []

    # ---------- domains ----------
    dmap = load_yaml(dec / "domains.yml")
    if isinstance(dmap, dict) and "domains" in dmap:
        dmap = dmap["domains"]
    if not isinstance(dmap, dict):
        errors.append("decisions/domains.yml must be a mapping of table -> domain_path")
        dmap = {}
    unknown = [t for t in dmap if t not in schema]
    if unknown:
        errors.append(f"domains.yml names {len(unknown)} tables not in the schema: "
                      f"{unknown[:5]}")
    missing = [t for t in must_assign if t not in dmap]
    if missing:
        errors.append(f"domains.yml is missing {len(missing)} of {len(must_assign)} "
                      f"tables (the identifier gate fails while any stay "
                      f"UNASSIGNED): {missing[:5]}")
    bad = [f"{t}={p!r}" for t, p in dmap.items()
           if not isinstance(p, str) or not p or p == "UNASSIGNED"]
    if bad:
        errors.append(f"{len(bad)} tables have an empty or UNASSIGNED domain: {bad[:5]}")

    # every domain_path needs a README to exist for the gate to pass
    readme_missing = set()
    for p in set(dmap.values()):
        if not isinstance(p, str) or not p:
            continue
        if not (args.run / "cassis" / "domains" / p / "README.md").exists():
            readme_missing.add(p)
    if readme_missing:
        warnings.append(f"{len(readme_missing)} domain paths have no README yet: "
                        f"{sorted(readme_missing)[:6]}")

    # ---------- joins ----------
    draft = load_yaml(args.run / "joins_draft.yml") or []
    jdec = load_yaml(dec / "joins.yml")
    if isinstance(jdec, dict) and "joins" in jdec:
        jdec = jdec["joins"]
    jdec = jdec or []

    def key(d):
        return (d.get("left"), d.get("right"), tuple(d.get("on") or []))
    decided = {}
    for d in jdec:
        decided[key(d)] = d
    undecided = [key(d) for d in draft if key(d) not in decided]
    if undecided:
        errors.append(f"{len(undecided)} of {len(draft)} joins_draft entries have no "
                      f"disposition: {undecided[:3]}")
    promoted, rejected = [], []
    for d in jdec:
        verdict = str(d.get("verdict", "")).lower()
        if verdict.startswith("promote"):
            promoted.append(d)
        elif verdict.startswith("reject"):
            if not d.get("reason"):
                errors.append(f"rejected join {key(d)} has no reason")
            rejected.append(d)
        else:
            errors.append(f"join {key(d)} has verdict {d.get('verdict')!r}; "
                          f"expected promote or reject")

    # validate promoted joins against the schema, both tables and both columns
    for d in promoted:
        for side in ("left", "right"):
            if d.get(side) not in schema:
                errors.append(f"promoted join names unknown table {d.get(side)!r}")
        for clause in (d.get("on") or []):
            if "=" not in str(clause):
                errors.append(f"promoted join {key(d)} has malformed on-clause {clause!r}")
                continue
            lc, rc = [c.strip() for c in str(clause).split("=", 1)]
            for tbl, col in ((d.get("left"), lc), (d.get("right"), rc)):
                cols = {c.upper() for c in (schema.get(tbl) or {})}
                if col.split(".")[-1].upper() not in cols:
                    errors.append(f"promoted join {d.get('left')}->{d.get('right')}: "
                                  f"column {col!r} not in {tbl}")

    if errors:
        print("REFUSING TO APPLY — fix these first:")
        for e in errors:
            print("  ERROR:", e)
        for w in warnings:
            print("  warn :", w)
        return 1

    print(f"validated: {len(dmap)} domain assignments, {len(promoted)} joins promoted, "
          f"{len(rejected)} rejected (of {len(draft)} candidates)")
    for w in warnings:
        print("  warn :", w)
    if args.dry_run:
        print("dry run — nothing written")
        return 0

    # ---------- write ----------
    n = 0
    for table, dpath in dmap.items():
        sch, name = table.split(".", 1)
        p = args.run / "cassis" / "tables" / sch / f"{name}.yml"
        if not p.exists():
            continue
        rec = yaml.safe_load(p.read_text())
        if rec.get("domain_path") != dpath:
            rec["domain_path"] = dpath
            p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
            n += 1
    print(f"domain_path set on {n} table files")

    out = []
    for d in promoted:
        ls, lt = d["left"].split(".", 1)
        rs, rt = d["right"].split(".", 1)
        out.append({"from_schema": ls, "from_table": lt,
                    "to_schema": rs, "to_table": rt,
                    "on": d.get("on") or [],
                    "relationship": d.get("relationship"),
                    "provenance": d.get("provenance") or "dbt_implicit+agent_reviewed"})
    (args.run / "cassis" / "joins.yml").write_text(
        yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    print(f"joins.yml: {len(out)} joins written")

    rej = ["# Rejected join candidates", "",
           "Every candidate the synthesis stage declined, with its reason. "
           "Mandatory disposition (§16) means silence is not an option — a "
           "candidate absent from joins.yml must appear here.", ""]
    for d in sorted(rejected, key=lambda x: (x.get("left") or "", x.get("right") or "")):
        rej.append(f"- `{d.get('left')}` -> `{d.get('right')}` on "
                   f"{d.get('on')}: {d.get('reason')}")
    (args.run / "REJECTED_JOINS.md").write_text("\n".join(rej) + "\n")
    print(f"REJECTED_JOINS.md: {len(rejected)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
