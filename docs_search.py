#!/usr/bin/env python3
"""Point a documentation corpus at the blocking questions, not at columns.

`--docs-dir` ingests free-form documentation that nothing downstream reads.
The tempting fix — apply the prose to columns — was measured on GitLab's public
handbook (4,736 pages, 47M chars) and made things worse: the only name-keyed
path prefilled 8 columns, all 8 wrong, each stamped as the warehouse owner's
own words. Wrong text at high trust is the one failure with no recovery.

So the corpus answers QUESTIONS instead. `questions.py` already tiers every
item, and `blocker` means a number is wrong or uncomputable until a human
answers. That is a small, high-value target: on the same run, 6 blockers gated
30 objects, and the currency blocker alone gated ten dollar metrics.

Three halves, and the split is the point:

  retrieval  this script. Deterministic, free, no model: BM25 over the corpus
             per blocker, document level then passage level, emitting one
             packet per question with the passages and their file paths.
  reading    an agent per packet. It decides whether a passage answers the
             question and writes a verdict — `answered` with a source, or
             `not_in_corpus`. Judging is the part a keyword score cannot do.
  confirming the human, at checkpoint 4, in QUESTIONS-REVIEW.md. A candidate
             is rendered beside the question it answers and nothing is applied
             until the human writes it in as an `answer:` line.

`not_in_corpus` is a first-class result, not a failure. On the measured run
four of the six blockers are answerable from no corpus that exists, and a pass
that answers all six is the glossary failure in a new costume.

Retrieval before tokens: 4,736 documents and 6 questions, and only the
candidates reach a model. `packets` prints the corpus cost it is handing over.

Usage:
  python3 docs_search.py packets --run RUN --workspace WS
      [--tiers blocker,...] [--docs-per-question N] [--passages N]
      [--max-chars-per-question N] [--out DIR]
  python3 docs_search.py score --run RUN --labels FILE [--strict]
"""
import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import die, dump_json, load_json, question_key
from questions import affected, answer_of, load_docs_answers, load_questions

