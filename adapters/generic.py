"""Adapter for a transformation repo that is neither dbt nor a dbt docs export
but keeps one file per table: SQLMesh, for one (checked on its public
`examples/sushi` project). One mapping config says where each table's file is and where its
name, SQL and documentation sit in it; this code applies it to every file the
same way. The config is written once per layout, by a person or by the agent
prompt in adapters/llm_config_fallback.md, never per file.

Config (JSON or YAML):

  table_files       glob relative to --input, or a list of globs: one file per table
  table_name_from   filename | field:<a.b> | regex:<pattern>
  schema_name_from  parent_dir | field:<a.b> | regex:<pattern> | constant:<NAME> | none
  sql_sibling       self | file:<template> | field:<a.b> | none
                    template placeholders: {stem} {name} {schema}; relative to
                    the table file's directory, or to --input when it starts with /
  sql_start         optional regex: the SQL starts after its first match (a
                    SQLMesh MODEL (...); header)
  fields            description, caveats, granularity: a spec as above, or none
                    columns: {list: field:<a.b>, name: <key>, description: <key>,
                              enum_values: <key>}  for YAML or JSON column docs
                    columns: {within: regex:<pattern>, pairs: regex:<pattern>}
                              for docs in the file's text: every match of
                              `pairs` (group 1 the column, group 2 its text)
                              inside group 1 of `within`, or the whole file

`field:` walks the file's YAML or JSON; `regex:` takes the first group that
matched in the first match in the file's text, so alternatives can sit in one
pattern. A table name carrying a dot with no schema spec splits into schema
and table.
"""
import json
import re
from pathlib import Path

import yaml


def load_config(path: Path) -> dict:
    text = Path(path).read_text()
    cfg = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"{path}: a mapping config is one JSON or YAML object")
    if cfg.get("no_stable_mapping"):
        return cfg
    for key in ("table_files", "table_name_from"):
        if not cfg.get(key):
            raise RuntimeError(f"{path}: `{key}` is required")
    return cfg


def _field(doc, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _resolve(spec, doc, text: str, path: Path):
    """One spec against one file. None when the file does not carry it."""
    if not spec or spec == "none":
        return None
    if spec == "filename":
        return path.stem
    if spec == "parent_dir":
        return path.parent.name
    kind, _, arg = str(spec).partition(":")
    if kind == "constant":
        return arg
    if kind == "field":
        return _field(doc, arg)
    if kind == "regex":
        m = re.search(arg, text, re.S | re.M)
        if not m:
            return None
        return next((g for g in m.groups() if g is not None), None)
    raise RuntimeError(f"unknown spec {spec!r}: use filename, parent_dir, "
                       f"constant:, field:, regex: or none")


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _prose(spec, v) -> str:
    """Text read out of a file's raw text (a comment, a quoted string) keeps
    the file's line breaks and indentation; YAML or JSON text is kept as
    written."""
    return " ".join(v.split()) if str(spec).startswith("regex:") and isinstance(
        v, str) else _text(v)


def _names(v) -> list:
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip().upper() for x in (v or []) if str(x).strip()]


