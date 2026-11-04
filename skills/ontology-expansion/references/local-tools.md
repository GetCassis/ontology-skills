# Local preparation and checking

Use these tools for substantial additions from source assets, or when their
extraction and comparison help the task. Focused edits with sufficient evidence
can use the project's checks and a saved baseline directly, as described in the
skill. The commands below require a schema export and source input for
preparation; those are not prerequisites for every use of the expansion skill.

The commands read sources and write review artifacts beside the project; they
never author or replace context, connect to a warehouse, call a model, commit,
upload or publish. They need no Cassis account or API key. Installing the toolkit
and dependencies needs network access unless those files are already available;
running these commands does not. The coding agent uses its configured model.

## Reuse or set up the tools once

Handle this setup as the agent; do not make the user choose an interpreter or
manage checkout paths. Reuse a known bootstrap/toolkit checkout and its working
Python environment when they contain `expansion.py` and its sibling modules.
An installed expansion skill directory alone does not contain those scripts.
Record the toolkit revision used; preserve its branch and unrelated work.

If no compatible checkout exists and installation is permitted, choose a new
tools directory outside the maintained ontology and source directories, clone
`https://github.com/GetCassis/ontology-skills` there and use a compatible revision
of this skill and the commands. Reuse a working environment or create a local
virtual environment and install `requirements.txt`, the same three runtime
packages as bootstrap. Do not install the development dependencies or Cassis CLI
just to use these commands. Do not overwrite or update another checkout to
make it compatible.

For a new checkout, after choosing its path as `KIT`:

```bash
git clone https://github.com/GetCassis/ontology-skills "$KIT"
python3 -m venv "$KIT/.venv"
"$KIT/.venv/bin/python" -m pip install -r "$KIT/requirements.txt"
```

Set `KIT_PYTHON` to the chosen environment's interpreter (for that new setup,
`KIT_PYTHON="$KIT/.venv/bin/python"`) and verify it before reading customer inputs:

```bash
"$KIT_PYTHON" "$KIT/expansion.py" prepare --help
"$KIT_PYTHON" "$KIT/expansion.py" check --help
```

Reuse these paths on subsequent runs. If installation is unavailable, use the
project's available evidence and checks, explain the missing tool coverage and
leave unsupported work open. Do not create placeholder evidence to satisfy the
command interface. Do not run bootstrap's `build`, `skeleton.py`,
`apply_synthesis.py` or `emit.py` against maintained context.

The examples below assume the current directory is the ontology project and
`KIT` and `KIT_PYTHON` refer to the verified tools. Paths and business questions
are examples; use the actual inputs the user supplied.

## Prepare once, before edits

```bash
"$KIT_PYTHON" "$KIT/expansion.py" prepare \
  --tree ./cassis --run ../sales-expansion-review \
  --schema ./exports/schema.json --input ../analytics --adapter dbt \
  --docs-dir ./exports/docs --dashboards-dir ./exports/dashboards \
  --question 'What is revenue by product?' \
  --table MAIN.ORDERS --table MAIN.ORDER_ITEMS --table MAIN.PRODUCTS
```

The tree path names the directory containing `tables/`, `metrics/`,
`domains/` and `joins.yml`. The run directory must be separate from the tree
and every supplied source. It may contain business data; keep it out of the
public tools repository and use the project's usual access controls.

Omit unavailable source flags. `--adapter none` supports a schema-only run;
`--input` then names an existing empty directory. The other adapters are `dbt`,
`dbtdocs`, `generic` (with `--adapter-config`) and `auto`. Optional repeatable
source flags are `--docs-dir`, `--context-dir`, `--glossary-dir`,
`--dashboards-dir`, `--questions-dir`, and `--declared` for supported semantic
layers. `--query-history` takes a JSON export. Use `--help` for current options.

`--table` bounds the candidate report, not the source extraction: upstream and
out-of-scope evidence remains available. Without it the report inventories all
schema tables; this does not authorize modeling them all or establish which
questions they answer. The agent maps the user's questions to the relevant
existing and missing objects, and proposes the domain structure for review.

