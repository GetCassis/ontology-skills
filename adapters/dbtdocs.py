"""Adapter for a dbt-docs markdown export: one .md per
model with metadata bullets, a Columns table, Refs, Raw SQL and Compiled SQL —
manifest-equivalents in another serialization.

Detection: a `dbt-docs-md/models/` directory under the root.
"""
import re
from pathlib import Path

_H1_RE = re.compile(r"^#\s+(\S+)", re.MULTILINE)
_SCHEMA_RE = re.compile(r"\*\*database\.schema\*\*:\s*`[^.`]+\.([^`]+)`")
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|$",
                     re.MULTILINE)
_FENCE_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL)
_REF_RE = re.compile(r"`?model\.[\w.]+\.(\w+)`?|^-\s+`?(\w+)`?\s*$", re.MULTILINE)


def detect(root: Path) -> bool:
    return any(root.rglob("dbt-docs-md/models"))


def _sections(text: str) -> dict:
    parts = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts[m.group(1).strip()] = text[m.end():end]
    return parts


def parse(root: Path) -> list[dict]:
    out = []
    models_dir = next(root.rglob("dbt-docs-md/models"))
    for p in sorted(models_dir.rglob("*.md")):
        text = p.read_text(errors="replace")
        m = _H1_RE.search(text)
        if not m:
            continue
        name = m.group(1)
        schema = ""
        sm = _SCHEMA_RE.search(text)
        if sm:
            schema = sm.group(1).upper()
        secs = _sections(text)
        cols, enums, descriptions = {}, {}, {}
        for row in _ROW_RE.finditer(secs.get("Columns", "")):
            cname = row.group(1).strip().strip("`")
            if cname.lower() in ("column", "---", ""):
                continue
            cu = cname.upper()
            cols[cu] = row.group(2).strip()
            if row.group(3).strip():
                descriptions[cu] = row.group(3).strip()
            tests = row.group(4).strip()
            if "accepted_values" in tests:
                vals = re.findall(r"'([^']+)'", tests)
                if vals:
                    enums[cu] = vals
        raw_sql = ""
        fm = _FENCE_RE.search(secs.get("Raw SQL", "") or secs.get("Compiled SQL", ""))
        if fm:
            raw_sql = fm.group(1)
        refs = []
        for rm in _REF_RE.finditer(secs.get("Refs", "")):
            refs.append((rm.group(1) or rm.group(2)))
        out.append({
            "table": f"{schema}.{name.upper()}" if schema else name.upper(),
            "dbt_model": name,
            "sql_path": str(p),
            "yml_path": str(p),
            "sql": raw_sql,
            "description": (secs.get("Description") or "").strip(),
            "caveats": "",
            "granularity": [],
            "column_descriptions": descriptions,
            "enums": enums,
            "tests": {},
            "outbound_refs": [r for r in refs if r],
            "adapter": "dbtdocs",
        })
    return out
