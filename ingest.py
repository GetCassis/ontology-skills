#!/usr/bin/env python3
"""Normalize an input bundle into the bootstrap-kit workspace layout.

Usage:
  python3 ingest.py --input <bundle-or-repo dir> --schema <csv|json> \
      --workspace <out dir> [--adapter auto|dbt|dbtdocs|none] \
      [--questions-dir DIR]... [--dashboards-dir DIR]... [--docs-dir DIR]... \
      [--query-history FILE]

Adapter auto-detection: dbt (dbt_project.yml) -> dbtdocs (a manifest/catalog
export). When neither matches, exit with the instruction to run the LLM
config fallback (adapters/llm_config_fallback.md) which emits a mapping config;
rerun with --adapter generic --adapter-config <file>.
"""
import argparse
import csv
import json
import re
from collections import Counter
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (die, dump_json, fact_id, is_agent_instructions, load_json,
                    map_records_to_schema, parse_column_glossary,
                    sql_table_refs, write_csv_rows)
from adapters import dbt as dbt_adapter
from adapters import dbtdocs as dbtdocs_adapter


# ---------- schema ----------

def load_schema_any(path: Path) -> tuple:
    """-> (schema, comments, keys)

    A schema export is not only names and types. Warehouses carry column COMMENTS
    and sometimes declare primary keys, and both are documentation somebody
    already wrote. Reading just the types threw away 1,772 column comments on
    one real warehouse — 58% of its surface, table-specific and therefore better
    evidence than any name-keyed glossary. Same silent-loss family as unresolved doc blocks:
    the field was there, nothing read it, and the run reported zero.
    """
    schema: dict[str, dict] = {}
    comments: dict[str, dict] = {}
    keys: dict[str, list] = {}
    if path.suffix == ".csv":
        with open(path) as f:
            rdr = csv.DictReader(f)
            # Header casing is whatever their export tool emitted. Snowflake
            # shouts, Postgres and BigQuery whisper, and a CSV saved out of a
            # spreadsheet does whatever the person did. Matching case-blind
            # costs nothing; the alternative is what a real run hit — a
            # `KeyError: 'TABLE_SCHEMA'` traceback naming a column WE expect
            # rather than the file they passed.
            fields = {str(h).strip().upper(): h for h in (rdr.fieldnames or [])}
            need = ("TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME")
            missing = [n for n in need if n not in fields]
            if missing:
                die(f"{path} is missing the column(s) {missing}. Its header row "
                    f"is: {rdr.fieldnames}. A schema export needs one row per "
                    f"column with at least TABLE_SCHEMA, TABLE_NAME and "
                    f"COLUMN_NAME (case does not matter); DATA_TYPE and COMMENT "
                    f"are used when present.")

            def g(row, name):
                h = fields.get(name)
                return (row.get(h) or "") if h else ""

            for r in rdr:
                t = f"{g(r, 'TABLE_SCHEMA').upper()}.{g(r, 'TABLE_NAME').upper()}"
                col = g(r, "COLUMN_NAME").upper()
                if not col or t == ".":
                    continue
                schema.setdefault(t, {})[col] = g(r, "DATA_TYPE")
                # information_schema exports vary in what they call this
                note = (g(r, "COMMENT") or g(r, "DESCRIPTION")
                        or g(r, "COLUMN_COMMENT") or "").strip()
                if note:
                    comments.setdefault(t, {})[col] = note
    else:
        data = json.load(open(path))
        tables = data.get("tables", data) if isinstance(data, dict) else data
        for t in tables:
            key = f"{str(t.get('schema_name', '')).upper()}.{str(t['name']).upper()}"
            schema[key] = {}
            for c in t.get("columns") or []:
                col = str(c["name"]).upper()
                schema[key][col] = str(c.get("data_type") or c.get("type") or "")
                note = str(c.get("comment") or c.get("description") or "").strip()
                if note:
                    comments.setdefault(key, {})[col] = note
                if c.get("is_primary_key"):
                    keys.setdefault(key, []).append(col)
            if t.get("description"):
                comments.setdefault(key, {})["__table__"] = str(t["description"]).strip()
    # A type column that exists but is empty is a silent data-quality hole in
    # THEIR export, not a formatting variant: one real export carried 3,399
    # rows with data_type blank on every one, and only a hand diff against a
    # second export surfaced it. Types feed grain, enum and
    # join evidence, so say it now, once, while a better export is cheap.
    total = sum(len(cols) for cols in schema.values())
    typed = sum(1 for cols in schema.values() for v in cols.values()
                if str(v).strip())
    if total >= 20 and not typed:
        print(f"WARN: {path.name}: data_type is empty on all {total} columns. "
              f"The run works, but type-derived evidence (grain, enums, join "
              f"mining) degrades — if another export of this warehouse carries "
              f"types, prefer it.")
    return schema, comments, keys


