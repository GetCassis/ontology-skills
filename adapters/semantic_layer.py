"""Definitions a semantic layer already declares — metrics, joins, column and
table docs — normalized into one shape the run validates against the schema
export before any of it counts as evidence.

Four readers, each checked on public projects:

  tmdl        a Power BI semantic model saved as TMDL (the `definition/` folder
              of a PBIP project's `<name>.SemanticModel`)
  metricflow  dbt MetricFlow, in both specs: top-level `semantic_models:` and
              `metrics:`, or the newer one where `semantic_model:`, entities,
              dimensions and metrics sit on the dbt model itself
  cube        Cube's YAML data model (`cubes:`)
  lookml      LookML views and explores (`.lkml` files)

Anything else can be written by hand in the same shape (adapters/semantic_layer.md):

  {"source": "...",
   "metrics": [{name, expression, original_expression, language, translated,
                table, filters, description, cite}],
   "joins":   [{left, right, on, relationship, cite}],
   "columns": [{table, column, description, enum_values, cite}],
   "tables":  [{table, description, cite}],
   "review":  [{name, original_expression, language, reason, cite}],
   "populations": [{table, rule, cite}],
   "rejects": [{kind, name, reason, cite}]}

A Power BI table is not a warehouse table: it is whatever its Power Query (M)
source returns. A table maps to the warehouse only through that source, and a
measure over a table whose source is a file, a web address or a native query is
rejected with that reason. DAX the kit translates mechanically (an aggregate
over one table's columns, a CALCULATE filter on the same table, DIVIDE) ships
with `translated: true` and its DAX beside it; it is the kit's SQL, not the
owner's, so it never counts as the owner's text. Time intelligence, filters
through a related table and filter-context functions have no single SQL
expression: they go to `review` with their DAX.
"""
import json
import re
from pathlib import Path

import yaml

TI_FUNCS = {
    "SAMEPERIODLASTYEAR", "TOTALYTD", "TOTALQTD", "TOTALMTD", "DATESYTD",
    "DATESQTD", "DATESMTD", "DATEADD", "PARALLELPERIOD", "PREVIOUSYEAR",
    "PREVIOUSQUARTER", "PREVIOUSMONTH", "PREVIOUSDAY", "NEXTYEAR", "NEXTQUARTER",
    "NEXTMONTH", "NEXTDAY", "DATESINPERIOD", "DATESBETWEEN", "OPENINGBALANCEYEAR",
    "CLOSINGBALANCEYEAR", "OPENINGBALANCEQUARTER", "CLOSINGBALANCEQUARTER",
    "OPENINGBALANCEMONTH", "CLOSINGBALANCEMONTH", "STARTOFYEAR", "ENDOFYEAR",
    "STARTOFQUARTER", "ENDOFQUARTER", "STARTOFMONTH", "ENDOFMONTH", "FIRSTDATE",
    "LASTDATE",
}
CONTEXT_FUNCS = {
    "ALL", "ALLEXCEPT", "ALLSELECTED", "REMOVEFILTERS", "KEEPFILTERS",
    "USERELATIONSHIP", "CROSSFILTER", "FILTER", "VALUES", "SELECTEDVALUE",
    "ISFILTERED", "HASONEVALUE", "RELATED", "RELATEDTABLE", "EARLIER",
}
AGGS = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
        "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNT DISTINCT"}
XAGGS = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX"}


class Untranslatable(Exception):
    """A valid definition with no single-table SQL expression: goes to review."""


class Unmapped(Exception):
    """A definition over something that is not a warehouse table: rejected."""


