#!/usr/bin/env python3
"""The export tests: a real round trip, not an assertion about one.

Both targets of `emit.py` are tested by handing their output to the tool that
has to accept it, because every previous version of "the output is fine" in
this kit was a self-check that agreed with itself:

  --emit dbt      `dbt parse` and `dbt docs generate` run over the merged
                  project (dbt-core + dbt-duckdb, test-only deps), and the
                  `meta.cassis.*` blocks have to survive into
                  target/manifest.json. Asserting the yml we just wrote proves
                  nothing about whether dbt accepts it.
  --emit cassis    `cassis ontology check` validates the tree, which is the
                  same check the Cassis PR gate runs. It needs CASSIS_API_KEY
                  and SKIPs without one; the structural assertions above it
                  run on any clone.

The input is the fixture dbt project under tests/fixtures/dbt-roundtrip, taken
through the kit's own deterministic chain (ingest -> evidence -> skeleton ->
prefill -> apply_synthesis) so what gets exported is a tree the kit produced,
not one this file typed. The enrichment stages are agents, so their output is
stood in for here: domain assignments, join dispositions, a few drafted
columns and four metrics.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parent.parent
FIXTURE = KIT / "tests" / "fixtures" / "dbt-roundtrip"
SCHEMA_CSV = KIT / "tests" / "fixtures" / "dbt-roundtrip-schema.csv"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import case, skip  # noqa: E402

PROFILES = """roundtrip:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{path}"
      schema: main
"""


def _py(*args, **kw):
    return subprocess.run([sys.executable] + [str(a) for a in args],
                          capture_output=True, text=True, **kw)


def _no_home_env(tmp: Path) -> dict:
    """HOME pointed at an empty directory, so the whole deterministic chain is
    proved to read nothing out of somebody's home.

    The dbt adapter's parser is vendored, and this is the assertion that keeps
    it vendored: were it imported from a path outside the repo, a clone on any
    other machine would have no dbt adapter at all and say nothing about it.
    PYTHONPATH carries the interpreter's own paths because a redirected HOME
    also hides per-user site-packages, which is an artifact of the test, not the
    thing under test.
    """
    empty = tmp / "no-home"
    empty.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(empty),
                PYTHONPATH=os.pathsep.join(p for p in sys.path if p))


def _dbt(project, *args):
    """dbt-core is importable but its console script is not always on PATH.

    cwd is the project, never the kit: the kit ships a `profile.py`, and with
    the kit directory on sys.path that shadows the stdlib module dbt imports.
    """
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="0")
    return subprocess.run(
        [sys.executable, "-m", "dbt.cli.main", *args,
         "--project-dir", str(project), "--profiles-dir", str(project)],
        capture_output=True, text=True, env=env, cwd=str(project))


def _tree_bytes(root: Path) -> dict:
    """Every file the export could touch, so "no diff on a second run" is a
    byte claim and not a spot check. dbt's own output is not ours."""
    out = {}
    for p in sorted(root.rglob("*")):
        parts = set(p.relative_to(root).parts)
        if not p.is_file() or parts & {"target", "logs", "dbt_packages"}:
            continue
        out[str(p.relative_to(root))] = p.read_bytes()
    return out