WORD_RE = re.compile(r"[a-z0-9]+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FRONT_TITLE_RE = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.M)

# Query terms come from the question's own prose, so the words that carry no
# question — and the ones every warehouse page repeats — have to go, or every
# blocker retrieves the same generic pages. IDF handles the corpus-frequent
# ones; this handles the ones frequent in the QUESTION.
STOP_WORDS = {
    "the", "and", "for", "are", "any", "not", "but", "its", "his", "her",
    "this", "that", "with", "from", "have", "has", "had", "was", "were",
    "been", "being", "into", "onto", "than", "then", "them", "they", "their",
    "there", "these", "those", "which", "what", "when", "where", "who",
    "whom", "how", "why", "all", "each", "one", "two", "own", "same", "such",
    "only", "other", "some", "more", "most", "over", "under", "also", "can",
    "cannot", "could", "would", "should", "will", "may", "might", "must",
    "does", "did", "done", "doing", "make", "makes", "made", "get", "gets",
    "got", "use", "used", "using", "say", "says", "said", "know", "known",
    "see", "seen", "look", "looks", "come", "comes", "way", "ways", "thing",
    "things", "anything", "nothing", "something", "everything", "here",
    "about", "above", "after", "before", "because", "between", "both",
    "during", "however", "instead", "itself", "least", "less", "many",
    "much", "never", "every", "either", "neither", "nor", "off", "out",
    "per", "rather", "since", "still", "yet", "you", "your", "our", "ours",
    "not", "none", "non",
    # question-shaped words: in every entry, in no answer
    "question", "questions", "answer", "answers", "answered", "unanswerable",
    "assumption", "assumed", "assume", "wrong", "error", "blocker", "tier",
    "affects", "evidence", "shipped", "ship", "run", "runs", "kit",
}

# What a term's origin is worth, and it is a MAX across fields rather than a
# sum of occurrences. Measured on the real corpus, summing lost both answerable
# blockers: the currency question gates 12 metrics and 8 tables, so summing
# every occurrence weighted `arr` 16, `total` 16 and `amount` 12 against
# `currency` 14 — the query stopped being "what currency are amounts in" and
# became "the warehouse's revenue vocabulary", which every long handbook page
# matches. How many objects a blocker gates is not how important a word is to
# the question it asks.
FIELD_WEIGHTS = {"title": 3.0, "why": 1.0}

# The seven §15 questions the kit mandates of every run, and the vocabulary an
# ANSWER to each is written in. A blocker asks its question in warehouse terms
# — "is any ARR/MRR/invoice dollar amount already normalized to one currency" —
# and the page that answers it is written in the domain's terms: reporting
# currency, functional currency, foreign currency translation. Measured on this
# corpus, the answer sat in the top-ranked document and lost its own packet by
# 50 places, because the question's other nine nouns each matched a hundred
# revenue pages and its subject matched one.
#
# So the kit carries the vocabulary of its own seven questions. These terms are
# chosen from the question, never from a client's corpus: any warehouse's answer
# to "what currency is this in" uses them, which is what makes them safe to
# hard-code and useless as a way to fit one handbook. `triggers` recognizes the
# question wherever it was filed — two of this run's six blockers are §15 items
# that reached the file through the metrics stage, not the standard one.
STANDARD_VOCAB = [
    {"item": "excluded populations",
     "triggers": ("exclud", "exclusion", "test account", "internal user",
                  "demo account"),
     "vocab": "excluded exclude internal employees test demo accounts default "
              "filter population synthetic seeded"},
    {"item": "week and calendar conventions",
     "triggers": ("week", "fiscal", "calendar", "quarter"),
     "vocab": "fiscal year quarter week starts monday sunday calendar "
              "convention period bucketing"},
    {"item": "timezone of timestamps",
     "triggers": ("timezone", "time zone", "timestamp"),
     "vocab": "timezone utc stored timestamp session default converted "
              "normalised local time"},
    {"item": "satisfaction metrics",
     "triggers": ("satisfaction", "csat", "nps", "promoter"),
     "vocab": "satisfaction csat nps survey score box mean threshold promoter "
              "respondents"},
    {"item": "service-level thresholds",
     "triggers": ("sla", "service level", "response time", "threshold"),
     "vocab": "sla service level agreement threshold response resolution "
              "target breach hours"},
    {"item": "currency and unit scale",
     "triggers": ("currency", "currencies", "money column", "unit scale"),
     "vocab": "currency currencies reporting functional exchange rate "
              "denominated converted translation local unit scale cents"},
    {"item": "metrics not computable in scope",
     # No corpus answers this one — it is about the run's own scope — so it
     # carries no vocabulary and falls back to the question's own words.
     "triggers": ("cannot be computed", "out-of-scope", "out of scope"),
     "vocab": ""},
]

# What a §15 vocabulary term is worth: under the question's own title, over the
# explanation of why the material is silent.
STANDARD_WEIGHT = 2.0

# BM25. b is high because a documentation corpus mixes 200-char stubs with
# 200k-char handbooks, and an unnormalized score just ranks the long ones.
BM25_K1 = 1.2
BM25_B = 0.75

PASSAGE_MAX = 1400          # chars; a heading section longer than this is windowed
PASSAGE_MIN = 60            # chars; a bare heading with no body answers nothing


# ---------- query ----------

def terms_of(text) -> list:
    return [w for w in WORD_RE.findall(str(text or "").lower())
            if len(w) >= 3 and w not in STOP_WORDS]


def query_of(q: dict) -> Counter:
    """The question as weighted terms — the question's own words, and only those.

    The objects a blocker gates are deliberately NOT in the query. A warehouse
    identifier decomposes into layer prefixes and type words (`mart`, `fct`,
    `dim`, `total`, `amount`) that describe no concept and match every page,
    and the whole identifier matches almost nothing: measured on this corpus,
    19% of revenue-core column names appear in the handbook as whole words and
    8% of the names three words or longer. A question that is about one of its
    tables names it in its own title, where it is already weighted.
    """
    query = {}
    for field, text in (("title", q.get("title")),
                        ("why", q.get("why_unanswerable"))):
        for w in terms_of(text):
            query[w] = max(query.get(w, 0.0), FIELD_WEIGHTS[field])
    for w in terms_of(" ".join(std["vocab"] for std in standards_of(q))):
        query[w] = max(query.get(w, 0.0), STANDARD_WEIGHT)
    return Counter(query)


def standards_of(q: dict) -> list:
    """Which §15 standard questions this entry is asking, if any.

    Matched on the title, because that is the question. An entry can be more
    than one — "what currency and what unit scale" is one §15 item and reads
    as two — so the vocabularies union rather than the first one winning.
    """
    title = str(q.get("title") or "").lower()
    return [std for std in STANDARD_VOCAB
            if any(t in title for t in std["triggers"])]


# ---------- corpus ----------

def read_corpus(workspace: Path, union: set):
    """One pass over the corpus: per-document term counts for the union of
    every question's terms, plus the lengths BM25 needs.

    Tokenize once and test membership, rather than running one regex per
    question — 47M characters is a scan you take once, not six times.
    """
    docs_dir = workspace / "docs"
    index_p = docs_dir / "index.json"
    if not index_p.exists():
        return None, {}, {}, {}
    # Instructions written for a model are not documentation and never answer a
    # question about a warehouse. They also arrive from a client's tree written
    # to steer an agent, and the agent reading these packets is ours — so they
    # are excluded here rather than ranked and quoted into a packet.
    full = load_json(index_p)
    index = [e for e in full if e.get("kind") != "agent_instructions"]
    skipped = len(full) - len(index)
    if skipped:
        print(f"corpus: {skipped} document(s) excluded — instructions written "
              f"for an agent, not documentation of anything")
    counts, lengths, titles = {}, {}, {}
    for entry in index:
        p = docs_dir / f"{entry['id']}.txt"
        if not p.exists():
            continue
        text = p.read_text(errors="replace")
        toks = WORD_RE.findall(text.lower())
        lengths[entry["id"]] = len(toks)
        counts[entry["id"]] = Counter(t for t in toks if t in union)
        m = FRONT_TITLE_RE.search(text[:2000])
        titles[entry["id"]] = m.group(1).strip() if m else ""
    return index, counts, lengths, titles


def idf_of(counts: dict, union: set) -> dict:
    n = max(len(counts), 1)
    df = Counter()
    for c in counts.values():
        for t in c:
            if t in union:
                df[t] += 1
    return {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            for t in union if df[t]}


def bm25(query: Counter, tf: Counter, length: int, avg_len: float,
         idf: dict) -> float:
    score = 0.0
    for term, weight in query.items():
        f = tf.get(term, 0)
        if not f or term not in idf:
            continue
        denom = f + BM25_K1 * (1 - BM25_B + BM25_B * length / max(avg_len, 1))
        score += weight * idf[term] * (f * (BM25_K1 + 1)) / denom
    return score


# ---------- passages ----------

def passages_of(text: str) -> list:
    """A markdown page split where its author split it: on headings, then on
    paragraph boundaries when a section runs long. Every passage keeps its
    line number, because a candidate answer without a citation is a guess."""
    lines = text.splitlines()
    sections, cur, start, heading = [], [], 1, ""
    for i, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m:
            if cur:
                sections.append((start, heading, "\n".join(cur)))
            cur, start, heading = [line], i, m.group(2).strip()
        else:
            cur.append(line)
    if cur:
        sections.append((start, heading, "\n".join(cur)))

    out = []
    for start, heading, body in sections:
        if len(body) <= PASSAGE_MAX:
            if len(body.strip()) >= PASSAGE_MIN:
                out.append({"line": start, "heading": heading, "text": body})
            continue
        # A long section is windowed on line boundaries, not paragraph ones: a
        # handbook page's paragraph is frequently one 2,000-character line, and
        # a windower that waits for a blank line emits a 6,000-character block
        # that crowds every other candidate out of the packet. A single line
        # longer than the window is still kept whole — the sentence that
        # answers the question sits inside it, and half a paragraph is a
        # citation to something nobody said.
        buf, buf_len, buf_start = [], 0, start
        for offset, line in enumerate(body.split("\n")):
            if buf and buf_len + len(line) > PASSAGE_MAX:
                chunk = "\n".join(buf)
                if len(chunk.strip()) >= PASSAGE_MIN:
                    out.append({"line": buf_start, "heading": heading,
                                "text": chunk})
                buf, buf_len, buf_start = [], 0, start + offset
            buf.append(line)
            buf_len += len(line) + 1
        chunk = "\n".join(buf)
        if len(chunk.strip()) >= PASSAGE_MIN:
            out.append({"line": buf_start, "heading": heading, "text": chunk})
    return out


def candidates_for(q, index, counts, lengths, titles, idf, avg_len,
                   docs_dir: Path, n_docs: int, n_passages: int,
                   max_chars: int) -> dict:
    query = query_of(q)
    scored = sorted(
        ((bm25(query, counts.get(e["id"], Counter()), lengths.get(e["id"], 0),
               avg_len, idf), e) for e in index if e["id"] in counts),
        key=lambda kv: (-kv[0], kv[1]["rel"]))
    hit_docs = [(s, e) for s, e in scored if s > 0]
    top = hit_docs[:n_docs]

    # Two passes, because BM25's length normalization is only meaningful
    # against the average length of the things being ranked. Scoring passages
    # against the DOCUMENT average (1,444 tokens here, versus ~200 for a
    # passage) makes every passage look short, and the boost that follows
    # ranks a two-word hit in a stub above the paragraph that states the
    # answer — measured: the currency answer sat in the corpus's 2nd-ranked
    # document and still lost its own packet.
    found = []
    for _score, entry in top:
        text = (docs_dir / f"{entry['id']}.txt").read_text(errors="replace")
        for p in passages_of(text):
            toks = WORD_RE.findall(p["text"].lower())
            found.append((entry, p, toks))
    # NOT covered by the suite, and it is not for want of trying: the bug only
    # bites when the document average and the passage average differ by an
    # order of magnitude (1,444 against 128 on the real corpus), and a fixture
    # small enough for a test has both averages in the same range, so every
    # candidate ranking comes out identical either way. Change this line and
    # the suite will stay green — re-measure on a real corpus instead.
    passage_avg = (sum(len(t) for _e, _p, t in found) / len(found)) if found else 1
    passages = []
    for entry, p, toks in found:
        tf = Counter(t for t in toks if t in query)
        s = bm25(query, tf, len(toks), passage_avg, idf)
        if s <= 0:
            continue
        passages.append({
            "score": round(s, 2), "source": entry["rel"],
            "line": p["line"], "heading": p["heading"],
            "page_title": titles.get(entry["id"], ""),
            "text": p["text"].strip(),
        })
    passages.sort(key=lambda p: (-p["score"], p["source"], p["line"]))

    kept, used = [], 0
    for p in passages:
        if len(kept) >= n_passages or used + len(p["text"]) > max_chars:
            continue
        kept.append(p)
        used += len(p["text"])
    return {
        "key": question_key(q),
        "title": str(q.get("title") or ""),
        "tier": q.get("tier"),
        "source_file": q.get("_source"),
        "why_unanswerable": str(q.get("why_unanswerable") or "").strip(),
        "assumption": str(q.get("assumption") or "").strip(),
        "error_if_wrong": str(q.get("error_if_wrong") or "").strip(),
        "affects": {k: affected(q, k) for k in
                    ("metrics", "tables", "columns", "domains")
                    if affected(q, k)},
        "docs_matched": len(hit_docs),
        "docs_read": len(top),
        "passages_considered": len(passages),
        "candidates": kept,
        "chars": used,
    }


# ---------- packets ----------

JOB = """## Your job

Decide whether the corpus answers THE QUESTION ABOVE, and nothing wider.

Return one verdict, as the YAML block below. You do not write any file, and
nothing you return is applied to the ontology: a human reads it at the
checkpoint and accepts it by hand.

```yaml
- question: {key}
  title: {title!r}
  verdict: answered        # answered | not_in_corpus
  answer: >                # one or two sentences, in the corpus's own terms
    ...
  source: path/to/page.md:123    # the file and line the answer is read from
  quote: >                 # the sentence that carries it, verbatim
    ...
  confidence: high         # high | medium | low
  rejected: >              # what looked relevant and why it is not
    ...
```

Rules, and the fourth one is the one that matters:

1. `answered` needs a passage that states the answer for this warehouse. Not a
   passage that would let you infer it, and not one that is about the same
   words in another context.
2. Cite the file and line you read it from. An answer without a citation is a
   guess wearing a source.
3. `low` confidence with an answer is worth more than `high` with a hedge. If
   the passage is close but not decisive, say `answered` with `low` and let the
   human see the quote — or say `not_in_corpus` and put the near-miss in
   `rejected`.
4. **`not_in_corpus` is the expected result for most questions, and a confident
   wrong answer is worse than a miss.** The candidates below are keyword hits,
   not answers: the retrieval had to return its best pages whether or not any
   of them is about your question. A page that shares vocabulary with the
   question — the same word used for a different concept, a policy about a
   similar-sounding thing — is a rejection, and naming it in `rejected` is a
   result. Never assemble an answer from two passages that each cover half.
"""


def render_packet(c: dict, label: str) -> str:
    lines = [f"# {label} — {c['title']}", ""]
    if c["why_unanswerable"]:
        lines += [c["why_unanswerable"], ""]
    for kind, title in (("metrics", "Metrics"), ("tables", "Tables"),
                        ("columns", "Columns"), ("domains", "Domains")):
        if c["affects"].get(kind):
            lines.append(f"- {title} it gates: "
                         + ", ".join(f"`{x}`" for x in c["affects"][kind]))
    # The stakes go in the packet; the assumption does NOT. A blocker's
    # assumption is the guess the run already made — this run's currency
    # blocker literally assumes "most likely USD" — and a judge shown the
    # guess it is about to confirm is not judging. The human sees the
    # assumption at the checkpoint, next to the candidate, which is where
    # the comparison belongs.
    if c["error_if_wrong"]:
        lines.append(f"- **What a wrong answer costs**: {c['error_if_wrong']}")
    lines += ["", JOB.format(key=c["key"], title=c["title"]), ""]
    lines += [f"## Candidates — {len(c['candidates'])} passages, "
              f"{c['chars']} characters", "",
              f"Retrieved from {c['docs_matched']} pages sharing any term with "
              f"the question; the {c['docs_read']} best were split into "
              f"{c['passages_considered']} scored passages and these lead. The "
              f"score is BM25 over the question's own words — it ranks, it does "
              f"not judge.", ""]
    if not c["candidates"]:
        lines += ["**No passage in the corpus shares a term with this "
                  "question.** The verdict is `not_in_corpus`; there is "
                  "nothing to read.", ""]
    for n, p in enumerate(c["candidates"], 1):
        cite = f"{p['source']}:{p['line']}"
        head = f"### C{n} — `{cite}`"
        if p["page_title"]:
            head += f" — page “{p['page_title']}”"
        lines += [head, "", f"score {p['score']}"
                  + (f" · section “{p['heading']}”" if p["heading"] else ""),
                  "", "```", p["text"], "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def cmd_packets(args):
    qs = load_questions(args.run)
    if not qs:
        print("no questions*.yml found — run this after the questions merge")
        return 0
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    targets = [q for q in qs if q.get("tier") in tiers and not answer_of(q)]
    if not targets:
        print(f"no unanswered {'/'.join(tiers)} questions — nothing to search")
        return 0

    union = set()
    for q in targets:
        union |= set(query_of(q))
    index, counts, lengths, titles = read_corpus(args.workspace, union)
    if index is None:
        print(f"no documentation corpus at {args.workspace / 'docs'} — this run "
              f"was given none, so every blocker stays unanswered. Nothing to do.")
        return 0
    if not counts:
        die(f"{args.workspace / 'docs'}/index.json lists {len(index)} documents "
            f"and none of them is on disk. Re-run ingest.py before searching.")
    idf = idf_of(counts, union)
    avg_len = sum(lengths.values()) / max(len(lengths), 1)

    out_dir = args.out or (args.run / "docs-packets")
    out_dir.mkdir(parents=True, exist_ok=True)
    cands, total = [], 0
    for n, q in enumerate(sorted(targets, key=lambda x: str(x.get("title"))), 1):
        c = candidates_for(q, index, counts, lengths, titles, idf, avg_len,
                           args.workspace / "docs", args.docs_per_question,
                           args.passages, args.max_chars_per_question)
        label = f"{str(q.get('tier'))[0].upper()}{n}"
        c["label"] = label
        c["packet"] = str(out_dir / f"{label.lower()}-{c['key']}.md")
        Path(c["packet"]).write_text(render_packet(c, label))
        cands.append(c)
        total += c["chars"]
        print(f"{label} {c['title'][:64]!r}: {c['docs_matched']} pages matched, "
              f"{len(c['candidates'])} passages kept ({c['chars']} chars)")

    dump_json(cands, args.run / "docs_candidates.json")
    print(f"\ncorpus: {len(index)} documents, {sum(lengths.values()):,} tokens. "
          f"Handed to a model: {total:,} characters (~{total // 4:,} tokens) "
          f"across {len(cands)} packets.")
    print(f"packets: {out_dir}")
    print("AGENT STEP — one agent per packet, each reading ONE packet and "
          "nothing else. Collect the verdict blocks into "
          f"{args.run / 'docs_answers.yml'}, then re-run this phase: every "
          "candidate is rendered beside its question in QUESTIONS-REVIEW.md, "
          "where the human accepts it or does not. Nothing is applied here.")
    return 0


# ---------- scoring ----------

def cmd_score(args):
    """The eval. Precision and recall separately, and a confident wrong answer
    counted apart from a miss — because they do not cost the same. A miss
    leaves the blocker blocking, which is where it started. A wrong answer at
    high confidence is the one failure the reviewer has no way to catch."""
    labels = yaml.safe_load(Path(args.labels).read_text()) or []
    cands = {c["key"]: c for c in (load_json(args.run / "docs_candidates.json")
                                  if (args.run / "docs_candidates.json").exists()
                                  else [])}
    answers = load_docs_answers(args.run)
    if not cands:
        die(f"no docs_candidates.json in {args.run} — run `packets` first")

    rows = []
    for lab in labels:
        needle = str(lab.get("match") or "").lower()
        hits = [c for c in cands.values() if needle in c["title"].lower()]
        if len(hits) != 1:
            die(f"label {needle!r} matches {len(hits)} questions — a label has "
                f"to name exactly one, or the score is measuring the matcher")
        c = hits[0]
        got = answers.get(c["key"]) or {}
        verdict = str(got.get("verdict") or "missing")
        expect = str(lab.get("expect"))
        src_want = str(lab.get("source_contains") or "")
        src_got = str(got.get("source") or "")
        src_ok = (not src_want) or (src_want in src_got)

        if verdict == "missing":
            outcome = "no verdict"
        elif expect == "answered" and verdict == "answered":
            outcome = "correct" if src_ok else "right call, wrong page"
        elif expect == "answered" and verdict == "not_in_corpus":
            outcome = "miss"
        elif expect == "not_in_corpus" and verdict == "not_in_corpus":
            outcome = "correct"
        elif expect == "not_in_corpus" and verdict == "answered":
            outcome = "FALSE ANSWER"
        else:
            outcome = f"unknown verdict {verdict!r}"
        rows.append({"label": c["label"], "title": c["title"], "expect": expect,
                     "verdict": verdict, "outcome": outcome,
                     "confidence": str(got.get("confidence") or ""),
                     "source": src_got, "source_ok": src_ok,
                     "retrieved": len(c["candidates"]),
                     "docs_matched": c["docs_matched"]})

    given = [r for r in rows if r["verdict"] == "answered"]
    correct = [r for r in given if r["outcome"] == "correct"]
    want = [r for r in rows if r["expect"] == "answered"]
    false_answers = [r for r in rows if r["outcome"] == "FALSE ANSWER"]
    confident_wrong = [r for r in false_answers if r["confidence"] == "high"]
    misses = [r for r in rows if r["outcome"] == "miss"]

    width = max(len(r["title"]) for r in rows)
    print(f"{'':4} {'question':{min(width, 64)}} {'expected':14} "
          f"{'verdict':14} outcome")
    for r in sorted(rows, key=lambda x: x["label"]):
        print(f"{r['label']:4} {r['title'][:min(width, 64)]:{min(width, 64)}} "
              f"{r['expect']:14} {r['verdict']:14} {r['outcome']}"
              + (f" [{r['confidence']}]" if r["confidence"] else ""))
    print()
    prec = len(correct) / len(given) if given else None
    rec = len(correct) / len(want) if want else None
    print(f"answers given     {len(given)} of {len(rows)} questions")
    print(f"precision         "
          + (f"{prec:.0%} ({len(correct)}/{len(given)})" if given
             else "n/a — no answer was given"))
    print(f"recall            "
          + (f"{rec:.0%} ({len(correct)}/{len(want)})" if want
             else "n/a — nothing was answerable"))
    print(f"not-in-corpus     "
          f"{sum(1 for r in rows if r['outcome'] == 'correct' and r['expect'] == 'not_in_corpus')}"
          f" of {sum(1 for r in rows if r['expect'] == 'not_in_corpus')} correct")
    print(f"misses            {len(misses)} (blocker stays blocking — no harm done)")
    print(f"FALSE ANSWERS     {len(false_answers)}"
          f"{f', {len(confident_wrong)} of them at high confidence' if false_answers else ''}")
    if false_answers:
        print("\nA false answer is the failure this design exists to avoid: it "
              "reaches the human as a candidate with a citation, and a citation "
              "is what makes prose look checked. Read each one.")
    dump_json(rows, args.run / "docs_score.json")
    if args.strict and false_answers:
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packets")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--tiers", default="blocker",
                   help="which question tiers to search for (default: blocker "
                        "— the only tier where an answer changes a number)")
    p.add_argument("--docs-per-question", type=int, default=12)
    p.add_argument("--passages", type=int, default=4)
    p.add_argument("--max-chars-per-question", type=int, default=6000)
    p.set_defaults(fn=cmd_packets)
    s = sub.add_parser("score")
    s.add_argument("--run", type=Path, required=True)
    s.add_argument("--labels", type=Path, required=True)
    s.add_argument("--strict", action="store_true",
                   help="exit 1 if any question was answered that no corpus "
                        "answers")
    s.set_defaults(fn=cmd_score)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
