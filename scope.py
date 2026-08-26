#!/usr/bin/env python3
"""Two-level scoping: what we MODEL vs what we READ.

Level 1 — technical (this script, no business input needed). A warehouse ships a
lot of surface that is not the business: source mirrors, export/serving layers,
tooling artifacts, intermediate plumbing, backups. Measured on three real
warehouses, entire schemas get thrown away — one excluded 96 of 96 staging
tables and all 4 seeds, another all 29 export tables and all 18 backend
tables. On the first, that is 59% of the columns the enrichment run paid for.

Level 2 — business (a human, or the question list). Of what survives, which
tables answer the questions people actually ask. This script proposes an ordering
for that conversation; it does not decide it.

**The invariant that matters: excluded from modeling is NOT excluded from
evidence.** A staging mirror's SQL is where the casts, renames and cleaning rules
live, and those facts are what make a mart column's description correct. Seeds
carry business vocabulary worth having as synonyms. So level 1 narrows what we
assemble and never narrows what we read.

Level-1 rules must never exclude a table a human would model. The rules below are
calibrated so that on all three fixtures they exclude nothing the gold keeps;
where they are unsure they keep, and let level 2 trim.

Usage:
  python3 scope.py propose --workspace WS [--config scope.yml] [--out FILE]
  python3 scope.py check   --workspace WS --scope FILE [--gold-scope FILE]
"""
import argparse
import csv
import fnmatch
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# Rules are graded by EVIDENCE STRENGTH, because a schema's name is weak evidence
# and two real warehouses proved it: one's reference scope KEEPS `BI_COST_MONITORING`
# (BI cost is a business subject there, not dbt plumbing) and another's keeps 6
# of 11 tables in a schema literally called RAW. So:
#
#   hard   -> read_only. Only where the exclusion is provable: the repo labels the
#             model a staging/intermediate layer, or the name is unmistakable
#             (dbt's own run artifacts, a dated backup).
#   soft   -> review. Probably not a business entity, but we cannot prove it, so a
#             human decides. Keeping it costs a review line; dropping it silently
#             costs an entity.
#
# Level 1's guarantee is about the hard tier only: it must never put a table a
# human would model into read_only.
RULES_HARD = [
    ("tooling", "dbt's own run artifacts, not business data",
     ["*dbt_artifact*", "*elementary*", "*monte_carlo*"],
     ["dbt_invocation*", "dbt_model*", "dbt_test*", "dbt_seed*", "dbt_source*",
      "run_result*", "*source_freshness*", "*schema_change*", "alerts_dbt_*"]),
    ("backup", "backup / dated copy / deprecated leftover",
     ["*_backup*", "*_bak"],
     ["*_bak", "*_backup", "*_old", "*_deprecated", "*_copy", "*_tmp", "*_temp",
      "tmp_*", "temp_*", "*_[0-9][0-9][0-9][0-9][0-9][0-9]",
      "*_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"]),
    ("sandbox", "development / personal sandbox schema",
     ["dev", "dev_*", "*_dev", "sandbox*", "scratch*", "playground*", "poc*",
      "staging_dev*", "dbt_[a-z]"],
     []),
    ("export", "export / reverse-ETL / application-serving layer: data leaving the "
               "warehouse, not a place analysts ask questions",
     ["*export*", "*reverse_etl*", "serving*", "*_serving", "backend*",
      "*_backend"],
     ["exp_*", "export_*"]),
    ("seed", "small static lookup; its VALUES stay as vocabulary evidence, but a "
             "seed is rarely an entity analysts query",
     ["*seed*"],
     []),
]

RULES_SOFT = [
    ("maybe_source_mirror", "schema name suggests a raw/landing layer — but the "
                            "repo does not label it one, and a table here may "
                            "still be the only home of a real entity",
     ["*staging*", "stg*", "raw*", "*_raw", "source*", "landing*", "bronze*",
      "ods*", "replica*"],
     ["stg_*", "src_*", "raw_*"]),
    ("maybe_intermediate", "name suggests intermediate plumbing",
     ["*intermediate*"],
     ["int_*", "prep_*", "prepare_*", "wk_*", "work_*", "*_intermediate"]),
    ("maybe_ml_artifact", "looks like an ML feature store or embedding table",
     ["*embedding*", "*feature_store*", "*vector*"],
     ["ml_feat*", "ml_reco*", "*_embedding", "*_embeddings", "*feature_vector*"]),
    ("maybe_monitoring", "looks like monitoring/alerting — which IS a business "
                         "subject in some companies; confirm before dropping",
     [], ["alerting_*", "monitoring_*", "*_anomaly_detection", "*metabase_cost*"]),
]
RULES = RULES_HARD + RULES_SOFT


def _match(patterns, *values) -> bool:
    for v in values:
        if not v:
            continue
        v = str(v).lower()
        for p in patterns:
            if fnmatch.fnmatch(v, p.lower()):
                return True
    return False


def classify(table: str, model: str = "", layer: str = "") -> tuple:
    """(decision, code, why) where decision is model | read_only | review."""
    schema, name = (table.split(".", 1) if "." in table else ("", table))

    # Strongest signal first: the repo's own layer label. It is what makes 96
    # staging mirrors a provable exclusion on one warehouse, and its absence is
    # what makes another's RAW schema unprovable.
    lay = (layer or "").lower()
    if lay in ("staging", "source"):
        return ("read_only", "source_mirror",
                "the repo labels this model a staging/source layer; its SQL stays "
                "as evidence")
    if lay in ("intermediate", "prep"):
        return ("read_only", "intermediate",
                "the repo labels this model an intermediate layer")

    for code, why, schema_pats, name_pats in RULES_HARD:
        if (schema_pats and _match(schema_pats, schema)) or \
           (name_pats and _match(name_pats, name, model)):
            return "read_only", code, why
    for code, why, schema_pats, name_pats in RULES_SOFT:
        if (schema_pats and _match(schema_pats, schema)) or \
           (name_pats and _match(name_pats, name, model)):
            return "review", code, why
    return "model", "keep", ""


