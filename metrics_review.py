#!/usr/bin/env python3
"""Build METRICS-REVIEW.md — every candidate metric, its provenance, one stamp.

Why this exists: an early run of the kit shipped 32 metrics and the reviewer
saw none of them before they landed. The complaint was volume, not autonomy — the synthesis stage is right
to decide alone when the evidence is clear, and wrong to leave the human
reading raw yml files to find out what it decided. So this script turns the
metrics into one reviewable page. It changes nothing; it reports.

Semantics — batch review, not interrogation:
  present the FILE to the user after Stage B2 + apply_synthesis, before
  Stage C, and wait for a go. Never relay metrics one by one in chat, and
  never ask a per-metric question the file already answers. Edits land in
  the metric yml files (or the metric moves to QUESTIONS.md); re-run this
  script after edits — it is deterministic and free.

Stamps (evidence units per §14: distinct corpus sources + the warehouse owner's own
text on the aggregated column — two independent units corroborate):
  ok          ≥2 independent evidence units. Listed so nothing lands silently.
  VERIFY      exactly one unit, or an expression shape the matcher cannot
              check (CASE WHEN, arithmetic composite). Confirm or demote.
  UNGROUNDED  a query corpus exists and holds nothing, and the aggregated
              column carries none of the owner's own text. §14 says do not ship
              these: corroborate, or move to QUESTIONS.md.

With no query corpus at all, corroboration is structurally unsatisfiable,
so every metric stamps VERIFY under one advisory rather
than drowning the file in UNGROUNDED noise — there, the human read IS the
gate.

Usage:
  python3 metrics_review.py --run <run dir> --workspace <ws>
      [--out <path>] [--strict]     # --strict: exit 1 if any UNGROUNDED
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dump_json, load_json, metric_expr_cores, normalize_expr
from verify import load_tree

STAMP_ORDER = {"UNGROUNDED": 0, "VERIFY": 1, "ok": 2}


def corpus_index(evidence_dir: Path):
    """normalized expression -> list of candidate records, or None if no corpus."""
    p = evidence_dir / "metric_candidates.json"
    if not p.exists():
        return None
    idx = {}
    cands = load_json(p)
    if not cands:
        return None
    for c in cands:
        idx.setdefault(normalize_expr(c["expression"]), []).append(c)
    return idx


def match_corpus(cores, idx):
    """Candidates matching any aggregate core, by full arg or last segment."""
    hits = []
    for fn, arg in cores:
        for form in {f"{fn}({arg})", f"{fn}({arg.split('.')[-1]})"}:
            hits.extend(idx.get(form) or [])
    seen, out = set(), []
    for h in hits:
        if h["id"] not in seen:
            seen.add(h["id"])
            out.append(h)
    return out


# The prefill tiers that carry the OWNER's words. Anything else — including
# a description with no stamp at all is model-written (or predates stamping) and
# must never count, or the finish-phase re-run would corroborate a metric
# with the enrichment agent's own prose (self-corroboration). Both the current
# `drafted` stamp and the legacy `authored` stamp remain outside OWNER_TIERS.
#
# `inferred_glossary` is deliberately absent, and that absence is the answer to
# the question the GitLab handbook pass raised: should a NAME-KEYED match
# against unstructured prose count as the warehouse owner's own words. No. A
# classifier that calls any file with three `**Bold**:` lines a glossary
# produced 647 "column definitions" on that corpus, 8 reached a column, and all
# 8 were wrong while stamped as the owner's text. `warehouse_glossary` stays,
# because a column dictionary a person handed over about this table is a claim
# a person made; `common.glossary_tier` is where the two are told apart. Both
# still ship their text and both still read VERIFY in a packet — the tier
# decides only what may corroborate a number.
OWNER_TIERS = {"repo_text", "warehouse_comment", "warehouse_glossary",
               "semantic_layer"}


def owner_text_on_args(run: Path, m: dict, cores) -> list:
    """The warehouse owner's own words on an aggregated column corroborate the
    metric (§14: definition-level corroboration, unit and meaning). Only stamped
    prefill tiers count; see OWNER_TIERS."""
    key_s, key_t = m.get("table_schema"), m.get("table_name")
    if not (key_s and key_t):
        return []
    p = run / "cassis" / "tables" / str(key_s) / f"{key_t}.yml"
    if not p.exists():
        return []
    try:
        rec = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return []
    by_name = {str(c.get("name", "")).upper(): c for c in rec.get("columns") or []}
    out = []
    for _fn, arg in cores:
        col = by_name.get(arg.split(".")[-1].upper())
        if col and col.get("description") \
                and col.get("description_source") in OWNER_TIERS:
            out.append((col["name"], col["description_source"]))
    return out


def review_rows(run: Path, workspace: Path):
    tree = load_tree(run / "cassis")
    idx = corpus_index(workspace / "evidence")
    # A semantic layer's declared metrics sit in the same candidate file but are
    # not a query corpus: with only those, a metric nobody queried is unread,
    # not UNGROUNDED.
    has_corpus = bool(idx) and any(h.get("provenance") != "declared"
                                   for hs in idx.values() for h in hs)
    rows = []
    for name, m in sorted(tree["metrics"].items()):
        cores = metric_expr_cores(m.get("expression"))
        hits = match_corpus(cores, idx) if (idx and cores) else []
        corpus_hits = [h for h in hits if h.get("provenance") != "declared"]
        declared_hits = [h for h in hits if h.get("provenance") == "declared"]
        occurrences = sum(h["occurrences"] for h in corpus_hits)
        corpus_sources = sorted({s for h in corpus_hits for s in h.get("sources") or []})
        owner_cols = owner_text_on_args(run, m, cores)

        evidence = []
        if corpus_hits:
            shown = ", ".join(corpus_sources[:2]) + ("…" if len(corpus_sources) > 2 else "")
            plural = "source" if len(corpus_sources) == 1 else "sources"
            evidence.append(f"corpus ×{occurrences} "
                            f"({len(corpus_sources)} {plural}: {shown})")
        translated = []
        for h in declared_hits:
            d = h.get("declared") or {}
            evidence.append(f"declared in {d.get('source')} as {d.get('name')!r} "
                            f"({(h.get('sources') or [''])[0].split(':', 1)[-1]})")
            if d.get("translated"):
                translated.append(h)
                evidence.append(f"translated by the kit from {d.get('language')} "
                                f"`{' '.join(str(d.get('original_expression')).split())}`"
                                f": confirm the SQL")
        for col, src in owner_cols:
            evidence.append(f"{col} described ({src})")

        units = len(corpus_sources) + (1 if owner_cols else 0) + len(declared_hits)
        if not has_corpus and not declared_hits:
            stamp = "VERIFY"
            if not evidence:
                evidence.append("no query corpus — human read is the gate")
        elif not cores:
            stamp = "VERIFY"
            evidence.append("expression not machine-checkable")
        elif units - len(translated) >= 2:
            stamp = "ok"
        elif units >= 1:
            # a translation the kit wrote is never the second unit that
            # lifts a metric to ok: a person confirms it first
            stamp = "VERIFY"
        elif has_corpus:
            stamp = "UNGROUNDED"
            evidence = ["none found"]
        else:
            stamp = "VERIFY"
            evidence.append("no query corpus — human read is the gate")

        definition = m.get("expression") or ""
        # The corpus miner keeps only the aggregate core, so a WHERE clause is
        # never machine-corroborated: an `ok` stamp vouches for the core, not
        # the filter. Say so per row rather than letting `ok` overstate.
        filter_unverified = bool(m.get("filters"))
        if filter_unverified:
            definition += f" WHERE {m['filters']}"
            evidence.append("WHERE not machine-corroborated — read the filter")
        rows.append({
            "stamp": stamp,
            "metric": name,
            "synonyms": [s for s in (m.get("synonyms") or []) if isinstance(s, str)],
            "table": f"{m.get('table_schema')}.{m.get('table_name')}",
            "definition": definition,
            "evidence": evidence,
            "filter_unverified": filter_unverified,
            "file": str(Path(m["path"]).resolve()),
        })
    # riskiest first: ungrounded, then single-source; business synonyms on a
    # weak stamp are the §14 red line, so they lead within their stamp
    rows.sort(key=lambda r: (STAMP_ORDER[r["stamp"]], not r["synonyms"], r["metric"]))
    return rows, has_corpus


def render_declared(declared: dict) -> list:
    """What a semantic layer declared that could not become a one-table metric,
    and what did not hold against the schema export. Not in the tree: a person
    decides which of these matter."""
    review, rejects = declared.get("review") or [], declared.get("rejects") or []
    if not (review or rejects):
        return []

    def cell(s):
        return " ".join(str(s).split()).replace("|", "\\|")
    lines = ["", "## Declared definitions that are not metrics yet", ""]
    if review:
        lines += [f"{len(review)} measures from the semantic layer have no single "
                  f"one-table SQL expression. None is in the tree. Define the ones "
                  f"that matter by hand, or record them in QUESTIONS.md.", "",
                  "| measure | why | definition | where |", "|---|---|---|---|"]
        for r in review:
            lines.append(f"| {cell(r['name'])} | {cell(r['reason'])} "
                         f"| `{cell(r.get('original_expression'))[:160]}` "
                         f"| {cell(r.get('source'))} {cell(r.get('cite'))} |")
    if rejects:
        by_kind = {}
        for r in rejects:
            by_kind.setdefault(r["kind"], []).append(r)
        lines += ["", f"{len(rejects)} declared definitions did not hold against the "
                  f"schema export and were left out "
                  f"({', '.join(f'{len(v)} {k}' for k, v in sorted(by_kind.items()))}). "
                  f"Each reason is in `declared.json` under `rejects`; the first:", ""]
        lines += [f"- {cell(r['kind'])} {cell(r['name'])}: {cell(r['reason'])}"
                  for r in rejects[:10]]
    return lines


def render_md(rows, has_corpus: bool, declared=None) -> str:
    counts = {s: sum(1 for r in rows if r["stamp"] == s) for s in STAMP_ORDER}
    lines = [
        "# Metrics review — present, then proceed",
        "",
        f"{len(rows)} metrics: {counts['UNGROUNDED']} UNGROUNDED, "
        f"{counts['VERIFY']} VERIFY, {counts['ok']} ok.",
        "",
        "This is the metrics checkpoint. Read it top to bottom — the riskiest",
        "rows lead. `ok` means the definition has at least two independent",
        "evidence units (distinct BI sources, your own text on the",
        "aggregated column); it is listed so nothing lands silently. `VERIFY`",
        "has exactly one unit, or a shape the matcher cannot check: confirm it",
        "or demote it. `UNGROUNDED` has none — corroborate it or move it to",
        "QUESTIONS.md; do not ship it (§14). ⚠ marks a business synonym riding",
        "on a weak stamp — the one thing §14 forbids outright. `ok*` means the",
        "aggregate core is corroborated but the WHERE clause cannot be (the",
        "corpus miner drops filters): read the filter yourself.",
        "",
        "To act on a row: edit its yml file (path in the last column) or move",
        "the metric to QUESTIONS.md, then re-run metrics_review.py.",
        "",
    ]
    if not has_corpus:
        lines += ["**Advisory: no query corpus exists for this run**, so"
                  " expression corroboration is unsatisfiable and every row"
                  " stamps VERIFY. The human read is the gate here.", ""]
    lines += ["| stamp | metric | on | definition | evidence | file |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        warn = " ⚠" if r["synonyms"] and r["stamp"] != "ok" else ""
        stamp = r["stamp"] + ("*" if r["stamp"] == "ok" and r["filter_unverified"]
                              else "")
        name = r["metric"] + (f" ({', '.join(r['synonyms'])})" if r["synonyms"] else "")

        def cell(s):
            return str(s).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {stamp}{warn} | {cell(name)} | {cell(r['table'])} "
                     f"| `{cell(r['definition'])}` | {cell('; '.join(r['evidence']))} "
                     f"| {cell(Path(r['file']).name)} |")
    lines += render_declared(declared or {})
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any metric is UNGROUNDED")
    args = ap.parse_args()

    rows, has_corpus = review_rows(args.run, args.workspace)
    out = args.out or (args.run / "METRICS-REVIEW.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    dp = args.workspace / "declared.json"
    out.write_text(render_md(rows, has_corpus, load_json(dp) if dp.exists() else None))
    dump_json(rows, args.run / "metrics_review.json")

    n_bad = sum(1 for r in rows if r["stamp"] == "UNGROUNDED")
    n_verify = sum(1 for r in rows if r["stamp"] == "VERIFY")
    print(f"{out}: {len(rows)} metrics — {n_bad} UNGROUNDED, {n_verify} VERIFY, "
          f"{len(rows) - n_bad - n_verify} ok")
    if not rows:
        print("note: no metrics found under run/cassis/metrics/ — run this "
              "AFTER Stage B2 has proposed them")
    else:
        print("CHECKPOINT — present this file to the user and wait for a go "
              "before Stage C. Batch review: never relay rows one by one.")
    if args.strict and n_bad:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