def build_run(tmp: Path):
    """The fixture project through the kit's deterministic chain, with the
    agent stages stood in for. -> (project, workspace, run)"""
    project, ws, run = tmp / "project", tmp / "ws", tmp / "run"
    shutil.copytree(FIXTURE, project)
    (project / "profiles.yml").write_text(
        PROFILES.format(path=str(tmp / "roundtrip.duckdb")))

    steps = [
        ("ingest", ["ingest.py", "--input", project, "--schema", SCHEMA_CSV,
                    "--workspace", ws, "--adapter", "dbt"]),
        ("evidence", ["evidence.py", "--workspace", ws]),
        ("skeleton", ["skeleton.py", "--workspace", ws, "--run", run,
                      "--batches", "2"]),
        ("prefill", ["dispatch_prep.py", "prefill", "--run", run,
                     "--workspace", ws]),
    ]
    env = _no_home_env(tmp)
    for name, argv in steps:
        r = _py(KIT / argv[0], *argv[1:], env=env)
        if r.returncode:
            case(f"kit chain: {name} runs on the fixture dbt project", False,
                 (r.stderr or r.stdout)[-300:])
            return None
    case("kit chain: ingest -> evidence -> skeleton -> prefill on a dbt "
         "project, with HOME pointed at an empty directory", True)

    # --- stand-in for stage B: the domain tree and the join dispositions ---
    dec = run / "decisions"
    dec.mkdir(parents=True, exist_ok=True)
    (dec / "domains.yml").write_text(yaml.safe_dump({
        "MAIN.ORDERS": "commerce",
        "MAIN.ORDER_ITEMS": "commerce",
        "MAIN.CUSTOMERS": "commerce/customers",
        "MAIN.PRODUCTS": "catalog"}))
    (dec / "joins.yml").write_text(yaml.safe_dump([
        {"left": "MAIN.ORDER_ITEMS", "right": "MAIN.ORDERS",
         "on": ["order_id = order_id"], "verdict": "promote",
         "relationship": "many_to_one",
         "description": "Each order item belongs to exactly one order."},
        {"left": "MAIN.ORDERS", "right": "MAIN.CUSTOMERS",
         "on": ["customer_id = customer_id"], "verdict": "promote",
         "relationship": "many_to_one",
         "description": "Each order is placed by exactly one customer."}]))
    for path, title, prose in (
            ("README.md", "Store",
             "The store's warehouse: customers place orders, an order carries "
             "order items, and products describe what is sold.\n\nAmounts are "
             "in EUR, tax included."),
            ("commerce/README.md", "Commerce",
             "Orders and the lines inside them. An order is either completed "
             "or cancelled, and a cancelled order keeps its amount."),
            ("commerce/customers/README.md", "Customers",
             "One row per person who signed up. Country is the signup "
             "country, not a billing country."),
            ("catalog/README.md", "Catalog",
             "What the store sells. One row per product.")):
        p = run / "cassis" / "domains" / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {title}\n\n{prose}\n")

    r = _py(KIT / "apply_synthesis.py", "--run", run, "--workspace", ws)
    case("apply_synthesis materializes the domain tree and the joins",
         r.returncode == 0 and (run / "cassis" / "joins.yml").exists(),
         (r.stdout + r.stderr)[-300:])

    # --- stand-in for stage C: drafted columns, and the metrics ---
    drafted = {
        "MAIN/ORDERS.yml": {
            "TOTAL_AMOUNT": {"description": "Order total in EUR, tax "
                                            "included. Cancelled orders keep "
                                            "their amount.",
                             "description_source": "drafted", "unit": "EUR"},
            "CUSTOMER_ID": {"description": "Customer who placed the order.",
                            "description_source": "drafted"},
        },
        "MAIN/CUSTOMERS.yml": {
            "COUNTRY_CODE": {"description": "Two-letter country code from "
                                            "signup.",
                             "description_source": "warehouse_comment",
                             "synonyms": ["country"]},
        },
    }
    for rel, cols in drafted.items():
        p = run / "cassis" / "tables" / rel
        doc = yaml.safe_load(p.read_text())
        for c in doc["columns"]:
            if c["name"] in cols:
                c.update(cols[c["name"]])
        p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))

    metrics = {
        "revenue_total": {"table_schema": "MAIN", "table_name": "ORDERS",
                          "expression": "SUM(TOTAL_AMOUNT)", "unit": "EUR",
                          "synonyms": ["revenue", "sales"],
                          "description": "Sum of order amounts in EUR."},
        # display_name deliberately sorts AFTER the others while the metric
        # name sorts before revenue_total: the nav region orders by name, and a
        # fixture whose two orders coincide cannot catch getting that wrong
        "orders_count": {"table_schema": "MAIN", "table_name": "ORDERS",
                         "expression": "COUNT(ORDER_ID)",
                         "display_name": "Volume of orders",
                         "synonyms": ["orders"]},
        "completed_revenue": {"table_schema": "MAIN", "table_name": "ORDERS",
                              "expression": "SUM(TOTAL_AMOUNT)",
                              "filters": "STATUS = 'completed'",
                              "synonyms": ["net revenue"],
                              "caveats": "Cancelled orders keep their amount, "
                                         "so the filter is mandatory."},
        "avg_quantity": {"table_schema": "MAIN", "table_name": "ORDER_ITEMS",
                         "expression": "AVG(QUANTITY)"},
    }
    for name, spec in metrics.items():
        p = run / "cassis" / "metrics" / f"{name}.yml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump({"name": name, **spec}, sort_keys=False,
                                    allow_unicode=True))
    return project, ws, run


