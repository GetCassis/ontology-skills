#!/usr/bin/env python3
"""Prepare evidence for, and check changes to, an existing Cassis context.

No model calls, warehouse connections, ontology writes, commits or publication.
The run directory is separate from both the maintained tree and the sources.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import sqlglot

from common import dump_json, load_json, metric_expr_cores, normalize_expr, read_csv_rows
from metrics_review import review_rows, render_md
from verify import load_tree, check_tree, has_errors, build_judge_input

KIT = Path(__file__).resolve().parent
SOURCE_FLAGS = ("docs_dir", "context_dir", "glossary_dir", "dashboards_dir",
                "questions_dir", "declared")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(tree):
    out = {}
    for p in sorted(tree.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"context contains a symlink: {p}; use an explicit file copy")
        if p.is_file():
            out[str(p.relative_to(tree))] = digest(p.read_bytes())
    return out


def git_state(path):
    def git(*args):
        r = subprocess.run(["git", "-C", str(path if path.is_dir() else path.parent),
                            *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    return {"commit": git("rev-parse", "HEAD"),
            "status": git("status", "--short"),
            "branch": git("branch", "--show-current")}


def source_state(paths):
    """Revision plus hashes of the supplied files, including uncommitted evidence."""
    result = []
    for source in paths:
        source = source.resolve()
        files = [source] if source.is_file() else sorted(
            p for p in source.rglob("*") if p.is_file()
            and not set(p.relative_to(source).parts) & {".git", "__pycache__", "node_modules", ".venv", "logs", "dbt_packages"})
        result.append({"path": str(source), "git": git_state(source),
                       "files": {str(p.relative_to(source)) if source.is_dir() else p.name:
                                 digest(p.read_bytes()) for p in files}})
    return result


def separate(run, path):
    if run == path or run in path.parents or path in run.parents:
        raise ValueError(f"--run must be separate from the context and every source: {path}")


def execute(script, *args):
    r = subprocess.run([sys.executable, str(KIT / script), *map(str, args)],
                       capture_output=True, text=True)
    if r.returncode:
        raise ValueError(f"{script} failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout + r.stderr


def maybe_json(path, default):
    return load_json(path) if path.exists() else default


def clean_record(rec):
    return {k: v for k, v in rec.items() if k != "path"}


def inventory(tree, workspace, selected):
    current = load_tree(tree)
    schema = load_json(workspace / "schema.json")
    unknown = sorted(set(selected) - set(schema))
    if unknown:
        raise ValueError(f"requested tables absent from schema: {unknown}")
    scoped = set(selected) or set(schema)
    records = load_json(workspace / "repo" / "tables.json").get("records", [])
    sources = {r["table"].upper(): r for r in records}
    comments = maybe_json(workspace / "column_comments.json", {})
    declared = maybe_json(workspace / "declared.json", {})
    table_rows, differences = [], []
    for name in sorted(scoped):
        existing = current["tables"].get(name)
        repo = sources.get(name, {})
        cols = {c["name"].upper(): c for c in (existing or {}).get("columns", [])}
        for col, text in repo.get("column_descriptions", {}).items():
            old = cols.get(col.upper(), {}).get("description")
            if old and text and old.strip() != text.strip():
                differences.append({"table": name, "column": col,
                                    "existing": old, "source_text": text,
                                    "source": repo.get("yml_path") or repo.get("sql_path"),
                                    "reason": "different wording; a person must assess meaning"})
        table_rows.append({
            "table": name, "modeled": existing is not None,
            "domain": (existing or {}).get("domain_path"),
            "missing_columns": sorted(set(schema[name]) - set(cols)),
            "description_gaps": sorted(c for c in schema[name]
                                       if not cols.get(c, {}).get("description")),
            "schema_columns": schema[name], "source_record": repo,
            "warehouse_comments": comments.get(name, {}),
            "existing": clean_record(existing) if existing else None,
        })
    joins = [j for j in load_json(workspace / "evidence" / "join_candidates.json")
             if j.get("left") in scoped or j.get("right") in scoped]
    for j in joins:
        j["outside_selected_scope"] = sorted({j.get("left"), j.get("right")} - scoped - {None})
    queries = []
    for fname in ("questions.csv", "dashboard_queries.csv", "query_history.csv"):
        for q in read_csv_rows(workspace / fname):
            if set(q.get("tables", "").split(";")) & scoped:
                queries.append({"corpus": fname, **q})
    variants = []
    for name, metric in current["metrics"].items():
        table = f"{metric.get('table_schema')}.{metric.get('table_name')}".upper()
        if table not in scoped:
            continue
        cores = set(metric_expr_cores(metric.get("expression")))
        for q in queries:
            if table not in q.get("tables", "").split(";") or not cores:
                continue
            if not cores & set(metric_expr_cores(q["sql"])):
                continue
            try:
                parsed = sqlglot.parse_one(q["sql"])
                where = parsed.args.get("where")
                clause = where.this.sql() if where else ""
            except sqlglot.errors.ParseError:
                clause = "(unparsed; read the original SQL)"
            if normalize_expr(clause) != normalize_expr(metric.get("filters") or ""):
                variants.append({"metric": name, "existing_filter": metric.get("filters"),
                                 "observed_filter": clause, "source": q["id"],
                                 "source_path": q.get("path"), "sql": q["sql"],
                                 "reason": "aggregate used with a different WHERE; check population and audience"})
    return {"selected_tables": sorted(scoped), "tables": table_rows,
            "existing_metrics": {n: clean_record(m) for n, m in current["metrics"].items()},
            "existing_joins": current["joins"], "join_candidates": joins,
            "metric_candidates": [m for m in load_json(workspace / "evidence" / "metric_candidates.json")
                                  if set(m.get("tables", [])) & scoped],
            "declared": declared, "source_queries": queries,
            "source_differences": differences, "metric_variants": variants,
            "documentation": maybe_json(workspace / "docs" / "index.json", []),
            "limits": ["Scope is a user/agent decision; the table inventory does not infer question coverage.",
                       "Different source wording or SQL is a review candidate, not proof of a contradiction.",
                       "Join candidates are not approved joins; check keys, cardinality and fan-out.",
                       "No SQL, model evaluation or publication check was executed."]}


def prepare(args):
    tree, run = args.tree.resolve(), args.run.resolve()
    if not tree.is_dir() or not (tree / "tables").is_dir():
        raise ValueError("--tree must name the existing Cassis directory containing tables/")
    separate(run, tree)
    inputs = [args.schema, args.input]
    for attr in SOURCE_FLAGS:
        inputs.extend(getattr(args, attr))
    inputs.extend(p for p in (args.adapter_config, args.query_history, args.cases, args.provenance) if p)
    for path in inputs:
        path = path.resolve()
        if not path.exists():
            raise ValueError(f"source does not exist: {path}")
        separate(run, path)
    config = {"tree": str(tree), "questions": args.question,
              "tables": sorted({t.upper() for t in args.table}),
              "sources": [str(p.resolve()) for p in inputs], "adapter": args.adapter,
              "cases_sha256": digest(args.cases.read_bytes()) if args.cases else None,
              "source_flags": {k: [str(p.resolve()) for p in getattr(args, k)] for k in SOURCE_FLAGS}}
    before = snapshot(tree)
    marker = run / "expansion.json"
    if run.exists() and any(run.iterdir()) and not marker.exists():
        raise ValueError("run directory is not empty and has no expansion baseline; choose a new one")
    previous = load_json(marker) if marker.exists() else None
    if previous and (previous["config"] != config or previous["files"] != before):
        raise ValueError("baseline or inputs changed; use check for the current draft, or a new run for new scope")
    cases = load_json(args.cases) if args.cases else []
    if not isinstance(cases, list) or not all(isinstance(c, dict) and c.get("question")
                                            and isinstance(c.get("tables"), list) for c in cases):
        raise ValueError("--cases must contain a JSON list of {question, tables: [...]} records")
    run.mkdir(parents=True, exist_ok=True)
    # Rebuild evidence from scratch so removed sources never remain in a reused workspace.
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=run) as td:
        ws = Path(td) / "workspace"
        ing = ["--input", args.input.resolve(), "--schema", args.schema.resolve(),
               "--workspace", ws, "--adapter", args.adapter]
        for attr in SOURCE_FLAGS:
            for path in getattr(args, attr):
                ing += ["--" + attr.replace("_", "-"), path.resolve()]
        for attr in ("adapter_config", "query_history"):
            if getattr(args, attr):
                ing += ["--" + attr.replace("_", "-"), getattr(args, attr).resolve()]
        log = execute("ingest.py", *ing)
        log += execute("evidence.py", "--workspace", ws)
        inv = inventory(tree, ws, config["tables"])
        inv = json.loads(json.dumps(inv).replace(str(ws), str(run / "workspace")))
        # Paths written by the extractors must survive the staging-directory move.
        for p in ws.rglob("*"):
            if p.is_file() and p.suffix in {".json", ".txt", ".csv"}:
                text = p.read_text()
                p.write_text(text.replace(str(ws), str(run / "workspace")))
        if snapshot(tree) != before:
            raise ValueError("context changed during preparation; keep the working tree and start a new run")
        if not previous:
            shutil.copytree(tree, run / "baseline" / "cassis")
            manifest = {"config": config, "files": before, "git": git_state(tree),
                        "source_state": [{"path": str(p.resolve()), "git": git_state(p.resolve()),
                                          "sha256": digest(p.read_bytes()) if p.is_file() else None}
                                         for p in inputs], "cases": cases,
                        "publication": "unverified; local preparation only"}
            dump_json(manifest, marker)
            if args.provenance:
                shutil.copyfile(args.provenance, run / "baseline" / "provenance.json")
        if (run / "workspace").exists():
            shutil.rmtree(run / "workspace")
        shutil.move(str(ws), run / "workspace")
    dump_json(source_state(inputs), run / "evidence-sources.json")
    dump_json(inv, run / "inventory.json")
    (run / "extraction.log").write_text(log)
    baseline = check_tree(tree, run / "workspace", args.provenance)
    dump_json(baseline, run / "baseline-check.json")
    rows, corpus = review_rows(None, run / "workspace", tree, args.provenance)
    (run / "METRICS-REVIEW.md").write_text(render_md(rows, corpus, inv["declared"]))
    lines = ["# Expansion preparation", "", "Questions: " + "; ".join(args.question), "",
             "Review the business scope and proposed structure before authoring. This inventory is evidence, not a domain proposal.", "",
             "| Table | Existing domain | Missing columns | Description gaps |", "|---|---|---:|---:|"]
    for row in inv["tables"]:
        lines.append(f"| {row['table']} | {row['domain'] if row['modeled'] else 'not modeled'} | {len(row['missing_columns'])} | {len(row['description_gaps'])} |")
    lines += ["", f"Source wording differences to review: {len(inv['source_differences'])}.",
              f"Metric/filter variants to review: {len(inv['metric_variants'])}.",
              "Read inventory.json for the original SQL, definitions, candidates and source locations; METRICS-REVIEW.md for corroboration.", "",
              "Existing cases potentially affected (table overlap; shared-rule changes can affect more):"]
    relevant = [c for c in cases if set(t.upper() for t in c["tables"]) & set(inv["selected_tables"])]
    lines += ["- " + c["question"] for c in relevant] or ["- No mapped reference cases supplied. Behaviour remains unverified."]
    lines += ["", "Limits:"] + ["- " + x for x in inv["limits"]]
    (run / "PREPARE.md").write_text("\n".join(lines) + "\n")
    print(f"Prepared {len(inv['tables'])} table candidates; maintained context unchanged. Read {run / 'PREPARE.md'}")
    return 1 if has_errors(baseline) else 0


def domain_body(text):
    return re.sub(r"<!-- cassis:nav:begin.*?<!-- cassis:nav:end -->", "", text,
                  flags=re.S).strip()


def objects(tree):
    t = load_tree(tree)
    result = {f"table:{n}": clean_record(v) for n, v in t["tables"].items()}
    result.update({f"metric:{n}": clean_record(v) for n, v in t["metrics"].items()})
    result.update({f"domain:{n}": domain_body(v) for n, v in t["readmes"].items()})
    # Compare each relationship, so additions do not conceal a removed or changed join.
    for j in t["joins"]:
        endpoints = [str(j.get(k, "")) for k in ("from_schema", "from_table", "to_schema", "to_table")]
        key = "join:" + ".".join(endpoints) + ":" + json.dumps(j.get("column_pairs", j.get("on", [])), sort_keys=True)
        if key in result:
            raise ValueError(f"duplicate join: {key}")
        result[key] = j
    return result


def check(args):
    run, tree = args.run.resolve(), args.tree.resolve()
    separate(run, tree)
    manifest = load_json(run / "expansion.json")
    if str(tree) != manifest["config"]["tree"]:
        raise ValueError("--tree differs from the recorded baseline path")
    base = run / "baseline" / "cassis"
    if snapshot(base) != manifest["files"]:
        raise ValueError("baseline snapshot changed; restore it before comparing the draft")
    before = snapshot(tree)
    report = check_tree(tree, run / "workspace", args.provenance)
    old, new = objects(base), objects(tree)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed, refactors = [], []
    for key in sorted(set(old) & set(new)):
        if old[key] == new[key]:
            continue
        if isinstance(old[key], dict) and isinstance(new[key], dict) and \
                {k: v for k, v in old[key].items() if k != "domain_path"} == \
                {k: v for k, v in new[key].items() if k != "domain_path"}:
            refactors.append({"object": key, "before": old[key].get("domain_path"),
                              "after": new[key].get("domain_path")})
        else:
            changed.append({"object": key, "before": old[key], "after": new[key]})
    files = {"added": sorted(set(before) - set(manifest["files"])),
             "removed": sorted(set(manifest["files"]) - set(before)),
             "modified": sorted(k for k in set(before) & set(manifest["files"])
                                if before[k] != manifest["files"][k])}
    git = git_state(tree)
    base_advanced = manifest["git"]["commit"] != git["commit"]
    needs_review = bool(changed or removed or refactors or files["removed"] or base_advanced)
    result = {"checks": report, "added": added, "removed": removed,
              "changed_definitions": changed, "refactors": refactors, "files": files,
              "baseline_commit": manifest["git"]["commit"], "current_commit": git["commit"],
              "base_advanced": base_advanced, "review_required": needs_review,
              "reference_cases": manifest["cases"],
              "behaviour": "not evaluated by this command; run the project's authorized reference cases",
              "publication": "unverified; do not infer merge/publish approval from this report"}
    dump_json(result, run / "expansion-check.json")
    dump_json(build_judge_input(load_tree(tree), run / "workspace"), run / "judge_input.json")
    rows, corpus = review_rows(None, run / "workspace", tree, args.provenance)
    declared = maybe_json(run / "workspace" / "declared.json", {})
    (run / "METRICS-REVIEW.md").write_text(render_md(rows, corpus, declared))
    lines = ["# Expansion review", "", f"Added objects: {len(added)}. Removed: {len(removed)}. Changed definitions: {len(changed)}. Refactors: {len(refactors)}.",
             "", "## Existing meaning requiring review"]
    lines += [f"- {c['object']} (before/after in expansion-check.json)" for c in changed]
    lines += [f"- Removed: {k}" for k in removed]
    lines += [f"- Moved: {c['object']}: {c['before']} → {c['after']}" for c in refactors]
    if not (changed or removed or refactors):
        lines.append("No existing object meaning changed in the structured comparison.")
    if base_advanced:
        lines += ["", "The checkout commit changed; reconcile the baseline before calling this ready to merge."]
    lines += ["", "## Checks"]
    lines += [f"- {k}: {len(v)} findings" for k, v in report.items()]
    lines += ["", "Read expansion-check.json for the file diff and reference cases, METRICS-REVIEW.md for metric evidence, and judge_input.json for claims requiring source review.",
              "No answer accuracy, human acceptance, live publication state or external consumers were verified."]
    (run / "REVIEW.md").write_text("\n".join(lines) + "\n")
    if snapshot(tree) != before:
        raise ValueError("context changed during checking; rerun against a stable draft")
    print(f"Structural errors: {has_errors(report)}; existing changes need review: {needs_review}. Read {run / 'REVIEW.md'}")
    return 1 if has_errors(report) else 2 if needs_review else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="extract evidence and record an immutable baseline")
    for name in ("tree", "run", "input", "schema"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--question", action="append", required=True)
    p.add_argument("--table", action="append", default=[], help="physical SCHEMA.TABLE; repeat for chosen scope, omit for inventory only")
    p.add_argument("--adapter", choices=["auto", "dbt", "dbtdocs", "generic", "none"], default="auto")
    for name in SOURCE_FLAGS:
        p.add_argument("--" + name.replace("_", "-"), type=Path, action="append", default=[])
    for name in ("adapter-config", "query-history", "cases", "provenance"):
        p.add_argument("--" + name, type=Path)
    p.set_defaults(fn=prepare)
    p = commands.add_parser("check", help="compare to baseline; exit 1 errors, 2 existing changes needing review, 0 no such findings")
    for name in ("tree", "run"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--provenance", type=Path)
    p.set_defaults(fn=check)
    args = ap.parse_args()
    try:
        return args.fn(args)
    except (ValueError, OSError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
