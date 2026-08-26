# What the run produces, and how to read it

A finished run leaves one directory. It contains two copies of the ontology,
which is deliberate, and a set of reports.

## The two trees

| Directory | What it is |
|---|---|
| `<run>/cassis/` | The **working tree**. The run's stages assemble it, and the checks read it. It carries provenance on every column — where each description came from — which is how the metrics review can tell the warehouse's own words apart from prose drafted during the run |
| `<run>/emit/cassis/` | The **ontology tree**, and the one to keep. `verify.py` checks it and needs nothing — no account, no network. It also happens to be the format `cassis ontology upload` imports as-is, if you ever want that |

They are not interchangeable, and the working tree is not simply an earlier
draft: the provenance keys that make it useful during the run are fields the
canonical format rejects, so they are moved to `emit/provenance.json` on the
way out.

**Never hand-edit `<run>/emit/`.** It is generated, and it is regenerated on
every `finish` — an edit there is lost. Change the working tree and re-run;
the emit is deterministic, free, and produces no diff when nothing changed.

## Inside the canonical tree

```
emit/
├── CLAUDE.md                       how to read the ontology (copied in for you)
├── provenance.json                 per column: where its description came from
└── cassis/
    ├── domains/README.md           the root domain: what the warehouse is about,
    │                               and the rules that hold everywhere
    ├── domains/<path>/README.md     one domain — prose, then a generated block
    │                               linking the tables and metrics it owns
    ├── tables/<schema>/<table>.yml  one modeled table: grain, domain, and every
    │                               column with description, unit and synonyms
    ├── metrics/<name>.yml           one metric: expression, table, mandatory
    │                               filter, caveats folded into the description
    └── joins.yml                    every promoted join: column pairs,
                                     cardinality, ready-made condition
```

`CLAUDE.md` is the short reading order for whatever agent you point at the
tree: root rules first, then the domain, then the table's grain, then prefer a
defined metric over rebuilding one, and join only through `joins.yml`. It
defers the modeling doctrine to `AGENTS.md`, which `cassis ontology fmt`
writes in if you install `cassis-cli`.

## The reports beside it

| File | What it answers |
|---|---|
| `QUESTIONS.md` | Every unknown, tiered. The blocking ones make a number wrong until answered; the findings are defects in your own pipeline and nothing for you to answer |
| `QUESTIONS-REVIEW.md` | The blocking subset, riskiest first — the fourth checkpoint. Where your own documentation answers one of these, the answer is here as a candidate with the page and line it was read from; it applies to nothing until you write it in as an `answer:` line |
| `docs-packets/` | One packet per blocking question: the passages your documentation corpus offers it, scored and cited, as the agent judging them saw them. `docs_candidates.json` and `docs_answers.yml` are the same thing as data — what was retrieved, and what the agent made of it |
| `METRICS-REVIEW.md` | Every metric with its provenance and one stamp, riskiest first |
| `verify_report.json` | Internal consistency of the tree. Not correctness |
| `judge_input.json` | Every computation claim paired with the SQL that defines it, for an adjudication pass before anyone trusts a number |
| `prefill_report.json` | How much of the ontology is your own existing words versus drafted from evidence during the run |

## If you also exported to dbt

`--emit dbt` writes into the dbt project you pointed the run at, not into the
run directory: descriptions land in the `schema.yml` files that were already
there, joins become `relationships` tests, and everything dbt has no field for
goes under `meta.cassis.*`. The ontology tree above stays the full-fidelity
copy — the dbt export is a projection of it. `emit/dbt-export-report.json`
lists exactly what was written and what was left alone.
