#!/usr/bin/env python3
"""Resolve dbt doc blocks into the workspace, then emit per-batch agent packets.

Two deterministic steps (design rule: if a step's inputs fully determine its
output, it is a script, never an agent):

1. `resolve-docs` — a dbt repo states descriptions as `{{ doc('name') }}`
   indirections whose bodies live in `docs/**/*.md` as `{% docs name %}` blocks.
   ingest.py's dbt adapter does not expand them, so `tables.json` ships with
   empty `description` / `column_descriptions` even when every column was
   documented by hand. This resolves both and writes them back, so every
   downstream consumer (packets, agents, verify.py) sees the owner's own words.

2. `packets` — one markdown packet per enrichment batch, carrying for each of
   its tables: the schema column list with types, grain, enum values, dbt tests,
   the resolved existing descriptions, the defining model SQL, and the assigned
   domain. A Stage C agent reads ONE file instead of globbing the repo.

Usage:
  python3 dispatch_prep.py resolve-docs --repo <dbt repo> --workspace <ws>
  python3 dispatch_prep.py packets --run <run dir> --workspace <ws> [--out <dir>]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GLOSSARY_TIERS, glossary_source_token, glossary_tier

DOC_BLOCK_RE = re.compile(r"{%\s*docs\s+([A-Za-z0-9_]+)\s*%}(.*?){%\s*enddocs\s*%}",
                          re.DOTALL)
DOC_REF_RE = re.compile(r"{{\s*doc\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)\s*}}")


def load_json(p: Path):
    return json.loads(Path(p).read_text())


def dump_json(o, p: Path):
    Path(p).write_text(json.dumps(o, indent=1, ensure_ascii=False, sort_keys=True))


# ---------- step 1: resolve doc blocks ----------

def collect_doc_blocks(repo: Path) -> dict:
    blocks = {}
    for md in repo.rglob("*.md"):
        for name, body in DOC_BLOCK_RE.findall(md.read_text(errors="replace")):
            blocks[name] = body.strip()
    return blocks


def resolve(text, blocks: dict, missing: set):
    """Expand every doc ref in a description. Plain text passes through."""
    if not isinstance(text, str):
        return None

    def sub(m):
        name = m.group(1)
        if name in blocks:
            return blocks[name]
        missing.add(name)
        return ""
    out = DOC_REF_RE.sub(sub, text).strip()
    return out or None


def cmd_resolve_docs(args):
    blocks = collect_doc_blocks(args.repo)
    tj = args.workspace / "repo" / "tables.json"
    data = load_json(tj)
    missing, n_tbl, n_col = set(), 0, 0
    for rec in data["records"]:
        yml_path = rec.get("yml_path")
        if not yml_path or not Path(yml_path).exists():
            continue
        try:
            doc = yaml.safe_load(Path(yml_path).read_text()) or {}
        except yaml.YAMLError:
            continue
        for model in (doc.get("models") or []) + (doc.get("seeds") or []):
            if model.get("name") != rec["dbt_model"]:
                continue
            desc = resolve(model.get("description"), blocks, missing)
            if desc and not rec.get("description"):
                rec["description"] = desc
                n_tbl += 1
            cols = rec.setdefault("column_descriptions", {})
            for col in (model.get("columns") or []):
                cd = resolve(col.get("description"), blocks, missing)
                if cd and col.get("name"):
                    cols[col["name"].upper()] = cd
                    n_col += 1
    data["doc_blocks_resolved"] = {"blocks_available": len(blocks),
                                   "table_descriptions": n_tbl,
                                   "column_descriptions": n_col,
                                   "unresolved_refs": sorted(missing)}
    dump_json(data, tj)
    print(f"doc blocks: {len(blocks)} defined | resolved {n_tbl} table + "
          f"{n_col} column descriptions | unresolved refs: {len(missing)}")
    if missing:
        print("  first unresolved:", sorted(missing)[:8])


# ---------- step 2: per-batch packets ----------

def cmd_packets(args):
    ws, run = args.workspace, args.run
    schema = load_json(ws / "schema.json")
    batches = load_json(run / "batches.json")
    _repo = load_json(ws / "repo" / "tables.json")
    recs = _repo["records"]
    # A macro body IS the computation. Without it the packet shows the call and
    # the agent guesses; with it the definition is in the same packet the rule
    # ("computation claims come from the defining SQL, in its own packet")
    # already points at. Emitted once per batch rather than per table: the ARR
    # macros are called by eight tables in one batch.
    macros = _repo.get("macros") or {}
    by_table = {}
    for r in recs:
        by_table.setdefault(r["table"].upper(), r)
    ev = ws / "evidence"
    # same bare-vs-qualified key normalization as skeleton.py
    grains = {}
    if (ev / "grains.json").exists():
        _bare = {t.split(".", 1)[1].upper(): t for t in schema}
        for g in load_json(ev / "grains.json"):
            key = g["table"] if g["table"] in schema else _bare.get(g["table"].upper())
            if key:
                grains[key] = g.get("columns")
    enums = {}
    if (ev / "enums.json").exists():
        for e in load_json(ev / "enums.json"):
            enums.setdefault(e["table"].upper(), []).append(e)
    nonsummable = set()
    if (ev / "nonsummable.json").exists():
        ns = load_json(ev / "nonsummable.json")
        nonsummable = {x if isinstance(x, str) else x.get("table") for x in ns}
    joins = []
    jp = run / "cassis" / "joins.yml"
    if jp.exists():
        joins = yaml.safe_load(jp.read_text()) or []
        if isinstance(joins, dict):
            joins = joins.get("joins") or []

    # optional profiling findings: facts measured against the data, which outrank
    # anything inferred from a name and settle questions the SQL cannot
    prof = {}
    pf = ws / "profile" / "_findings.json"
    if pf.exists():
        for f in load_json(pf):
            prof.setdefault(str(f.get("table")).upper(), []).append(f)

    out = args.out or (run / "packets")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    resolved_total = 0
    for i, tables in enumerate(batches["batches"]):
        L = [f"# Enrichment packet — batch {i} ({len(tables)} tables)", "",
             "Everything below is extracted verbatim from the input material.",
             "",
             "Columns marked `ok` already carry the warehouse owner's OWN documentation, "
             "written into the file by script. They are not drafts for you to "
             "improve: keep the wording, and change one only where the defining SQL "
             "contradicts it or where it omits a unit or convention the SQL proves. "
             "Rewriting them in your own voice loses the vocabulary the company "
             "already uses, and costs the run its biggest saving.",
             "",
             "Columns marked `VERIFY` carry text from the warehouse-WIDE "
             "column glossary, keyed by column name rather than by this table. Treat it "
             "as a strong default that still needs checking: confirm the unit and the "
             "convention against the defining SQL below, and correct it where this "
             "table differs. Do not silently delete it.",
             "",
             "Columns marked `NEEDS DESCRIPTION` are the gaps nobody documented. "
             "Draft only what the supplied evidence supports; leave an explicit "
             "question where it does not. Stamp every description you draft "
             "`description_source: drafted`. Every computation claim comes from "
             "the defining SQL in THIS section, never from a sibling table or a "
             "same-named column elsewhere.",
             "",
             "Where a model's SQL calls a project macro, the call is not the "
             "computation: the macro body at the end of this packet is. Read it "
             "before describing any column the call produces.",
             ""]
        batch_macros = {}
        for tname in tables:
            # Records arrive keyed EITHER qualified (SCHEMA.TABLE, once ingest has
            # mapped a model name onto a schema table) or bare (when it could not).
            # Looking up only the bare name silently found nothing for every table
            # in a run whose schema export is qualified — and the packet then told
            # the enrichment agent "no dbt model exists for this table, make no
            # computation claims" for 50 of 50 tables that all had SQL. Same
            # silent-data-loss family as the unresolved doc blocks: try both keys.
            rec = (by_table.get(tname.upper())
                   or by_table.get(tname.split(".", 1)[1].upper()))
            cols = schema.get(tname, {})
            tpath = run / "cassis" / "tables" / tname.replace(".", "/")
            L += [f"## {tname}", "",
                  f"- file to edit: `cassis/tables/{tname.replace('.', '/')}.yml`",
                  f"- columns: {len(cols)}"]
            if grains.get(tname):
                L.append(f"- grain (from dbt tests): {grains[tname]}")
            if tname in nonsummable:
                L.append("- **non-summable**: stacks aggregation levels; do not sum blindly")
            if rec:
                L.append(f"- dbt model: `{rec['dbt_model']}` (layer: {rec.get('layer')}, "
                         f"materialization: {rec.get('materialization')})")
                if rec.get("tests"):
                    L.append(f"- dbt tests: {json.dumps(rec['tests'], ensure_ascii=False)}")
                if rec.get("outbound_refs"):
                    L.append(f"- refs: {rec['outbound_refs']}")
                called = [n for n in (rec.get("macro_calls") or []) if n in macros]
                if called:
                    batch_macros.update({n: macros[n] for n in called})
                    L.append("- macros called (definitions at the end of this "
                             "packet): " + ", ".join(f"`{n}`" for n in called))
                if rec.get("description"):
                    L += ["", "**Existing description of this table:**", "",
                          "> " + rec["description"].replace("\n", "\n> ")]
            else:
                L.append("- **no dbt model exists for this table** — no repo grounding; "
                         "describe from the column list only, and make no computation claims")
            t_enums = (enums.get(tname.upper())
                       or enums.get(tname.split(".", 1)[1].upper()) or [])
            if t_enums:
                L += ["", "**Enum values found in the repo's own yml:**"]
                for e in t_enums:
                    L.append(f"- `{e.get('column') or '(unnamed)'}`: "
                             f"{json.dumps(e.get('values'), ensure_ascii=False)}")
            # What is already in the shell (existing text written by `prefill`)
            # versus what still needs a description. An agent that cannot tell the
            # difference rewrites work that was already correct.
            shell_desc = {}
            if tpath.with_suffix(".yml").exists():
                try:
                    sh = yaml.safe_load(tpath.with_suffix(".yml").read_text()) or {}
                    for col in (sh.get("columns") or []):
                        if (col.get("description") or "").strip():
                            shell_desc[str(col.get("name", "")).upper()] = (
                                col["description"].strip(),
                                col.get("description_source") or "table",
                                col.get("description_source_detail"))
                except yaml.YAMLError:
                    pass
            cdesc = (rec or {}).get("column_descriptions") or {}
            todo = [c for c in cols if c.upper() not in shell_desc]
            owner_gaps = [c for c in todo
                          if (cdesc.get(c.upper(), "") or "").strip()]
            if shell_desc:
                L += ["", f"**{len(shell_desc)} of {len(cols)} columns are ALREADY "
                          f"described** (the warehouse owner's own documentation, "
                          f"pre-filled by script). Do not rewrite those — read each "
                          f"one against the defining SQL below and change it ONLY if "
                          f"the SQL contradicts it or it omits a unit/convention the "
                          f"SQL proves. Your enrichment work is the "
                          f"{len(todo)} column(s) marked NEEDS DESCRIPTION."]
            if owner_gaps:
                # Measured on a real run: 20 of 52 to-author
                # columns carried owner text the prefill had skipped as a name
                # restatement, and 18 came back as agent paraphrase.
                L += ["", f"**{len(owner_gaps)} NEEDS-DESCRIPTION column(s) already "
                          f"carry the owner's own text** (their text cell below is "
                          f"non-empty). Prefill left them out only because the wording "
                          f"restates the column name. Their words, not yours: keep the "
                          f"owner's sentence and add the substance the defining SQL "
                          f"proves (unit, filter, edge case). If their sentence "
                          f"survives, stamp `description_source: repo_text`; stamp "
                          f"`drafted` only on a full replacement."]
            if prof.get(tname):
                L += ["", "**Measured against the actual data** — these outrank any "
                      "inference from a column name, and contradicting one is an "
                      "error, not a judgment call:"]
                for f in prof[tname]:
                    L.append(f"- `{f.get('column')}`: {f.get('detail')}")
            L += ["", "**Columns** (name | type | status | text):", ""]
            for c, ctype in cols.items():
                cu = c.upper()
                if cu in shell_desc:
                    d, src, det = shell_desc[cu]
                    # naming the glossary's entity makes a cross-entity fill
                    # visible at the point of checking: VERIFY(glossary:iris)
                    # on a department table reads as wrong in one glance
                    status = (f"VERIFY(glossary:{det})" if det else "VERIFY") \
                        if src in GLOSSARY_TIERS else "ok"
                else:
                    d = " ".join((cdesc.get(cu, "") or "").split())
                    status = ("NEEDS DESCRIPTION (owner text present)" if d
                              else "NEEDS DESCRIPTION")
                L.append(f"- `{c}` | {ctype} | {status} | {d}")
            rel = [j for j in joins
                   if f"{j.get('from_schema')}.{j.get('from_table')}" == tname
                   or f"{j.get('to_schema')}.{j.get('to_table')}" == tname]
            if rel:
                L += ["", "**Joins already dispositioned for this table:**"]
                for j in rel:
                    L.append(f"- {j.get('from_schema')}.{j.get('from_table')} -> "
                             f"{j.get('to_schema')}.{j.get('to_table')} on {j.get('on')}")
            if rec and rec.get("sql"):
                resolved_total += 1
                L += ["", "**Defining SQL** (the ground truth for every computation claim):",
                      "", "```sql", rec["sql"].strip(), "```"]
            L.append("")
        if batch_macros:
            L += ["## Macro definitions used in this batch", "",
                  "Verbatim from the project's own `macros/`. A column produced by "
                  "one of these is defined HERE, not by the call site above.", ""]
            for name in sorted(batch_macros):
                m = batch_macros[name]
                L += [f"### `{m['signature']}`", "", f"- defined in: `{m['path']}`"]
                if m.get("description"):
                    L.append(f"- the project's own description: {m['description']}")
                for a in (m.get("arguments") or []):
                    if a.get("description"):
                        L.append(f"  - `{a.get('name')}`: {a['description']}")
                L += ["", "```sql", m["body"], "```", ""]
        p = out / f"batch-{i}.md"
        p.write_text("\n".join(L))
        written.append((p, len(tables), p.stat().st_size))
    for p, n, size in written:
        print(f"{p.name}: {n} tables, {size // 1024} KB")
    n_tables = sum(n for _, n, _ in written)
    print(f"defining SQL reached {resolved_total} of {n_tables} packet tables")
    if n_tables and not resolved_total:
        print("WARN: not one packet table resolved to a repo record, so every "
              "packet says 'no dbt model exists' and every computation claim in "
              "stage C would be written blind. That is a key mismatch between "
              "schema.json and repo/tables.json, not a warehouse with no repo — "
              "check both before spending a single agent token.")


def _owner_text(workspace: Path, schema: dict) -> tuple:
    """(table_desc, column_desc) keyed to QUALIFIED schema tables."""
    recs = load_json(workspace / "repo" / "tables.json")["records"]
    bare = {}
    for k in schema:
        bare.setdefault(k.split(".", 1)[1].upper(), []).append(k)

    def canon(name):
        n = str(name).upper()
        if n in schema:
            return n
        hits = bare.get(n, [])
        return hits[0] if len(hits) == 1 else None
    tdesc, cdesc = {}, {}
    for r in recs:
        t = canon(r["table"])
        if not t:
            continue
        if r.get("description"):
            tdesc[t] = r["description"]
        for c, d in (r.get("column_descriptions") or {}).items():
            if d and str(d).strip():
                cdesc[(t, c.upper())] = str(d).strip()
    return tdesc, cdesc


def cmd_prefill(args):
    """Write the owner's OWN documentation into the shells before any agent runs.

    The kit's design rule says a step whose inputs determine its output is a
    script. Column descriptions the owner already wrote are exactly that, and
    having an agent retype them is the single most expensive thing the pipeline
    can do: on one real run, 1,432 of 2,556 columns (56%) arrived documented, and
    the enrichment stage re-emitted ~38k tokens of prose that was sitting in the workspace.

    After this, enrichment is not "describe 2,556 columns" but "describe the
    ~1,100 nobody documented, and check the rest against the SQL" — which is
    judgment, and therefore an agent's job.

    Never overwrites an existing description: re-runnable, and safe after agents
    have started editing.
    """
    schema = load_json(args.workspace / "schema.json")
    tdesc, cdesc = _owner_text(args.workspace, schema)
    # Not all existing text is worth importing. "CONTACT_ID: id of contact" restates
    # the column name and the restatement gate deletes it downstream, so importing
    # it buys nothing and costs the agent a column it thinks is already done.
    # Measured on one real run: 60 of 1,492 candidate descriptions were restatements — hand
    # those back as description gaps instead.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify import is_restatement
    # Warehouse-wide column glossary, if the material shipped one. Weaker evidence
    # than per-table text — the same column name can mean something slightly
    # different in another table — so it fills only where nothing else does, and
    # the packet labels it so the agent verifies it against that table's SQL.
    gp = args.workspace / "column_glossary.json"
    glossary = load_json(gp) if gp.exists() else {}
    # Warehouse COMMENTs are table-specific, so they rank with the repo's own doc
    # blocks and above a name-keyed glossary. Precedence:
    #   repo doc block  >  warehouse comment  >  warehouse-wide glossary  >  draft from evidence
    cc = args.workspace / "column_comments.json"
    comments = load_json(cc) if cc.exists() else {}
    filled_cols = filled_tables = kept = skipped_tables = restated = 0
    from_glossary = 0
    from_inferred = 0
    per_table = {}
    restated_cols: dict[str, list[str]] = {}
    for table in sorted(schema):
        sch, name = table.split(".", 1)
        p = args.run / "cassis" / "tables" / sch / f"{name}.yml"
        if not p.exists():
            skipped_tables += 1
            continue
        rec = yaml.safe_load(p.read_text())
        if not isinstance(rec, dict):
            continue
        changed = False
        tbl_note = tdesc.get(table) or (comments.get(table) or {}).get("__table__")
        if not (rec.get("description") or "").strip() and tbl_note:
            rec["description"] = tbl_note
            filled_tables += 1
            changed = True
        n_fill = n_keep = 0
        for col in (rec.get("columns") or []):
            cname = str(col.get("name", "")).upper()
            existing = (col.get("description") or "").strip()
            if existing:
                n_keep += 1
                kept += 1
                continue
            # Every tier gets an explicit provenance stamp. An UNMARKED
            # description must mean exactly one thing — "not the owner's
            # text" — or metrics_review.py cannot refuse to count an agent's
            # own drafted words as the owner's when finish re-runs it.
            text = cdesc.get((table, cname))
            source = "repo_text" if text else None
            if not text:
                text = ((comments.get(table) or {}).get(cname) or "").strip() or None
                source = "warehouse_comment" if text else None
            detail = None
            if not text and cname in glossary:
                g = glossary[cname] or {}
                text = g.get("description")
                # Two tiers, split on whether the match was checkable at all:
                # a dictionary a person handed over, about this table, versus
                # a classifier's guess or another entity's wording. Only the
                # first counts as the owner's own words in metrics_review.
                source = glossary_tier(g, table, cname) if text else None
                # which entity's glossary file this wording came from — the
                # IRIS lesson: name-keyed text can carry another entity's
                # framing onto a table that is not that entity, and without
                # the token nobody can see the mismatch without archaeology
                detail = glossary_source_token(g.get("source")) if text else None
            if text and is_restatement(cname, text):
                # The skip is deliberate; losing the owner's words is not.
                # One real run measured the cost of hiding this: 18 of
                # 20 such columns came back as agent paraphrase. The report
                # names them and the packet hands the text to the agent.
                restated += 1
                restated_cols.setdefault(table, []).append(cname)
                continue
            if text:
                col["description"] = text
                col["description_source"] = source
                if detail:
                    col["description_source_detail"] = detail
                if source in GLOSSARY_TIERS:
                    from_glossary += 1
                    if source == "inferred_glossary":
                        from_inferred += 1
                n_fill += 1
                changed = True
        filled_cols += n_fill
        total = len(rec.get("columns") or [])
        per_table[table] = {"columns": total, "prefilled": n_fill,
                            "already_had": n_keep,
                            "needs_description": total - n_fill - n_keep}
        if restated_cols.get(table):
            per_table[table]["restatement_skips"] = restated_cols[table]
        if changed:
            p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
    needs_description = sum(v["needs_description"] for v in per_table.values())
    total_cols = sum(v["columns"] for v in per_table.values())
    dump_json({"prefilled_columns": filled_cols, "prefilled_tables": filled_tables,
               "already_had": kept, "needs_description": needs_description,
               "skipped_restatements": restated,
               "from_warehouse_glossary": from_glossary - from_inferred,
               "from_inferred_glossary": from_inferred,
               "total_columns": total_cols, "per_table": per_table},
              args.run / "prefill_report.json")
    pct = (100 * filled_cols // total_cols) if total_cols else 0
    print(f"prefill: {filled_cols} of {total_cols} columns ({pct}%) were already "
          f"documented by your own team, in their words — kept as written, not "
          f"rewritten. Plus {filled_tables} table descriptions.")
    print(f"         {needs_description} columns still need an evidence-backed description"
          + (f" ({kept} already written)" if kept else ""))
    if from_glossary:
        print(f"         of those, {from_glossary} came from the warehouse-wide column "
              f"glossary (agent must verify against each table's SQL)")
    if restated:
        print(f"         {restated} existing descriptions skipped as restatements of the "
              f"column name (left as description gaps — the owner's text still reaches "
              f"the packet, marked for reuse; columns listed per table in the report)")
    if skipped_tables:
        print(f"         {skipped_tables} schema tables have no shell yet "
              f"(run skeleton.py first)")


def cmd_backfill_grain(args):
    """Set `grain` on existing shells without regenerating them.

    skeleton.py owns grain at generation time, but re-running it would clobber
    an in-flight run's domain assignments and descriptions. This repairs grain
    in place, so a run that started under the bare-vs-qualified key bug does not
    have to be thrown away.
    """
    schema = load_json(args.workspace / "schema.json")
    ev = args.workspace / "evidence"
    bare = {t.split(".", 1)[1].upper(): t for t in schema}
    grains = {}
    for g in load_json(ev / "grains.json"):
        key = g["table"] if g["table"] in schema else bare.get(g["table"].upper())
        if key:
            grains[key] = g["columns"]
    n = skipped = 0
    for table, cols in grains.items():
        sch, name = table.split(".", 1)
        p = args.run / "cassis" / "tables" / sch / f"{name}.yml"
        if not p.exists():
            continue
        rec = yaml.safe_load(p.read_text())
        if rec.get("grain"):
            skipped += 1
            continue
        # only claim a grain whose columns actually exist in the table
        real = [c for c in cols if c.upper() in {k.upper() for k in schema[table]}]
        if not real:
            continue
        rec["grain"] = real
        p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
        n += 1
    print(f"grain backfilled on {n} tables ({skipped} already had one, "
          f"{len(grains)} available in evidence)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("prefill")
    pf.add_argument("--run", type=Path, required=True)
    pf.add_argument("--workspace", type=Path, required=True)
    pf.set_defaults(fn=cmd_prefill)
    b = sub.add_parser("backfill-grain")
    b.add_argument("--run", type=Path, required=True)
    b.add_argument("--workspace", type=Path, required=True)
    b.set_defaults(fn=cmd_backfill_grain)
    r = sub.add_parser("resolve-docs")
    r.add_argument("--repo", type=Path, required=True)
    r.add_argument("--workspace", type=Path, required=True)
    r.set_defaults(fn=cmd_resolve_docs)
    p = sub.add_parser("packets")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(fn=cmd_packets)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