def sql_ident(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else \
        '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------ TMDL text

_NAME = r"'(?:[^']|'')+'|[^\s=']+"
_OBJ = re.compile(rf"^(table|column|measure|partition|expression|relationship)"
                  rf"\s+({_NAME})\s*(?:=\s*(.*))?$")


def _unquote(n: str) -> str:
    n = n.strip()
    return n[1:-1].replace("''", "'") if n.startswith("'") else n


def _depth(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _value(lines, i, first, base):
    """The text after `=` on line i: inline, a ``` fence, or the lines indented
    two levels deeper below it. -> (text, index of the next unread line)."""
    first = first.strip()
    if first.startswith("```"):
        body, j = [], i + 1
        while j < len(lines) and lines[j].strip() != "```":
            body.append(lines[j].strip("\t"))
            j += 1
        return "\n".join(body).strip(), j + 1
    if first:
        return first, i + 1
    body, j = [], i + 1
    while j < len(lines) and (not lines[j].strip() or _depth(lines[j]) >= base + 2):
        body.append(lines[j].strip("\t"))
        j += 1
    return "\n".join(body).strip(), j


def read_tmdl(root: Path) -> dict:
    """-> {"tables": {name: {...}}, "expressions": {name: m}, "relationships": [...]}"""
    tables, exprs, rels = {}, {}, []
    for path in sorted(root.rglob("*.tmdl")):
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        rel = path.relative_to(root).as_posix()
        doc, table, obj, i = [], None, None, 0
        while i < len(lines):
            line, s = lines[i], lines[i].strip()
            d = _depth(line)
            if s.startswith("///"):
                doc.append(s[3:].strip())
                i += 1
                continue
            m = _OBJ.match(s)
            if m and (m.group(1) in ("table", "expression", "relationship") and d == 0
                      or m.group(1) in ("column", "measure", "partition") and d == 1
                      and table is not None):
                kind, name, rest = m.group(1), _unquote(m.group(2)), m.group(3)
                desc = " ".join(x for x in doc if x).strip()
                doc = []
                cite = f"{rel}:{i + 1}"
                if kind == "table":
                    table = tables.setdefault(name, {"name": name, "columns": {},
                                                     "measures": {}, "partitions": [],
                                                     "description": desc, "cite": cite})
                    obj, i = table, i + 1
                elif kind == "expression":
                    text, i = _value(lines, i, rest or "", 0)
                    exprs[name] = re.sub(r"\s+meta\s*\[.*$", "", text, flags=re.S)
                    table = obj = None
                elif kind == "relationship":
                    obj = {"cite": cite}
                    rels.append(obj)
                    table, i = None, i + 1
                elif kind == "column":
                    calc = None
                    if rest is not None:
                        calc, i = _value(lines, i, rest, 1)
                    else:
                        i += 1
                    obj = table["columns"].setdefault(name.lower(), {
                        "name": name, "description": desc, "calculated": calc,
                        "cite": cite})
                elif kind == "measure":
                    text, i = _value(lines, i, rest or "", 1)
                    obj = table["measures"][name] = {"name": name, "dax": text,
                                                     "description": desc, "cite": cite}
                else:  # partition
                    obj = {"kind": (rest or "").strip(), "cite": cite}
                    table["partitions"].append(obj)
                    i += 1
                continue
            doc = []
            if obj is not None and d >= 1 and ":" in s and not s.startswith("annotation"):
                key, _, val = s.partition(":")
                if key in ("sourceColumn", "dataType", "fromColumn", "toColumn",
                           "fromCardinality", "toCardinality", "isActive"):
                    obj[key] = val.strip()
                i += 1
                continue
            sm = re.match(r"^source\s*=\s*(.*)$", s)
            if sm and obj is not None and "kind" in obj:
                obj["source"], i = _value(lines, i, sm.group(1), d)
                continue
            if s.endswith("```") and s.count("```") == 1:  # a fenced block we skip
                i += 1
                while i < len(lines) and lines[i].strip() != "```":
                    i += 1
            i += 1
    return {"tables": tables, "expressions": exprs, "relationships": rels}


def _m_target(m_text: str, exprs: dict, seen=()) -> tuple:
    """The warehouse table a Power Query chain reads. -> ((schema, table) or
    None, reason or None, [the M texts the chain went through])."""
    nav = re.findall(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"(\w+)"\s*\]', m_text)
    schema = next((n for n, k in nav if k.lower() == "schema"), "")
    table = next((n for n, k in nav if k.lower() in ("table", "view")), None)
    if table:
        return (schema, table), None, [m_text]
    # SQL Server, Fabric warehouses, PostgreSQL: Source{[Schema="raw",Item="store"]}
    si = re.search(r'\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]', m_text)
    if si:
        return (si.group(1), si.group(2)), None, [m_text]
    if re.search(r"Value\.NativeQuery|\[\s*Query\s*=", m_text):
        return None, "its source is a native SQL query, not one table", [m_text]
    ref = re.match(r'(?s)\s*let\s+Source\s*=\s*(#"[^"]+"|[A-Za-z_][\w.]*)\s*,', m_text)
    if ref:
        name = ref.group(1)
        name = name[2:-1] if name.startswith('#"') else name
        if name in exprs and name not in seen:
            tgt, why, chain = _m_target(exprs[name], exprs, seen + (name,))
            return tgt, why, [m_text] + chain
    if re.search(r"\b(Csv\.Document|Excel\.Workbook|Web\.Contents|File\.Contents|"
                 r"Json\.Document|SharePoint\.\w+)\b", m_text):
        return None, "its source reads a file or a web address, not a warehouse table", \
            [m_text]
    return None, "its source does not name one warehouse table", [m_text]


def _renames(chain: list) -> dict:
    """new name -> original name, across every RenameColumns step in the chain.
    The chain runs from the table back to its upstream queries, so it is read
    in reverse; the pairs inside one step rename at once (a swap is legal)."""
    out = {}
    for text in reversed(chain):
        for block in re.findall(r"Table\.RenameColumns\([^,]+,\s*\{(.*?)\}\s*\)",
                                text, re.S):
            prev = dict(out)
            for old, new in re.findall(r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\}', block):
                out[new] = prev.get(old, old)
    return out


def _added(chain: list) -> set:
    """Columns the Power Query computes (Table.AddColumn): not in the warehouse."""
    return {n for text in chain
            for n in re.findall(r'Table\.AddColumn\([^,]+,\s*"([^"]+)"', text)}


def _row_filters(chain: list) -> list:
    out = []
    for text in chain:
        for line in text.splitlines():
            m = re.search(r"Table\.SelectRows\([^,]+,\s*each\s+(.*?)\)\s*,?\s*$", line)
            if m:
                out.append(m.group(1).strip())
    return out


# ----------------------------------------------------------------- DAX → SQL

_TOK = re.compile(r"""\s*(?:
    (?P<comment>--[^\n]*|//[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<qtab>'(?:[^']|'')+')
  | (?P<br>\[[^\]]*\])
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<op><>|<=|>=|&&|\|\||[-+*/=<>&(),{}])
  | (?P<id>[A-Za-z_][\w.]*)
  )""", re.X | re.S)


def _tokens(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            raise Untranslatable(f"DAX the kit cannot read near {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "comment":
            out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, text):
        self.t, self.i = _tokens(text), 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise Untranslatable(f"expected {val!r}, found {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        if self.peek()[0] == "id" and self.peek()[1].upper() == "VAR":
            raise Untranslatable("VAR/RETURN blocks are not translated")
        node = self.expr()
        if self.peek()[0] is not None:
            raise Untranslatable(f"unexpected {self.peek()[1]!r}")
        return node

    def expr(self):
        node = self.additive()
        while self.peek()[1] in ("&&", "||"):
            op = self.take()[1]
            node = ("bin", op, node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while self.peek()[1] in ("+", "-", "&", "=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            node = ("bin", op, node, self.term())
        if self.peek()[0] == "id" and self.peek()[1].upper() == "IN":
            self.take()
            self.take("{")
            vals = [self.primary()]
            while self.peek()[1] == ",":
                self.take()
                vals.append(self.primary())
            self.take("}")
            node = ("in", node, vals)
        return node

    def term(self):
        node = self.unary()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            node = ("bin", op, node, self.unary())
        return node

    def unary(self):
        if self.peek()[1] == "-":
            self.take()
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return ("num", val)
        if kind == "str":
            return ("str", val[1:-1].replace('""', '"'))
        if val == "(":
            node = self.expr()
            self.take(")")
            return node
        if kind == "br":
            return ("mref", val[1:-1])
        if kind in ("qtab", "id"):
            name = _unquote(val) if kind == "qtab" else val
            if kind == "id" and val.upper() in ("TRUE", "FALSE") \
                    and self.peek()[1] == "(" and self.peek(1)[1] == ")":
                self.take("(")
                self.take(")")
                return ("bool", val.upper())
            if kind == "id" and self.peek()[1] == "(":
                self.take("(")
                args = []
                if self.peek()[1] != ")":
                    args.append(self.expr())
                    while self.peek()[1] == ",":
                        self.take()
                        args.append(self.expr())
                self.take(")")
                return ("call", val.upper(), args)
            if self.peek()[0] == "br":
                return ("col", name, self.take()[1][1:-1])
            if kind == "id" and val.upper() in ("TRUE", "FALSE"):
                return ("bool", val.upper())
            return ("tbl", name)
        raise Untranslatable(f"unexpected {val!r}")


class _DaxContext:
    def __init__(self, model, tables_map):
        self.model, self.map = model, tables_map
        self.measures = {m["name"].lower(): m for t in model["tables"].values()
                         for m in t["measures"].values()}
        self.cache = {}

    def table(self, pbi_name):
        info = self.map.get(pbi_name.lower())
        if not info:
            raise Unmapped(f"{pbi_name} is not a table of this model")
        if info.get("reject"):
            raise Unmapped(f"{info['name']}: {info['reject']}")
        return info["key"]

    def col(self, pbi_table, pbi_col):
        key = self.table(pbi_table)
        info = self.map[pbi_table.lower()]
        col = info["columns"].get(pbi_col.lower())
        if not col:
            raise Unmapped(f"{pbi_table}[{pbi_col}] is not a column of this model")
        if not col.get("warehouse"):
            raise Unmapped(f"{pbi_table}[{pbi_col}] is computed in the model, not "
                           f"stored in the warehouse")
        return key, col["warehouse"]

    def measure(self, name, stack):
        m = self.measures.get(name.lower())
        if not m:
            raise Untranslatable(f"[{name}] is neither a measure nor a column here")
        if name.lower() in stack:
            raise Untranslatable(f"[{name}] refers to itself")
        if name.lower() not in self.cache:
            node = _Parser(m["dax"]).parse()
            self.cache[name.lower()] = _agg(node, self, stack + (name.lower(),))
        return self.cache[name.lower()]


def _lit(node):
    if node[0] == "str":
        return "'" + node[1].replace("'", "''") + "'"
    if node[0] in ("num", "bool"):
        return node[1]
    if node[0] == "neg" and node[1][0] == "num":
        return "-" + node[1][1]
    return None


def _row(node, ctx, key, tbl):
    k = node[0]
    if k == "col":
        t, c = ctx.col(node[1], node[2])
        if t != key:
            raise Untranslatable(f"reads {node[1]} from inside {tbl}: a related table")
        return sql_ident(c)
    if k == "mref":
        col = ctx.map[tbl.lower()]["columns"].get(node[1].lower())
        if col and col.get("warehouse"):
            return sql_ident(col["warehouse"])
        raise Untranslatable(f"[{node[1]}] inside a row expression")
    if _lit(node) is not None:
        return _lit(node)
    if k == "neg":
        return "-" + _row(node[1], ctx, key, tbl)
    if k == "bin" and node[1] in "+-*/":
        return f"({_row(node[2], ctx, key, tbl)} {node[1]} {_row(node[3], ctx, key, tbl)})"
    raise Untranslatable("a row expression the kit does not translate")


def _filter(node, ctx, key):
    k = node[0]
    if k == "call":
        if node[1] in TI_FUNCS:
            raise Untranslatable(f"time intelligence ({node[1]})")
        raise Untranslatable(f"filter-context function ({node[1]})")
    if k == "bin" and node[1] in ("&&", "||"):
        op = "AND" if node[1] == "&&" else "OR"
        return f"({_filter(node[2], ctx, key)}) {op} ({_filter(node[3], ctx, key)})"
    if k == "bin" and node[1] in ("=", "<>", "<", ">", "<=", ">="):
        col, val = (node[2], node[3]) if node[2][0] == "col" else (node[3], node[2])
        if col[0] == "col" and _lit(val) is not None:
            t, c = ctx.col(col[1], col[2])
            if t != key:
                raise Untranslatable(f"filters through a related table ({col[1]}), "
                                     f"which a one-table metric cannot hold")
            return f"{sql_ident(c)} {node[1]} {_lit(val)}"
    if k == "in" and node[1][0] == "col" and all(_lit(v) for v in node[2]):
        t, c = ctx.col(node[1][1], node[1][2])
        if t != key:
            raise Untranslatable(f"filters through a related table ({node[1][1]}), "
                                 f"which a one-table metric cannot hold")
        return f"{sql_ident(c)} IN ({', '.join(_lit(v) for v in node[2])})"
    raise Untranslatable("a CALCULATE filter the kit does not translate")


def _agg(node, ctx, stack):
    """-> (sql, table key, [filters])"""
    k = node[0]
    if k == "mref":
        return ctx.measure(node[1], stack)
    if k == "call":
        fn, args = node[1], node[2]
        if fn in TI_FUNCS:
            raise Untranslatable(f"time intelligence ({fn})")
        if fn in AGGS and len(args) == 1 and args[0][0] == "col":
            key, c = ctx.col(args[0][1], args[0][2])
            agg = AGGS[fn]
            sql = f"COUNT(DISTINCT {sql_ident(c)})" if agg == "COUNT DISTINCT" \
                else f"{agg}({sql_ident(c)})"
            return sql, key, []
        if fn == "COUNTROWS" and len(args) == 1 and args[0][0] == "tbl":
            return "COUNT(*)", ctx.table(args[0][1]), []
        if fn in XAGGS and len(args) == 2 and args[0][0] == "tbl":
            key = ctx.table(args[0][1])
            return f"{XAGGS[fn]}({_row(args[1], ctx, key, args[0][1])})", key, []
        if fn == "CALCULATE" and args:
            sql, key, flt = _agg(args[0], ctx, stack)
            return sql, key, flt + [_filter(a, ctx, key) for a in args[1:]]
        if fn == "DIVIDE" and len(args) in (2, 3):
            (a, ka, fa), (b, kb, fb) = (_agg(args[0], ctx, stack),
                                        _agg(args[1], ctx, stack))
            if ka != kb:
                raise Untranslatable("divides measures over two tables")
            if fa or fb:
                raise Untranslatable("divides measures that carry different filters")
            return f"{a} / NULLIF({b}, 0)", ka, []
        if fn in CONTEXT_FUNCS:
            raise Untranslatable(f"filter-context function ({fn})")
        raise Untranslatable(f"{fn} is not translated")
    if k == "bin" and node[1] in ("+", "-", "*", "/"):
        sides = []
        for part in (node[2], node[3]):
            lit = _lit(part)
            sides.append((lit, None, []) if lit is not None else _agg(part, ctx, stack))
        keys = {s[1] for s in sides if s[1]}
        if len(keys) != 1:
            raise Untranslatable("combines measures over two tables")
        if any(s[2] for s in sides):
            raise Untranslatable("combines measures that carry filters")
        return f"{sides[0][0]} {node[1]} {sides[1][0]}", keys.pop(), []
    raise Untranslatable("not an aggregate the kit translates")


def from_tmdl(root: Path) -> dict:
    root = Path(root)
    model = read_tmdl(root)
    out = _empty(f"tmdl:{_label(root)}")
    tmap = {}
    for name, t in model["tables"].items():
        part = next((p for p in t["partitions"] if p.get("source")), None)
        info = {"name": name, "columns": {}, "cite": t["cite"]}
        calculated = part is None or part["kind"].startswith("calculated")
        if calculated:
            info["reject"] = "a calculated table, built in the model, not stored in " \
                             "the warehouse"
            chain = []
        else:
            tgt, why, chain = _m_target(part["source"], model["expressions"])
            if tgt:
                info["key"] = f"{tgt[0]}.{tgt[1]}" if tgt[0] else tgt[1]
            else:
                info["reject"] = why
        renamed, added = _renames(chain), _added(chain)
        for c in t["columns"].values():
            src = (c.get("sourceColumn") or "").strip()
            stored = src and not c.get("calculated") and not src.startswith("[") \
                and renamed.get(src, src) not in added
            info["columns"][c["name"].lower()] = {
                "name": c["name"], "warehouse": renamed.get(src, src) if stored else None}
        tmap[name.lower()] = info
        rules = _row_filters(chain)
        if info.get("key"):
            out["populations"] += [{"table": info["key"], "rule": r, "cite": t["cite"]}
                                   for r in rules]
            if t["description"]:
                out["tables"].append({"table": info["key"], "description": t["description"],
                                      "cite": t["cite"]})
            for c in t["columns"].values():
                wc = info["columns"][c["name"].lower()]["warehouse"]
                if wc and c["description"]:
                    out["columns"].append({"table": info["key"], "column": wc,
                                           "description": c["description"],
                                           "cite": c["cite"]})
        elif not calculated:
            out["rejects"].append({
                "kind": "table", "name": name, "cite": t["cite"],
                "reason": info["reject"] + (f"; its M also filters rows: {rules}"
                                            if rules else "")})
    ctx = _DaxContext(model, tmap)
    for t in model["tables"].values():
        for m in t["measures"].values():
            base = {"name": m["name"], "original_expression": m["dax"],
                    "language": "dax", "cite": m["cite"]}
            try:
                # read off the text, so a time function inside a VAR block or a
                # construct the parser refuses still says what it is
                ti = sorted(set(re.findall(r"\b([A-Z]+)\s*\(", m["dax"].upper()))
                            & TI_FUNCS)
                if ti:
                    raise Untranslatable(f"time intelligence ({', '.join(ti)}): no "
                                         f"single SQL expression")
                if re.search(r"\bVAR\b", m["dax"], re.I):
                    raise Untranslatable("VAR/RETURN blocks are not translated")
                sql, key, flt = ctx.measure(m["name"], ())
            except Unmapped as e:
                out["rejects"].append({**base, "kind": "metric", "reason": str(e)})
                continue
            except Untranslatable as e:
                out["review"].append({**base, "reason": str(e)})
                continue
            out["metrics"].append({**base, "expression": sql, "table": key,
                                   "filters": " AND ".join(flt) or None,
                                   "translated": True,
                                   "description": m["description"] or None})
    for r in model["relationships"]:
        ends = []
        for side in ("fromColumn", "toColumn"):
            mm = re.match(rf"^({_NAME})\.({_NAME})$", r.get(side, ""))
            ends.append((_unquote(mm.group(1)), _unquote(mm.group(2))) if mm else None)
        name = f"{r.get('fromColumn')} -> {r.get('toColumn')}"
        try:
            if None in ends:
                raise Unmapped("a relationship the kit cannot read")
            (lk, lc), (rk, rc) = (ctx.col(*ends[0]), ctx.col(*ends[1]))
        except Unmapped as e:
            out["rejects"].append({"kind": "join", "name": name, "reason": str(e),
                                   "cite": r["cite"]})
            continue
        card = {"many": "many", "one": "one"}
        rel = (f"{card.get(r.get('fromCardinality', 'many'), 'many')}_to_"
               f"{card.get(r.get('toCardinality', 'one'), 'one')}")
        out["joins"].append({"left": lk, "right": rk,
                             "on": [f"{lc} = {rc}"], "relationship": rel,
                             "active": r.get("isActive", "true") != "false",
                             "cite": r["cite"]})
    return out


# -------------------------------------------------------- MetricFlow (classic)

_MF_AGG = {"sum": "SUM", "average": "AVG", "avg": "AVG", "min": "MIN", "max": "MAX",
           "count": "COUNT"}


def _mf_line(text: str, name: str) -> int:
    m = re.search(rf"^\s*-\s*name:\s*['\"]?{re.escape(name)}['\"]?\s*$", text, re.M)
    return text[:m.start()].count("\n") + 1 if m else 1


def from_metricflow(root: Path) -> dict:
    root = Path(root)
    out = _empty(f"metricflow:{_label(root)}")
    sms, metrics = {}, []
    for p in sorted(root.rglob("*.yml")) + sorted(root.rglob("*.yaml")):
        if any(part in ("target", "dbt_packages", "node_modules") for part in p.parts):
            continue
        text = p.read_text()
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        rel = p.relative_to(root).as_posix()
        for sm in doc.get("semantic_models") or []:
            sms[sm["name"]] = {**sm, "_cite": f"{rel}:{_mf_line(text, sm['name'])}"}
        for m in doc.get("metrics") or []:
            metrics.append({**m, "_cite": f"{rel}:{_mf_line(text, m['name'])}"})
        # The newer spec: the semantic model sits on the dbt model itself,
        # entities and dimensions on its columns, simple metrics beside them.
        models = doc.get("models")
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict):
                continue
            smc = model.get("semantic_model")
            if not (smc is True or isinstance(smc, dict) and smc.get("enabled", True)):
                continue
            name = (smc.get("name") if isinstance(smc, dict) else None) or model["name"]
            sm = {"name": name, "model": f"ref('{model['name']}')",
                  "description": model.get("description"), "entities": [],
                  "dimensions": [], "measures": [], "_docs": [],
                  "_cite": f"{rel}:{_mf_line(text, model['name'])}"}
            for c in model.get("columns") or []:
                ent, dim = c.get("entity"), c.get("dimension")
                if ent:
                    ent = ent if isinstance(ent, dict) else {"type": ent}
                    sm["entities"].append({"name": ent.get("name") or c["name"],
                                           "type": ent.get("type"), "expr": c["name"]})
                if dim:
                    dim = dim if isinstance(dim, dict) else {"type": dim}
                    sm["dimensions"].append({"name": dim.get("name") or c["name"],
                                             "type": dim.get("type"), "expr": c["name"]})
                if (c.get("description") or "").strip():
                    sm["_docs"].append((c["name"], c["description"]))
            sms[name] = sm
            for m in model.get("metrics") or []:
                entry = {**m, "_cite": f"{rel}:{_mf_line(text, m['name'])}",
                         "_orig": {k: m[k] for k in ("type", "agg", "expr", "filter")
                                   if m.get(k) is not None}}
                if m.get("type", "simple") == "simple":
                    measure = f"__inline__{name}__{m['name']}"
                    sm["measures"].append({"name": measure, "agg": m.get("agg"),
                                           "expr": m["expr"] if m.get("expr") is not None
                                           else m["name"]})
                    entry["type_params"] = {"measure": measure}
                metrics.append(entry)

    def model_table(sm):
        mm = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]", str(sm.get("model") or ""))
        return mm.group(1).upper() if mm else None

    measures, primary = {}, {}
    for sm in sms.values():
        key = model_table(sm)
        if not key:
            out["rejects"].append({"kind": "table", "name": sm["name"], "cite": sm["_cite"],
                                   "reason": "its model is not a ref() the kit can resolve"})
            continue
        if (sm.get("description") or "").strip():
            out["tables"].append({"table": key, "description": " ".join(
                sm["description"].split()), "cite": sm["_cite"]})
        for e in sm.get("entities") or []:
            if e.get("type") in ("primary", "unique", "natural"):
                primary[e["name"]] = (sm, e.get("expr") or e["name"])
        for ms in sm.get("measures") or []:
            measures[ms["name"]] = (sm, ms)
        for d in sm.get("dimensions") or []:
            if (d.get("description") or "").strip() and \
                    re.fullmatch(r"\w+", str(d.get("expr") or d["name"])):
                out["columns"].append({"table": key, "column": str(d.get("expr") or d["name"]),
                                       "description": d["description"].strip(),
                                       "cite": sm["_cite"]})
        for col, desc in sm.get("_docs") or []:
            out["columns"].append({"table": key, "column": col,
                                   "description": " ".join(str(desc).split()),
                                   "cite": sm["_cite"]})
    for sm in sms.values():
        for e in sm.get("entities") or []:
            if e.get("type") != "foreign" or e["name"] not in primary:
                continue
            other, pk = primary[e["name"]]
            if other is sm or not model_table(sm) or not model_table(other):
                continue
            out["joins"].append({"left": model_table(sm), "right": model_table(other),
                                 "on": [f"{e.get('expr') or e['name']} = {pk}"],
                                 "relationship": "many_to_one", "active": True,
                                 "cite": sm["_cite"]})

    def dims_of(sm):
        ents = {e["name"] for e in sm.get("entities") or []}
        return ents, {d["name"]: str(d.get("expr") or d["name"])
                      for d in sm.get("dimensions") or []}

    def measure_sql(mname):
        if mname not in measures:
            raise Untranslatable(f"measure {mname} is not defined in this project")
        sm, ms = measures[mname]
        agg, expr = str(ms.get("agg", "")).lower(), str(ms.get("expr") or ms["name"])
        if agg == "count_distinct":
            return sm, f"COUNT(DISTINCT {expr})"
        if agg == "sum_boolean":
            return sm, f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END)"
        if agg not in _MF_AGG:
            raise Untranslatable(f"aggregation {agg!r} has no single SQL expression")
        return sm, f"{_MF_AGG[agg]}({expr})"

    def mf_filter(flt, sm):
        if not flt:
            return None
        ents, dims = dims_of(sm)

        def repl(mm):
            entity, _, dim = mm.group(1).rpartition("__")
            if entity not in ents or dim not in dims:
                raise Untranslatable(f"filters on {mm.group(1)}, outside this model: "
                                     f"a related table a one-table metric cannot hold")
            return dims[dim]
        sql = re.sub(r"\{\{\s*Dimension\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", repl, flt)
        if "{{" in sql:
            raise Untranslatable("a filter on a time dimension or an entity")
        return " ".join(sql.split())

    def metric_sql(m):
        typ = m.get("type", "simple")
        tp = m.get("type_params") or {}
        if typ == "simple":
            ref = tp.get("measure")
            mname = ref.get("name") if isinstance(ref, dict) else ref
            sm, sql = measure_sql(mname)
            flts = [f for f in (mf_filter(m.get("filter"), sm),
                                mf_filter(ref.get("filter") if isinstance(ref, dict)
                                          else None, sm)) if f]
            return sm, sql, " AND ".join(flts) or None
        if typ == "ratio":
            parts = []
            for side in ("numerator", "denominator"):
                ref = tp.get(side) or m.get(side)
                rname = ref.get("name") if isinstance(ref, dict) else ref
                sub = next((x for x in metrics if x["name"] == rname), None)
                if not sub:
                    raise Untranslatable(f"ratio {side} {rname} is not defined here")
                parts.append(metric_sql(sub))
            (sa, a, fa), (sb, b, fb) = parts
            if sa is not sb:
                raise Untranslatable("a ratio over two tables")
            if fa or fb:
                raise Untranslatable("a ratio whose parts carry filters")
            return sa, f"{a} / NULLIF({b}, 0)", None
        raise Untranslatable(f"a {typ} metric has no single SQL expression")

    for m in metrics:
        base = {"name": m["name"], "language": "metricflow", "cite": m["_cite"],
                "original_expression": json.dumps(
                    m.get("_orig") or {k: m.get(k) for k in (
                        "type", "type_params", "numerator", "denominator", "filter")
                        if m.get(k)}, sort_keys=True)}
        try:
            sm, sql, flt = metric_sql(m)
        except Untranslatable as e:
            out["review"].append({**base, "reason": str(e)})
            continue
        out["metrics"].append({**base, "expression": sql, "table": model_table(sm),
                               "filters": flt, "translated": False,
                               "description": " ".join(str(m.get("description") or "")
                                                       .split()) or None})
    return out


# ---------------------------------------------------------------- Cube (YAML)

_CUBE_AGG = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}
_CUBE_REL = {"many_to_one": "many_to_one", "belongs_to": "many_to_one",
             "belongsTo": "many_to_one", "one_to_many": "one_to_many",
             "has_many": "one_to_many", "hasMany": "one_to_many",
             "one_to_one": "one_to_one", "has_one": "one_to_one", "hasOne": "one_to_one"}


def from_cube(root: Path) -> dict:
    """Cube's YAML data model: one table per cube (`sql_table`), measures
    whose SQL is already SQL, filters, and joins written as SQL conditions."""
    root = Path(root)
    out = _empty(f"cube:{_label(root)}")
    cubes = {}
    for p in sorted(root.rglob("*.yml")) + sorted(root.rglob("*.yaml")):
        if any(part in ("node_modules", ".cubestore") for part in p.parts):
            continue
        text = p.read_text()
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        rel = p.relative_to(root).as_posix()
        for c in doc.get("cubes") or []:
            cubes[c["name"]] = {**c, "_rel": rel, "_text": text,
                                "_cite": f"{rel}:{_mf_line(text, c['name'])}"}

    def table_of(c):
        if c.get("sql_table"):
            return str(c["sql_table"]).strip().strip('"`').upper()
        m = re.fullmatch(r"(?is)\s*select\s+\*\s+from\s+([\w.\"`]+)\s*;?\s*",
                         str(c.get("sql") or ""))
        return m.group(1).replace('"', "").replace("`", "").upper() if m else None

    def local(sql, c):
        """{CUBE}.x and {<own name>}.x become x; another cube's column is a
        related table."""
        def repl(m):
            ref, col = m.group(1), m.group(2)
            if ref in ("CUBE", "TABLE", c["name"]):
                return col
            raise Untranslatable(f"reads {ref}.{col}: a related table a one-table "
                                 f"metric cannot hold")
        sql = re.sub(r"\$?\{(\w+)\}\.(\w+)", repl, str(sql))
        return re.sub(r"\$?\{(?:CUBE|TABLE)\}\.?", "", sql)

    def measure(c, name, stack=()):
        ms = next((m for m in c.get("measures") or [] if m["name"] == name), None)
        if ms is None:
            raise Untranslatable(f"{{{name}}} is not a measure of {c['name']}")
        if name in stack:
            raise Untranslatable(f"{name} refers to itself")
        typ = str(ms.get("type", "")).lower()
        if ms.get("rolling_window") or typ in ("running_total",
                                               "count_distinct_approx"):
            raise Untranslatable(f"a {ms.get('rolling_window') and 'rolling-window' or typ}"
                                 f" measure has no single SQL expression")
        flts = [local(f["sql"], c) for f in ms.get("filters") or [] if f.get("sql")]
        sql = local(ms["sql"], c) if ms.get("sql") is not None else None
        if typ == "count":
            expr = f"COUNT({sql})" if sql else "COUNT(*)"
        elif typ == "count_distinct":
            expr = f"COUNT(DISTINCT {sql})"
        elif typ in _CUBE_AGG:
            expr = f"{_CUBE_AGG[typ]}({sql})"
        elif typ == "number" and sql:
            refs = re.findall(r"\{(\w+)\}", sql)
            for r in refs:
                sub_expr, sub_f = measure(c, r, stack + (name,))
                if sub_f:
                    raise Untranslatable(f"combines measures that carry filters ({r})")
                sql = sql.replace(f"{{{r}}}", f"({sub_expr})" if " " in sub_expr
                                  else sub_expr)
            expr = sql
        else:
            raise Untranslatable(f"a {typ or 'untyped'} measure has no single SQL "
                                 f"expression")
        return expr, flts

    for c in cubes.values():
        key = table_of(c)
        if not key:
            out["rejects"].append({"kind": "table", "name": c["name"], "cite": c["_cite"],
                                   "reason": "its sql is a query, not one table"})
            continue
        if (c.get("description") or "").strip():
            out["tables"].append({"table": key, "description": " ".join(
                c["description"].split()), "cite": c["_cite"]})
        for d in c.get("dimensions") or []:
            col = re.fullmatch(r"\s*(?:\$?\{(?:CUBE|TABLE)\}\.)?(\w+)\s*",
                               str(d.get("sql") or ""))
            if col and (d.get("description") or "").strip():
                out["columns"].append({"table": key, "column": col.group(1),
                                       "description": " ".join(d["description"].split()),
                                       "cite": f"{c['_rel']}:{_mf_line(c['_text'], d['name'])}"})
        for j in c.get("joins") or []:
            other = cubes.get(j["name"])
            m = re.fullmatch(r"\s*\$?\{(\w+)\}\.(\w+)\s*=\s*\$?\{(\w+)\}\.(\w+)\s*",
                             str(j.get("sql") or ""))
            jcite = f"{c['_rel']}:{_mf_line(c['_text'], j['name'])}"
            if not (other and table_of(other) and m):
                out["rejects"].append({"kind": "join", "name": f"{c['name']} -> {j['name']}",
                                       "cite": jcite, "reason": "not one column-equality "
                                       "condition between two table cubes"})
                continue
            sides = {m.group(1): m.group(2), m.group(3): m.group(4)}
            mine = sides.get("CUBE") or sides.get(c["name"])
            theirs = sides.get(j["name"])
            if not (mine and theirs):
                out["rejects"].append({"kind": "join", "name": f"{c['name']} -> {j['name']}",
                                       "cite": jcite, "reason": "the condition does not "
                                       "name both cubes"})
                continue
            out["joins"].append({"left": key, "right": table_of(other),
                                 "on": [f"{mine} = {theirs}"],
                                 "relationship": _CUBE_REL.get(j.get("relationship"),
                                                               "many_to_one"),
                                 "active": True, "cite": jcite})
        for ms in c.get("measures") or []:
            base = {"name": f"{c['name']}.{ms['name']}", "language": "cube",
                    "cite": f"{c['_rel']}:{_mf_line(c['_text'], ms['name'])}",
                    "original_expression": json.dumps(
                        {k: ms.get(k) for k in ("type", "sql", "filters", "rolling_window")
                         if ms.get(k) is not None}, sort_keys=True)}
            try:
                expr, flts = measure(c, ms["name"])
            except Untranslatable as e:
                out["review"].append({**base, "reason": str(e)})
                continue
            out["metrics"].append({**base, "expression": expr, "table": key,
                                   "filters": " AND ".join(f"({f})" if " OR " in f.upper()
                                                           else f for f in flts) or None,
                                   "translated": False,
                                   "description": " ".join(str(ms.get("description") or "")
                                                           .split()) or None})
    return out


# ------------------------------------------------------------------ LookML

_LK_SQL_KEY = re.compile(r"^(sql\w*|html|expression|label_from_parameter)$")


def _lk_parse(text: str, i: int = 0, depth: int = 0):
    """LookML text -> [(key, name, value)]. A value is a nested list for a
    `key: name { ... }` block, the raw text up to `;;` for sql-like keys, a
    string, a list's raw text, or a bare token."""
    out, n = [], len(text)
    while i < n:
        m = re.compile(r"(?:\s+|#[^\n]*)*").match(text, i)
        i = m.end()
        if i >= n:
            break
        if text[i] == "}":
            return out, i + 1
        km = re.compile(r"([A-Za-z_][\w]*)\s*:\s*").match(text, i)
        if not km:  # something the reader does not model: skip the line
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        key, i = km.group(1), km.end()
        if _LK_SQL_KEY.match(key):
            end = text.find(";;", i)
            end = n if end < 0 else end
            out.append((key, None, text[i:end].strip()))
            i = end + 2
            continue
        if i < n and text[i] == '"':
            sm = re.compile(r'"((?:[^"\\]|\\.)*)"').match(text, i)
            out.append((key, None, sm.group(1) if sm else ""))
            i = sm.end() if sm else i + 1
            continue
        if i < n and text[i] == "[":
            level, j = 0, i
            while j < n:
                if text[j] == "[":
                    level += 1
                elif text[j] == "]":
                    level -= 1
                    if level == 0:
                        break
                elif text[j] == '"':
                    j = text.find('"', j + 1)
                    if j < 0:
                        j = n
                j += 1
            out.append((key, None, text[i + 1:j].strip()))
            i = j + 1
            continue
        tm = re.compile(r"([^\s{}]+)?\s*").match(text, i)
        token, i = tm.group(1), tm.end()
        if i < n and text[i] == "{":
            body, i = _lk_parse(text, i + 1, depth + 1)
            out.append((key, token, body))
        else:
            out.append((key, None, token))
    return out, i


def _lk_get(block, key, default=None):
    return next((v for k, _, v in block if k == key), default)


def from_lookml(root: Path) -> dict:
    """LookML views and explores: one table per view (`sql_table_name`, or the
    view's own name), measures whose SQL is SQL, and joins from explores."""
    root = Path(root)
    out = _empty(f"lookml:{_label(root)}")
    views, explores = {}, []
    for p in sorted(root.rglob("*.lkml")):
        text = p.read_text(errors="replace")
        rel = p.relative_to(root).as_posix()
        top, _ = _lk_parse(text)
        for key, name, body in top:
            if key == "view" and isinstance(body, list):
                base = name.lstrip("+")
                v = views.setdefault(base, {"name": base, "fields": {}, "props": {},
                                            "cite": f"{rel}:{_line(text, name)}"})
                for k, fname, val in body:
                    if fname and isinstance(val, list):
                        v["fields"][fname] = {"kind": k, "name": fname,
                                              **{kk: vv for kk, _, vv in val},
                                              "cite": f"{rel}:{_line(text, fname)}"}
                    elif fname is None:
                        v["props"][k] = val
                    elif k == "derived_table":
                        v["props"]["derived_table"] = True
                if any(k == "derived_table" for k, _, _ in body):
                    v["props"]["derived_table"] = True
            elif key == "explore" and isinstance(body, list):
                explores.append((name, body, f"{rel}:{_line(text, name)}"))
    for v in views.values():  # extends: the parent's fields under the child's
        for parent in re.findall(r"\w+", str(v["props"].get("extends") or "")):
            for fname, f in (views.get(parent, {}).get("fields") or {}).items():
                v["fields"].setdefault(fname, f)

    def table_of(v):
        if v["props"].get("derived_table"):
            return None
        t = v["props"].get("sql_table_name") or v["name"]
        parts = [x for x in re.split(r"\.", str(t).strip().strip("`\"").replace("`", "")
                                     .replace('"', "")) if x]
        return ".".join(parts[-2:]).upper() if parts else None

    def dim_sql(v, fname, stack=()):
        f = v["fields"].get(fname)
        if not f or f["kind"] not in ("dimension", "dimension_group"):
            raise Untranslatable(f"{v['name']}.{fname} is not a dimension")
        if fname in stack:
            raise Untranslatable(f"{fname} refers to itself")
        sql = f.get("sql") or f"${{TABLE}}.{fname}"
        return local(v, sql, stack + (fname,))

    def local(v, sql, stack=()):
        def repl(m):
            ref = m.group(1)
            if ref == "TABLE":
                return ""
            if "." in ref:
                view, _, field = ref.partition(".")
                if view in (v["name"], "TABLE"):
                    return dim_sql(v, field, stack)
                raise Untranslatable(f"reads {ref}: another view, a related table a "
                                     f"one-table metric cannot hold")
            return dim_sql(v, ref, stack)
        sql = re.sub(r"\$\{TABLE\}\.", "", str(sql))
        return re.sub(r"\$\{([\w.]+)\}", repl, sql).strip()

    def lk_filter(v, spec):
        """Looker's filter expressions on a dimension: "value", "-value",
        "a,b", "yes"/"no", and comparisons on numbers."""
        conds = []
        for fname, val in re.findall(r'([\w.]+)\s*:\s*"((?:[^"\\]|\\.)*)"', spec):
            view, _, field = fname.rpartition(".")
            if view and view != v["name"]:
                raise Untranslatable(f"filters on {fname}: another view, a related "
                                     f"table a one-table metric cannot hold")
            fname = field
            f = v["fields"].get(fname)
            col = dim_sql(v, fname)
            typ = (f or {}).get("type")
            if typ == "yesno":
                if val.lower() not in ("yes", "no"):
                    raise Untranslatable(f"a yes/no filter on {fname} with {val!r}")
                conds.append(f"({col})" if val.lower() == "yes" else f"NOT ({col})")
            elif re.fullmatch(r"(<=|>=|<|>|=)\s*-?\d+(\.\d+)?", val.strip()):
                conds.append(f"{col} {val.strip()}")
            elif re.fullmatch(r"-?[\w ]+(,\s*[\w ]+)*", val) and "%" not in val:
                neg = val.startswith("-")
                vals = [x.strip() for x in val.lstrip("-").split(",")]
                lits = ", ".join("'" + x.replace("'", "''") + "'" for x in vals)
                op = ("NOT IN" if neg else "IN") if len(vals) > 1 else \
                    ("<>" if neg else "=")
                conds.append(f"{col} {op} ({lits})" if len(vals) > 1
                             else f"{col} {op} {lits}")
            else:
                raise Untranslatable(f"a Looker filter expression ({val!r}) the kit "
                                     f"does not translate")
        return conds

    def measure(v, fname, stack=()):
        f = v["fields"][fname]
        typ = str(f.get("type", "count")).lower()
        if fname in stack:
            raise Untranslatable(f"{fname} refers to itself")
        flts = lk_filter(v, f["filters"]) if f.get("filters") else []
        if typ == "number":
            sql = str(f.get("sql") or "")
            for ref in re.findall(r"\$\{(\w+)\}", sql):
                g = v["fields"].get(ref)
                if g and g["kind"] == "measure":
                    sub, sub_f = measure(v, ref, stack + (fname,))
                    if sub_f:
                        raise Untranslatable(f"combines measures that carry filters ({ref})")
                    sql = sql.replace(f"${{{ref}}}", f"({sub})")
            return local(v, sql), flts
        sql = local(v, f["sql"]) if f.get("sql") else None
        if typ == "count":
            return (f"COUNT({sql})" if sql else "COUNT(*)"), flts
        if typ == "count_distinct" and sql:
            return f"COUNT(DISTINCT {sql})", flts
        agg = {"sum": "SUM", "average": "AVG", "min": "MIN", "max": "MAX"}.get(typ)
        if agg and sql:
            return f"{agg}({sql})", flts
        raise Untranslatable(f"a {typ} measure has no single SQL expression")

    for v in views.values():
        key = table_of(v)
        measures = [f for f in v["fields"].values() if f["kind"] == "measure"]
        if not key:
            if measures or v["fields"]:
                out["rejects"].append({"kind": "table", "name": v["name"], "cite": v["cite"],
                                       "reason": "a derived table built in Looker, not "
                                                 "one warehouse table"})
            continue
        for f in v["fields"].values():
            m = re.fullmatch(r"\s*\$\{TABLE\}\.(\w+)\s*", str(f.get("sql") or ""))
            if f["kind"] == "dimension" and m and (f.get("description") or "").strip():
                out["columns"].append({"table": key, "column": m.group(1),
                                       "description": " ".join(f["description"].split()),
                                       "cite": f["cite"]})
        for f in measures:
            base = {"name": f"{v['name']}.{f['name']}", "language": "lookml",
                    "cite": f["cite"], "original_expression": json.dumps(
                        {k: f.get(k) for k in ("type", "sql", "filters") if f.get(k)},
                        sort_keys=True)}
            try:
                expr, flts = measure(v, f["name"])
            except Untranslatable as e:
                out["review"].append({**base, "reason": str(e)})
                continue
            out["metrics"].append({**base, "expression": expr, "table": key,
                                   "filters": " AND ".join(flts) or None,
                                   "translated": False,
                                   "description": " ".join(str(f.get("description") or "")
                                                           .split()) or None})
    seen = set()
    for ename, body, cite in explores:
        alias = {ename: _lk_get(body, "from") or _lk_get(body, "view_name") or ename}
        joins = [(n, b) for k, n, b in body if k == "join" and isinstance(b, list)]
        for jname, jb in joins:
            alias[jname] = _lk_get(jb, "from") or jname
        for jname, jb in joins:
            on = str(_lk_get(jb, "sql_on") or "")
            m = re.fullmatch(r"\s*\$\{(\w+)\.(\w+)\}\s*=\s*\$\{(\w+)\.(\w+)\}\s*", on)
            name = f"{ename} joins {jname}"
            try:
                if not m:
                    raise Untranslatable("not one field equality")
                (la, lf, ra, rf) = m.groups()
                lv, rv = views.get(alias.get(la, la)), views.get(alias.get(ra, ra))
                if not (lv and rv and table_of(lv) and table_of(rv)):
                    raise Untranslatable("a side is not a warehouse-table view")
                lc, rc = dim_sql(lv, lf), dim_sql(rv, rf)
                if not (re.fullmatch(r"\w+", lc) and re.fullmatch(r"\w+", rc)):
                    raise Untranslatable("a side is computed, not a column")
            except Untranslatable as e:
                out["rejects"].append({"kind": "join", "name": name, "cite": cite,
                                       "reason": str(e)})
                continue
            pair = (table_of(lv), table_of(rv), lc, rc)
            if pair in seen:
                continue
            seen.add(pair)
            out["joins"].append({"left": pair[0], "right": pair[1], "on": [f"{lc} = {rc}"],
                                 "relationship": str(_lk_get(jb, "relationship")
                                                     or "many_to_one"),
                                 "active": True, "cite": cite})
    return out


def _line(text: str, name) -> int:
    m = re.search(rf":\s*\+?{re.escape(str(name).lstrip('+'))}\s*\{{", text)
    return text[:m.start()].count("\n") + 1 if m else 1


# ------------------------------------------------------------ load, validate

def _label(root: Path) -> str:
    """A PBIP model's folder is `<name>.SemanticModel/definition`: name it by
    the model, not by `definition`."""
    return root.parent.name if root.name == "definition" else root.name


def _empty(source: str) -> dict:
    return {"source": source, "metrics": [], "joins": [], "columns": [], "tables": [],
            "review": [], "populations": [], "rejects": []}


def load(path: Path) -> dict:
    """A declared-definitions file, or a folder one of the readers recognizes."""
    path = Path(path)
    if path.is_file():
        doc = json.loads(path.read_text())
        base = _empty(doc.get("source") or f"file:{path.name}")
        base.update({k: v for k, v in doc.items() if k in base})
        return base
    if any(path.rglob("*.tmdl")):
        return from_tmdl(path)
    if any(path.rglob("*.lkml")):
        return from_lookml(path)
    ymls = list(path.rglob("*.yml")) + list(path.rglob("*.yaml"))
    for p in ymls:
        if re.search(r"^cubes\s*:", p.read_text(errors="replace"), re.M):
            return from_cube(path)
    for p in ymls:
        if re.search(r"^semantic_models\s*:|^\s+semantic_model\s*:",
                     p.read_text(errors="replace"), re.M):
            return from_metricflow(path)
    raise RuntimeError(f"{path}: neither a declared-definitions file, a TMDL folder, "
                       f"a MetricFlow project, a Cube data model, nor a LookML project")


def _resolve(key, schema):
    if not key:
        return None
    k = str(key).upper()
    if k in schema:
        return k
    hits = [t for t in schema if t.split(".", 1)[-1] == k.split(".")[-1]]
    return hits[0] if len(hits) == 1 else None


def validate(declared: dict, schema: dict, dialect=None) -> dict:
    """Every table and column against the schema export, every expression and
    filter through sqlglot. Survivors keep the schema's key; the rest move to
    `rejects` with the reason, never dropped."""
    import sqlglot
    from sqlglot import exp
    out = {**declared, "metrics": [], "joins": [], "columns": [], "tables": [],
           "populations": [], "rejects": list(declared.get("rejects") or [])}

    def reject(kind, item, reason):
        name = (f"{item.get('left')} -> {item.get('right')}" if kind == "join"
                else item.get("name") or item.get("column") or item.get("table"))
        out["rejects"].append({"kind": kind, "name": name, "reason": reason,
                               "cite": item.get("cite")})

    for m in declared.get("metrics") or []:
        key = _resolve(m.get("table"), schema)
        if not key:
            reject("metric", m, f"table {m.get('table')} is not in the schema export"
                   if m.get("table") else "names no table")
            continue
        sql = f"SELECT {m['expression']} FROM t" + (f" WHERE {m['filters']}"
                                                    if m.get("filters") else "")
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError as e:
            reject("metric", m, f"the expression does not parse: {str(e).splitlines()[0]}")
            continue
        cols = {c.name.upper() for c in tree.find_all(exp.Column)}
        missing = sorted(c for c in cols if c not in schema[key])
        if missing:
            reject("metric", m, f"{', '.join(missing)} not in {key}")
            continue
        out["metrics"].append({**m, "table": key})
    for j in declared.get("joins") or []:
        lk, rk = _resolve(j.get("left"), schema), _resolve(j.get("right"), schema)
        if not (lk and rk):
            reject("join", j, f"{j.get('left') if not lk else j.get('right')} is not "
                              f"in the schema export")
            continue
        bad = []
        for pair in j.get("on") or []:
            a, _, b = str(pair).partition("=")
            if a.strip().upper() not in schema[lk]:
                bad.append(f"{a.strip()} not in {lk}")
            if b.strip().upper() not in schema[rk]:
                bad.append(f"{b.strip()} not in {rk}")
        if bad or not j.get("on"):
            reject("join", j, "; ".join(bad) or "no column pair")
            continue
        out["joins"].append({**j, "left": lk, "right": rk})
    for c in declared.get("columns") or []:
        key = _resolve(c.get("table"), schema)
        if not key or str(c.get("column", "")).upper() not in schema[key]:
            reject("column", c, f"{c.get('table')}.{c.get('column')} is not in the "
                                f"schema export")
            continue
        vals = c.get("enum_values")
        if vals is not None and not (isinstance(vals, list) and vals and all(
                isinstance(v, (str, int, float)) and not isinstance(v, bool)
                and str(v) != "" for v in vals)):
            reject("column", c, "enum_values must be a list of non-empty text values")
            continue
        out["columns"].append({**c, "table": key, "column": str(c["column"]).upper()})
    for kind in ("tables", "populations"):
        for t in declared.get(kind) or []:
            key = _resolve(t.get("table"), schema)
            if not key:
                reject(kind[:-1], t, f"{t.get('table')} is not in the schema export")
                continue
            out[kind].append({**t, "table": key})
    return out
