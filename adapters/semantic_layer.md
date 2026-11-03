# A semantic layer as evidence

A semantic layer or BI model already declares metrics, joins and column docs that a person
wrote. `ingest.py --declared <path>` (repeatable, or `--declared` at intake) reads them, holds
every one against the schema export, and hands the survivors to the run as declared evidence.
Nothing from the layer is trusted on the way in.

## What it reads

| `--declared` points at | Read by | Checked on |
|---|---|---|
| A Power BI model saved as TMDL: the `definition/` folder of a PBIP project's `<name>.SemanticModel` | `adapters/semantic_layer.py`, `from_tmdl` | The Snowflake TPC-H model in `databricks-solutions/powerbi-on-databricks-migration-accelerator` (`samples-pbip/Snowflake.SemanticModel`); the SQL warehouse model in `RuiRomano/fabric-cli-powerbi-cicd-sample` (`SM_SalesSense.SemanticModel`); and `pbi-tools/sales-sample`, whose tables read CSV files |
| A dbt project using MetricFlow, in either spec: top-level `semantic_models:` and `metrics:` (classic), or `semantic_model:`, entities, dimensions and metrics on the dbt model itself (newer) | `from_metricflow` | `dbt-labs/jaffle-sl-template` (classic) and `dbt-labs/jaffle-shop` (newer) |
| A Cube YAML data model (`cubes:`) | `from_cube` | The analytics model in `tam159/next-role` (`analytics/cube/model`) |
| A LookML project (`.lkml` views and explores, refinements included) | `from_lookml` | Three projects over Google's public thelook_ecommerce data: `bytecodeio/thelook_ecommerce`, `onesecondbefore/lookml-framework`, `looker-open-source/bqml_semantic_search_block` |
| A declared-definitions JSON file, written by hand | `load` | the kit's own tests |

On the TPC-H model:
- 26 measures. One translates (`SUM(orders[o_totalprice])` becomes `SUM(O_TOTALPRICE)` on
  `TPCH_SF1.ORDERS`). The other 25 filter through a related table, so they go to review.
- All 5 relationships become joins, held against the TPC-H schema.

On the SQL warehouse model (`Source{[Schema="raw",Item="sales"]}`):
- 7 measures translate and 3 relationships become joins, held against the `raw` tables
  its own notebook loads.
- Its Power Query renames `UnitCost` to `ShippingCost` and `NetPrice` to `UnitCost` in one
  step, over a `NetPrice` an upstream query computes. So the model's "Total Unit Cost" is
  rejected as computed in the model rather than mapped to a warehouse column, along with
  every measure over a column the M adds.

On the sales sample:
- Every table reads a CSV file over the web, so each of its 13 measures and 5 relationships
  over those tables is rejected, with that reason.
- Its 6 time-intelligence measures go to review.

On the MetricFlow template (classic spec):
- 10 metrics and 4 joins hold.
- 7 metrics go to review: derived, cumulative, a percentile, and a filter on a related model.

On jaffle-shop (newer spec):
- 18 metrics, 4 joins and 16 column docs hold, against a schema built from its own model SQL.
- 5 go to review: derived, cumulative, a median.

On the three LookML projects, held against thelook_ecommerce's columns:
- 21, 7 and 15 measures, with 8, 8 and 1 joins. A refined view (`view: +orders`) adds its
  measure to the view it refines.
- The Looker block's 5 filtered measures go to review, because their yes/no dimensions read
  another view. Its derived table and the 3 joins through it are rejected.

On the Cube model:
- 42 measures, 8 joins and 48 column docs hold against the project's dbt marts.
- 5 ratios go to review, because their numerator carries a filter.
- 2 percentiles are rejected: they use ClickHouse's `quantile(0.5)(x)`, which does not parse as
  standard SQL.

A Power BI `.pbix` file is a compressed binary, so it is not read. Save the model as a PBIP
project, which writes TMDL, and point `--declared` at its `definition/` folder.

## How a Power BI model maps to the warehouse

A Power BI table is whatever its Power Query (M) source returns, so it maps to a warehouse
table only through that source.
- **Navigation steps.** A source that navigates to a table maps to it, in either form the
  connectors write: `{[Name="TPCH_SF1",Kind="Schema"]}` then `{[Name="ORDERS",Kind="Table"]}`
  (Snowflake), or `{[Schema="raw",Item="sales"]}` (SQL Server, Fabric, PostgreSQL). A source
  that starts from a shared query is followed into that query.
- **Renamed and computed columns.** Columns renamed in the M are traced back to their
  warehouse names, upstream query first. A column the M computes (`Table.AddColumn`) is not a
  warehouse column, so a measure over it is rejected.
