#!/usr/bin/env python3
"""Open questions as structured data, so the document and the gate are generated.

One real run produced ~7,800 words of genuinely useful open questions — and its
own triage, invented on the fly, turned out to be the right taxonomy. This
formalizes it, because a question list is only useful if you can act on it
differentially: 6 of those ~35 items blocked a number, and the other 29 did not.

Agents emit `questions-*.yml` (structured judgment); this materializes
QUESTIONS.md and answers the operational question — what can I publish?

    tier      meaning                                   blocks publishing?
    ------    ---------------------------------------   ------------------
    blocker   a number is wrong or uncomputable until   YES, but only the
              a human answers                           metrics/tables it names
    ruling    two defensible definitions exist; an      no — ship with the
              owner must pick one                       assumption stated
    provenance is this table live? which generation     no
              is authoritative?
    finding   not a question for anyone to answer: a    no — it is a bug report
              defect the run found in YOUR pipeline     for your data team
    unbound   a rule is stated but not attached to      no — coverage gap
              every table whose numbers it changes
    standard  the §15 items every warehouse owes an     no
              answer to

Building the ontology is never blocked: every unknown ships with a stated
assumption, visible at query time, because a tool that needs every unknown
answered before it produces anything does not survive first contact. Publishing
a specific metric IS blocked by its own blockers.

`review` is the interactive half of that, and the only tiers it shows are the
two a person can act on: `blocker`, and `ruling` offered without insisting.
The rest stay file-only — a page that asks about coverage and hygiene is the
interrogation this kit refuses. Answering is a file edit (`answer:` on the
entry) and a re-run, never a per-item chat exchange: an answered blocker drops
off the page and stops blocking its own metric.

Usage:
  python3 questions.py merge  --run RUN [--out QUESTIONS.md]
  python3 questions.py review --run RUN [--out QUESTIONS-REVIEW.md]
  python3 questions.py gate   --run RUN            # what is publish-blocked
  python3 questions.py template > questions-mine.yml
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import question_key

TIERS = ["blocker", "ruling", "provenance", "finding", "unbound", "standard"]
TIER_TITLE = {
    "blocker": "Blocks a number outright",
    "ruling": "Definition needs an owner's ruling",
    "provenance": "Structure and provenance unknown",
    "finding": "Defects found in your pipeline (not questions for you to answer)",
    "unbound": "Rules stated but not bound to every table they change",
    "standard": "Standard questions (§15) — every warehouse owes these an answer",
}
TIER_INTRO = {
    "blocker": "Each of these makes a specific number wrong or uncomputable. The "
               "ontology ships regardless, with the assumption stated, but the "
               "metrics and tables named here should not be trusted until answered.",
    "ruling": "Two or more defensible definitions exist and the material cannot "
              "choose. The run picked one, said so in the ontology, and recorded what "
              "changes if you pick the other.",
    "provenance": "Questions about what a table IS, which the run could not settle from "
                  "the material.",
    "finding": "Nothing to answer here — these are defects in the warehouse or its "
               "documentation that the run hit while modeling. Sending them to whoever "
               "owns the pipeline is usually worth more than the ontology itself.",
    "unbound": "A population rule stated in one place governs numbers elsewhere. "
               "These are the tables it should be attached to and is not.",
    "standard": "The rules that live in people's heads and appear in no artifact.",
}
REQUIRED = ["tier", "title", "why_unanswerable", "assumption", "error_if_wrong"]

# An error nobody can bound outranks one you can size, so it leads the page.
UNBOUNDED_RE = re.compile(r"unbounded|unknown|unquantifiab|not estimable|"
                          r"cannot be estimated|no way to (size|bound)", re.I)

TEMPLATE = """# One entry per genuine unknown. Padding this file is worse than
# leaving it short: an entry that does not name a decision a human actually has
# to make is noise, and noise is why question lists get ignored.
- tier: blocker          # blocker | ruling | provenance | finding | unbound | standard
  title: The savings arithmetic does not exist in this repository
  why_unanswerable: >
    FACT_DAILY_TOTALS selects savings_kwh straight from a MongoDB passthrough.
    No formula for it exists anywhere in the input material.
  affects:
    tables: [DBT_CORE.FACT_DAILY_TOTALS]
    columns: [DBT_CORE.FACT_DAILY_TOTALS.TOTAL_UNITS]
    metrics: [savings_kwh]
  assumption: >
    Described as the material documents it, with the provenance stated and no
    formula claimed.
  error_if_wrong: >
    Unknown. Any savings figure inherits an unaudited upstream computation, so
    the error is unbounded rather than estimable.
  evidence: models/core/fact_daily_savings.sql
  asked_of: data team          # optional: who owns the answer
  # answer: >                  # filled in at the review checkpoint. An
  #   ...                      # answered blocker stops blocking its metrics.