def _columns(spec, doc, text: str, path: Path) -> tuple:
    if not isinstance(spec, dict):
        return {}, {}
    if spec.get("pairs"):
        kind, _, pattern = str(spec["pairs"]).partition(":")
        if kind != "regex":
            raise RuntimeError("columns.pairs takes regex:<pattern>")
        scope = (_resolve(spec["within"], doc, text, path) if spec.get("within")
                 else text) or ""
        return ({str(m.group(1)).upper(): " ".join(m.group(2).split())
                 for m in re.finditer(pattern, scope, re.S | re.M)
                 if m.group(2) and m.group(2).strip()}, {})
    if not spec.get("list"):
        return {}, {}
    items = _resolve(spec["list"], doc, "", Path("."))
    if isinstance(items, dict):  # {COL: {...}} keyed by name
        items = [{**(v if isinstance(v, dict) else {}), "__name__": k}
                 for k, v in items.items()]
    descriptions, enums = {}, {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = item.get("__name__") or item.get(spec.get("name") or "name")
        if not name:
            continue
        cu = str(name).upper()
        d = _text(item.get(spec.get("description") or "description"))
        if d:
            descriptions[cu] = d
        vals = item.get(spec.get("enum_values") or "") if spec.get("enum_values") else None
        if isinstance(vals, list) and vals and not any(isinstance(x, bool) for x in vals):
            enums[cu] = [str(x) for x in vals]
    return descriptions, enums


def _sql(cfg: dict, doc, text: str, path: Path, root: Path, schema: str,
         name: str) -> tuple:
    spec = cfg.get("sql_sibling") or "none"
    if spec == "same_stem_.sql":
        spec = "file:{stem}.sql"
    if spec == "none":
        return "", None
    if spec == "self":
        sql, where = text, path
    elif spec.startswith("field:"):
        sql, where = _text(_field(doc, spec[6:])), path
    elif spec.startswith("file:"):
        rel = spec[5:].format(stem=path.stem, name=name, schema=schema)
        where = root / rel[1:] if rel.startswith("/") else path.parent / rel
        sql = where.read_text(errors="replace") if where.exists() else ""
        if not sql:
            return "", None
    else:
        raise RuntimeError(f"unknown sql_sibling {spec!r}")
    if cfg.get("sql_start"):
        m = re.search(cfg["sql_start"], sql, re.S | re.M)
        if m:
            sql = sql[m.end():]
    return sql.strip(), where


def parse(root: Path, cfg: dict) -> tuple:
    """-> (records, report). The report says what the mapping could not use,
    so a layout that maps half its files says so instead of looking complete."""
    globs = cfg["table_files"]
    files = sorted({p for g in ([globs] if isinstance(globs, str) else globs)
                    for p in root.glob(g) if p.is_file()})
    if not files:
        raise RuntimeError(f"table_files {globs!r} matched no file under {root}")
    fields = cfg.get("fields") or {}
    out, seen = [], {}
    report = {"files": len(files), "unnamed": [], "duplicates": []}
    for path in files:
        text = path.read_text(errors="replace")
        doc = None
        if path.suffix in (".yml", ".yaml", ".json"):
            try:
                doc = (json.loads(text) if path.suffix == ".json"
                       else yaml.safe_load(text))
            except (ValueError, yaml.YAMLError):
                doc = None
        name = _text(str(_resolve(cfg["table_name_from"], doc, text, path) or ""))
        if not name:
            report["unnamed"].append(str(path.relative_to(root)))
            continue
        schema = _text(str(_resolve(cfg.get("schema_name_from"), doc, text, path)
                           or ""))
        if not schema and "." in name:
            schema, name = name.split(".")[-2:]
        table = f"{schema}.{name}".upper() if schema else name.upper()
        if table in seen:
            report["duplicates"].append((table, seen[table],
                                         str(path.relative_to(root))))
            continue
        seen[table] = str(path.relative_to(root))
        sql, sql_path = _sql(cfg, doc, text, path, root, schema, name)
        descriptions, enums = _columns(fields.get("columns"), doc, text, path)
        out.append({
            "table": table,
            "dbt_model": None,
            "model_file": str(path.relative_to(root)),
            "layer": None,
            "sql_path": str(sql_path) if sql_path else None,
            "yml_path": str(path) if doc is not None else None,
            "sql": sql,
            "macro_calls": [],
            "description": _prose(fields.get("description"),
                                  _resolve(fields.get("description"), doc, text, path)),
            "caveats": _prose(fields.get("caveats"),
                              _resolve(fields.get("caveats"), doc, text, path)),
            "granularity": _names(_resolve(fields.get("granularity"), doc, text,
                                           path)),
            "column_descriptions": descriptions,
            "enums": enums,
            "tests": {},
            "outbound_refs": [],
            "adapter": "generic",
        })
    return out, report