If existing reference questions can be mapped to tables, pass `--cases` with a
JSON list of `{ "id": "...", "question": "...", "tables": ["SCHEMA.TABLE"] }`.
This lists potentially affected cases; it does not execute them or replace the
project's eval suite. Shared-rule changes may affect cases beyond that mapping.

Read these outputs:

- `PREPARE.md`: scoped table coverage and existing cases.
- `inventory.json`: current definitions, source descriptions and defining SQL,
  join/metric candidates, original query SQL, declared definitions, documentation
  index, and differences requiring interpretation. A different WHERE clause can
  reflect a different audience; do not automatically replace an approved filter.
- `METRICS-REVIEW.md`: metric corroboration and filter-review limits.
- `evidence-sources.json`: source revisions/status and file hashes, including
  uncommitted source changes. Source paths in the inventory refer to these inputs.
- `baseline/cassis/` and `expansion.json`: the unchanged starting tree, file
  hashes, checkout commit/status, questions and reference cases.
- `baseline-check.json` and `extraction.log`: structural findings and source-read
  warnings. A source that was not read is missing evidence, never empty coverage.

Preparation does not generate domains, add objects or decide business meaning.
Agree any new or changed structure, then edit the actual project. An unchanged
preparation can be repeated; once the tree or scope changes, it refuses to reset
the baseline. Use `check` for the draft. Use a new run directory for a new increment.

## Check the draft

Use this for a run prepared before editing. It is not a standalone checker for
a direct-edit task without a prepared baseline; use that task's saved starting
files and available project checks instead. Do not prepare after editing to
make the draft appear unchanged.

```bash
"$KIT_PYTHON" "$KIT/expansion.py" check \
  --tree ./cassis --run ../sales-expansion-review
```

Read `REVIEW.md`, `expansion-check.json`, `METRICS-REVIEW.md` and
`judge_input.json`. The comparison lists added/removed objects, changed existing
definitions, domain moves, file changes and checkout-commit drift. Changes to
existing joins and curated domain prose are included. Generated navigation is
excluded from the prose comparison, but relative context links are checked.

Exit status:

- `0`: no structural errors or existing-object changes requiring review found.
- `1`: structural/input errors; inspect the report or error message and repair.
- `2`: existing definitions changed, objects/files removed, domains moved, or the
  checkout commit changed. Review the before/after and consequences. This is a
  review checkpoint, not an instruction to undo a legitimate change or to invent
  an approval. Keep the status visible in the handoff.

These commands do not check the live publication branch or pending UI edits.
They do not run the project's formatter, answering agent, eval suite or reference
queries. Use those separately within the user's authorization, including old
questions touched by new shared rules and joins. A structural success is not an
answer-accuracy result.

## Provenance and shared checks

`--provenance /path/to/provenance.json` is optional on both commands. It is the
sidecar beside the context, not an extra serialized field inside table YAML.
Column entries use the emitted shape:

```json
{"MAIN.ORDERS": {"TOTAL_AMOUNT": {
  "description_source": "repo_text",
  "description_sha256": "sha256-of-the-exact-description-text"
}}}
```

Only supported owner tiers with a matching description hash count toward metric
corroboration. A changed description, missing hash or legacy unbound stamp does
not count. The hash binds a claim to text; it does not prove the source or grant
approval. Keep source locations in the review evidence. Drafted descriptions
remain `drafted` and never corroborate themselves. The new bootstrap emitter
records these hashes automatically.

The underlying `verify.py` accepts working and canonical trees. Identifier,
parse and broken-reference errors fail its command; semantic heuristics remain
advisory findings. `metrics_review.py --tree ... --workspace ... --out ...`
also supports canonical trees and an optional provenance sidecar. Output must
stay outside the context. The judge-input extractor is heuristic, not a complete
inventory of every prose claim; review additional consequential claims directly.
