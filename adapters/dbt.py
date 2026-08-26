"""Adapter for real dbt projects.

Thin wrapper around inventory.py — vendored from GetCassis/dbt-agent-readiness
into adapters/vendor/, see the README there — which handles compiled-manifest vs
source-only mode, dialect detection, {% docs %} resolution, tests, and SQL
parsing. We map its output into the shared per-table record shape consumed by
evidence.py.

dbt models carry no warehouse schema; the record sets "table" to the model
name uppercased and evidence.py reconciles against schema.json by table name.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (collect_doc_blocks, descriptions_from_yml,  # noqa: E402
                    resolve_doc_refs)


# A dbt macro is a function whose BODY is the computation. The SQL of a model
# that calls one contains the call, never the logic — so a source-only run hands
# an enrichment agent `{{ type_of_arr_change(arr, prev, rn) }}` and no way to know
# it returns New / Churn / Contraction / Expansion / No Impact. Measured on
# GitLab's public analytics project: 8 of 50 scoped tables, 159 columns, were described
# against call sites whose definitions sat unread in macros/marts/arr/. The
# packets carried the call 175 times and the body zero times.
MACRO_DEF_RE = re.compile(
    r"{%-?\s*macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*-?%}(.*?)"
    r"{%-?\s*endmacro\s*-?%}", re.DOTALL)
MACRO_CALL_RE = re.compile(r"{{-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _macro_paths(root: Path) -> list:
    """dbt-project setting, not a convention to assume: `macro-paths` moves them."""
    try:
        import yaml
        cfg = yaml.safe_load((root / "dbt_project.yml").read_text()) or {}
        paths = cfg.get("macro-paths") or ["macros"]
    except Exception:
        paths = ["macros"]
    return [root / str(d) for d in paths]


def collect_macros(root: Path) -> dict:
    """{name: {signature, body, description, path}} for the project's own macros.

    Descriptions come from the macro yml dbt already supports (`macros:` with
    `name`/`description`/`arguments`) — the warehouse owner's own words about a
    computation, which is exactly the tier prefill would keep if it were a column.
    """
    macros: dict[str, dict] = {}
    for base in _macro_paths(root):
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.sql")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for name, args, body in MACRO_DEF_RE.findall(text):
                if name in macros:
                    continue
                macros[name] = {
                    "signature": f"{name}({' '.join(args.split())})",
                    "body": body.strip(),
                    "description": "",
                    "arguments": [],
                    "path": str(f.relative_to(root)),
                }
        for f in sorted(base.rglob("*.yml")):
            try:
                import yaml
                doc = yaml.safe_load(f.read_text(errors="replace")) or {}
            except Exception:
                continue
            for m in (doc.get("macros") or []):
                rec = macros.get(str(m.get("name")))
                if not rec:
                    continue
                rec["description"] = str(m.get("description") or "").strip()
                rec["arguments"] = [
                    {"name": a.get("name"), "description": a.get("description")}
                    for a in (m.get("arguments") or []) if isinstance(a, dict)]
    return macros


def detect(root: Path) -> bool:
    return (root / "dbt_project.yml").exists()


def _load_inventory_module():
    """The vendored parser. Imported by path, not by package name, so it works
    the same whether the kit is on sys.path or not — and so nothing reaches for
    a copy in somebody's home directory: a fresh clone must have its dbt
    adapter or fail loudly, never silently lack it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
    import inventory  # noqa: E402
    return inventory


def parse(root: Path) -> list[dict]:
    inv_mod = _load_inventory_module()
    inv = inv_mod.build_inventory(root)
    if inv.get("error"):
        raise RuntimeError(f"inventory.py: {inv['error']}: {inv.get('message')}")

    # inventory.py surfaces descriptions from the COMPILED MANIFEST. Without one
    # — the normal state of a repo read from source — every description arrives
    # empty even when every column was documented by hand in yml + doc blocks.
    # Measured on one real project: 0 of 176 models carried a description while
    # the repo defined 1,491 doc blocks covering ~1,500 columns. So resolve from
    # source ourselves and use it to fill whatever inventory left blank.
    doc_blocks = collect_doc_blocks(root)
    from_yml = descriptions_from_yml(root, doc_blocks)
    unresolved = from_yml.pop("__unresolved_refs__", [])

    cols_by_model: dict[str, list[dict]] = {}
    for c in inv.get("columns") or []:
        cols_by_model.setdefault(c.get("model"), []).append(c)

    macros = collect_macros(root)
    out = []
    for m in inv.get("models") or []:
        name = m.get("name") or ""
        mcols = cols_by_model.get(name, [])
        enums, tests, descriptions = {}, {}, {}
        src = from_yml.get(name) or {}
        for c in mcols:
            cu = str(c.get("name", "")).upper()
            desc = resolve_doc_refs(c.get("description"), doc_blocks) \
                if c.get("description") else None
            if desc:
                descriptions[cu] = desc
            for t in c.get("tests") or []:
                tests.setdefault(cu, []).append(t)
                if isinstance(t, str) and t.startswith("accepted_values:"):
                    enums[cu] = [v for v in t.split(":", 1)[1].split(",") if v]
        # fill from source yml for every column inventory did not describe,
        # including columns inventory never listed at all
        for cu, cd in (src.get("columns") or {}).items():
            descriptions.setdefault(cu, cd)
        pk = m.get("pk_column")
        model_sql = _read_sql(root, m.get("sql_path"))
        out.append({
            "table": name.upper(),
            "dbt_model": name,
            "layer": m.get("layer"),
            "sql_path": m.get("sql_path"),
            "yml_path": m.get("yaml_path") or m.get("yml_path"),
            "sql": model_sql,
            # only names this project defines: ref/source/config/var and package
            # macros are excluded by construction, not by a skip-list
            "macro_calls": sorted({n for n in MACRO_CALL_RE.findall(model_sql)
                                   if n in macros}),
            "description": resolve_doc_refs(m.get("description"), doc_blocks)
                           or src.get("description") or "",
            "caveats": "",
            "granularity": [str(pk).upper()] if pk else [],
            "column_descriptions": descriptions,
            "enums": enums,
            "tests": tests,
            "outbound_refs": m.get("outbound_refs") or [],
            "inbound_refs": m.get("inbound_refs"),
            "materialization": m.get("materialization"),
            "adapter": "dbt",
        })
    # Keep the raw inventory next to the records for downstream consumers
    # (relationships, catalogs) without forcing them through the record shape.
    return out, {
        "relationships": inv.get("relationships"),
        "catalogs_keys": sorted((inv.get("catalogs") or {}).keys()),
        "manifest_used": inv.get("manifest_used"),
        "dialect": inv.get("dialect"),
        "doc_blocks": {
            "defined": len(doc_blocks),
            "unresolved_refs": unresolved,
            "models_described": sum(1 for r in out if r.get("description")),
            "columns_described": sum(len(r.get("column_descriptions") or {})
                                     for r in out),
        },
        "raw_inventory": inv,
        "macros": macros,
    }


def _read_sql(root: Path, sql_path) -> str:
    if not sql_path:
        return ""
    p = Path(sql_path)
    if not p.is_absolute():
        p = root / sql_path
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""