- **Other sources.** A table whose source is a file, a web address, a native SQL query or a
  calculated table does not map. Every measure and relationship over it is rejected with that
  reason.
- **Row filters.** A row filter in the M (`Table.SelectRows`) is recorded as a rule on that
  table's population. The enrichment packet for the table states it, and no metric carries it.

## MetricFlow and Cube

Both write SQL already, so their metrics ship as `translated: false`.
- **MetricFlow:** a simple metric is its aggregation over its expression, and a filter on a
  dimension of the same model becomes its filter. A ratio of two unfiltered metrics on one
  model becomes `a / NULLIF(b, 0)`. A foreign entity joins the model whose primary entity it
  names.
- **Cube:** a measure's type and SQL give its expression. `{CUBE}.` prefixes are dropped, its
  `filters` become the metric's filter, and a `number` measure resolves its `{measure}`
  references when none of them carries a filter. A join condition between two table cubes
  becomes a join with its relationship.
- **LookML:** a view maps to its `sql_table_name`, or to the view's own name when it has
  none. `${TABLE}.` prefixes and `${dimension}` references resolve to SQL. A measure's
  `filters` become its filter: `"yes"`/`"no"` on a yes/no dimension, a value, `-value`, a
  list, or a numeric comparison. A `number` measure resolves its `${measure}` references when
  none of them carries a filter. An explore's `sql_on` between two fields of two table views
  becomes a join with its relationship.
- **What goes to review:** derived, cumulative and conversion metrics, medians and
  percentiles, rolling windows, ratios over filtered parts, and filters on another model, cube
  or view. A cube built on a SQL query, and a LookML derived table, are rejected.

## DAX: what becomes a metric and what does not

The kit translates DAX mechanically when the result is one aggregate over one table:
- `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `DISTINCTCOUNT` over a column;
- `COUNTROWS` of a table;
- `SUMX`, `AVERAGEX`, `MINX`, `MAXX` over arithmetic on one table's columns;
- a measure reference;
- `CALCULATE` with filters on the same table;
- `DIVIDE`, and arithmetic between measures on the same table.

The SQL is the kit's, not the owner's. A translated metric carries `translated: true` and its
DAX, never counts as the owner's text, and in `METRICS-REVIEW.md` it cannot reach `ok` on the
translation alone.

These go to review with their DAX, and none of them becomes a metric:
- time intelligence (`SAMEPERIODLASTYEAR`, `TOTALYTD`, `DATESINPERIOD` and the rest);
- a filter through a related table;
- filter-context functions (`ALL`, `REMOVEFILTERS`, `USERELATIONSHIP`, `FILTER`);
- `VAR`/`RETURN` blocks;
- anything else outside the list above.

`METRICS-REVIEW.md` lists them under "Declared definitions that are not metrics yet".

## The declared-definitions shape

The two readers produce this shape, and a file written by hand uses it too:

```json
{
  "source": "where the definitions came from",
  "metrics": [{"name": "", "expression": "SUM(AMOUNT)", "table": "SCHEMA.TABLE",
               "filters": "STATUS = 'completed'", "description": "",
               "translated": false, "original_expression": "", "language": "sql",
               "cite": "file:line"}],
  "joins": [{"left": "SCHEMA.A", "right": "SCHEMA.B", "on": ["A_ID = ID"],
             "relationship": "many_to_one", "cite": "file:line"}],
  "columns": [{"table": "SCHEMA.TABLE", "column": "STATUS", "description": "",
               "enum_values": ["completed", "cancelled"], "cite": "file:line"}],
  "tables": [{"table": "SCHEMA.TABLE", "description": "", "cite": "file:line"}],
  "review": [{"name": "", "original_expression": "", "language": "", "reason": "",
              "cite": "file:line"}],
  "populations": [{"table": "SCHEMA.TABLE", "rule": "", "cite": "file:line"}]
}
```

`ingest.py` checks:
- every table and column against the schema export;
- every expression and filter, parsed with sqlglot;
- every `enum_values` list, which must be text values.

What holds goes to `workspace/declared.json`. What does not goes under `rejects` in the same
file, each with its reason. Ingest prints the first few. What holds then enters the run:
- **Metrics** become metric candidates, each counting as one source of corroboration.
  `apply_synthesis.py` writes them as metric files once the domain tree is applied, for every
  metric whose table is modelled. It never overwrites a file, skips a metric already defined
  with the same expression and filter, and records the source, citation and any DAX under
  `declared`, which `emit.py` moves to `emit/provenance.json`.
- **Joins** become join candidates with provenance `declared`.
- **Value lists** become `enum_values`.
- **Column and table docs** are prefilled as the owner's own text, `description_source:
  semantic_layer`. They rank below the repo's doc blocks and the warehouse comments, and above
  a glossary.
