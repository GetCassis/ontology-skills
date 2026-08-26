## 14. Harness addendum — evidence index and verification gates

Throughout this document, benchmark A, B and C name the three real warehouses the kit was
built and measured against, anonymized; a "gold" is the reference ontology a benchmark run
is graded against. Dated measurements identify specific runs on those warehouses.

This workspace ships a pre-computed evidence index at `workspace/evidence/` plus the
normalized corpora (`workspace/questions.csv`, `workspace/dashboard_queries.csv`).
Everything in it is a CANDIDATE with provenance, not a truth.

- **Evidence files.** `enums.json` and `grains.json` come from the transformation repo's own
  tests and yml. `join_candidates.json` is mined from every SQL corpus with occurrence counts.
  `metric_candidates.json` is every aggregate expression appearing ≥3 times in production
  queries, with counts and source ids. `usage.json` ranks tables by how often analysts actually
  query them — spend description depth accordingly. `usage_out_of_scope.json` lists tables
  users query that are NOT in scope: name them in QUESTIONS.md where they block a metric.
  `nonsummable.json` lists tables that stack aggregation levels.

- **Computation claims need the defining SQL, per column.** Before writing any claim about HOW
  a value is computed (week convention, timezone, scale, business-time, per-session vs
  per-conversation), re-read the defining expression of THAT column in THAT table's SQL. Never
  generalize a convention from a sibling column or a sibling table — same-named columns in this
  warehouse use different conventions.

