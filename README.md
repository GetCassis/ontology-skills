# Cassis ontology skills

Skills for building and expanding the context an analytics agent needs. Both start from
your data assets, attach evidence to definitions, and leave business decisions to you.

- **ontology-bootstrap** builds a first ontology from what your stack already has.
  [Try it on your data ↓](#try-it-on-your-data)
- **ontology-expansion** grows one you already maintain.
  [Expand an existing ontology ↓](#expand-an-existing-ontology)

## What a run leaves you

A tree of files you review like code, and the questions nothing could answer:

```text
<run>/
├── emit/cassis/
│   ├── domains/commerce/README.md     rules in prose, your words or drafted
│   ├── tables/MAIN/ORDERS.yml         grain, column meaning, stored values, units
│   ├── metrics/completed_revenue.yml  the formula and its mandatory filter
│   └── joins.yml                      how tables connect, with cardinality
├── METRICS-REVIEW.md                  how well each metric is corroborated
└── QUESTIONS.md                       what nothing answered, and what was assumed
```

One rule, carried end to end. The domain README states it in words:

> Revenue is counted on completed orders: a cancelled order keeps its amount.

and the metric carries it as a filter, so an agent asking for revenue gets the formula and the
filter together:

```yaml
name: completed_revenue
table_schema: MAIN
table_name: ORDERS
expression: SUM(TOTAL_AMOUNT)
filters: STATUS = 'completed'
description: Cancelled orders keep their amount, so the filter is mandatory.
```

These excerpts come from the four-model fixture in [`sample-output/`](sample-output/). On
GitLab's public analytics project, a run covered 2,573 of 2,641 columns (97.4%), left 68 blank
rather than guess, and filed 45 questions ([write-up](https://blog.getcassis.com/a-blank-beats-a-guess/)).

## Why not just ask an LLM?

Hand a model your dbt project and ask it to document the warehouse, and every column comes back
described, because a model has a plausible meaning for almost any column name. On GitLab's
public analytics project, a model read `is_arpu` as average revenue per user. The project's own
docs say Annual Revenue Per User, excluding the Storage tier and the EDU rate plan. Nothing in that kind of output tells
you which descriptions are like this one.

The kit uses LLMS too. What changes is what the model is allowed to write:

| | Asking a model directly | This kit |
|---|---|---|
| Where a description comes from | What the column name suggests | Your own text first (dbt docs, a glossary, warehouse comments), kept as written. A draft only where every clause traces to SQL or source text |
| When nothing supports a definition | It writes one anyway | It leaves a blank and files a question: 68 of 2,641 columns on GitLab's public analytics project |
| Telling a guess from a fact | Both read the same | Every claim names its source, and a verification pass rereads each one against it. That pass caught `is_arpu` |
| Who settles business meaning | The model | You, at four checkpoints |

The run also turns up defects in your own pipeline: documentation that contradicts the SQL, rules
stated in one place that silently govern numbers somewhere else, metrics nobody can corroborate.
Each one is proved from your own SQL.

## Try it on your data

1. Install the skill:

   ```bash
   npx skills add GetCassis/ontology-skills --skill ontology-bootstrap --full-depth
   ```

2. Gather what the run reads ([details](#what-it-needs)):
   - **Required:** a schema export, every table and column from your information schema.
   - **Recommended:** the repository that builds your tables. A dbt project is read directly;
     another layout, such as SQLMesh, through a [mapping config](adapters/llm_config_fallback.md).
     Without one the run still works, but expect structure rather than meaning.
   - **Optional, and worth it:** a column glossary or your warehouse's comments, docs, dashboard
     exports carrying SQL, a query log, a semantic layer (a Power BI model saved as a PBIP
     project, dbt MetricFlow, Cube, or LookML).
3. Open your coding agent in a folder next to them and ask:

   > Use ontology-bootstrap on our warehouse. The schema export is ./schema.csv and the dbt
   > project is ./analytics. Start with the sample profile.

| Run | Scope | Models | Tokens written | Agent time | At list API prices |
|---|---|---|---|---|---|
| sample | ~10 tables, one story | Claude Sonnet 5.5: the driver and ~5 subagents | ~0.12M | ~6 min | ~$4 |
| full | 20–30 tables, a first increment | Claude Opus 5.5 driver, ~20 Claude Sonnet 5.5 subagents | ~0.2–0.3M | ~15 min | ~$9–13 |

Start with the sample: it is the fastest way to see the output on your own data, and the
cheapest way to find out that an export is malformed. [What drives the cost](#run-it-by-hand).

It runs on your machine. No account, no key, no telemetry, no call home: the only thing that
leaves your laptop is what the judgment stages send to whatever model you drive the run with.

## What happens during a run

The driver stops at each of the four checkpoints and prints what to look at. Each stop is a
generated file you read top to bottom, never a list of questions asked one at a time.

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

## What it needs

| Input | Why it is needed |
|---|---|
| A schema export: every table and column, from your information schema | Mandatory. It is what makes an invented column name impossible rather than merely catchable. If the warehouse is already connected to Cassis, `cassis schema pull` writes one (`cassis/.schema.json`) that also carries the warehouse's primary keys, foreign keys and view definitions: they become grains, join candidates and defining SQL |
| The repository that builds your tables: a dbt project or a dbt docs export of one, or another layout that keeps one file per table, such as SQLMesh | Strongly recommended: defining SQL is where grain, join and unit evidence come from. dbt is read directly. Any other layout takes one mapping config, written by hand or by the prompt in `adapters/llm_config_fallback.md`, and `--adapter generic` applies it to every file. With no repository at all, `--adapter none` still runs from the schema export and whatever else you bring, but expect structure rather than meaning: on a 29-table Postgres schema with nothing else, the run found 29 grains from primary keys and 44 join candidates, and no evidence to describe any of its 231 columns |
| A column glossary, your warehouse's own column comments, or dbt `{% docs %}` blocks | Optional, and the single biggest saving: these three go straight into the ontology in your own words, kept as written. A description that only repeats a warehouse comment is left to the comment in the shipped tree, because Cassis reads comments itself and two copies drift. A column glossary has to be handed over as one. Nothing harvests definitions out of prose, because a bold heading matching a column name is not evidence it defines that column |
| Free-form documentation: a wiki export, reference docs, PDFs | Optional. It answers the run's blocking questions, and never fills in a column: for each question where a wrong answer makes a number wrong, retrieval pulls the passages that might answer it, an agent decides whether any of them does, and the candidate reaches you at the fourth checkpoint with the page and line it came from. You accept it or you do not. Applying prose to a column by name instead was measured on a 4,700-page public handbook: 8 columns filled, 8 wrong |
| Dashboard or saved-question exports carrying SQL | Optional. Join evidence, usage ranking and metric corroboration at once |
| A semantic layer: a Power BI model saved as TMDL (a PBIP project), dbt MetricFlow YAML (classic or newer spec), a Cube YAML data model, or a LookML project | Optional. Its metrics, joins and column docs enter as declared evidence, each held against the schema export first. A Power BI table counts only through the warehouse table its Power Query source reads. DAX the kit can translate ships marked as translated, for a person to confirm. Time intelligence and filters through a related table are listed for review, never forced into a metric. See [`adapters/semantic_layer.md`](adapters/semantic_layer.md) |
| A query log, if you can export one | Optional. Ranks what people actually query |

Nothing here asks you to write documentation. Point the kit at what already exists.

If you cannot run SQL against the warehouse yourself, say so at intake and nothing will try to
connect, ever. Questions only the warehouse could settle go into the open-questions file instead.

## Run it by hand

The skill drives these steps for you. To drive them yourself, clone this repository and install
its dependencies, three pure-python packages:

```bash
python3 -m pip install -r requirements.txt
```

Then run it from a coding-agent session in the clone; the repo ships a `CLAUDE.md` the agent
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

`--profile sample` hard-scopes the run to about ten tables carrying one story end to end. The
only things the profile changes are the scope target and the cost.

The sample row is a 10-table run on GitLab's public analytics project (508 columns in scope).
About three quarters of a run's list price is context the agents write to the cache and re-read
from it, so the total moves with how many turns the agents take more than with the ontology's
size. Agent time excludes the time you spend reading at the checkpoints. On a Claude
subscription the currency is your usage window rather than dollars.

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
description: Orders placed in the store.
grain:
- ORDER_ID
columns:
- name: ORDER_ID
  description: Primary key of the order, assigned by the storefront at checkout.
  data_type: VARCHAR
- name: STATUS
  description: Order lifecycle status.
  data_type: VARCHAR
  enum_values:
  - value: completed
  - value: cancelled
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
  warehouse itself as column comments, where any agent querying it can see them, Cassis included.

Two limits, both measured:

- **Metrics reach dbt as `semantic_models` and `metrics` only if your project already has a
  MetricFlow time spine.** Without one, a project stops parsing the moment any metric exists, so
  metrics ship under `meta.cassis.metrics` instead and your build keeps working.
- **A metric with a mandatory filter is never exported as a dbt metric.** The filter needs a
  dimension reference the kit cannot synthesize, and the aggregate without its filter is a wrong
  number that looks governed.

Verified by handing the merged project to dbt itself: `dbt parse` over real projects, including
GitLab's public analytics project (2,921 models), plus `dbt run` and `dbt docs generate` on this repo's fixture in CI.

## What it does not do

- **SQL dialects: two are exercised, three are mapped, the rest parse as ANSI.** The dbt adapter
  detects your project's adapter and hands its dialect to sqlglot for BigQuery, Snowflake,
  DuckDB, Redshift and Postgres. BigQuery and Snowflake have carried real multi-hundred-model
  runs; DuckDB runs in this repo's CI; Redshift and Postgres are mapped but have not carried a
  real run yet. Any other adapter (Databricks, Trino, ClickHouse) falls back to ANSI parsing:
  the run completes, but SQL-derived evidence (grain, enums, join mining) degrades on
  dialect-specific syntax. Nothing the schema export provides is affected.
- **A layout that is not dbt gives what its mapping names, and nothing a dbt manifest would
  add.** `--adapter generic` reads one file per table: its SQL, and whatever the config points
  at (description, grain, column docs, value lists). There is no ref graph, no test suite and
  no macro expansion, so Jinja and macros stay as written, a model named through a macro is
  reported and skipped, and Python models are not read. Checked on SQLMesh's public example
  project: 11 of its 12 SQL models mapped with their grains and descriptions, and the joins in
  their SQL became join candidates.
- **Join mining is regex and alias based, not a SQL parser.** It under-counts CTE-heavy queries,
  and where a shared key has several legitimate hubs it elects one.
- **The restatement check is lenient.** It catches a description that restates the column name,
  not one that says nothing.
- **Metric corroboration over-flags.** It matches expression shapes, so the flag means "confirm
  this", not "this is wrong".
- **Your `dbt test` suite may go red** after an export: a `relationships` test that fails is a
  finding about the data, not a bug in the export, but it is still your suite that turns red.
- **Nothing in any phase calls out.** The one check that does, validating the emitted tree
  against the Cassis import format, is a test of this repo, skipped in its own suite without a
  key, and never a step in your run.
- **There is no evaluation harness.** The kit cannot tell you whether the ontology it produced is
  good. The four checkpoints and the reports are how you tell.

## Status

The kit is how we bootstrap context on real projects today, and what we hand teams to run on
their own. It is published standalone and not yet part of the Cassis CLI; when it graduates
there, we will keep a standalone version available. Run it and tell us where it breaks.

## Keeping it current

The ontology you get here is a snapshot of what your warehouse means today, and warehouses move:
a column changes meaning, a metric gains an exception, a definition turns out to be wrong the
first time somebody asks a question it cannot answer. Keeping that context true as the warehouse
moves is what [Cassis](https://getcassis.com) does: the same ontology, enriched from real use,
with every change reviewed by the data team before it becomes truth.

The output here is a Cassis ontology already, so there is nothing to migrate. Cassis records the
commit each version comes from, so the tree goes into a repository first:

```bash
pip install cassis-cli
cd <run>/emit
cassis context fmt          # Cassis's canonical file form, plus its modeling guide, cassis/AGENTS.md
git init && git add cassis && git commit -m "Bootstrapped context"
cassis context upload --project <id> --no-publish   # review it in Cassis first
```

From there, dbt changes reach it from CI. `cassis dbt check` on a pull request lists the tables,
columns, joins and metrics the change puts in doubt, with the context change each one needs.
`cassis dbt push` after `dbt docs generate` on production sets the baseline those checks compare
against, and a merged change that puts the context in doubt becomes an issue in Cassis.

## Expand an existing ontology

```bash
npx skills add GetCassis/ontology-skills --skill ontology-expansion --full-depth
```

Run the expansion skill from your existing ontology repository. Tell the agent which business
domain you want to cover and point it to the available schema/dbt exports and documentation.
If you are unsure about the scope, it helps you define it before enrichment.

For example:

> Use ontology-expansion to model partner engagement in our existing Git-synced ontology.
> Help me define the domain, identify missing context, and propose refactors where the current
> structure gets in the way. Use our schema and dbt exports; do not connect to the warehouse.
> Prepare a reviewable diff and check the existing evaluation cases.

The skill starts from your curated definitions and proposes additions or refactors for review.
It checks how Git changes reach the published ontology, preserves pending work, and distinguishes
SQL judged without execution from results verified against data. It does not run the bootstrap
generator over your existing tree. It is an agent workflow, not an automatic merge utility;
validation uses the tools available in your project. Metadata expansion needs no live warehouse
connection, but your configured model's data-handling constraints still apply.

The expansion skill is newly published; it has not yet been validated on a real customer
expansion. The implementation, measurements and detailed instructions above describe **bootstrap**.

`--full-depth` is what lets `npx skills add` find this skill beside the root-level bootstrap
skill; `npx skills add GetCassis/ontology-skills --list --full-depth` shows both. This repository
was previously named `GetCassis/ontology-bootstrap`; existing GitHub links redirect here.

## Contributing

```bash
python3 -m pip install -r requirements-dev.txt
python3 tests/test_kit.py
```

The tests run on a fresh clone with no data of your own and no environment variables, and skip
rather than fail what they cannot run.

Issues are on. What you get is best-effort: we read them, real failures get fixed, and there is
no SLA. **If a run fails: stop at the phase banner, keep the run directory, and open an issue with
the banner and the phase name.** The banner names the stage and what it was reading, which is most
of the diagnosis.

## License

MIT. See `LICENSE`. `adapters/vendor/inventory.py` is vendored from `dbt-agent-readiness` (MIT),
with its license and provenance beside it.
