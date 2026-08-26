# LLM config fallback — unknown transformation-repo layouts

When `ingest.py --adapter auto` matches neither dbt nor the Syncer layout, do
NOT parse the corpus with an LLM file-by-file. Run one Sonnet agent with this
prompt to emit a mapping config; deterministic code then applies it corpus-wide.

## Prompt (fill the placeholders)

> You are configuring a deterministic parser for a transformation repository
> whose layout is unknown. Read-only: use Read, Grep, Glob only.
>
> Repo root: {INPUT_DIR}
>
> Inspect the layout (directory structure, 3–5 sample files of each type) and
> emit a JSON mapping config, nothing else:
>
> ```json
> {
>   "table_files": "<glob relative to root matching one-file-per-table, e.g. models/*/*.yml>",
>   "table_name_from": "<'filename'|'field:<yaml/json path>'>",
>   "schema_name_from": "<'parent_dir'|'field:...'|'constant:<NAME>'|'none'>",
>   "sql_sibling": "<'same_stem_.sql'|'field:...'|'none'>",
>   "fields": {
>     "description": "<yaml/json path or 'none'>",
>     "caveats": "<path or 'none'>",
>     "granularity": "<path or 'none'>",
>     "column_descriptions": "<path pattern or 'none'>",
>     "enums": "<path pattern + value format note, or 'none'>",
>     "tests": "<path or 'none'>"
>   },
>   "notes": "<anything the parser author must know: encodings, jinja, multi-table files>"
> }
> ```
>
> Rules: every path you name must be verified against at least 3 real files.
> If a field genuinely has no home in this layout, say 'none' — never guess.
> If the layout has no stable per-table mapping at all, reply instead with
> {"no_stable_mapping": true, "why": "..."} — the caller will fall back to
> per-file LLM normalization and must flag that in the run log.

## Applying the config

A `generic` adapter reading this config is written when first needed (the first
case will be a dbt-docs markdown export).
Every fallback use is recorded in the workspace run log.