# ---------- BI corpora ----------

def ingest_questions(dirs: list[Path], known_tables: set) -> list[dict]:
    rows = []
    for d in dirs:
        for p in sorted(d.rglob("*.json")):
            try:
                q = json.load(open(p))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            sql = q.get("question_query") or ""
            if not sql:
                continue
            declared = [str(t).upper() for t in q.get("tables_used_by_the_question") or []]
            tables = set(declared) | sql_table_refs(sql, known_tables)
            rows.append({
                "id": fact_id("q", p.name),
                "name": q.get("question_name") or p.stem,
                "path": q.get("question_path") or str(p),
                "views": q.get("question_views_last_3_months") or "",
                "sql": sql,
                "tables": ";".join(sorted(tables)),
            })
    return rows


def ingest_dashboards(dirs: list[Path], known_tables: set) -> list[dict]:
    rows = []
    for d in dirs:
        for p in sorted(list(d.rglob("*.json")) + list(d.rglob("*.yaml")) +
                        list(d.rglob("*.yml"))):
            try:
                if p.suffix == ".json":
                    data = json.load(open(p))
                else:
                    import yaml as _yaml
                    data = _yaml.safe_load(open(p))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            # cards-YAML shape: {dashboard: {name}, cards: [{name, sql}]}
            if "cards" in data and isinstance(data.get("dashboard"), dict):
                dname = data["dashboard"].get("name") or p.stem
                for i, card in enumerate(data.get("cards") or []):
                    sql = card.get("sql") or ""
                    if not sql:
                        continue
                    rows.append({
                        "id": fact_id("d", p.name, i),
                        "dashboard": dname,
                        "card": card.get("name") or str(i),
                        "sql": sql,
                        "tables": ";".join(sorted(sql_table_refs(sql, known_tables))),
                    })
                continue
            for dash in (data.get("dashboards") or []) if isinstance(data, dict) else []:
                dname = dash.get("name") or p.stem
                for dc in dash.get("dashcards") or []:
                    card = dc.get("card") or {}
                    sql = (((card.get("dataset_query") or {}).get("native") or {})
                           .get("query") or "")
                    if not sql:
                        continue
                    rows.append({
                        "id": fact_id("d", p.name, dc.get("id")),
                        "dashboard": dname,
                        "card": card.get("name") or str(dc.get("id")),
                        "sql": sql,
                        "tables": ";".join(sorted(sql_table_refs(sql, known_tables))),
                    })
    return rows


def ingest_query_history(path: Path, known_tables: set) -> list[dict]:
    data = json.load(open(path))
    rows = []
    for i, entry in enumerate(data):
        sql = entry.get("QUERY_TEXT") or entry.get("query_text") or ""
        if not sql:
            continue
        rows.append({
            "id": fact_id("h", i, sql[:80]),
            "sql": sql,
            "tables": ";".join(sorted(sql_table_refs(sql, known_tables))),
        })
    return rows


# ---------- docs ----------

def ingest_docs(dirs: list[Path], out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for d in dirs:
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            text, kind = None, None
            if p.suffix.lower() == ".pdf":
                text, kind = _pdf_text(p), "pdf"
            elif p.suffix.lower() in (".md", ".mdx", ".txt", ".rst"):
                text, kind = p.read_text(errors="replace"), "text"
            if not text or not text.strip():
                continue
            # Keyed by PATH, not basename, and consistent with ingest_context's
            # fact_id("ctx", str(f)). A real documentation tree is a static-site
            # repo where the page name is the DIRECTORY and every leaf is
            # _index.md: GitLab's public handbook is 4,736 markdown files under
            # 3,018 distinct basenames, so a name-keyed id overwrote 1,708 of
            # them (1,302 collapsed onto one _index.md) and left an index
            # claiming 46.9M chars over files holding 34M. Same silent-loss
            # family as the unresolved doc blocks and the dropped column
            # comments: the material was there, the run reported it read, and
            # a third of it was not on disk.
            doc_id = fact_id("doc", str(p))
            (out_dir / f"{doc_id}.txt").write_text(text)
            rel = p.relative_to(d)
            if is_agent_instructions(rel):
                kind = "agent_instructions"
            index.append({"id": doc_id, "source": str(p), "kind": kind,
                          "rel": str(rel), "chars": len(text)})
    ids = [e["id"] for e in index]
    if len(set(ids)) != len(ids):
        die(f"docs ingest: {len(ids) - len(set(ids))} of {len(ids)} documents "
            f"share an id, so the corpus on disk is smaller than the index says. "
            f"Fix the id derivation before trusting any doc count.")
    return index


CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".java", ".go", ".rb", ".rs", ".kt",
                 ".scala", ".php", ".cs", ".swift", ".sql"}
TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx"}


# A file with three or more `**Bold**:` lines LOOKS like a column dictionary.
# It is reported as one and nothing is harvested from it, because looking like
# one is all a classifier can establish.
#
# Measured, twice, on GitLab's public handbook. Harvesting produced 647 "column
# definitions" — one keyed `10` — of which 8 reached a column and 0 were right.
# Then a precision floor was tried: a candidate had to have some fraction of its
# bold labels naming a real column. It does not work, and the reason is the
# point. The top-scoring file under that rule was a product-marketing page whose
# bold labels matched a real column name 30 times out of 30, because in a
# warehouse of 12,798 distinct column names the column vocabulary IS common
# English — status, source, type, owner, region. A name match carries no
# evidence that the text defines that column, which is the same lesson the rest
# of this kit keeps learning about name-keyed prose.
#
# So a column dictionary is something a PERSON declares, with --glossary-dir.
# What the classifier can usefully do is notice the shape and say so.
def _classify_context(path: Path, text: str) -> str:
    """What KIND of thing did the user hand us? Shape, not transport.

    The transport is the user's to choose — a Notion export, a Slack archive, a git
    clone of the product, a folder of PDFs. What we have to recognize is the shape,
    because that decides how the content becomes evidence. All three benchmark
    fixtures hid their documentation in a different shape and needed a parser, not
    a connector.
    """
    suf = path.suffix.lower()
    if suf in TABULAR_SUFFIXES:
        return "tabular"
    if suf in CODE_SUFFIXES:
        return "code"
    if len(DOC_GLOSSARY_RE.findall(text or "")) >= 3:
        return "glossary"
    if re.search(r"\bSELECT\b.{0,200}\bFROM\b", text or "", re.I | re.S):
        return "sql"
    return "prose"


DOC_GLOSSARY_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*([A-Za-z0-9_\\]+)\*\*\s*:",
                             re.M)