- **Metrics need corroboration.** Define a metric only when its exact definition is supported
  by at least two independent sources (repeated corpus expressions in `metric_candidates.json`,
  the repo's own yml, a dashboard). If you cannot corroborate a definition, do not ship the
  metric: record the candidate and its variants in QUESTIONS.md instead. NEVER attach a
  business-name synonym (CSAT, churn, adoption, revenue) to a definition you inferred yourself.

  **Corroboration is about the definition, not about the provenance of the numbers.** A column
  that exists in the schema and carries the owner's own description, including its unit, is
  corroborated for an aggregate over that column — `SUM(TOTAL_EUR)` on a table documented as
  "économies financières réalisées en euros" is a shippable metric even when the value arrives
  from an upstream system whose formula lives outside this repo. Say where the number comes from
  in the metric's notes; do not withhold the metric. Measured cost of getting this backwards
  (benchmark C, 2026-08-11): three gold metrics missed because the enrichment agent treated an upstream
  passthrough as disqualifying. Withhold when the DEFINITION is a guess, not when the upstream
  pipeline is opaque.

- **Population rules must be bound, not just stated.** Any default exclusion or population
  filter you state in a domain README must also be stated on every high-usage table
  (`usage.json`) whose numbers it changes, and `joins.yml` must give those tables a path to the
  column that carries the flag — or the gap goes in QUESTIONS.md.

- **Tree first.** Before writing any table file, write the complete domain tree (all READMEs
  with frontmatter) and a `TREE.md` at the workspace root summarizing it — one line per domain
  with its tables. It stands in for the human structure review this run cannot have.

- **Self-check before finishing.** Run:
  `python3 $KIT/verify.py --tree cassis --workspace workspace
  --out verify_report.json --judge-input judge_input.json`
  (`$KIT` is wherever the bootstrap kit is checked out; an enrichment agent is given
  the absolute path in its prompt.)
  Fix every `identifiers` and `prose_refs` finding. For `restatement`, delete or ground each
  flagged description. For `metric_grounding` and `population_rules`, fix or justify in
  QUESTIONS.md. A `divergent_duplicates` finding means the same source text was corrected on
  another table during this run: read the flagged copy against its OWN defining SQL — never
  copy the other packet's edit across. Do not finish with unaddressed findings.
  An agent that owns a subset of the tree adds `--only 'tables/<its-domain>/*'` to scope the
  report to its own files — same gates, siblings' in-progress edits excluded. Never write a
  proxy validator.

## 15. Mandatory standard questions

Every warehouse has rules that live in people's heads and appear in no artifact. QUESTIONS.md
must therefore END with a section titled "Standard questions" answering EVERY item below — each
either with the rule and its evidence, or with a QUESTIONS entry stating what you assumed:

1. Which populations are excluded from analytics by default (employees, test users, demo
   accounts, specific customer types), and which tables carry the excluding flags?
2. What are the week/calendar conventions (week start day, fiscal calendar, any company-specific
   bucketing function), and which columns use which?
3. What timezone are timestamps stored in?
4. How are satisfaction metrics defined (mean vs top-box, which threshold, which column)?
5. What SLA/service-level thresholds exist, and do the code and documentation agree?
6. What currencies and unit scales do money columns use?
7. Which frequently-asked business metrics cannot be computed from the in-scope tables, and
   which out-of-scope tables would they need?

Do not skip an item because the material is silent — silence is exactly what makes it a
question for the data team.

## 16. Staged assembly protocol (v3)

Assembly is a DAG, not a monologue. The mechanical parts are pre-generated by scripts
(`skeleton.py`): every table file already exists as a shell (name, grain, full column-name list,
`domain_path: UNASSIGNED`), join candidates sit in `joins_draft.yml` awaiting disposition, and
`batches.json` orders tables by real usage — or, with no BI corpus, by dbt relationship in-degree.
`dispatch_prep.py packets` then builds one packet per batch carrying each table's columns, types,
grain, enums, dispositioned joins, the owner's own resolved descriptions and the defining SQL.
Agents EDIT; they do not type boilerplate, and they do not go hunting for context a script can
hand them.

**Measured on benchmark C, 2026-08-11 — the staging did NOT pay off, and the reason is structural.**
134 min and 3.58M tokens against a single-pass baseline of 63 min and 803k. Barriers cost the sum
of per-stage maxima, and a stage is only as fast as its heaviest agent, so three barriers cost
roughly three single-pass runs. Parallel fan-out also does not reduce tokens: enrichment is
output-dominated, so splitting 1,000 column descriptions across six agents costs what one agent
would have spent writing them. Use staging for what it actually bought — join precision rose from
76% to 88% because one agent had to disposition every candidate with a reason — and drop stages
that only re-read material a script can extract. Prefer running Stage C's batches without waiting
for every sibling (pipeline, not barrier) whenever the batches are genuinely independent.

**Run the prep scripts before any agent, in this order:** `ingest.py` (resolves the repo's doc
blocks), `evidence.py` (candidate index; `--strict` in CI), `skeleton.py` (shells + joins draft +
ranked batches), `dispatch_prep.py prefill` (writes the owner's own descriptions into the shells),
then `dispatch_prep.py packets`. On benchmark C `prefill` lands 1,432 of 2,556 column descriptions (56%)
and 171 table descriptions without a model writing a word, which turns enrichment from "describe
2,556 columns" into "draft 1,124 from evidence and check the rest". Skipping it means paying an agent to retype
documentation that was already in the workspace.

**Report prefill as whose words survived**: this many of your columns were already documented by
your own team, kept as written. What it saved follows from that and is never the headline.

- **Stage A — parallel corpus briefs. NOT the default. Skip it for a repo-only run.** Several
  readers digest the sources concurrently (one per source family or domain cluster) and produce
  short per-area briefs: entities, rules found, join evidence, metric candidates, discrepancies.
  No files in `cassis/` are touched. On benchmark C this stage cost 1.16M tokens — a third of the run —
  and most of it went on hand-reconstructing the doc-block descriptions `ingest.py` now resolves
  for free. Run it only when the corpus holds judgment the scripts cannot extract: conflicting
  dashboards, tribal prose in a wiki, a source family with no schema. Never as a warm-up.
- **Every enrichment agent writes incrementally.** Create the output file with its section
  headings before reading widely, and append after each section's worth of reading. Agents die
  (watchdog stalls, API drops, the host sleeping); on benchmark C three of five died and only the one
  that had been writing as it went kept its work. Whatever is on disk is the deliverable.
- **Stage B — synthesis (the one sequential judgment), split by what needs which model.**
  - **B1 (Opus, alone):** the domain tree — `TREE.md`, every domain README (rules and conventions
    live here), and a `decisions/domains.yml` assigning every table. This is the judgment that
    cannot be parallelised and cannot be cheapened; everything downstream reads it.
  - **B2 (Sonnet, parallel by domain or hub table):** disposition every `joins_draft.yml` entry
    into promote-or-reject-with-reason, and ship corroborated metrics. Both are bounded judgments
    against a written rule, and both fan out. On benchmark C, B as one Opus agent took 34 minutes — half
    of a whole single-pass baseline run — for work that is mostly this.
  - A script applies the result: `apply_synthesis.py` validates the decisions against the real
    schema and materializes them. Never hand-edit hundreds of shells; an agent doing 198 edits is
    slower, costlier, and free to typo a table name.
  - **After apply_synthesis, run `metrics_review.py` and stop.** It renders every candidate
    metric as one page — definition, base table, corpus corroboration with source ids, the
    owner's own text on the aggregated column, one stamp (ok / VERIFY / UNGROUNDED), riskiest
    first. Present the FILE to the user and wait for a go before Stage C. This is a batch
    review, not an interrogation: the first independent run (benchmark B, 2026-08-18) shipped
    32 metrics the reviewer never saw before they landed, and the complaint was volume, not
    autonomy — so the fix is a review surface between stages, never per-metric questions in
    chat. Edits land in the metric yml files or move the metric to QUESTIONS.md; the script
    re-runs deterministic and free.
  - Batch the writes: one write per domain or file group, never one per line-item.
- **Stage C — parallel enrichment.** One agent per batch from `batches.json`, each reading ONE
  packet. Its job is the columns marked `NEEDS DESCRIPTION`; columns pre-filled from the owner's own
  documentation are checked against the SQL, not rewritten. Every column the agent drafts gets
  `description_source: drafted` — provenance must survive, and the metrics review refuses to
  count anything but the owner's stamped tiers as evidence, so an unstamped drafted column is
  merely invisible to it, never mistaken for the owner's text. Legacy `authored` stamps remain
  valid non-owner provenance and are treated the same way. Coverage floor still applies (every
  column described or deliberately skipped). Compound rules keep every clause; computation claims
  come from THAT column's defining SQL.
  **Size the batches for wall-clock, not for tidiness.** A stage ends when its slowest agent ends,
  so more, smaller batches shorten the stage at identical token cost — 12 batches of ~16 tables
  finish sooner than 6 of ~33. Keep each batch's description-gap load (not its table count) balanced;
  `prefill_report.json` gives the per-table figure.

**Start small, then expand.** Batch 0 is the high-usage slice by construction. Tree + rules +
batch 0 is a publishable, queryable v0.1 — publish it, then let later batches land as
increments (in a git-synced project, each batch is a commit and merge publishes it). Do not
hold the ontology hostage to the long tail: the last low-traffic table's descriptions block
nothing.

**Completeness bars (unchanged from v2):** the coverage floor, compound-clause completeness,
and mandatory join-candidate disposition all still apply; verify.py gates every stage.

## 17. The bootstrap process, end to end

What the run needs, what runs alone, and where a human is genuinely needed. Every
number below is measured on the three benchmark fixtures (A, B and C), not estimated.

**Mandatory inputs — two.** A schema export of the tables in scope (it defines the
universe and makes invented column names structurally impossible), and the
transformation repo (the only source of computation truth: without defining SQL
there is no grain, no join evidence, no unit claim — you get a glossary, not an
ontology).

**Recommended, and where the value actually is — point the kit at it and it finds it.**
Existing documentation in ANY shape, wherever it lives. This was the largest lever
on all three fixtures and in each case it sat somewhere nothing thought to look:
dbt doc blocks behind `{{ doc() }}` (benchmark C, 1,502 columns), inline YAML comments
(benchmark A, 232), a column-name-keyed glossary in an unread folder (benchmark B,
1,673). Then the BI layer (dashboards, saved questions, query logs), which drives
what to document deeply, join evidence, and metric corroboration at once — benchmark C had
none and its metric-grounding gate was structurally unsatisfiable as a result. Then
20-50 questions people really ask, harvested not authored. Then a named human who
will answer roughly twenty questions.

Never ask anyone to write documentation for the run. Preparation is where these
projects die, and every hour spent writing docs for the tool is an hour spent
proving the tool does not work. `--context-dir` takes whatever already exists —
a Notion export, a Slack archive, product source, PDFs — and classifies it by
SHAPE, because shape is what needs a parser and the transport is whatever the
export happens to be.

**Step 0 — intake (`intake.py`, once per run, and the answers bind).** Eight
questions, relayed in one message: the mandatory two, documentation and BI
paths (or an explicit "we have none" — `--absent` — so a missing family and an
unread family never look the same again), whether the user can run SQL
against the warehouse, and how much one pass covers (`full` increment vs `sample`
springboard, which sets the scope targets). Every path is validated at write
time. `warehouse_access: false` is a standing order: nothing downstream —
script or agent — attempts a connection, suggests profiling, or asks again;
questions only the warehouse could settle go to QUESTIONS.md. Measured
motivation: the first independent run had the driving agent repeatedly try to
reach a warehouse the user had no access to, and the user had to know
from experience to scope the core first — both facts now live in the config
before anything runs.

**Step 1 — preflight (seconds, automatic, and it can stop the project).**
`evidence.py --scope` reports what the material can support: how many in-scope
tables have a model, how many have documentation, how many joins are derivable,
whether a usage signal exists. Benchmark B reads `11/97 in-scope tables have a
repo model` — a conversation worth having before spending anything.

**Step 2 — scoping, two levels (`scope.py`).** Level 1 is technical and needs no
business input: source mirrors, export/serving layers, tooling artifacts,
intermediate plumbing, backups, seeds. On benchmark C that is 101 of 198 tables and 59% of
the columns an unscoped run pays for. Its guarantee is asymmetric — it may keep too
much, and must never hard-drop a table a human would model, so anything it cannot
prove goes to `review` rather than out. Level 2 is a conversation: of what
survives, which tables answer the top ~20 questions. A first complete ontology is
60-100 tables; the first publishable increment is 20-30.
**Excluded from modeling is never excluded from evidence.**

**Step 3 — optional profiling (`profile.py`, on the user's own machine only, and
only when intake recorded warehouse access).** `plan` writes SQL for the user to
review and run in their own warehouse; nothing connects out and nothing
is transmitted. Value samples are off by default. Each measurement earns its place
by settling a class of question code cannot: a `..._ratio` holding 0-1 versus
0-100, a STRING column carrying timestamps, a column NULL in every row, a declared
grain that does not actually identify a row.

**Step 4 — structure (automatic, then the two reviews that matter).** The synthesis
stage proposes the domain tree, the rules per domain, the join dispositions and the
metrics. **The tree is the highest-leverage human checkpoint and is a hard
gate**: twenty minutes on one page, because the tree decides where everything lands
and every later stage inherits it. **The metrics get the same gate**
(`metrics_review.py` → METRICS-REVIEW.md): every metric on one page with its
provenance and a stamp, presented before enrichment — because a batch of
model-drafted business definitions landing unseen is how trust dies, however
good the definitions are.

**Step 5 — enrichment (automatic).** Column content per batch, mostly keeping the
owner's own words and checking them against the SQL rather than rewriting.

**Step 6 — what comes back.** A git repo the user owns — domain READMEs carrying the
rules, one file per table, joins, metrics — plus `QUESTIONS.md` and a coverage
report. It runs outside the product; publishing to Cassis is a separate choice.

**How anyone knows it is done.** Not one number, because "done" is not binary:
gates pass (they prove internal consistency, not correctness); coverage is quoted
against the achievable ceiling rather than 100%; every claim carries provenance
(the owner's own words / a warehouse-wide default needing confirmation / drafted
from SQL / no grounding), so a reviewer checks the weak tiers instead of the whole
tree; the eval suite passes on the top questions from intake; and the residual error rate is
stated up front — the judge found 2 real contradictions in 200 checked claims, so a
warehouse-scale tree contains tens of wrong claims and saying otherwise is how trust
dies. The system never announces that it is finished. It hands back a labeled
artifact and an explicit list of what only a human can settle.