"""


def load_questions(run: Path) -> list:
    out = []
    for p in sorted(run.rglob("questions*.yml")) + sorted(run.rglob("questions*.yaml")):
        try:
            data = yaml.safe_load(p.read_text()) or []
        except yaml.YAMLError as e:
            print(f"WARN: {p.name} does not parse ({str(e).splitlines()[0]}) — skipped")
            continue
        if isinstance(data, dict):
            data = data.get("questions") or []
        for q in data:
            if isinstance(q, dict):
                q["_source"] = p.name
                # Absolute, because every message that tells someone to EDIT a
                # file has to be a path they can open. A basename is a
                # scavenger hunt for anyone who did not lay out the run.
                q["_source_path"] = str(p.resolve())
                out.append(q)
    return out


def validate(qs: list) -> list:
    problems = []
    for i, q in enumerate(qs):
        missing = [f for f in REQUIRED if not str(q.get(f) or "").strip()]
        if missing:
            problems.append(f"{q.get('_source')} entry {i} "
                            f"({str(q.get('title'))[:40]!r}) missing {missing}")
        if q.get("tier") and q["tier"] not in TIERS:
            problems.append(f"{q.get('_source')} entry {i}: unknown tier "
                            f"{q['tier']!r} (expected one of {TIERS})")
    return problems


def affected(q, kind) -> list:
    a = q.get("affects") or {}
    v = a.get(kind) or []
    return [v] if isinstance(v, str) else list(v)


def answer_of(q) -> str:
    """A recorded answer. Set it on the entry in the questions*.yml file the
    review page names — the page is generated, so an answer written into the
    page itself is lost on the next run."""
    return str(q.get("answer") or "").strip()


def load_docs_answers(run: Path) -> dict:
    """key -> the agent's verdict on whether the documentation corpus answers
    that question (`docs_search.py`). Empty when no corpus was given, or when
    the agent step has not run — both are normal, and neither blocks anything."""
    p = run / "docs_answers.yml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text()) or []
    except yaml.YAMLError as e:
        print(f"WARN: docs_answers.yml does not parse "
              f"({str(e).splitlines()[0]}) — candidates not shown")
        return {}
    if isinstance(data, dict):
        data = data.get("answers") or []
    return {str(r.get("question")): r for r in data
            if isinstance(r, dict) and r.get("question")}


def candidate_lines(q, docs: dict, indent: str = "") -> list:
    """A documentation candidate, rendered beside the question it answers.

    This is the whole of what a documentation corpus does to a question: it
    offers text and a citation, and stops. Nothing here is applied to a table,
    a column or a metric — the answer only exists once the human writes it in
    as an `answer:` line, which is also the only thing that stops the blocker
    blocking. An unconfirmed candidate leaves no trace but these lines.
    """
    rec = docs.get(question_key(q))
    if not rec:
        return []
    verdict = str(rec.get("verdict") or "").strip()
    if verdict == "not_in_corpus":
        L = [f"{indent}- Searched your documentation: **no page answers this**."]
        if str(rec.get("rejected") or "").strip():
            L.append(f"{indent}  Closest thing found, and why it is not an "
                     f"answer: {str(rec['rejected']).strip()}")
        return L
    if verdict != "answered":
        return []
    conf = str(rec.get("confidence") or "").strip()
    L = [f"{indent}- **Candidate answer from your own documentation** "
         f"(nothing is applied until you accept it): "
         f"{str(rec.get('answer') or '').strip()}"]
    if rec.get("source"):
        L.append(f"{indent}  - Read from: `{str(rec['source']).strip()}`"
                 + (f" · confidence {conf}" if conf else ""))
    if str(rec.get("quote") or "").strip():
        quote = " ".join(str(rec["quote"]).split())
        L.append(f"{indent}  - It says: \u201c{quote}\u201d")
    if str(rec.get("rejected") or "").strip():
        L.append(f"{indent}  - Rejected on the way: "
                 f"{str(rec['rejected']).strip()}")
    L.append(f"{indent}  - To accept it, copy it into the `answer:` line "
             f"below. Read the quote first — it was retrieved and judged, "
             f"not verified against your warehouse.")
    return L


def risk_key(q):
    """Riskiest first, and risk here is what a wrong assumption costs.

    A blocker naming a metric leads: a metric with a wrong assumption under it
    is a number that looks governed, which is the one failure the reviewer
    cannot catch downstream. Then an error nobody could bound, then breadth."""
    objs = sum(len(affected(q, k))
               for k in ("metrics", "tables", "columns", "domains"))
    return (0 if affected(q, "metrics") else 1,
            0 if UNBOUNDED_RE.search(str(q.get("error_if_wrong") or "")) else 1,
            -objs, str(q.get("title") or ""))


def review_item(q, label: str, docs: dict = None) -> list:
    L = [f"### {label}. {q.get('title')}", "",
         str(q.get("why_unanswerable") or "").strip(), ""]
    for kind, title in (("metrics", "Metrics"), ("tables", "Tables"),
                        ("columns", "Columns"), ("domains", "Domains")):
        v = affected(q, kind)
        if v:
            L.append(f"- {title}: " + ", ".join(f"`{x}`" for x in v))
    L.append(f"- **Assumed for now**: {str(q.get('assumption') or '').strip()}")
    L.append("- **If that assumption is wrong**: "
             f"{str(q.get('error_if_wrong') or '').strip()}")
    if q.get("evidence"):
        L.append(f"- Evidence: `{q['evidence']}`")
    if q.get("asked_of"):
        L.append(f"- Owner: {q['asked_of']}")
    L += candidate_lines(q, docs or {})
    L.append(f"- Answer it here: `{q.get('_source_path') or q.get('_source')}`"
             f" — add an `answer:` line to this entry")
    L.append("")
    return L


def cmd_review(args):
    """The blocker checkpoint: the items where an answer changes a number."""
    qs = load_questions(args.run)
    for p in validate(qs):
        print(f"WARN: {p}")
    docs = load_docs_answers(args.run)
    blockers = [q for q in qs if q.get("tier") == "blocker"]
    rulings = [q for q in qs if q.get("tier") == "ruling"]
    open_b = sorted((q for q in blockers if not answer_of(q)), key=risk_key)
    open_r = sorted((q for q in rulings if not answer_of(q)), key=risk_key)
    answered = [q for q in blockers + rulings if answer_of(q)]

    L = ["# Answer these, then re-run", ""]
    if not open_b and not open_r:
        L += ["Nothing here needs you. Every item that changes a number has an "
              "answer recorded, and the rest of what the run could not settle "
              "is in QUESTIONS.md with the assumption it shipped.", ""]
    else:
        L += ["The run is finished and the ontology is built. This page is the "
              "short list where your answer changes a number, riskiest first: "
              "each item says what was assumed instead and what the number "
              "becomes if that assumption is wrong.", "",
              "Read it top to bottom and answer what you can. To answer one, "
              "open the file named under it, add an `answer:` line to that "
              "entry, and re-run — the script is deterministic and free, an "
              "answered item drops off this page, and a metric stops being "
              "publish-blocked once its own blockers are answered.", "",
              "Everything else the run could not settle is already in "
              "QUESTIONS.md, assumption stated. The defects it found in your "
              "own pipeline are in there too, under findings — those are not "
              "questions for you to answer.", ""]
    if open_b:
        L += ["## Blocks a number until you answer", "",
              "Each of these makes a specific number wrong or uncomputable. "
              "The ontology ships either way; the objects named here should "
              "not be trusted until the item is answered.", ""]
        for n, q in enumerate(open_b, 1):
            L += review_item(q, f"B{n}", docs)
    if open_r:
        L += ["## Worth a ruling, but nothing is blocked", "",
              "Two defensible definitions exist and the material cannot "
              "choose. One was picked and stated. Skipping these is fine — the "
              "assumption ships, visible in the ontology — so answer the ones "
              "you have an opinion about and leave the rest.", ""]
        for n, q in enumerate(open_r, 1):
            L += review_item(q, f"R{n}", docs)
    if answered:
        L += ["## Answered", "",
              "Recorded, and folded into QUESTIONS.md on the next merge.", ""]
        for q in sorted(answered, key=risk_key):
            L.append(f"- **{q.get('title')}** — {answer_of(q)}")
        L.append("")

    out = args.out or (args.run / "QUESTIONS-REVIEW.md")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L).rstrip() + "\n")
    dump = [{"tier": q.get("tier"), "title": q.get("title"),
             "source": q.get("_source"), "answer": answer_of(q),
             "affects": q.get("affects") or {}}
            for q in open_b + open_r + answered]
    (args.run / "questions_review.json").write_text(
        json.dumps(dump, indent=1, ensure_ascii=False, default=str))
    print(f"{out}: {len(open_b)} blocking, {len(open_r)} offered, "
          f"{len(answered)} already answered")
    if open_b or open_r:
        print("CHECKPOINT — present this file to the user and wait for an "
              "answer. Batch review: never relay items one by one, and never "
              "ask about a tier this page leaves out.")
    return 0


def no_questions_failure(run: Path) -> int:
    """A run with zero question files is incomplete, never clean. The standard
    entries alone (timezone, week-start convention, currency, what is out of
    scope) are mandatory on every run, so an empty set means authoring skipped
    a required artifact — it never means there was nothing to ask. Measured on
    a real run: finish printed a green publish gate while the authored prose
    pointed readers at a QUESTIONS.md that did not exist."""
    print("FAIL — no questions*.yml anywhere under the run, and a finished run "
          "always has one: the standard entries (timezone, week-start "
          "convention, currency, out-of-scope) are mandatory on every run.")
    refs = []
    tree = run / "cassis"
    if tree.exists():
        for p in sorted(tree.rglob("*")):
            if p.is_file() and p.suffix.lower() in {".md", ".yml", ".yaml"}:
                try:
                    if "QUESTIONS.md" in p.read_text():
                        refs.append(str(p.relative_to(run)))
                except (UnicodeDecodeError, OSError):
                    continue
    if refs:
        print(f"Worse: {len(refs)} authored file(s) already point readers at a "
              f"QUESTIONS.md that was never generated:")
        for r in refs[:10]:
            print(f"  {r}")
        if len(refs) > 10:
            print(f"  ... and {len(refs) - 10} more")
    print("Write the question file(s) — `python3 questions.py template` prints "
          "the schema — then re-run this phase (deterministic and free).")
    return 1


def cmd_merge(args):
    qs = load_questions(args.run)
    docs = load_docs_answers(args.run)
    if not qs:
        return no_questions_failure(args.run)
    problems = validate(qs)
    for p in problems:
        print(f"WARN: {p}")

    by_tier = defaultdict(list)
    for q in qs:
        by_tier[q.get("tier") or "provenance"].append(q)

    L = ["# Open questions", "",
         "Generated from the assembly run's structured question files. Every entry "
         "names a decision a human owns, what was assumed instead, and how wrong the "
         "number gets if the assumption is wrong.", "",
         "| tier | count | blocks publishing |", "|---|---|---|"]
    for t in TIERS:
        if by_tier.get(t):
            L.append(f"| {t} | {len(by_tier[t])} | "
                     f"{'yes, the items it names' if t == 'blocker' else 'no'} |")
    L.append("")
    for t in TIERS:
        items = by_tier.get(t)
        if not items:
            continue
        L += [f"## {TIER_TITLE[t]}", "", TIER_INTRO[t], ""]
        for n, q in enumerate(items, 1):
            L.append(f"### {t[0].upper()}{n}. {q.get('title')}")
            L.append("")
            L.append(f"{str(q.get('why_unanswerable') or '').strip()}")
            for kind, label in (("tables", "Tables"), ("columns", "Columns"),
                                ("metrics", "Metrics"), ("domains", "Domains")):
                v = affected(q, kind)
                if v:
                    L.append(f"- {label}: " + ", ".join(f"`{x}`" for x in v))
            if q.get("evidence"):
                L.append(f"- Evidence: `{q['evidence']}`")
            if q.get("asked_of"):
                L.append(f"- Owner: {q['asked_of']}")
            L.append(f"- **Assumed instead**: {str(q.get('assumption') or '').strip()}")
            L.append(f"- **If that is wrong**: {str(q.get('error_if_wrong') or '').strip()}")
            L += candidate_lines(q, docs)
            if answer_of(q):
                L.append(f"- **Answered**: {answer_of(q)}")
            L.append("")
    out = args.out or (args.run / "QUESTIONS.md")
    Path(out).write_text("\n".join(L))
    (args.run / "questions.json").write_text(
        json.dumps(qs, indent=1, ensure_ascii=False, default=str))
    counts = ", ".join(f"{t} {len(by_tier[t])}" for t in TIERS if by_tier.get(t))
    print(f"QUESTIONS.md: {len(qs)} questions ({counts}) -> {out}")
    if problems:
        print(f"{len(problems)} entries are incomplete — fix them; an entry without "
              f"an assumption and an error estimate is not actionable")
    return 0


def cmd_gate(args):
    qs = load_questions(args.run)
    if not qs:
        # Without this, a run that never filed its questions sailed through to
        # "no unanswered blockers — everything drafted is publishable".
        return no_questions_failure(args.run)
    all_blockers = [q for q in qs if q.get("tier") == "blocker"]
    # An answered blocker is settled, not blocking: that is what the review
    # checkpoint is for, and a gate that ignored the answer would make
    # answering pointless.
    blockers = [q for q in all_blockers if not answer_of(q)]
    answered = len(all_blockers) - len(blockers)
    if answered:
        print(f"{answered} blocker(s) answered at the review checkpoint — "
              f"no longer blocking")
    blocked = defaultdict(list)
    for q in blockers:
        for kind in ("metrics", "tables", "domains", "columns"):
            for name in affected(q, kind):
                blocked[(kind, name)].append(q.get("title"))
    if not blockers:
        print("publish gate: no unanswered blockers — everything drafted is "
              "publishable")
        return 0
    print(f"publish gate: {len(blockers)} blocker(s) affecting "
          f"{len(blocked)} object(s). The ontology is publishable; these specific "
          f"objects should not be trusted until answered.\n")
    for kind in ("metrics", "domains", "tables", "columns"):
        rows = [(n, t) for (k, n), t in sorted(blocked.items()) if k == kind]
        if not rows:
            continue
        print(f"  {kind}:")
        for name, titles in rows:
            print(f"    {name}")
            for t in titles:
                print(f"        - {t}")
    # a metric named by a blocker is the one case worth failing a pipeline over
    if args.strict and any(k == "metrics" for k, _ in blocked):
        print("\n--strict: metrics are blocked, exiting non-zero")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--run", type=Path, required=True)
    m.add_argument("--out", type=Path)
    m.set_defaults(fn=cmd_merge)
    v = sub.add_parser("review")
    v.add_argument("--run", type=Path, required=True)
    v.add_argument("--out", type=Path)
    v.set_defaults(fn=cmd_review)
    g = sub.add_parser("gate")
    g.add_argument("--run", type=Path, required=True)
    g.add_argument("--strict", action="store_true")
    g.set_defaults(fn=cmd_gate)
    t = sub.add_parser("template")
    t.set_defaults(fn=lambda a: (print(TEMPLATE), 0)[1])
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
