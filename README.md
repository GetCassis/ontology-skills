# Cassis context bootstrap

Assemble a reviewable first version of an analytics agent's context from the dbt models,
warehouse schema, dashboards, query history, and documentation you already have.

The method behind it, measured end to end on GitLab's public analytics project (2,921 dbt
models), is written up in [this blog post](https://blog.getcassis.com/a-blank-beats-a-guess/).

The kit recovers what those sources already contain, preserves the evidence behind every claim,
uses agents to organize, verify, and translate that evidence, and turns unresolved meaning into
questions. It never treats model-written prose as evidence. Scripts do everything the inputs
determine; you decide the genuine judgment calls at four checkpoints.

What comes out is an ontology: your domains, tables, columns, metrics and joins, described in the
words your company already uses, in files you review like code. Point an agent at it and it knows
what a row means, which metric is the defined one, and how two tables join, before it writes any
SQL.

What also comes out is a list of defects in your own pipeline: documentation that contradicts the
SQL, rules stated in one place that silently govern numbers somewhere else, metrics nobody can
corroborate. The ontology is the point and the defect list is a byproduct, but it is one worth
handing to whoever owns the pipeline, because every entry is proved from your own SQL.

It runs on your machine. No account, no key, no telemetry, no call home: the only thing that
leaves your laptop is what the judgment stages send to whatever model you drive the run with.

## Status

The kit is how we bootstrap context on real projects today, and what we hand teams to run on
their own. It is published standalone and not yet part of the Cassis CLI; when it graduates
there, we will keep a standalone version available. Run it and tell us where it breaks.

## What we assume

- Most of the context an agent needs already exists in your stack, written for other readers.
  → The first version is a sorting job, not a writing one.
- A wrong answer costs more than a missing one, because nobody can see it. → A blank beats a
  guess, at every step.
- Bootstrapping moves your source of truth: what agents read becomes this ontology, not the dbt
  docs and glossaries it was assembled from. → It carries the evidence those sources gave it,
  and it is maintained from there.

## What it needs

| Input | Why it is needed |
|---|---|
| A schema export: every table and column, from your information schema | Mandatory. It is what makes an invented column name impossible rather than merely catchable |
| A dbt project, or a dbt docs export of one | Mandatory. Without defining SQL there is no grain, no join evidence, no unit |
| A column glossary, your warehouse's own column comments, or dbt `{% docs %}` blocks | Optional, and the single biggest saving: these three go straight into the ontology in your own words, kept as written. A column glossary has to be handed over as one. Nothing harvests definitions out of prose, because a bold heading matching a column name is not evidence it defines that column |
| Free-form documentation: a wiki export, reference docs, PDFs | Optional. It answers the run's blocking questions, and never fills in a column: for each question where a wrong answer makes a number wrong, retrieval pulls the passages that might answer it, an agent decides whether any of them does, and the candidate reaches you at the fourth checkpoint with the page and line it came from. You accept it or you do not. Applying prose to a column by name instead was measured on a 4,700-page public handbook: 8 columns filled, 8 wrong |
| Dashboard or saved-question exports carrying SQL | Optional. Join evidence, usage ranking and metric corroboration at once |
| A query log, if you can export one | Optional. Ranks what people actually query |

Nothing here asks you to write documentation. Point the kit at what already exists.

If you cannot run SQL against the warehouse yourself, say so at intake and nothing will try to
connect, ever. Questions only the warehouse could settle go into the open-questions file instead.

## Install

```bash
python3 -m pip install -r requirements.txt
```

Three pure-python packages. Test dependencies are separate (`requirements-dev.txt`) and you do not
need them to run the kit.

## Run it

Drive it from a coding-agent session in this directory; the repo ships a `CLAUDE.md` the agent
reads. The phases below are scripts and cost nothing. Where the driver prints an enrichment
stage, that is the agent's evidence-backed drafting and it is where the tokens go.

```bash
python3 intake.py questions                 # nine questions, answered once
python3 intake.py write --name mywarehouse \
    --schema ~/exports/schema.json \
    --input ~/code/our-dbt-project --adapter dbt \
    --docs-dir ~/exports/wiki --dashboards-dir ~/exports/dashboards \
    --warehouse-access no --profile sample \
    --top-question "how much revenue did we make last month"

python3 bootstrap.py prep   --config configs/mywarehouse.yml   # stops for the scope
python3 bootstrap.py build  --config configs/mywarehouse.yml   # stops for tree, then metrics
python3 bootstrap.py finish --config configs/mywarehouse.yml   # stops for the blocking questions
```

`--name` just names the run and its config file; the phases read every path and every recorded
fact from that file, so they are stated once. Add `--emit dbt` to `finish` to also merge the
result back into the dbt project it came from.

**Start with a sample.** `--profile sample` hard-scopes the run to about ten tables carrying one
story end to end. It is the fastest way to see what the output looks like on your own data, and the
cheapest way to find out that a schema export is malformed or a docs directory holds nothing the
kit can read. The only things the profile changes are the scope target and the cost.

| Run | Scope | The model writes | Wall clock | At list API prices |
|---|---|---|---|---|
| `--profile sample` | ~10 tables, one story | ~0.6M tokens, all Sonnet | ~40 min | roughly $30–60 |
| `--profile full` | 20–30 tables, a first increment | ~1.6M tokens: an Opus driver plus ~20 Sonnet subagents | ~2 h | roughly $120–200 |

Both rows are one real warehouse each, recounted in full from the session transcripts rather
than projected. Treat the dollars as an order of magnitude: most of a run's list price is agents
re-reading their own context as cache reads, so the total moves with cache behavior and with how
many turns the agents take, not with the ontology's size. On a Claude subscription the currency
is your usage window rather than dollars, and cache reads are cheap there: a sample run is one
sitting's worth of work, and a first increment is the largest thing you will run that day. Give
it its own session.

What moves a run up its range: a large docs corpus (one packet agent per blocking question),
corrections at the checkpoints (a corrected domain or metric redrafts its files), and the judge
pass, an agent that re-reads every drafted claim against the evidence behind it. The judge is
part of the run, not an option: it is the only stage that has caught a description contradicting
its own SQL, on two different warehouses.

The scope is yours (it is checkpoint 1), and the cost follows the scope, not the warehouse. At
2,000 tables the shape is the same, not bigger: the deterministic phases read the whole export
(they are scripts, and free), the classifier proposes the cut, and the modeled increment stays
20–30 tables per run. You grow the ontology increment by increment rather than paying for the
warehouse in one sitting, and every deterministic stage is free to re-run.

## The four checkpoints

The driver stops at each one and prints what to look at. Each stop is a generated file you read
top to bottom, never a list of questions asked one at a time.

1. **The scope.** A classifier settles the technical layer (staging, intermediate, marts) and
   routes what it cannot prove to `review`. Which tables are worth modeling is a business
   judgment, and it is yours: cut the file to the tables your top questions actually touch.
2. **The domain tree.** Everything downstream inherits it, so it is presented before any column
   work. Correct the domain names and the shape here rather than later.
3. **The metrics.** Every metric on one page with its provenance and one stamp: corroborated by
   at least two independent sources, corroborated by one, or corroborated by nothing. The
   uncorroborated ones lead, and a business synonym riding on a weak stamp is flagged outright.
   One consequence to expect on a first run: corroboration comes from dashboards and query logs,
   so a run without either (the normal first-try shape) stamps every metric VERIFY by design.
   That is not a defect in your metrics; it means no independent source confirmed the formula, so
   business names ("DAU") wait until a person confirms them at this checkpoint.
4. **The blocking questions.** Only the items where your answer changes a number, riskiest first,
   each carrying the assumption that was made instead and what the number becomes if that
   assumption is wrong. Answering is optional and re-runnable: the ontology ships either way, with
   the assumption stated, and answering one stops it blocking its own metric.

Everything the run could not settle but that does not change a number stays in the open-questions
file, with the assumption it shipped. A tool that needs every unknown answered before it produces
anything does not survive first contact.

## What you get

`OUTPUT.md` is the map: two copies of the ontology and a set of reports.

- `<run>/emit/cassis/` is the canonical tree, and the one to keep: domain READMEs, one file per
  table, one per metric, and the joins. `<run>/cassis/` is the working tree the run assembles and
  the checks read; it carries per-column provenance that the canonical format does not accept.
- `<run>/emit/CLAUDE.md` is copied in beside the tree: what each file holds and the order to read
  it in, for whoever you hand it to.
- The reports say how much to trust it: which descriptions are your own words versus drafted
  from evidence during the run, every metric's corroboration, every computation claim paired with the SQL that
  defines it, and the open questions.

This is what a table file looks like. It is real output, from a run over the four-model fixture
project this repo ships:

```yaml
schema_name: MAIN
table_name: ORDERS
domain_path: commerce
description: One row per order placed in the store.
grain:
- ORDER_ID
columns:
- name: ORDER_ID
  description: Primary key of the order, assigned by the storefront at checkout.
  data_type: VARCHAR
- name: STATUS
  description: 'Order lifecycle status: completed or cancelled.'
  data_type: VARCHAR
- name: TOTAL_AMOUNT
  description: Order total in EUR, tax included. Cancelled orders keep their amount.
  unit: EUR
  data_type: DECIMAL
```

[`sample-output/`](sample-output/) holds the rest of the excerpt (a metric with a mandatory
filter, a domain README, and the file tree of the whole emitted run), regenerable with
`python3 tools/make_sample_output.py`.

## The dbt export

`--emit dbt` merges the ontology into your dbt project's own `schema.yml` files, in place:

- Descriptions are written **only where the project has none**. Nothing you already wrote is
  overwritten, comments and key order survive, and a second export changes no byte.
- Joins become `relationships` tests, with the grain and cardinality beside them under
  `meta.cassis.join`.
- Everything dbt has no field for (the domain hierarchy, synonyms, metric caveats) goes under
  `meta.cassis.*`, one documented namespace. With `persist_docs` on, the descriptions reach the
  warehouse itself as column comments, where an agent querying it directly can see them.

Two limits, both measured:

- **Metrics reach dbt as `semantic_models` and `metrics` only if your project already has a
  MetricFlow time spine.** Without one, a project stops parsing the moment any metric exists, so
  metrics ship under `meta.cassis.metrics` instead and your build keeps working.
- **A metric with a mandatory filter is never exported as a dbt metric.** The filter needs a
  dimension reference the kit cannot synthesize, and the aggregate without its filter is a wrong
  number that looks governed.

Verified by handing the merged project to dbt itself: `dbt parse` over real projects, including a
public one of 2,921 models, plus `dbt run` and `dbt docs generate` on this repo's fixture in CI.

## What it does not do

- **SQL dialects: two are exercised, three are mapped, the rest parse as ANSI.** The dbt adapter
  detects your project's adapter and hands its dialect to sqlglot for BigQuery, Snowflake,
  DuckDB, Redshift and Postgres. BigQuery and Snowflake have carried real multi-hundred-model
  runs; DuckDB runs in this repo's CI; Redshift and Postgres are mapped but have not carried a
  real run yet. Any other adapter (Databricks, Trino, ClickHouse) falls back to ANSI parsing:
  the run completes, but SQL-derived evidence (grain, enums, join mining) degrades on
  dialect-specific syntax. Nothing the schema export provides is affected.
- **Join mining is regex and alias based, not a SQL parser.** It under-counts CTE-heavy queries,
  and where a shared key has several legitimate hubs it elects one.
- **The restatement check is lenient.** It catches a description that restates the column name,
  not one that says nothing.
- **Metric corroboration over-flags.** It matches expression shapes, so the flag means "confirm
  this", not "this is wrong".
- **Your `dbt test` suite may go red** after an export: a `relationships` test that fails is a
  finding about the data, not a bug in the export, but it is still your suite that turns red.
- **A run needs no account, no key and no network.** Nothing in any phase calls out. The one
  check that does, validating the emitted tree against the Cassis import format, is a test of
  this repo, skipped in its own suite without a key, and never a step in your run.
- **There is no evaluation harness.** The kit cannot tell you whether the ontology it produced is
  good. The four checkpoints and the reports are how you tell.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 tests/test_kit.py
```

Runs on a fresh clone with no data of your own and no environment variables, and skips rather than
fails what it cannot run.

## Issues

Issues are on. What you get is best-effort: we read them, real failures get fixed, and there is
no SLA. **If a run fails: stop at the phase banner, keep the run directory, and open an issue with
the banner and the phase name.** The banner names the stage and what it was reading, which is most
of the diagnosis.

## Keeping it current

The ontology you get here is a snapshot of what your warehouse means today, and warehouses move:
a column changes meaning, a metric gains an exception, a definition turns out to be wrong the
first time somebody asks a question it cannot answer. Keeping that context true as the warehouse
moves is what [Cassis](https://getcassis.com) does: the same ontology, enriched from real use,
with every change reviewed by the data team before it becomes truth.

The output here is a Cassis ontology already, so there is nothing to migrate:

```bash
pip install cassis-cli
cassis ontology upload --project <id> --no-publish   # review it in Cassis first
cassis ontology fmt                                  # writes AGENTS.md into the checkout
```

## License

MIT. See `LICENSE`. `adapters/vendor/inventory.py` is vendored from `dbt-agent-readiness` (MIT),
with its license and provenance beside it.
