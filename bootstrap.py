#!/usr/bin/env python3
"""One command per phase, so nobody has to remember the order.

The deterministic chain is seven scripts with flags that must agree. Asking a
person — or an agent — to sequence that by hand is how a run ends up skipping
prefill and paying an agent to retype documentation that was already on disk.

Intake comes first, then three phases, with four points where a human
genuinely decides:

  intake.py  nine questions, asked ONCE (sources, warehouse access, how much
             of the warehouse, where the result lands)
  prep    ingest -> evidence -> scope propose      STOP 1: you choose the scope
  build   scope check -> skeleton -> prefill -> packets
          then the enrichment stages (agent work)  STOP 2: you review the tree
          B2 + apply_synthesis + metrics_review    STOP 3: you review the metrics
  finish  verify -> metrics review -> questions
          merge -> review -> gate -> emit          STOP 4: you answer the
          (the ontology tree always; --emit dbt          blocking questions
           to also merge it back into the dbt
           project it came from)

Everything else runs unattended. Your paths AND intake facts live in a
config file so they are stated once:

  python3 intake.py questions          # a new run: relay these, then `write`
  python3 bootstrap.py prep   --config configs/<name>.yml
  python3 bootstrap.py build  --config configs/<name>.yml
  python3 bootstrap.py finish --config configs/<name>.yml
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parent


def expand(v):
    return os.path.expandvars(os.path.expanduser(str(v)))


def run(script, *args, optional=False):
    cmd = [sys.executable, str(KIT / script)] + [str(a) for a in args]
    print(f"\n$ {' '.join(cmd[1:])}")
    r = subprocess.run(cmd)
    if r.returncode and not optional:
        print(f"\nFAILED: {script} exited {r.returncode}. Fix this before continuing "
              f"— a later phase will inherit the problem silently.")
        sys.exit(r.returncode)
    return r.returncode


def resolve_config(path):
    """The config path as typed, with one fallback: a path that does not
    exist but whose file is in `configs/` (where intake.py writes them)
    resolves there instead of failing on a missing file."""
    p = Path(path)
    if p.exists():
        return p
    alt = KIT / "configs" / p.name
    if alt.exists():
        print(f"NOTE: {p} does not exist; reading {alt} instead "
              f"(configs/ is where intake.py writes these).")
        return alt
    return p


def load_cfg(path):
    cfg = yaml.safe_load(resolve_config(path).read_text()) or {}
    for k in ("input", "schema", "workspace", "run", "scope_file", "gold", "scope"):
        if cfg.get(k):
            cfg[k] = expand(cfg[k])
    for k in ("glossary_dirs", "context_dirs", "docs_dirs", "dashboards_dirs",
              "questions_dirs"):
        cfg[k] = [expand(x) for x in (cfg.get(k) or [])]
    return cfg


# What a phase costs, printed before it starts. Measured, not projected: the
# sample profile is one 10-table run with stage C skipped; the full increment is
# the range across the paid runs. No enforcement — the point is that nobody
# starts a phase without knowing what it spends, since it is your money.
PHASE_COST = {
    "prep": {
        "*": ("no model tokens — every step is a script",
              "a few minutes, mostly reading your material off disk"),
    },
    "build": {
        # the deterministic prep here is free; the enrichment stages are the run
        "sample": ("~0.6M written tokens, all Sonnet-tier, with the per-column "
                   "per-column enrichment skipped", "~40 min"),
        "full": ("~1.6M written tokens — the driving model plus ~20 Sonnet "
                 "subagents; on the API, re-read context roughly doubles "
                 "the bill beyond what is written", "1-2 h"),
    },
    "finish": {
        "*": ("no model tokens for the scripts, plus the judge pass over "
              "judge_input.json — ~100k per 25 claims, and the only step that "
              "has ever caught a description contradicting its own SQL",
              "a few minutes"),
    },
}


def cost_banner(phase, profile):
    spec = PHASE_COST[phase]
    tokens, wall = spec.get(profile) or spec.get("*") or spec["full"]
    banner(f"{phase} — expect {wall}", [
        f"tokens : {tokens}",
        f"time   : {wall}",
        f"profile: {profile} (recorded at intake)",
        "This is a range from previous runs, not a budget the kit enforces.",
    ])


def banner(title, lines):
    w = max(len(title), *(len(l) for l in lines)) + 4
    print("\n" + "=" * w)
    print(f"  {title}")
    print("=" * w)
    for l in lines:
        print(f"  {l}")
    print("=" * w + "\n")


def normalize_profile(profile: str) -> str:
    """`demo` is an accepted alias for `sample`: what the profile sets is how
    much of the warehouse one pass covers, and a config that says `demo` keeps
    working because an intake answer is a recorded fact, not a spelling."""
    return "sample" if profile == "demo" else profile


def intake_facts(c, config_path):
    """The two intake facts every phase honors. A config that predates
    intake.py gets a nudge, not a failure — old configs keep working."""
    if "warehouse_access" not in c:
        print(f"NOTE: {config_path} predates intake.py (no warehouse_access/"
              f"profile keys). Run `python3 intake.py questions` next time a "
              f"run starts; assuming no warehouse access and a full run.")
    return (bool(c.get("warehouse_access", False)),
            normalize_profile(c.get("profile", "full")))


def cmd_prep(args):
    c = load_cfg(args.config)
    wh, profile = intake_facts(c, args.config)
    cost_banner("prep", profile)
    ing = ["--input", c["input"], "--schema", c["schema"],
           "--workspace", c["workspace"]]
    if c.get("adapter"):
        ing += ["--adapter", c["adapter"]]
    for k, flag in (("glossary_dirs", "--glossary-dir"),
                    ("context_dirs", "--context-dir"),
                    ("docs_dirs", "--docs-dir"),
                    ("dashboards_dirs", "--dashboards-dir"),
                    ("questions_dirs", "--questions-dir")):
        for d in c.get(k) or []:
            ing += [flag, d]
    run("ingest.py", *ing)
    ev = ["--workspace", c["workspace"]]
    if c.get("scope"):
        ev += ["--scope", c["scope"]]
    run("evidence.py", *ev)
    scope_out = c.get("scope_file") or str(Path(c["run"]) / "scope.tsv")
    Path(scope_out).parent.mkdir(parents=True, exist_ok=True)
    run("scope.py", "propose", "--workspace", c["workspace"], "--out", scope_out)
    if profile == "sample":
        target = ["  2. cut level 2 HARD: you asked for a sample at intake, so "
                  "keep only the",
                  "     ~10 tables that carry ONE story end to end — nothing else"]
    else:
        target = ["  2. cut level 2 to the tables answering your top ~20 questions",
                  "     20-30 tables for a first increment; 60-100 for a full ontology"]
    if c.get("top_questions"):
        target += ["     (intake recorded your top questions — start from "
                   "`top_questions:` in the config)"]
    profiling = (["", "Optional while you are here: profile.py plan writes SQL "
                      "YOU run yourself;", "nothing connects out."] if wh else
                 ["", "No warehouse access (recorded at intake): profiling is "
                      "OFF. Do not attempt", "any connection; data questions go "
                      "to QUESTIONS.md."])
    banner("CHECKPOINT 1 of 4 — the scope is yours to decide", [
        f"Edit {scope_out}:",
        "  1. settle every `review` row (the classifier could not prove those)",
        "     The `decision` column takes one of three values: `model` = build",
        "     it, `read_only` = out of this run's scope, `review` = undecided.",
        "     Cutting a table means writing `read_only`, not deleting the row.",
        *target, *profiling,
        "",
        "Then: python3 bootstrap.py build --config " + str(args.config),
    ])
    return 0


def cmd_build(args):
    c = load_cfg(args.config)
    _, profile = intake_facts(c, args.config)
    cost_banner("build", profile)
    scope_file = c.get("scope_file") or str(Path(c["run"]) / "scope.tsv")
    run("scope.py", "check", "--workspace", c["workspace"], "--scope", scope_file)
    run("skeleton.py", "--workspace", c["workspace"], "--run", c["run"],
        "--batches", str(c.get("batches", 12)), "--scope", scope_file)
    run("dispatch_prep.py", "prefill", "--run", c["run"],
        "--workspace", c["workspace"])
    run("dispatch_prep.py", "packets", "--run", c["run"],
        "--workspace", c["workspace"])
    banner("Prep is done. Evidence-backed enrichment is next — see addendum_v2.md §16", [
        "B1  the domain tree, rules, and decisions/domains.yml   (Opus, alone)",
        "",
        "CHECKPOINT 2 of 4 — read TREE.md and the domain READMEs and correct them",
        "BEFORE B2 and any column work. Everything downstream inherits the tree.",
        "",
        "B2  joins disposition + metrics                          (Sonnet, parallel)",
        "    then: python3 apply_synthesis.py --run <run> --workspace <ws>",
        "    then: python3 metrics_review.py  --run <run> --workspace <ws>",
        "",
        "CHECKPOINT 3 of 4 — present METRICS-REVIEW.md to the user and wait for",
        "a go. Batch review: the file carries every metric with its provenance",
        "and stamp (UNGROUNDED/VERIFY first); never relay rows one by one.",
        "",
        "C   one agent per batch, each reading ONE packet from packets/.",
        "    It drafts the columns marked NEEDS DESCRIPTION and CHECKS the rest.",
        "",
        "Do NOT run a corpus-brief stage: measured at 1.16M tokens on one real run,",
        "mostly re-deriving what ingest extracts for free.",
        "",
        "Then: python3 bootstrap.py finish --config " + str(args.config),
    ])
    return 0


def cmd_finish(args):
    c = load_cfg(args.config)
    _, profile = intake_facts(c, args.config)
    cost_banner("finish", profile)
    runp = Path(c["run"])
    run("verify.py", "--tree", runp / "cassis", "--workspace", c["workspace"],
        "--out", runp / "verify_report.json",
        "--judge-input", runp / "judge_input.json")
    # refresh the review page so the final package carries current stamps
    run("metrics_review.py", "--run", runp, "--workspace", c["workspace"],
        optional=True)
    # Free-form documentation answers QUESTIONS, never columns: retrieval is
    # deterministic and runs here, an agent judges the packets out of band, and
    # the candidate is rendered at checkpoint 4 for a human to accept. A run
    # with no docs corpus prints one line and changes nothing.
    run("docs_search.py", "packets", "--run", runp,
        "--workspace", c["workspace"], optional=True)
    # merge and gate are NOT optional: with zero question files they fail loud
    # (measured: a run whose prose referenced QUESTIONS.md got a green publish
    # gate while the file did not exist). review only renders what merge
    # validated, so it stays tolerant.
    run("questions.py", "merge", "--run", runp)
    run("questions.py", "review", "--run", runp, optional=True)
    run("questions.py", "gate", "--run", runp)
    if c.get("gold") and Path(c["gold"]).exists():
        run("grade/ceilings.py", "--workspace", c["workspace"],
            "--gold", c["gold"], "--scope", c.get("scope") or "", optional=True)

    # The canonical tree is not optional: `emit/cassis/` is the copy Cassis
    # imports, and the working tree is
    # not in that format. Everything else in this phase is a report, so this
    # runs last — a tree too broken to emit should not also cost you the
    # reports that say why.
    emit = list(dict.fromkeys(["cassis"] + (args.emit or []) + (c.get("emit") or [])))
    lines = []
    for target in emit:
        extra = []
        if target == "dbt":
            extra = ["--dbt-project", c["input"]]
        run("emit.py", "--emit", target, "--run", runp,
            "--workspace", c["workspace"], *extra)
        lines.append(
            "emit/cassis/         the ontology tree — the standalone result,"
            " checked by verify.py, no account or network needed"
            if target == "cassis" else
            "the dbt project      the ontology merged into the project you"
            " ingested (paths above)")

    banner("Done. The ontology is the result; the rest says how far to trust it", [
        *lines,
        "",
        "That tree IS the deliverable: your tables, columns, metrics and joins,",
        "in your own vocabulary, in files a person or an agent can read to answer",
        "a question about this warehouse correctly. Everything below exists to",
        "tell you which parts of it to trust.",
        "",
        "QUESTIONS.md         under `findings`: defects this run hit in YOUR",
        "                     pipeline — documentation contradicting the SQL, and",
        "                     the like. Nothing for you to answer, and usually",
        "                     worth sending to whoever owns that pipeline",
        "METRICS-REVIEW.md    every metric with where its definition came from",
        "                     and one stamp; UNGROUNDED and VERIFY rows first",
        "QUESTIONS-REVIEW.md  the questions where your answer changes a number —",
        "                     checkpoint 4, below",
        "prefill_report.json  how much of this was already your team's words",
        "verify_report.json   internal consistency of the tree, NOT correctness",
        "judge_input.json     every computation claim paired with the SQL that",
        "                     defines it. ADJUDICATE THESE — on two runs now it is",
        "                     the only step that caught a description contradicting",
        "                     its own SQL, and a rate that was an unweighted mean",
        "docs-packets/        per blocking question, the passages your own",
        "                     documentation offers it, each with its file and line",
    ])
    banner("CHECKPOINT 4 of 4 — the questions that change a number", [
        "Present QUESTIONS-REVIEW.md and wait for answers. It carries only the",
        "items where an answer changes a number, riskiest first, each with the",
        "assumption already made and what the number becomes if it is wrong.",
        "",
        "Batch review, like the metrics: never relay items one by one, and",
        "never ask about a tier the page leaves out. Answers go in as an",
        "`answer:` line on the entry in its questions*.yml file; then re-run",
        "this phase (deterministic and free) and an answered blocker stops",
        "blocking its metric.",
        "",
        "If docs-packets/ has packets, that is the documentation step: one",
        "agent per packet, verdicts collected into docs_answers.yml, then",
        "re-run this phase. Each candidate appears beside its question with",
        "the page it came from, and nothing is applied until you write it in.",
        "",
        "The findings in QUESTIONS.md are the other half of the payoff:",
        "defects in the pipeline the material came from, and nothing anyone",
        "has to answer. Show them.",
    ])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("prep", cmd_prep), ("build", cmd_build), ("finish", cmd_finish)):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "finish":
            p.add_argument("--emit", action="append", choices=["cassis", "dbt"],
                           help="extra export target; the canonical Cassis "
                                "tree is always emitted. Repeatable, and also "
                                "readable from `emit:` in the config")
        p.set_defaults(fn=fn, emit=None)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
