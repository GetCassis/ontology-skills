# Mapping config for a layout that is not dbt

When `ingest.py` matches neither a dbt project nor a dbt docs export, the
transformation repo still holds what the run needs: each table's defining SQL,
and often its description, grain and column docs. `adapters/generic.py` reads
any layout that keeps one file per table, given a mapping config that says
where those sit. Write the config by hand, or run one Sonnet agent with the
prompt below. Do not parse the corpus with an LLM file by file: the config is
written once and applied by code to every file the same way.

Then rerun ingest with `--adapter generic --adapter-config <file>` (or record
`adapter: generic` and `adapter_config: <file>` in the intake config).

## Prompt (fill the placeholder)

> You are configuring a deterministic parser for a transformation repository
> whose layout is not dbt. Read-only: use Read, Grep, Glob only.
>
> Repo root: {INPUT_DIR}
>
> Inspect the layout (directory structure, 3 to 5 sample files of each type)
> and emit one JSON object, nothing else:
>
> ```json
> {
>   "table_files": "<glob relative to the root, one file per table, e.g. models/**/*.sql>",
>   "table_name_from": "<filename | field:<a.b> | regex:<pattern, group 1 is the name>>",
>   "schema_name_from": "<parent_dir | field:<a.b> | regex:<pattern> | constant:<NAME> | none>",
>   "sql_sibling": "<self | file:<template with {stem} {name} {schema}> | field:<a.b> | none>",
>   "sql_start": "<optional regex: the SQL starts after its first match>",
>   "fields": {
>     "description": "<field:<a.b> | regex:<pattern> | none>",
>     "caveats": "<field:<a.b> | regex:<pattern> | none>",
>     "granularity": "<field:<a.b> | regex:<pattern> | none>",
>     "columns": {"list": "field:<a.b>", "name": "<key>", "description": "<key>",
>                 "enum_values": "<key>"}
>       or {"within": "regex:<pattern>", "pairs": "regex:<pattern>"}
>   }
> }
> ```
>
> How the parser reads it:
> - `field:` walks the file's YAML or JSON by dotted keys. `regex:` takes
>   group 1 of the first match in the file's raw text (multiline, dot matches
>   newline). `filename` is the file name without extension; `parent_dir` is
>   the directory holding it.
> - A table name containing a dot, with `schema_name_from: none`, splits into
>   schema and table.
> - `sql_sibling: self` means the table file is the SQL. `file:` names another
>   file, relative to the table file's directory, or to the root when it
>   starts with `/`. `field:` means the YAML or JSON carries the SQL inline.
> - `sql_start` strips a header the SQL sits under: a SQLMesh `MODEL (...);`
>   block, for instance.
> - `columns.list` is a list of objects or an object keyed by column name.
>   For column docs written in the file's text instead, `columns.pairs` is a
>   regex whose group 1 is the column and group 2 its text, applied to every
>   match inside group 1 of `columns.within` (or the whole file). Leave
>   `columns` out when the layout documents no columns.
> - A `regex:` spec returns the first group that matched, so one pattern can
>   hold alternatives: `description '(...)'|\A/\*(...)\*/`.
> - `granularity` resolves to a list or a comma-separated string of columns.
>
> Rules: every spec you write must resolve on at least 3 real files; check it
> against them before answering. If a field has no home in this layout, write
> `none`. Never guess. If the layout has no stable one-file-per-table mapping,
> reply instead with `{"no_stable_mapping": true, "why": "..."}`: ingest then
> says to run with `--adapter none`, building on the schema, docs, dashboards
> and query history without defining SQL.

## Examples

SQLMesh, where each model is one `.sql` file under a `MODEL (...)` header that
carries its name, grain and description (or a leading `/* */` comment), and
sometimes a `column_descriptions (...)` block. Checked against the public
`examples/sushi` project: 11 of its 12 SQL models map; the twelfth names
itself through a macro (`name @name`) and is reported as unnamed. Python
models are outside this mapping; their tables come from the schema export.

```json
{
  "table_files": "models/**/*.sql",
  "table_name_from": "regex:MODEL\\s*\\(\\s*name\\s+([\\w.]+)",
  "schema_name_from": "none",
  "sql_sibling": "self",
  "sql_start": "MODEL\\s*\\(.*?\\)\\s*;",
  "fields": {
    "description": "regex:\\bdescription\\s+(?:'([^']*)'|\"([^\"]*)\")|\\A\\s*/\\*\\s*(.*?)\\s*\\*/",
    "granularity": "regex:\\bgrains?\\s+(?:[\\[(]([^\\])]*)[\\])]|(\\w+))",
    "columns": {"within": "regex:column_descriptions\\s*\\((.*?)\\n\\s*\\)",
                "pairs": "regex:(\\w+)\\s*=\\s*'([^']*)'"}
  }
}
```

Every ingest run prints how many files the mapping read, how many tables it
named, how many carry SQL, and which files it could not name.
