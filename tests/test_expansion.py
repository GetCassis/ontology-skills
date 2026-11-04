#!/usr/bin/env python3
"""Offline acceptance cases for an existing context expanded into product sales.

The scripted valid draft is a test fixture, not an agent-quality evaluation.
DuckDB executes synthetic data only; no network or configured credentials.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
from expansion import snapshot
from metrics_review import review_rows
from verify import load_tree

FIXTURE = KIT / "tests" / "fixtures" / "expansion"


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def example_draft(tree):
    """Known-valid authoring stand-in used to exercise preservation and checks."""
    for name, grain, cols in (
        ("ORDER_ITEMS", "ORDER_ITEM_ID", ["ORDER_ITEM_ID", "ORDER_ID", "PRODUCT_ID", "QUANTITY", "UNIT_PRICE"]),
        ("PRODUCTS", "PRODUCT_ID", ["PRODUCT_ID", "PRODUCT_NAME", "CATEGORY"]),
    ):
        write_yaml(tree / "tables" / "MAIN" / f"{name}.yml", {
            "schema_name": "MAIN", "table_name": name, "domain_path": "products",
            "grain": [grain], "columns": [{"name": c} for c in cols]})
    joins = yaml.safe_load((tree / "joins.yml").read_text())
    for target, key in (("ORDERS", "ORDER_ID"), ("PRODUCTS", "PRODUCT_ID")):
        joins.append({"from_schema": "MAIN", "from_table": "ORDER_ITEMS",
                      "to_schema": "MAIN", "to_table": target,
                      "column_pairs": [{"from_column": key, "to_column": key}],
                      "cardinality": "many_to_one",
                      "condition_sql": f'MAIN.ORDER_ITEMS.{key} = MAIN.{target}.{key}'})
    write_yaml(tree / "joins.yml", joins)
    p = tree / "domains" / "products" / "README.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('''---
type: Domain
title: Product sales
description: Revenue from product line items on completed orders.
---

# Product sales

Use quantity times unit_price for line revenue, joined to the order for its
completed status. Group by product_id, with product_name as a label. An order
can have several lines: summing its total after this join multiplies revenue.
Line amounts reconcile to order totals in this sample; source docs establish
that it has no order-level fees, discounts or shipping amounts.

```sql
SELECT p.PRODUCT_ID, p.PRODUCT_NAME, SUM(i.QUANTITY * i.UNIT_PRICE) AS REVENUE
FROM MAIN.ORDER_ITEMS i
JOIN MAIN.ORDERS o ON i.ORDER_ID = o.ORDER_ID
JOIN MAIN.PRODUCTS p ON i.PRODUCT_ID = p.PRODUCT_ID
WHERE o.STATUS = 'completed'
GROUP BY p.PRODUCT_ID, p.PRODUCT_NAME
ORDER BY p.PRODUCT_ID
```
''')


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tree = self.root / "context"
        self.sources = self.root / "sources"
        self.run = self.root / "run"
        shutil.copytree(FIXTURE / "baseline", self.tree)
        shutil.copytree(FIXTURE / "sources", self.sources)
        self.before = snapshot(self.tree)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"CASSIS_API_KEY", "CASSIS_PROJECT_ID"}}

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args):
        return subprocess.run([sys.executable, *map(str, args)], cwd=KIT,
                              env=self.env, capture_output=True, text=True)

    def prep(self, *extra):
        return self.command("expansion.py", "prepare", "--tree", self.tree,
                            "--run", self.run, "--schema", self.sources / "schema.csv",
                            "--input", self.sources / "dbt", "--adapter", "dbt",
                            "--docs-dir", self.sources / "docs",
                            "--dashboards-dir", self.sources / "dashboards",
                            "--question", "Revenue by product", "--cases", FIXTURE / "cases.json",
                            "--table", "MAIN.ORDERS", "--table", "MAIN.ORDER_ITEMS",
                            "--table", "MAIN.PRODUCTS", *extra)

    def check(self, *extra):
        return self.command("expansion.py", "check", "--tree", self.tree,
                            "--run", self.run, *extra)

    def prepared(self):
        r = self.prep()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_preparation_extracts_evidence_and_preserves_all_existing_bytes(self):
        self.prepared()
        self.assertEqual(self.before, snapshot(self.tree))
        self.assertEqual(self.before, snapshot(self.run / "baseline" / "cassis"))
        inv = json.loads((self.run / "inventory.json").read_text())
        rows = {r["table"]: r for r in inv["tables"]}
        self.assertTrue(rows["MAIN.ORDERS"]["modeled"])
        self.assertFalse(rows["MAIN.ORDER_ITEMS"]["modeled"])
        self.assertIn("PRODUCT_ID", rows["MAIN.ORDER_ITEMS"]["missing_columns"])
        self.assertIn("50.50", rows["MAIN.ORDER_ITEMS"]["source_record"]["sql"])
        self.assertTrue(any(j["right"] == "MAIN.PRODUCTS" for j in inv["join_candidates"]))
        self.assertEqual(len(inv["metric_variants"]), 3)
        self.assertTrue(all(Path(v["source_path"]).exists() for v in inv["metric_variants"]))
        self.assertTrue(all(v["metric"] == "completed_revenue" for v in inv["metric_variants"]))
        self.assertTrue(any("Sales definitions" in p.read_text()
                            for p in (self.run / "workspace" / "docs").glob("*.txt")))

    def test_repeated_preparation_is_idempotent_and_does_not_duplicate(self):
        self.prepared()
        before = (self.run / "inventory.json").read_bytes()
        baseline = (self.run / "expansion.json").read_bytes()
        self.prepared()
        self.assertEqual(before, (self.run / "inventory.json").read_bytes())
        self.assertEqual(baseline, (self.run / "expansion.json").read_bytes())
        self.assertEqual(self.before, snapshot(self.tree))

    def test_bootstrap_emitted_tree_enters_expansion_without_regeneration(self):
        import contextlib, io, roundtrip
        bootstrap_root = self.root / "bootstrap"
        bootstrap_root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            _, ws, run = roundtrip.build_run(bootstrap_root)
        r = self.command("emit.py", "--emit", "cassis", "--run", run, "--workspace", ws)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.tree = run / "emit" / "cassis"
        self.before = snapshot(self.tree)
        provenance = run / "emit" / "provenance.json"
        r = self.prep("--provenance", provenance)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.before, snapshot(self.tree))
        loaded = load_tree(self.tree, provenance)
        self.assertEqual(len(loaded["tables"]), 4)
        col = next(c for c in loaded["tables"]["MAIN.ORDERS"]["columns"] if c["name"] == "ORDER_ID")
        self.assertEqual(col["description_source"], "repo_text")
        inv = json.loads((self.run / "inventory.json").read_text())
        items = next(t for t in inv["tables"] if t["table"] == "MAIN.ORDER_ITEMS")
        self.assertIn("PRODUCT_ID", items["missing_columns"])

    def test_removed_source_is_not_retained_in_refreshed_evidence(self):
        self.prepared()
        (self.sources / "dashboards" / "booked.yml").unlink()
        self.prepared()
        inv = json.loads((self.run / "inventory.json").read_text())
        self.assertFalse(inv["metric_variants"])
        self.assertFalse(inv["source_queries"])
        self.assertEqual(self.before, snapshot(self.tree))

    def test_preparation_never_rebaselines_an_edited_draft(self):
        self.prepared()
        p = self.tree / "domains" / "README.md"
        p.write_text(p.read_text() + "\nA new curated rule.\n")
        r = self.prep()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.before, snapshot(self.run / "baseline" / "cassis"))
        self.assertIn("new curated", p.read_text())

    def test_run_cannot_overlap_context_or_sources(self):
        self.run = self.tree / "run"
        self.assertNotEqual(self.prep().returncode, 0)
        self.assertEqual(self.before, snapshot(self.tree))
        self.run = self.sources / "dbt" / "run"
        self.assertNotEqual(self.prep().returncode, 0)

    def test_missing_source_or_unknown_scope_stops_before_drafting(self):
        shutil.rmtree(self.sources / "docs")
        self.assertNotEqual(self.prep().returncode, 0)
        (self.sources / "docs").mkdir()
        self.assertNotEqual(self.prep("--table", "MAIN.MISSING").returncode, 0)
        self.assertEqual(self.before, snapshot(self.tree))

    def test_valid_additions_preserve_curated_meaning(self):
        self.prepared()
        example_draft(self.tree)
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads((self.run / "expansion-check.json").read_text())
        self.assertFalse(result["changed_definitions"])
        self.assertFalse(result["removed"])
        self.assertIn("table:MAIN.PRODUCTS", result["added"])
        for path, sha in self.before.items():
            if path != "joins.yml":
                self.assertEqual(sha, snapshot(self.tree)[path], path)
        first = snapshot(self.tree)
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(first, snapshot(self.tree))

    def test_filter_replacement_requires_explicit_review(self):
        self.prepared()
        p = self.tree / "metrics" / "completed_revenue.yml"
        m = yaml.safe_load(p.read_text()); m.pop("filters")
        write_yaml(p, m)
        self.assertEqual(self.check().returncode, 2)
        report = json.loads((self.run / "expansion-check.json").read_text())
        self.assertTrue(report["review_required"])
        self.assertEqual(report["changed_definitions"][0]["object"], "metric:completed_revenue")

    def test_refactor_keeps_formula_and_reports_old_to_new_domain(self):
        self.prepared()
        (self.tree / "domains" / "commerce").rename(self.tree / "domains" / "sales")
        for path in ("tables/MAIN/ORDERS.yml", "metrics/completed_revenue.yml"):
            p = self.tree / path; d = yaml.safe_load(p.read_text()); d["domain_path"] = "sales"
            write_yaml(p, d)
        r = self.check()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        result = json.loads((self.run / "expansion-check.json").read_text())
        self.assertEqual(len(result["refactors"]), 2)
        self.assertFalse(result["changed_definitions"])
        self.assertFalse(result["checks"]["identifiers"])

    def test_unfinished_refactor_or_broken_navigation_fails(self):
        self.prepared()
        (self.tree / "domains" / "commerce").rename(self.tree / "domains" / "sales")
        self.assertEqual(self.check().returncode, 1)
        (self.tree / "domains" / "sales").rename(self.tree / "domains" / "commerce")
        p = self.tree / "domains" / "commerce" / "README.md"
        p.write_text(p.read_text() + "\n[Old table](../../tables/MAIN/MISSING.yml)\n")
        self.assertEqual(self.check().returncode, 1)

    def test_canonical_invalid_column_and_duplicate_table_fail(self):
        self.prepared()
        self.assertEqual(len(load_tree(self.tree)["tables"]), 2)
        p = self.tree / "tables" / "MAIN" / "ORDERS.yml"
        d = yaml.safe_load(p.read_text()); d["columns"].append({"name": "DOES_NOT_EXIST"})
        write_yaml(p, d)
        r = self.command("verify.py", "--tree", self.tree, "--workspace", self.run / "workspace")
        self.assertEqual(r.returncode, 1)
        shutil.copyfile(p, p.with_name("duplicate.yml"))
        self.assertEqual(self.check().returncode, 1)

    def test_canonical_physical_case_and_yaml_extension_are_supported(self):
        self.prepared()
        p = self.tree / "tables/MAIN/ORDERS.yml"
        d = yaml.safe_load(p.read_text())
        d["schema_name"], d["table_name"] = "main", "orders"
        d["grain"] = [x.lower() for x in d["grain"]]
        for col in d["columns"]:
            col["name"] = col["name"].lower()
        p.unlink()
        write_yaml(p.with_suffix(".yaml"), d)
        r = self.command("verify.py", "--tree", self.tree, "--workspace", self.run / "workspace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(load_tree(self.tree)["tables"]), 2)

    def test_relationship_cannot_reference_unmodeled_table(self):
        self.prepared()
        (self.tree / "tables/MAIN/CUSTOMERS.yml").unlink()
        self.assertEqual(self.check().returncode, 1)
        result = json.loads((self.run / "expansion-check.json").read_text())
        self.assertTrue(any("no modeled table file" in f["detail"] for f in result["checks"]["identifiers"]))

    def test_quoted_and_lowercase_missing_metric_columns_fail(self):
        self.prepared()
        p = self.tree / "metrics" / "completed_revenue.yml"
        m = yaml.safe_load(p.read_text())
        for expression in ('SUM("ghost_amount")', 'SUM(ghost_amount)', 'SUM(`ghost_amount`)'):
            m["expression"] = expression; write_yaml(p, m)
            self.assertEqual(self.check().returncode, 1, expression)

    def test_empty_or_malformed_tree_fails(self):
        self.prepared()
        p = self.tree / "tables" / "MAIN" / "ORDERS.yml"
        p.write_text("columns: bad-shape\n")
        self.assertEqual(self.check().returncode, 1)
        shutil.rmtree(self.tree / "tables")
        self.assertEqual(self.check().returncode, 1)

    def test_removed_join_and_invalid_join_are_distinct(self):
        self.prepared()
        p = self.tree / "joins.yml"
        original = p.read_text()
        p.write_text("[]\n")
        self.assertEqual(self.check().returncode, 2)
        joins = yaml.safe_load(original)
        joins[0]["column_pairs"][0]["from_column"] = "GHOST"
        write_yaml(p, joins)
        self.assertEqual(self.check().returncode, 1)

    def test_sidecar_corroboration_expires_when_description_changes(self):
        self.prepared()
        p = self.tree / "tables" / "MAIN" / "ORDERS.yml"
        d = yaml.safe_load(p.read_text())
        col = next(c for c in d["columns"] if c["name"] == "TOTAL_AMOUNT")
        prov = self.root / "provenance.json"
        prov.write_text(json.dumps({"MAIN.ORDERS": {"TOTAL_AMOUNT": {
            "description_source": "repo_text",
            "description_sha256": hashlib.sha256(col["description"].encode()).hexdigest()}}}))
        rows, _ = review_rows(None, self.run / "workspace", self.tree, prov)
        self.assertTrue(any("TOTAL_AMOUNT described" in s for s in rows[0]["evidence"]))
        col["description"] = "An agent's new interpretation."
        write_yaml(p, d)
        rows, _ = review_rows(None, self.run / "workspace", self.tree, prov)
        self.assertFalse(any("TOTAL_AMOUNT described" in s for s in rows[0]["evidence"]))

    def test_baseline_tampering_is_detected(self):
        self.prepared()
        (self.run / "baseline" / "cassis" / "joins.yml").write_text("[]\n")
        self.assertEqual(self.check().returncode, 1)

    @unittest.skipUnless(importlib.util.find_spec("duckdb"), "duckdb is a test dependency")
    def test_executed_sales_results_and_regression_traps(self):
        import duckdb
        self.prepared()
        example_draft(self.tree)
        self.assertEqual(self.check().returncode, 0)
        db = duckdb.connect(":memory:")
        try:
            import re
            for model in ("customers", "orders", "products", "order_items"):
                sql = (self.sources / "dbt" / "models" / f"{model}.sql").read_text()
                sql = re.sub(r"{{\s*ref\('([^']+)'\)\s*}}", r"MAIN.\1", sql)
                db.execute(f"CREATE TABLE MAIN.{model} AS {sql}")
            m = yaml.safe_load((self.tree / "metrics" / "completed_revenue.yml").read_text())
            value = db.execute(f"SELECT {m['expression']} FROM MAIN.ORDERS WHERE {m['filters']}").fetchone()[0]
            self.assertEqual(float(value), 120.50)
            prose = (self.tree / "domains" / "products" / "README.md").read_text()
            sql = re.search(r"```sql\n(.*?)```", prose, re.S).group(1)
            rows = db.execute(sql).fetchall()
            self.assertEqual([(r[0], float(r[2])) for r in rows], [("p1", 50.50), ("p2", 70.00)])
            self.assertEqual(sum(float(r[2]) for r in rows), float(value))
            multiplied = db.execute("SELECT SUM(o.total_amount) FROM orders o JOIN order_items i USING(order_id) WHERE o.status = 'completed'").fetchone()[0]
            all_orders = db.execute("SELECT SUM(total_amount) FROM orders").fetchone()[0]
            self.assertEqual(float(multiplied), 241.00)
            self.assertEqual(float(all_orders), 200.50)
            self.assertNotEqual(multiplied, value)
            self.assertNotEqual(all_orders, value)
        finally:
            db.close()


def run_tests():
    return unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ExpansionTests)).wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_tests() else 1)