# ------------------------------------------------------------ --emit cassis

def cassis_emit_tests(tmp: Path, ws: Path, run: Path):
    r = _py(KIT / "emit.py", "--emit", "cassis", "--run", run,
            "--workspace", ws)
    out = run / "emit" / "cassis"
    case("emit cassis writes a tree", r.returncode == 0
         and (out / "joins.yml").exists(), (r.stdout + r.stderr)[-400:])
    if r.returncode:
        return

    orders = yaml.safe_load((out / "tables" / "MAIN" / "ORDERS.yml").read_text())
    case("a table file is keyed schema_name + table_name, not SCHEMA.TABLE",
         orders.get("schema_name") == "MAIN"
         and orders.get("table_name") == "ORDERS" and "name" not in orders,
         str(list(orders))[:120])
    total = next(c for c in orders["columns"] if c["name"] == "TOTAL_AMOUNT")
    case("column enrichment survives; the provenance keys Cassis rejects do not",
         total.get("unit") == "EUR" and total.get("data_type") == "DECIMAL"
         and not any(k.startswith("description_source") for k in total),
         str(total)[:160])
    prov = json.loads((run / "emit" / "provenance.json").read_text())
    case("stripped provenance is moved to provenance.json, not dropped",
         prov["MAIN.ORDERS"]["TOTAL_AMOUNT"]["description_source"] == "drafted"
         and "COUNTRY_CODE" in prov["MAIN.CUSTOMERS"], str(prov)[:160])

    # Without cassis-cli there is no AGENTS.md in the output, so the tree
    # arrives as YAML nobody has been told how to read. The reader guide
    # travels with it, at the checkout root where `fmt` would put AGENTS.md.
    guide = run / "emit" / "CLAUDE.md"
    case("the emitted tree arrives with the reader guide beside it",
         guide.exists()
         and guide.read_text() == (KIT / "templates"
                                   / "output-CLAUDE.md").read_text(),
         str(sorted(p.name for p in (run / "emit").iterdir())))

    met = yaml.safe_load((out / "metrics" / "revenue_total.yml").read_text())
    case("a metric gets the display_name the import requires, and its domain",
         met.get("display_name") == "Revenue total"
         and met.get("domain_path") == "commerce", str(met)[:160])
    cav = yaml.safe_load((out / "metrics" / "completed_revenue.yml").read_text())
    case("a metric caveat folds into the description, the only field every "
         "tool reads",
         "filter is mandatory" in (cav.get("description") or ""),
         str(cav.get("description"))[:160])

    joins = yaml.safe_load((out / "joins.yml").read_text())
    j = next(x for x in joins if x["from_table"] == "ORDER_ITEMS")
    case("a join carries column_pairs and condition_sql, cased and quoted as "
         "physical identifiers",
         j["column_pairs"] == [{"from_column": "ORDER_ID",
                                "to_column": "ORDER_ID"}]
         and j["condition_sql"] ==
         '"MAIN"."ORDER_ITEMS"."ORDER_ID" = "MAIN"."ORDERS"."ORDER_ID"'
         and j["cardinality"] == "many_to_one", str(j)[:200])

    readme = (out / "domains" / "commerce" / "customers" / "README.md").read_text()
    case("a domain README gets the frontmatter and a nav region at its own depth",
         readme.startswith("---\ntype: Domain\n")
         # unquoted where YAML allows it, which is how `cassis ontology fmt`
         # writes it — so a later fmt leaves no diff at all
         and "title: Customers\n" in readme
         and "[MAIN.CUSTOMERS](../../../tables/MAIN/CUSTOMERS.yml)" in readme
         and "cassis:nav:end" in readme, readme[:200])
    root = (out / "domains" / "README.md").read_text()
    case("the root README keeps its prose and gets no nav it does not need",
         "Amounts are in EUR" in root and "cassis:nav" not in root,
         root[:160])
    commerce = (out / "domains" / "commerce" / "README.md").read_text()
    case("the nav lists what is placed in that domain only, tables then metrics",
         "[MAIN.ORDERS](../../tables/MAIN/ORDERS.yml)" in commerce
         and "MAIN.CUSTOMERS" not in commerce
         and "[Revenue total](../../metrics/revenue_total.yml)" in commerce,
         commerce[commerce.find("cassis:nav"):][:300])
    nav_metrics = re.findall(r"^- \[.*?\]\(\.\./\.\./metrics/(.+?)\.yml\)$",
                             commerce, re.M)
    case("the nav orders metrics by metric name, not by the label it shows",
         nav_metrics == sorted(nav_metrics)
         and nav_metrics == ["avg_quantity", "completed_revenue",
                             "orders_count", "revenue_total"],
         str(nav_metrics))

    before = _tree_bytes(out)
    r2 = _py(KIT / "emit.py", "--emit", "cassis", "--run", run,
             "--workspace", ws)
    case("a second emit produces no diff", r2.returncode == 0
         and _tree_bytes(out) == before)

    # the failsafe: an unapplied domain tree must stop the emit, not ship
    # a tree whose tables all say UNASSIGNED
    doc = yaml.safe_load((run / "cassis" / "tables" / "MAIN" /
                          "PRODUCTS.yml").read_text())
    doc["domain_path"] = "UNASSIGNED"
    (run / "cassis" / "tables" / "MAIN" / "PRODUCTS.yml").write_text(
        yaml.safe_dump(doc, sort_keys=False))
    r3 = _py(KIT / "emit.py", "--emit", "cassis", "--run", run,
             "--workspace", ws)
    case("emit refuses a tree with an UNASSIGNED domain",
         r3.returncode != 0 and "UNASSIGNED" in (r3.stderr + r3.stdout),
         (r3.stderr + r3.stdout)[-160:])
    doc["domain_path"] = "catalog"
    (run / "cassis" / "tables" / "MAIN" / "PRODUCTS.yml").write_text(
        yaml.safe_dump(doc, sort_keys=False))
    _py(KIT / "emit.py", "--emit", "cassis", "--run", run, "--workspace", ws)


