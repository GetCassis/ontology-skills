# Sample output

Real output of a run, not a mock-up: the four-model fixture project in
`tests/fixtures/dbt-roundtrip` taken through the kit's deterministic chain,
with the enrichment stages stood in for by the test suite's fixtures (enrichment
is agent work, so a committed sample uses the same stand-ins the round-trip
test hands to `dbt parse` and `cassis ontology check`). Regenerate with
`python3 tools/make_sample_output.py`.

A real run's tree looks exactly like this, with more of everything. The full
emitted tree for these four models:

```
domains/README.md
domains/catalog/README.md
domains/commerce/README.md
domains/commerce/customers/README.md
joins.yml
metrics/avg_quantity.yml
metrics/completed_revenue.yml
metrics/orders_count.yml
metrics/revenue_total.yml
tables/MAIN/CUSTOMERS.yml
tables/MAIN/ORDERS.yml
tables/MAIN/ORDER_ITEMS.yml
tables/MAIN/PRODUCTS.yml
```

This directory keeps an excerpt of it — one table, one metric, one domain
README:

- `tables/MAIN/ORDERS.yml`
- `metrics/completed_revenue.yml`
- `domains/commerce/README.md`