def load_config(path):
    if not path or not Path(path).exists() or yaml is None:
        return {}
    return yaml.safe_load(Path(path).read_text()) or {}


def cmd_propose(args):
    schema = load_json(args.workspace / "schema.json")
    recs = {}
    rp = args.workspace / "repo" / "tables.json"
    if rp.exists():
        bare = {}
        for k in schema:
            bare.setdefault(k.split(".", 1)[1].upper(), []).append(k)
        for r in load_json(rp)["records"]:
            nm = str(r["table"]).upper()
            hits = [nm] if nm in schema else bare.get(nm, [])
            if len(hits) == 1:
                recs[hits[0]] = r

    cfg = load_config(args.config)
    force_in = cfg.get("include") or []
    force_out = cfg.get("exclude") or []

    rows, counts = [], Counter()
    for t in sorted(schema):
        rec = recs.get(t, {})
        decision, code, why = classify(t, rec.get("dbt_model", ""),
                                       rec.get("layer", ""))
        # explicit config always wins, and is recorded as such
        if force_out and _match(force_out, t, t.split(".", 1)[-1]):
            decision, code, why = "read_only", "config_exclude", "excluded by scope config"
        elif force_in and _match(force_in, t, t.split(".", 1)[-1]):
            decision, code, why = "model", "keep", "kept by scope config"
        counts[(decision, code)] += 1
        rows.append({"table": t, "decision": decision,
                     "rule": code, "why": why,
                     "columns": len(schema[t]),
                     "has_model": "yes" if t in recs else "no"})

    out = args.out or (args.workspace / "scope-proposed.tsv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["table", "decision", "rule", "columns", "has_model", "why"])
        for r in rows:
            w.writerow([r["table"], r["decision"], r["rule"], r["columns"],
                        r["has_model"], r["why"]])

    model = [r for r in rows if r["decision"] == "model"]
    review = [r for r in rows if r["decision"] == "review"]
    mcols = sum(r["columns"] for r in model)
    tcols = sum(r["columns"] for r in rows)
    print(f"level 1 (technical): model {len(model)}/{len(rows)} tables, "
          f"{mcols}/{tcols} columns ({100 * mcols // max(tcols, 1)}% of the surface)"
          + (f" | {len(review)} need a human decision" if review else ""))
    for (dec, code), n in counts.most_common():
        if code == "keep":
            continue
        why = next((w for c, w, _, _ in RULES if c == code), "")
        print(f"    {dec:9s} {n:4d} {code:20s} {why[:58]}")
    print(f"-> {out}")
    print("\nLevel 2 is a conversation, not a rule: of the tables above marked "
          "`model`, start with the ones that answer your top ~20 questions.\n"
          "A first complete ontology is 60-100 tables; the first publishable "
          "increment is 20-30. Edit the file and re-run `scope.py check`.")
    return 0


def cmd_check(args):
    schema = load_json(args.workspace / "schema.json")
    chosen, review, dropped, unknown = set(), set(), set(), []
    rules = {}
    for row in csv.DictReader(open(args.scope), delimiter="\t"):
        t = (row.get("table") or "").strip().upper()
        if not t:
            continue
        if t not in schema:
            unknown.append(t)
        d = (row.get("decision") or "model").strip()
        rules[t] = row.get("rule") or ""
        (chosen if d == "model" else review if d == "review" else dropped).add(t)
    print(f"scope file: {len(chosen)} to model, {len(review)} awaiting a human "
          f"decision, {len(dropped)} read-only "
          f"({sum(len(schema[t]) for t in chosen if t in schema)} columns needing descriptions)")
    if unknown:
        print(f"ERROR: {len(unknown)} tables are not in schema.json: {unknown[:5]}")
        return 1
    if args.gold_scope:
        gold = {r[0].strip().upper() for r in
                csv.reader(open(args.gold_scope), delimiter="\t") if r and r[0].strip()}
        # The only real failure is a HARD drop of a table a human would model.
        # A `review` line is the classifier saying "I cannot prove this" — it costs
        # a review, not an entity, and that is the intended behavior.
        hard_miss = sorted(gold & dropped)
        pending = sorted(gold & review)
        print(f"vs a reference scope of {len(gold)}: {len(gold & chosen)} agreed "
              f"outright, {len(pending)} routed to human review, "
              f"{len(chosen - gold)} extra kept (level 2 trims those), "
              f"{len(hard_miss)} HARD-DROPPED")
        for t in pending[:6]:
            print(f"    review: {t}  ({rules.get(t)})")
        if hard_miss:
            print("  FAIL: level 1 must never hard-drop a table a human would model:")
            for t in hard_miss[:10]:
                print(f"    {t}  (rule: {rules.get(t)})")
            return 1
        print("  PASS: nothing the reference scope keeps was hard-dropped")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--out", type=Path)
    p.set_defaults(fn=cmd_propose)
    c = sub.add_parser("check")
    c.add_argument("--workspace", type=Path, required=True)
    c.add_argument("--scope", type=Path, required=True)
    c.add_argument("--gold-scope", type=Path)
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
