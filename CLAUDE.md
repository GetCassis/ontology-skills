# Bootstrap kit — how to run this

You are assembling a first Cassis ontology from the evidence the user pointed you at. The
repository starts empty; the company's meaning does not. Read `addendum_v2.md` §17 (the process)
and §14-16 (the evidence and enrichment rules) before starting.

## Drive it with the driver

```bash
export BOOTSTRAP_FIXTURES=<where the input bundles live>
python3 intake.py questions       # NEW RUN ONLY: relay these, then `write`
python3 bootstrap.py prep   --config configs/<name>.yml   # stops for the scope
python3 bootstrap.py build  --config configs/<name>.yml   # stops for tree + metrics
python3 bootstrap.py finish --config configs/<name>.yml   # add --emit dbt for
                                                         # the dbt export
```

Run these yourself. Do not re-derive the sequence, and do not run the individual
scripts unless a phase fails and you are debugging it.

**A new run starts with the intake, not with a config.** Print
`intake.py questions`, relay the nine questions to the user in ONE message,
collect the answers in chat, then run `intake.py write` — it validates every
path now (a typo caught at intake costs seconds; caught by evidence.py it
costs a run that silently read nothing) and writes `configs/<name>.yml`.

**Intake answers are recorded facts, and they bind you.** Never re-ask one
mid-run. When the config says `warehouse_access: false`, you never attempt a
database connection of any kind — no database client, no CLI, no "just checking" —
and you never suggest profiling; a question only the warehouse could settle
goes to QUESTIONS.md. When `confirmed_absent` lists a material family, an
empty evidence class for it is expected: do not go hunting and do not ask.

## Four places you must stop and ask the user

The driver prints all four. Stop there and wait for an actual answer; do not
choose on the user's behalf and do not carry on with a placeholder.

1. **The scope** (after `prep`). The classifier settles the technical layer and
   routes what it cannot prove to `review`. Which tables are worth modeling is a
   business judgment — start from the config's `top_questions`, ask what is
   missing, and cut to the tables those touch. The config's `profile` sets the
   size: `sample` means ~10 tables carrying one story, `full` means 20-30
   for a first increment.
2. **The domain tree** (during `build`, after B1, before B2 and any column
   work). Everything downstream inherits it. Present `TREE.md` and the domain
   READMEs and ask for corrections.
3. **The metrics** (during `build`, after `apply_synthesis.py`, before stage
   C). Run `metrics_review.py`, present `METRICS-REVIEW.md`, and wait for a
   go. This is a BATCH review: the file already carries every metric with its
   provenance and stamp, riskiest first — never relay metrics one by one in
   chat, and never ask a per-metric question the file answers. Edits land in
   the metric yml files or move the metric to QUESTIONS.md; re-run the script
   after edits (it is deterministic and free).
4. **The blocking questions** (during `finish`, after the questions merge).
   `questions.py review` writes `QUESTIONS-REVIEW.md`: only the items where an
   answer changes a number, riskiest first, each carrying the assumption
   already made and what the number becomes if it is wrong. Ruling-tier items
   are offered in the same page without insisting; every other tier stays in
   QUESTIONS.md, because a page asking about coverage and hygiene is the
   interrogation this kit refuses. Same batch semantics as the metrics: present
   the file, take the answers, write each one as an `answer:` line on that
   entry in its questions*.yml file, then re-run the phase. An answered blocker
   stops blocking its own metric.

   Before that page is written, `finish` runs `docs_search.py packets`: it
   searches any free-form documentation corpus for each blocker and writes one
   packet per question into `<run>/docs-packets/`. Where packets exist, the
   agent step is not optional — one agent per packet, reading THAT packet and
   nothing else, returning a verdict: `answered` with the page and line it read
   it from, or `not_in_corpus`, which is the normal answer and not a failure.
   Collect the verdicts into `<run>/docs_answers.yml` and re-run the phase;
   each candidate then appears beside its own question, cited. It applies to
   nothing — only the human writing the `answer:` line settles a blocker — and
   a retrieved passage never goes into a column, a table or a metric.

Everything else runs unattended. Do not invent extra checkpoints, and do not
turn a checkpoint into an interrogation — the review artifact IS the question.