def cassis_check_tests(run: Path):
    """The hard postcondition: the tree the kit produces has to pass the same
    check the Cassis PR gate runs, or the documented upgrade path
    (`cassis ontology upload`) is broken and nobody finds out until a stranger
    tries it."""
    name = "the emitted tree passes `cassis ontology check`"
    if not shutil.which("cassis"):
        return skip(name, "cassis-cli not installed (pip install cassis-cli)")
    if not os.environ.get("CASSIS_API_KEY"):
        return skip(name, "no CASSIS_API_KEY in the environment")
    env = dict(os.environ)
    # An unbound checkout is the point: this validates the FORMAT, and binding
    # a project would add source-schema warnings about a fixture warehouse.
    env.pop("CASSIS_PROJECT_ID", None)
    r = subprocess.run(["cassis", "ontology", "check", str(run / "emit")],
                       capture_output=True, text=True, env=env)
    case(name, r.returncode == 0, (r.stdout + r.stderr)[-500:])


# --------------------------------------------------------------- --emit dbt

def dbt_emit_tests(project: Path, ws: Path, run: Path):
    have_dbt = importlib.util.find_spec("dbt") is not None
    before = _tree_bytes(project)
    r = _py(KIT / "emit.py", "--emit", "dbt", "--run", run, "--workspace", ws,
            "--dbt-project", project)
    case("emit dbt runs", r.returncode == 0, (r.stdout + r.stderr)[-500:])
    if r.returncode:
        return
    report = json.loads((run / "emit" / "dbt-export-report.json").read_text())

    text = (project / "models" / "schema.yml").read_text()
    case("the project's own comment survives the merge",
         "# Hand-written by the project's own team." in text, text[:200])
    doc = yaml.safe_load(text)
    orders = next(m for m in doc["models"] if m["name"] == "orders")
    case("a description the project already had is never overwritten",
         orders["description"] == "One row per order placed in the store."
         and next(c for c in orders["columns"] if c["name"] == "status"
                  )["description"]
         == "Order lifecycle status: completed or cancelled.",
         str(orders["description"])[:120])
    case("their own meta keys are kept, ours land under meta.cassis",
         orders["meta"]["owner"] == "analytics-team"
         and orders["meta"]["cassis"]["domain"] == "commerce"
         and orders["meta"]["cassis"]["grain"] == ["order_id"],
         str(orders["meta"])[:200])
    items_ref = yaml.safe_load(
        (project / "models" / "order_items.yml").read_text())
    oi_join = next(c for c in next(m for m in items_ref["models"]
                                   if m["name"] == "order_items")["columns"]
                   if c["name"] == "order_id")["meta"]["cassis"]["join"][0]
    case("the join's other side contributes its grain, in the project's casing",
         oi_join["to_grain"] == ["order_id"]
         and oi_join["column_pairs"][0]["to_column"] == "order_id",
         str(oi_join)[:200])
    amount = next(c for c in orders["columns"] if c["name"] == "total_amount")
    case("an undocumented column gets the evidence-backed description and its unit",
         amount["description"].startswith("Order total in EUR")
         and amount["meta"]["cassis"]["unit"] == "EUR", str(amount)[:200])
    cust_col = next(c for c in orders["columns"] if c["name"] == "customer_id")
    cust_join = cust_col["meta"]["cassis"]["join"][0]
    case("a join's relationships test joins the column's OWN test list, so dbt "
         "never sees both tests: and data_tests: on one column",
         "data_tests" not in cust_col
         and [t for t in cust_col["tests"] if t == "not_null"]
         and any(isinstance(t, dict) and "relationships" in t
                 for t in cust_col["tests"])
         and cust_join["cardinality"] == "many_to_one"
         and cust_join["column_pairs"] == [{"from_column": "customer_id",
                                            "to_column": "customer_id"}]
         and "customers" in cust_join["to"]
         # customers declares no uniqueness test, so there is no grain to
         # claim — and an absent grain must read as absent, not as a guess
         and "to_grain" not in cust_join, str(cust_col)[:250])
    case("a metric dbt cannot hold stays on the table under meta.cassis.metrics",
         any(m["name"] == "completed_revenue"
             for m in orders["meta"]["cassis"]["metrics"])
         and any(d["metric"] == "completed_revenue"
                 and "filter" in d["reason"]
                 for d in report["metrics_not_exported"]),
         str(report["metrics_not_exported"])[:300])

    items = yaml.safe_load((project / "models" / "order_items.yml").read_text())
    oi = next(m for m in items["models"] if m["name"] == "order_items")
    oi_order_id = [c for c in oi["columns"] if c["name"] == "order_id"]
    rels = [t for c in oi_order_id
            for k in ("data_tests", "tests")
            for t in (c.get(k) or [])
            if isinstance(t, dict) and "relationships" in t]
    case("a relationships test the project already declares is not duplicated, "
         "even when the column is declared twice",
         len(oi_order_id) == 2 and len(rels) == 1
         and "order_items.ORDER_ID" in report["columns_declared_twice"],
         f"declarations={len(oi_order_id)} rels={rels} "
         f"reported={report.get('columns_declared_twice')}")

    created = project / "models" / "schema.yml"
    case("a model with no yml entry anywhere gets one, without touching the "
         "others",
         "products" in report["files_created"][0] if report["files_created"]
         else any(m["name"] == "products" for m in
                  yaml.safe_load(created.read_text())["models"]),
         str(report["files_created"]))

    sem = yaml.safe_load(
        (project / "models" / "cassis_semantic_models.yml").read_text())
    case("metrics dbt can hold become a semantic model plus metrics",
         sem["semantic_models"][0]["name"] == "orders"
         and sem["semantic_models"][0]["defaults"]["agg_time_dimension"]
         == "placed_at"
         and sorted(m["name"] for m in sem["metrics"])
         == ["orders_count", "revenue_total"], str(sem)[:300])

    after_first = _tree_bytes(project)
    case("the export changed the project (guard against a no-op passing)",
         after_first != before)
    r2 = _py(KIT / "emit.py", "--emit", "dbt", "--run", run, "--workspace", ws,
             "--dbt-project", project)
    case("a second export produces no diff",
         r2.returncode == 0 and _tree_bytes(project) == after_first,
         (r2.stdout + r2.stderr)[-200:])

    if not have_dbt:
        return skip("dbt parse and docs generate accept the merged project",
                    "dbt-core not installed (pip install dbt-core dbt-duckdb)")
    p = _dbt(project, "parse")
    case("dbt parse accepts the merged project", p.returncode == 0,
         (p.stdout + p.stderr)[-700:])
    if p.returncode:
        return
    run_r = _dbt(project, "run")
    case("dbt run still builds the models", run_r.returncode == 0,
         (run_r.stdout + run_r.stderr)[-500:])
    d = _dbt(project, "docs", "generate")
    case("dbt docs generate accepts the merged project", d.returncode == 0,
         (d.stdout + d.stderr)[-700:])
    man_p = project / "target" / "manifest.json"
    if not man_p.exists():
        return case("meta.cassis.* survives into target/manifest.json", False,
                    "no manifest written")
    man = json.loads(man_p.read_text())
    node = next(n for n in man["nodes"].values()
                if n.get("name") == "orders" and n["resource_type"] == "model")
    col_meta = (node["columns"].get("total_amount") or {}).get("meta") or {}
    case("meta.cassis.* survives into target/manifest.json",
         (node.get("meta") or {}).get("cassis", {}).get("domain") == "commerce"
         and col_meta.get("cassis", {}).get("unit") == "EUR",
         str(node.get("meta"))[:200])
    case("the exported metric is in the manifest's semantic manifest",
         any(m.get("name") == "revenue_total" for m in
             (man.get("metrics") or {}).values())
         and any(s.get("name") == "orders" for s in
                 (man.get("semantic_models") or {}).values()),
         str(sorted((man.get("metrics") or {}).keys()))[:200])