def ingest_context(dirs: list, out_dir: Path) -> tuple:
    """Index whatever the user chose to bring, preserving where it came from.

    Nothing is interpreted here beyond classification: a context file is evidence
    with provenance, and provenance means the agent (and later a reviewer) can see
    that a rule came from a Slack thread rather than from the warehouse.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    index, glossary = [], {}
    # files shaped like a column dictionary, so the user can pass the ones that
    # really are with --glossary-dir instead of losing them silently
    looks_like = []
    for d in dirs:
        d = Path(d)
        files = sorted(d.rglob("*")) if d.is_dir() else [d]
        # the folder name is the provenance label unless the user gave one
        label = d.name
        for f in files:
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            text = ""
            if f.suffix.lower() not in TABULAR_SUFFIXES | {".pdf", ".png", ".jpg"}:
                text = raw.decode("utf-8", errors="replace")
            elif f.suffix.lower() == ".pdf":
                text = _pdf_text(f) or ""
            rel = f.relative_to(d) if d.is_dir() else Path(f.name)
            kind = ("agent_instructions" if is_agent_instructions(rel)
                    else _classify_context(f, text))
            fid = fact_id("ctx", str(f))
            if text:
                (out_dir / f"{fid}.txt").write_text(text)
            index.append({"id": fid, "origin": label,
                          "source": str(f), "kind": kind,
                          "bytes": len(raw),
                          "rel": str(rel)})
            if kind == "glossary":
                looks_like.append(str(f))
    # No harvest. The glossary dict stays empty from this path by design; the
    # comment above GLOSSARY-shaped classification says why.
    return index, glossary, looks_like


def _pdf_text(path: Path):
    try:
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception as e:
        print(f"  pdf extraction failed for {path.name}: {e}", file=sys.stderr)
        return None


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True,
                    help="transformation-repo root (a dbt project, or a dbt docs export)")
    ap.add_argument("--schema", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--adapter", default="auto",
                    choices=["auto", "dbt", "dbtdocs", "none"])
    ap.add_argument("--questions-dir", type=Path, action="append", default=[])
    ap.add_argument("--dashboards-dir", type=Path, action="append", default=[])
    ap.add_argument("--docs-dir", type=Path, action="append", default=[])
    ap.add_argument("--context-dir", type=Path, action="append", default=[],
                    help="anything else the user chose to bring: a Notion export, a "
                         "Slack archive, product source, PDFs. Indexed as evidence "
                         "with its origin preserved; the transport is theirs to pick")
    ap.add_argument("--glossary-dir", type=Path, action="append", default=[],
                    help="markdown column glossary (`**column**: description`), keyed "
                         "by COLUMN NAME across the whole warehouse rather than by model")
    ap.add_argument("--query-history", type=Path)
    args = ap.parse_args()

    ws = args.workspace
    ws.mkdir(parents=True, exist_ok=True)

    schema, col_comments, declared_keys = load_schema_any(args.schema)
    dump_json(schema, ws / "schema.json")
    if col_comments:
        dump_json(col_comments, ws / "column_comments.json")
        n = sum(len(v) for v in col_comments.values())
        total = sum(len(c) for c in schema.values())
        print(f"warehouse column comments: {n} on {len(col_comments)} tables "
              f"({100 * n // max(total, 1)}% of columns) — table-specific, so they "
              f"outrank a name-keyed glossary")
    if declared_keys:
        dump_json(declared_keys, ws / "declared_keys.json")
        print(f"primary keys declared in the schema export: "
              f"{sum(len(v) for v in declared_keys.values())} on "
              f"{len(declared_keys)} tables")
    known = set(schema)
    print(f"schema: {len(schema)} tables, "
          f"{sum(len(v) for v in schema.values())} columns")

    adapter = args.adapter
    if adapter == "auto":
        if dbt_adapter.detect(args.input):
            adapter = "dbt"
        elif dbtdocs_adapter.detect(args.input):
            adapter = "dbtdocs"
        else:
            die("no adapter matched. Run the LLM config fallback "
                "(adapters/llm_config_fallback.md) to generate a mapping, "
                "or pass --adapter explicitly.")

    extra = {}
    if adapter == "dbt":
        try:
            records, extra = dbt_adapter.parse(args.input)
        except RuntimeError as e:
            # A stranger's likeliest mistake is pointing --input at the repo
            # root when the dbt project lives in a subdirectory. That is worth
            # a sentence, not a stack trace.
            die(f"{e}\n  --input has to be the directory holding "
                f"dbt_project.yml. In a monorepo that is usually a "
                f"subdirectory (warehouse/, transform/, dbt/), not the repo "
                f"root.")
        extra.pop("raw_inventory", None)
    elif adapter == "dbtdocs":
        records = dbtdocs_adapter.parse(args.input)
    elif adapter == "none":
        records = []
    else:
        die(f"unknown adapter {adapter!r}. Run the LLM config fallback "
            f"(adapters/llm_config_fallback.md) for an unrecognized layout — "
            f"an adapter nobody implements would read zero tables and say nothing.")
    # Repos name models (mrt_sales__orders), warehouses name tables
    # (SALES_ORDERS). Unless the two are joined here, every downstream
    # linker silently drops the record — one warehouse read 35 of 427 tables
    # until this mapping existed. Only unambiguous mappings apply; the rest WARN.
    mapped, collisions = map_records_to_schema(records, schema)
    dump_json({"adapter": adapter, "records": records, **extra},
              ws / "repo" / "tables.json")
    with_sql = sum(1 for r in records if r.get("sql"))
    reaching = sum(1 for r in records if r.get("table") in schema)
    print(f"repo ({adapter}): {len(records)} tables, {with_sql} with SQL, "
          f"{reaching} reaching the schema"
          + (f" ({mapped} mapped by naming convention)" if mapped else ""))
    for src, tgt in collisions:
        print(f"WARN: repo record {src} looks like schema table {tgt} but the "
              f"mapping is contested or already claimed — left unmapped, its "
              f"SQL/descriptions will NOT reach that table")
    # A run where nothing links is not a run. Every downstream stage reads the
    # join of these two inputs; with an empty join there is no grain, no SQL,
    # no unit and nothing to author. Failing loud here beats printing
    # "workspace ready" and letting the next phase propose a scope of nothing
    # with the reason three screens back.
    if records and not reaching:
        examples = ", ".join(sorted(schema)[:3]) or "(none)"
        rec_ex = ", ".join(str(r.get("table") or r.get("name"))
                           for r in records[:3])
        die(f"none of the {len(records)} tables in {args.input} match any of "
            f"the {len(schema)} tables in {args.schema}, so there is nothing "
            f"to model.\n"
            f"  schema names look like: {examples}\n"
            f"  repo names look like:   {rec_ex}\n"
            f"  Usually one of two things: the schema export is from a "
            f"different database than the project builds into, or the "
            f"project's models land in schemas the export does not cover. "
            f"Check one table you know is in both.")

    if args.questions_dir:
        rows = ingest_questions(args.questions_dir, known)
        write_csv_rows(ws / "questions.csv", rows,
                       ["id", "name", "path", "views", "sql", "tables"])
        print(f"questions: {len(rows)}")
    if args.dashboards_dir:
        rows = ingest_dashboards(args.dashboards_dir, known)
        write_csv_rows(ws / "dashboard_queries.csv", rows,
                       ["id", "dashboard", "card", "sql", "tables"])
        print(f"dashboard queries: {len(rows)}")
    if args.query_history:
        rows = ingest_query_history(args.query_history, known)
        write_csv_rows(ws / "query_history.csv", rows, ["id", "sql", "tables"])
        print(f"query history: {len(rows)}")
    if args.docs_dir:
        index = ingest_docs(args.docs_dir, ws / "docs")
        dump_json(index, ws / "docs" / "index.json")
        if not index:
            shown = ", ".join(str(d) for d in args.docs_dir)
            print(f"WARN: --docs-dir {shown} produced 0 documents. "
                  f"Readable shapes are .md, .mdx, .txt, .rst and .pdf; "
                  f"anything else is skipped. If you expected content here, "
                  f"the path or the format is wrong — nothing downstream will "
                  f"say so again.")
        agentic = sum(1 for e in index if e["kind"] == "agent_instructions")
        print(f"docs: {len(index)}"
              + (f" ({agentic} of them instructions written for an agent, "
                 f"indexed and excluded from search)" if agentic else ""))
    if args.context_dir:
        idx, glos_from_ctx, looks_like = ingest_context(
            args.context_dir, ws / "context")
        dump_json(idx, ws / "context" / "index.json")
        kinds = Counter(e["kind"] for e in idx)
        print(f"user context: {len(idx)} files ({', '.join(f'{v} {k}' for k, v in kinds.most_common())})")
        if looks_like:
            print(f"              {len(looks_like)} file(s) are SHAPED like a "
                  f"column dictionary. Nothing was read out of them: pass any "
                  f"that really is one with --glossary-dir, where you are the "
                  f"one saying so. First few:")
            for f in looks_like[:5]:
                print(f"                {f}")
        if glos_from_ctx:
            existing = load_json(ws / "column_glossary.json") \
                if (ws / "column_glossary.json").exists() else {}
            for k, v in glos_from_ctx.items():
                existing.setdefault(k, v)
            dump_json(existing, ws / "column_glossary.json")
            print(f"              {len(glos_from_ctx)} column definitions found inside it")
    if args.glossary_dir:
        glos = parse_column_glossary(args.glossary_dir)
        # A handed-over dictionary wins per key, and what the context classifier
        # guessed fills the gaps. A plain overwrite here would silently discard
        # every context-derived entry whenever both flags are given — the same
        # silent-loss family as the basename doc ids.
        existing = load_json(ws / "column_glossary.json") \
            if (ws / "column_glossary.json").exists() else {}
        merged = dict(existing)
        merged.update(glos)
        dump_json(merged, ws / "column_glossary.json")
        kept = len(merged) - len(glos)
        if kept:
            print(f"                 {kept} definition(s) kept from the context "
                  f"classifier, where your glossary has no entry")
        glos = merged
        covered = sum(1 for t, cols in schema.items()
                      for c in cols if c.upper() in glos)
        total = sum(len(c) for c in schema.values())
        print(f"column glossary: {len(glos)} definitions | covers {covered}/{total} "
              f"schema columns ({100 * covered // max(total, 1)}%)")

    print(f"workspace ready: {ws}")


if __name__ == "__main__":
    main()
