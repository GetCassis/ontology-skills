#!/usr/bin/env python3
"""Stage 0 — intake. Ask once, record the answers, never re-ask mid-run.

Why this exists: an early run had the driving agent repeatedly try to reach
a warehouse the user had no access to, and the user had to know from
experience to scope the core first and iterate. Both facts belong in the config before anything runs: what material
exists, whether a warehouse connection exists, and what kind of run this is.
A recorded answer is binding — an agent that re-asks, or "just tries" a
connection the intake said does not exist, is the bug this step removes.

Two subcommands:

  questions   print the intake questionnaire. The driving agent relays it to
              the user VERBATIM in one message and collects the answers in
              chat — no per-question back-and-forth.
  write       turn the answers into configs/<name>.yml. Every path is
              validated to exist NOW: an intake typo caught here costs
              seconds; the same typo caught by evidence.py costs a run that
              silently read nothing (the silent-data-loss class).

Missing material must be declared, not implied. The two families whose silent
absence has cost real runs (documentation and the BI corpus) are mandatory:
point at them with a path flag, or state
--absent documentation / --absent bi_corpus explicitly. "Empty because the
warehouse has none" and "empty because nobody passed the flag" must never look
the same again.

Usage:
  python3 intake.py questions
  python3 intake.py write --name acme \\
      --schema $BOOTSTRAP_FIXTURES/acme/schema.json \\
      --input  $BOOTSTRAP_FIXTURES/acme/dbt --adapter dbt \\
      --docs-dir ... --dashboards-dir ... [--absent query_history] \\
      --warehouse-access no --profile full \\
      [--top-question "..." ...] [--out configs/acme.yml] [--force]
"""
import argparse
import os
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent

ABSENT_VOCAB = ("documentation", "bi_corpus", "query_history")

QUESTIONS = """\
Intake — nine questions, one message. Relay them verbatim; collect the
answers in chat; then run `intake.py write`. Nothing here asks you to WRITE
documentation — point the kit at what you already have, in any shape.

1. Schema export — a file listing every table and column in the warehouse
   (information-schema export, schema.json). Mandatory.        --schema
2. Transformation repo — the dbt project (or equivalent), or a docs export
   of it. Mandatory: without defining SQL there is no grain, no join
   evidence, no unit claim.                        --input, --adapter
   (adapters: dbt = a real dbt project, dbtdocs = a dbt docs export;
    anything else = LLM-config fallback)
3. Existing documentation, ANY shape — wiki or Notion export, reference
   docs, a column glossary, model pages. Paths, or "we have none".
                 --docs-dir / --context-dir / --glossary-dir (repeatable),
                 or --absent documentation
4. BI layer — dashboard exports or saved questions carrying native SQL.
   Paths, or "we have none". This is join evidence, usage ranking and
   metric corroboration at once.       --dashboards-dir / --questions-dir,
                 or --absent bi_corpus
5. Query history — a log of real queries, if exportable. Optional.
                 --questions-dir, or --absent query_history
6. Can you run SQL against the warehouse right now, yourself? yes/no.
   Recorded once and binding: "no" means the kit and every agent driving it
   never attempt a connection and skip profiling — nothing will ask again.
                 --warehouse-access yes|no
7. How much of the warehouse should this pass cover? Only the scope
   target and the cost change; the output is the same shape either way.
   "sample" = about ten tables carrying one story end to end — the cheap
   way to see what the output looks like on your own data before paying
   for the rest. "full" = the 20-30 tables that answer your questions in
   (8), which is a first increment you can actually use (60-100 tables is
   full coverage, over several passes).            --profile sample|full
8. The ~20 questions you most want answered from this data (free text, as
   many as you have now — the scope checkpoint refines them).
                 --top-question (repeatable)
9. Where should the result land?
   "tree"  = a standalone ontology: one directory of YAML and Markdown —
             your domains, tables, columns with descriptions and units,
             metrics and joins — that a person or an agent reads to answer
             questions about this warehouse. Tool-independent, diffable,
             lives in git next to anything.
   "dbt"   = the same thing merged back into the dbt project it came from:
             descriptions into the `schema.yml` files you already have
             (never overwriting what is there), joins as `relationships`
             tests, and everything dbt has no field for under `meta`.
             Needs the input to BE a dbt project, not a docs export.
   "both"  = both, and this is the common answer. The tree is written
             either way, because the dbt export is a projection of it.
                 --emit tree|dbt|both
"""