def no_time_spine_tests(tmp: Path, ws: Path, run: Path):
    """The guard that matters most: exporting into a project with no MetricFlow
    time spine must not write a semantic model, because dbt then refuses to
    parse the project at all — and it is their project."""
    project = tmp / "project-no-spine"
    shutil.copytree(FIXTURE, project)
    (project / "models" / "time_spine.yml").unlink()
    (project / "models" / "metricflow_time_spine.sql").unlink()
    r = _py(KIT / "emit.py", "--emit", "dbt", "--run", run, "--workspace", ws,
            "--dbt-project", project)
    report = json.loads((run / "emit" / "dbt-export-report.json").read_text())
    reasons = {d["metric"]: d["reason"] for d in report["metrics_not_exported"]}
    case("no time spine: no semantic model is written, and every metric says "
         "why",
         r.returncode == 0
         and not (project / "models" / "cassis_semantic_models.yml").exists()
         and report["time_spine_found"] is False
         and len(reasons) == 4
         and all("time spine" in v for v in reasons.values()),
         str(reasons)[:200])
    doc = yaml.safe_load((project / "models" / "schema.yml").read_text())
    orders = next(m for m in doc["models"] if m["name"] == "orders")
    case("no time spine: the metrics still ship, under meta.cassis.metrics",
         sorted(m["name"] for m in orders["meta"]["cassis"]["metrics"])
         == ["completed_revenue", "orders_count", "revenue_total"],
         str(orders["meta"]["cassis"].get("metrics"))[:200])
    if importlib.util.find_spec("dbt") is None:
        return skip("no time spine: dbt parse still accepts their project",
                    "dbt-core not installed")
    (project / "profiles.yml").write_text(
        PROFILES.format(path=str(tmp / "nospine.duckdb")))
    p = _dbt(project, "parse")
    case("no time spine: dbt parse still accepts their project",
         p.returncode == 0, (p.stdout + p.stderr)[-400:])


def export_tests():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        built = build_run(tmp)
        if not built:
            return
        project, ws, run = built
        cassis_emit_tests(tmp, ws, run)
        cassis_check_tests(run)
        dbt_emit_tests(project, ws, run)
        no_time_spine_tests(tmp, ws, run)


if __name__ == "__main__":
    import harness
    export_tests()
    print()
    if harness.failures:
        print(f"{len(harness.failures)} FAILURES: {harness.failures}")
        sys.exit(1)
    print("all export tests passed")
