#!/usr/bin/env python3
"""Assemble an isolated bootstrap package for an independent evaluator.

An independent run is only worth anything if the evaluator's tools cannot see the
answer. For a warehouse we have already modeled, the answer exists in several
places, and they are easy to forget:

  the gold tree and its JSON          the ontology we grade against
  previous runs (`run*/`)             ontologies we already built here
  the scope file                      encodes the level-2 scoping DECISION
  the grading fixture and test cases  the eval answers
  our own reports and handoff         our findings: the traps, the counts

Only the first two are obvious. `scope-gold.txt` is the one people ship by
accident: handing over the agreed table list gives away the business-scoping
judgment that the run is supposed to exercise.

What DOES go in the package is everything the warehouse owner provided —
including their own curated warehouse docs, which are input, not answer.

Usage:
  python3 handoff_package.py --fixture-dir DIR --out DIR [--include NAME ...]
"""
import argparse
import shutil
import sys
from pathlib import Path

# Anything matching these is an answer, not an input.
LEAK = [
    ("gold*", "the gold ontology we grade against"),
    ("*gold*.json", "gold in JSON form"),
    ("run", "an ontology we already built"),
    ("run-*", "an ontology we already built"),
    ("run_*", "an ontology we already built"),
    ("workspace*", "derived from the inputs; the evaluator regenerates it"),
    ("scope-gold*", "encodes the level-2 scoping DECISION the run must make itself"),
    ("scope-*.txt", "encodes a scoping decision"),
    ("fixture*.csv", "grading fixture"),
    ("test-cases", "evaluation answers"),
    ("verdicts.json", "adjudicated results"),
    ("adjudication*", "adjudicated results"),
    ("spec.json", "benchmark spec"),
]


def leaks(name: str):
    from fnmatch import fnmatch
    for pat, why in LEAK:
        if fnmatch(name, pat):
            return why
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture-dir", type=Path, required=True,
                    help="the fixture directory for this benchmark warehouse")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--include", nargs="*", default=[],
                    help="extra top-level names to copy despite a LEAK match "
                         "(use only when you have checked the file yourself)")
    args = ap.parse_args()

    if args.out.exists():
        print(f"ERROR: {args.out} already exists — remove it or pick another path")
        return 1
    src = args.fixture_dir
    if not src.is_dir():
        print(f"ERROR: {src} is not a directory")
        return 1

    copied, withheld = [], []
    args.out.mkdir(parents=True)
    for child in sorted(src.iterdir()):
        why = leaks(child.name)
        if why and child.name not in args.include:
            withheld.append((child.name, why))
            continue
        if child.is_dir():
            shutil.copytree(child, args.out / child.name)
        else:
            shutil.copy2(child, args.out / child.name)
        copied.append(child.name)

    print(f"package -> {args.out}")
    print(f"\nincluded ({len(copied)}):")
    for c in copied:
        print(f"    {c}")
    print(f"\nwithheld ({len(withheld)}):")
    for n, why in withheld:
        print(f"    {n:24s} {why}")
    print("\nNow verify, do not assume:\n"
          f"  python3 grade/leakage_gate.py --gold {src}/gold-tree "
          f"--inputs {args.out}\n"
          "and grep the package for the gold ontology's vocabulary before sending.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