**Relay the cost banner before the phase that spends it.** Each phase prints
what it expects to cost before it starts. Pass that on — an independent run
never once put a token figure in front of the person before spending it, and
then spent about 1.5M on twelve subagents. It is their key and their usage
window; the estimate is worthless in a banner they did not read.

**Point at the review artifact; do not reproduce it.** The file is the question
— restating its contents in chat is what buries the ask underneath them. One
line on what it says and what it needs, then the path. That run rewrote the
whole 28-table scope, the whole domain tree and the whole metrics summary into
chat, and the person read the chat instead of the files, twice asking what was
being asked of them.

**Open every checkpoint with the ask, on the first line.** One line: what you
need, or "nothing needs deciding — say go". Then the disclosure. An independent
run stopped twice with the person asking "what precisely do you need from me?"
and "go? or do I need to make a specific decision?", because both checkpoints
opened with three paragraphs of calls already made and reversible. Disclosing a
decision you took is not asking for one, and formatting it like a question costs
the reader a turn to find out it was not.

**Never put a question to them that is not about their warehouse.** How you
parallelise, which model you drive with, whether you use subagents — yours, and
invisible. The same run spent a checkpoint asking whether to use subagents for
the enrichment stage, which the person could only answer by learning how the kit
is built.

## Their words, not ours

Every message they read is written in the vocabulary of their warehouse and
plain English. These names are ours and are meaningless to someone who cloned
this an hour ago:

- **stage names** — `B1`, `B2`, `stage C`, `prep`/`build`/`finish` as jargon.
  Say what is happening: "writing the domain tree", "describing the columns from evidence".
- **internal artifacts** — `prefill`, `packet`, `batch`, `NEEDS DESCRIPTION`,
  `repo_text`, `apply_synthesis`, `scope.tsv` level-2. If you must name a file,
  say what it is in the same breath.
- **stamps and tiers** — `VERIFY`, `UNGROUNDED`, `blocker`/`ruling`/`finding`/
  `unbound`. The generated pages define them; chat that uses them earlier does
  not. Define inline on first use or use plain words.
- **section numbers** — never cite `§14` or any other section of a document
  they have not opened as the reason for something. State the reason.
- **this repo's own source** as the explanation. Linking `metrics_review.py:91`
  to explain why a metric cannot be corroborated makes our implementation the
  answer to a question about their data. Say what is true of their warehouse.

Their table names, their column names, their domain words. If they call it a
mart, it is a mart.

## Silent while working, substantive at the stops

Measured on an independent run: **100 messages, 72 of them under 320 characters
of process narration, and 50 ending in a colon before a tool call the reader
cannot see.** "Verifying this myself before I apply it:", "Getting the exact
counts:", "Checking the metric file contract before I fan out:". Every one was
true and none of them changed anything the reader had to do. Somebody who
started a run and went to make coffee came back to a wall of your thinking.

- **A message is worth sending when something changed, was decided, or is
  needed.** Not when you are about to look at something. They can see the tool
  calls; they do not need a caption for each one.
- **Never end a message on a colon** promising a result they will not see.
- **Corrections: the corrected fact, once, in a line.** That run carried 28
  messages of first-person error narrative — "I asserted that from pattern",
  "my README was wrong", "the mistake I had to walk back on 17 metrics". Each
  was honest and the honesty is right; the volume is what reads as instability.
  State what is now true, batch them, and skip the account of how you got there.
  No self-criticism: it is not reassuring, it is a reason to distrust the rest.
- **Never grade your own subagents to them.** "The payments agent was the
  strongest of the three" is a fact about your workers. They have a warehouse,
  not workers. Report what was found, never who found it well.
- **Never critique this kit to them.** They cloned it to model their warehouse,
  not to watch it debug itself. If a gate or a script is wrong, fix it and say
  what you changed in one line, or record it — do not narrate the diagnosis.
- **Our previous runs are not a reference point in theirs.** No "the failure
  mode these runs usually have backwards", no benchmark comparisons. Their
  warehouse is the only subject.

The three messages that run got right, as the model: one that ended "Need: go,
or edits."; one that listed what was left for the person **in order of what it
would change** — every rate, then every money figure, then the rest; and one
that opened "Nothing needs you." Terse, consequence-ordered, and about them.

## What ships at the end

