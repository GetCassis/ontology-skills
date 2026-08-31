---
name: ontology-bootstrap
description: |
  Assemble a reviewable first version of an analytics agent's context from what a data stack
  already has: a warehouse schema export, a dbt project, docs, dashboards, query logs. Produces
  a Cassis ontology tree (domain READMEs, table and metric YAML, joins) with the evidence behind
  every claim attached and unresolved meaning filed as questions. Scripts do everything the
  inputs determine; the user decides at four checkpoints. Runs fully offline: no account, no
  key, no network. Optional dbt write-back merges descriptions into the project's own schema.yml
  files without overwriting anything already there. Use when asked to "bootstrap an ontology",
  "build context for an analytics agent", "document the warehouse for AI", "turn our dbt project
  into an ontology", or "make our data agent-ready".
---

# Ontology bootstrap

This repository is both the skill and the tool: Python phases that do everything the inputs
determine, plus an operating contract for the judgment stages.

## Before anything else

The run needs a full, writable checkout of this repository as its working directory, because
every phase reads and writes inside it. If you are reading this file from an installed skill or
plugin directory, do not run there. Clone the repository into a working directory and drive the
run from the clone:

```bash
git clone https://github.com/GetCassis/ontology-bootstrap
cd ontology-bootstrap
python3 -m pip install -r requirements.txt
```

## How to run

Read `CLAUDE.md` in the checkout and follow it exactly. It is the operating contract: the driver
sequence (`intake.py` for a new run, then `bootstrap.py prep`, `build`, `finish`), the four
checkpoints where you stop and wait for the user, the evidence and enrichment rules, and the
reporting rules. Do not re-derive the sequence from the scripts.

`README.md` says what inputs the kit needs (a schema export and a dbt project or dbt docs export
are the two mandatory ones, everything else is optional) and what a run costs. Relay each phase's
cost banner to the user before the phase spends it.

## What comes out

`<run>/emit/cassis/` is the result: domains as Markdown, tables, metrics and joins as YAML, with
per-column provenance, ready for review like code. `OUTPUT.md` maps the rest of the run
directory, including the open questions and the defect list the run surfaces along the way.