CONFIG_TEMPLATE = """\
# Intake for {name} — generated by intake.py, edit by hand as things change.
# Facts recorded here are binding for every stage and every agent: do not
# re-ask them mid-run, and never attempt what they rule out.
name: {name}

# "no" at intake means: no connection attempts of ANY kind, no profile.py,
# no retry. Data questions only the warehouse could settle go to QUESTIONS.md.
warehouse_access: {warehouse_access}

# How much of the warehouse this pass covers: sample = ~10 tables carrying one
# story, full = the 20-30 that answer your questions. Drives the scope
# checkpoint's target and the cost estimate, nothing else.
profile: {profile}
{absent_block}{questions_block}
input: {input}
{adapter_line}schema: {schema}

workspace: {workspace}
run: {run}
batches: {batches}
{emit_block}{dir_blocks}
# scope_file: where your scoping decision is recorded (defaults to <run>/scope.tsv)
"""


def expand(v):
    return os.path.expandvars(os.path.expanduser(str(v)))


def cmd_questions(_args):
    print(QUESTIONS)
    return 0


def _dir_block(comment, key, paths):
    if not paths:
        return ""
    lines = [f"{key}:{' ' * max(1, 30 - len(key) - 1)}# {comment}"]
    lines += [f"  - {p}" for p in paths]
    return "\n".join(lines) + "\n"


