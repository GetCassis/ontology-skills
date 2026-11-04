# Synthetic expansion acceptance case

This is test data written for this repository. The starting context is a
hand-curated two-table fixture; it is not a customer artifact or a measured
agent-generated ontology. The sources extend the existing dbt round-trip
fixture with a product key and multiple items on one order.

`request.txt` is the ordinary expansion request. `baseline/` contains reviewed
order/customer context and a completed-revenue metric. `sources/` contains dbt,
schema, docs and a deliberately different booked-orders dashboard.
`cases.json` lists existing questions; it does not execute queries.

Run `python3 tests/test_expansion.py` from the repository root, or run the full
`python3 tests/test_kit.py` suite (which includes these cases). With the test
dependencies installed, DuckDB executes the synthetic data locally. No model,
account, customer warehouse or network access is required.

The accepted total is EUR 120.50; the two completed product contributions are
EUR 50.50 and EUR 70.00. A cancelled EUR 80 order is excluded. Summing order
totals after joining the two completed line items wrongly produces EUR 241;
dropping the cancellation filter wrongly produces EUR 200.50. The checks also
cover source conflicts, immutable baselines, removed evidence, domain moves,
references, provenance freshness and adoption of actual bootstrap-emitted files.

The scripted draft in `test_expansion.py` is an authoring stand-in for testing
the tools. It does not prove an agent follows the skill. For a separate authoring
trial, give an agent only the request, baseline, raw sources, skill and prepared
evidence; keep this expected-results page and the tests out of its inputs.
Evaluate its proposed diff and run the local reference checks separately.
