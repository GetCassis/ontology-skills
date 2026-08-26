# Reading this ontology

This directory is a Cassis ontology: the business meaning of a warehouse,
written as files a person can review and an agent can read before it writes
SQL. It was produced from the material the warehouse already had — the dbt
project or schema export, the dashboards, the documentation — and every
judgment call in it was reviewed by whoever ran the tool.

`cassis/` is the ontology. Nothing else in this directory is part of it.

| Path | What it holds |
|---|---|
| `cassis/domains/README.md` | The root domain: what this warehouse is about, and the rules that hold everywhere (currency, tax, timezone, exclusions) |
| `cassis/domains/<path>/README.md` | One domain. Its prose is the business meaning; the generated block at the bottom links the tables and metrics that live in it |
| `cassis/tables/<schema>/<table>.yml` | One modeled table: its grain, its domain, and every column with a description, a unit and any synonyms |
| `cassis/metrics/<name>.yml` | One metric: the expression, the table it aggregates, its mandatory filter if it has one, and the caveats folded into its description |
| `cassis/joins.yml` | Every join anyone should use, with the column pairs, the cardinality and the ready-made condition |
| `provenance.json` | Per column, where its description came from — the warehouse, the repository, a glossary, or drafted from evidence during the run |

## How to read it before answering a question

1. **Start at `cassis/domains/README.md`.** The rules stated there apply to
   everything below it. A global exclusion or a currency convention stated
   once at the root is the most common reason an otherwise correct query
   returns a wrong number.
2. **Find the domain that owns the concept**, and read its README. The nav
   block at the bottom is the shortest route to the tables and metrics that
   belong to it.
3. **Read the table file before you name a column.** The grain says what one
   row is. A column's description carries the unit and the meaning; its
   synonyms are what people call it out loud.
4. **If a metric exists, use it rather than rebuilding it.** Its expression
   and its filter are the definition someone signed off on. A metric with a
   filter is wrong without that filter.
5. **Use `joins.yml` for every join.** The cardinality tells you whether a
   join fans out and needs a pre-aggregate.

## What is deliberately not here

The modeling doctrine — how to decide what belongs in an ontology, what makes
a description worth keeping, when a metric earns its place — is not in this
file. It ships as `AGENTS.md`, written into this directory by
`cassis ontology fmt` (`pip install cassis-cli`). Two documents describing the
same format drift apart, so this one stays descriptive and defers to that one.

## Trust

Not every description carries the same weight. `provenance.json` separates
what the warehouse and the repository already said from what was drafted
during the run, and the run directory holds `QUESTIONS.md` — the unknowns that
were assumed rather than answered, and the defects the run found in the
pipeline it read. Read both before treating a number as governed.