def cmd_write(args):
    errors = []

    # every path must exist now — a typo here is the cheapest bug in the kit
    for label, values in (("--schema", [args.schema]), ("--input", [args.input]),
                          ("--glossary-dir", args.glossary_dir),
                          ("--docs-dir", args.docs_dir),
                          ("--context-dir", args.context_dir),
                          ("--dashboards-dir", args.dashboards_dir),
                          ("--questions-dir", args.questions_dir)):
        for v in values:
            if not Path(expand(v)).exists():
                errors.append(f"{label} {v!r} does not exist (expanded: {expand(v)!r})")

    absent = set(args.absent or [])
    unknown = absent - set(ABSENT_VOCAB)
    if unknown:
        errors.append(f"--absent accepts {ABSENT_VOCAB}, got {sorted(unknown)}")

    # missing material is declared, never implied
    if not (args.docs_dir or args.context_dir or args.glossary_dir) \
            and "documentation" not in absent:
        errors.append("no documentation paths given: pass --docs-dir/--context-dir/"
                      "--glossary-dir, or state --absent documentation explicitly")
    if not (args.dashboards_dir or args.questions_dir) and "bi_corpus" not in absent:
        errors.append("no BI paths given: pass --dashboards-dir/--questions-dir, "
                      "or state --absent bi_corpus explicitly")
    # Writing back into a project needs the project. A dbt docs export is a
    # snapshot of one, with no schema.yml files to merge into.
    if args.emit in ("dbt", "both") and (args.adapter or "").lower() != "dbt":
        errors.append(f"--emit {args.emit} merges the result back into the dbt "
                      f"project's own schema.yml files, so it needs "
                      f"--adapter dbt; got {args.adapter or 'none'}. Use "
                      f"--emit tree, or point --input at the dbt project itself")

    declared_but_present = [a for a in absent if
                            (a == "documentation" and (args.docs_dir or args.context_dir
                                                       or args.glossary_dir)) or
                            (a == "bi_corpus" and (args.dashboards_dir
                                                   or args.questions_dir))]
    if declared_but_present:
        errors.append(f"--absent {declared_but_present} contradicts the paths you "
                      f"passed for the same material")

    out = args.out or (KIT / "configs" / f"{args.name}.yml")
    if out.exists() and not args.force:
        errors.append(f"{out} already exists — it encodes intake decisions; "
                      f"pass --force only if you mean to redo the intake")

    if errors:
        print("REFUSING TO WRITE — fix these first:", file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1

    absent_block = ""
    if absent:
        absent_block = ("\n# Confirmed absent at intake — these do not exist. An empty "
                        "evidence class here is\n# expected — do not go hunting "
                        "for it, and do not ask again.\nconfirmed_absent:\n"
                        + "".join(f"  - {a}\n" for a in sorted(absent)))
    questions_block = ""
    if args.top_question:
        questions_block = ("\n# What you most want answered — the scope "
                           "checkpoint starts from these.\ntop_questions:\n"
                           + "".join(f"  - {q!r}\n" for q in args.top_question))

    dir_blocks = "\n" + "".join(filter(None, [
        _dir_block("column glossary keyed by COLUMN NAME", "glossary_dirs",
                   args.glossary_dir),
        _dir_block("curated reference docs", "docs_dirs", args.docs_dir),
        _dir_block("anything else: prose, exports, code", "context_dirs",
                   args.context_dir),
        _dir_block("dashboard exports with native SQL", "dashboards_dirs",
                   args.dashboards_dir),
        _dir_block("saved questions / query history", "questions_dirs",
                   args.questions_dir),
    ]))

    # The tree is always written — the dbt export is a projection of it — so
    # this key records only whether to ALSO merge into the project.
    emit_block = ""
    if args.emit in ("dbt", "both"):
        emit_block = ("\n# Also merge the result back into the dbt project it "
                      "came from: descriptions\n# into the schema.yml files "
                      "already there, joins as relationships tests, and what\n"
                      "# dbt has no field for under meta. The ontology tree is "
                      "written either way.\nemit:\n  - dbt\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(CONFIG_TEMPLATE.format(
        name=args.name,
        warehouse_access="true" if args.warehouse_access == "yes" else "false",
        profile="sample" if args.profile == "demo" else args.profile,
        absent_block=absent_block,
        questions_block=questions_block,
        input=args.input,
        adapter_line=f"adapter: {args.adapter}\n" if args.adapter else "",
        schema=args.schema,
        workspace=args.workspace or f"{args.name}-run/workspace",
        run=args.run or f"{args.name}-run",
        batches=args.batches,
        emit_block=emit_block,
        dir_blocks=dir_blocks,
    ))
    print(f"wrote {out}")
    print(f"next: python3 bootstrap.py prep --config {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("questions", help="print the intake questionnaire")
    q.set_defaults(fn=cmd_questions)
    w = sub.add_parser("write", help="validate answers and write configs/<name>.yml")
    w.add_argument("--name", required=True,
                   help="short name for this run; names the config file and the "
                        "run directory")
    w.add_argument("--schema", required=True)
    w.add_argument("--input", required=True)
    w.add_argument("--adapter", help="dbt | dbtdocs (else: LLM-config fallback)")
    w.add_argument("--warehouse-access", required=True, choices=("yes", "no"))
    # `demo` resolves as an alias for `sample`, because a config is a recorded
    # fact and should keep working whichever spelling it carries.
    w.add_argument("--profile", required=True,
                   choices=("full", "sample", "demo"),
                   help="run size; `demo` is an alias for `sample`")
    for flag in ("--glossary-dir", "--docs-dir", "--context-dir",
                 "--dashboards-dir", "--questions-dir"):
        w.add_argument(flag, action="append", default=[])
    w.add_argument("--emit", default="tree", choices=("tree", "dbt", "both"),
                   help="where the result lands: the standalone ontology tree "
                        "(always written), also merged into the dbt project, "
                        "or both")
    w.add_argument("--absent", action="append", default=[],
                   help=f"declare missing material: {', '.join(ABSENT_VOCAB)}")
    w.add_argument("--top-question", action="append", default=[])
    w.add_argument("--workspace")
    w.add_argument("--run")
    w.add_argument("--batches", type=int, default=12)
    w.add_argument("--out", type=Path)
    w.add_argument("--force", action="store_true")
    w.set_defaults(fn=cmd_write)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