`finish` writes `<run>/emit/cassis/` — the ontology tree, the standalone result, which is what
`cassis ontology upload` imports, for anyone who later wants that. The
working tree under `<run>/cassis/` is not that format: it carries
`description_source` on every column, which the import rejects, and its table
files are keyed `name: SCHEMA.TABLE`. Keep enrichment in the working tree and
never hand-edit the emitted one — re-run `emit.py` instead.

`finish` also copies `templates/output-CLAUDE.md` in as `<run>/emit/CLAUDE.md`,
so the tree can be handed to someone with no context: it names each file and
gives the reading order, and defers the modeling doctrine to `AGENTS.md`
(which `cassis ontology fmt` writes in for anyone who installs cassis-cli).
`OUTPUT.md` in this repo is the same map for the whole run directory.

With `--emit dbt` the ontology is also merged back into the dbt project it came
from: descriptions into the existing `schema.yml` files without overwriting
anything already there, and everything dbt has no field for under
`meta.cassis.*`. It never touches QUESTIONS.md.

## Rules that cost real money when broken

- **A script's job is never an agent's job.** If `ingest`, `evidence`, `scope`,
  `skeleton`, `prefill` or `packets` can produce something, do not have an agent
  produce it instead. Measured: an agent stage that re-read the repo to summarize it
  cost 1.16M tokens and mostly re-derived documentation `ingest` extracts for free.
- **Never run a corpus-brief stage** unless the user explicitly asks. It is not the
  default; §16 says why.
- **Columns marked `ok` in a packet are already correct.** They carry the warehouse
  owner's own words. Read them against the defining SQL and change one only if the
  SQL contradicts it. Rewriting them in your own voice destroys the vocabulary the
  company already uses, and the run's biggest saving. Your work is the `NEEDS DESCRIPTION`
  columns. Draft only what the supplied evidence supports, leave the rest as questions,
  and stamp every drafted column `description_source: drafted` so the metrics review can
  never mistake model-written prose for the owner's own text. Legacy `authored` stamps
  remain valid non-owner provenance.
- **Every enrichment subagent writes incrementally**: create its output file with
  section headings before reading widely, then append as it goes. Agents die
  mid-run; whatever is on disk is the deliverable.
- **Computation claims come from that column's defining SQL, in its own packet.**
  Never from a sibling table or a same-named column elsewhere.
- **Isolation**: enrichment agents read and write only inside the run directory
  (plus this kit). Never any path containing `gold`, never a previous run, never
  parent directories.
- **Check your own files with the real gates, never a proxy.** An agent that
  wants to validate its work before handing it back runs
  `python3 verify.py --tree <run>/cassis --workspace <ws> --only 'tables/<DOMAIN>/*'`
  — the same gates as the final verification, report scoped to its own files,
  siblings' in-progress edits excluded. An independent run built three ad-hoc
  validators for want of this flag, trusted one of them over the real tool, and
  retracted in front of the user twice.

## Reporting

**Lead with whether anything needs them.** One line, first: "nothing needs you",
or the thing that does. Then the state. An independent run ended with a complete
and accurate final report that the reader answered with "tldr? actions for me?" —
the third time in that run they had to ask what was being asked of them. A report
they have to summarize has not reported.

After that line: the gates (internal consistency, not correctness), coverage
against the achievable ceiling rather than 100%, what carries which provenance,
and the blocker-tier questions that gate publishing.

**The judge pass is not optional and it is not free.** Run a judge over
`judge_input.json` before anyone trusts a number. Measured twice now: 2 real
contradictions in 200 claims on one run, 3 in 99 on an independent one — and on
that second run the judge was the only thing that caught them, including a
column whose description contradicted its own SQL and a coverage rate that was
an unweighted mean of daily ratios. Budget it: roughly 100k tokens per 25
claims, parallelisable by chunk.

**A judge finding that changes a metric you already reviewed goes back to them.**
The judge runs after the enrichment stage, so it lands after the metrics
checkpoint by construction. When it changes a reviewed metric, say so
explicitly and say what changed — an approval given at checkpoint 3 was given
on different content. Do not fold it silently into the final report.

## Tests

`python3 tests/test_kit.py`. No warehouse data, no env var: it runs on a fresh
clone and SKIPs what it cannot run (`dbt parse` without dbt-core installed,
`cassis ontology check` without a `CASSIS_API_KEY`). Run it after changing
anything in the kit — including the exporter, whose test is a real round trip
through dbt and the Cassis CLI rather than an assertion about the yml.
