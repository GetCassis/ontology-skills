#!/usr/bin/env python3
"""
Deterministic inventory builder for the dbt-agent-readiness skill.

Extracts structured metadata from YAML and SQL files without LLM interpretation.
Produces JSON matching the dbt-agent-readiness inventory schema.

Usage:  python3 inventory.py /path/to/dbt/project
Output: JSON to stdout. Exit 0 on success, 1 on error (JSON error on stdout).
"""

import sys
import re
import json
from pathlib import Path
from collections import defaultdict

try:
    import yaml
except ImportError:
    json.dump({
        "error": "pyyaml_not_installed",
        "message": (
            "pyyaml is required for the deterministic inventory script. "
            "Without it, the skill falls back to LLM-based inventory — "
            "slower, less accurate, no concept index / review queue. "
            "Install with: `pip install pyyaml` (or `pip3 install pyyaml`, "
            "or `brew install pyyaml` on macOS). Then re-run the audit."
        ),
        "degraded_fallback_available": True,
    }, sys.stdout)
    sys.exit(1)

try:
    import sqlglot  # noqa: F401
except ImportError:
    json.dump({
        "error": "sqlglot_not_installed",
        "message": (
            "sqlglot is required for dialect-aware SQL parsing "
            "(BigQuery, Snowflake, DuckDB, Redshift, Postgres). "
            "Without it, the skill cannot produce a reliable inventory. "
            "Install with: `pip install -r requirements.txt` "
            "(or `pip install 'sqlglot>=30.0,<31.0'`). Then re-run the audit."
        ),
        "degraded_fallback_available": True,
    }, sys.stdout)
    sys.exit(1)


# ── Constants ────────────────────────────────────────────────────────────────

EXCLUDE_DIRS = frozenset(
    ['dbt_packages', 'target', '.git', 'node_modules', 'venv', '.venv'])

PLACEHOLDER_WORDS = ['todo', 'tbd', 'doc pour', 'fixme', 'placeholder',
                     'to be documented', 'to document', 'à documenter']

# Descriptions that convey only technical-mechanical meaning (no business
# concept). Useful for keys/FKs especially — "Primary key" tells an agent
# nothing about what the entity is.
GENERIC_TECH_DESCS = frozenset([
    'primary key', 'pk', 'foreign key', 'fk', 'unique identifier',
    'identifier', 'id', 'index', 'row id', 'record id', 'auto-increment',
    'surrogate key', 'natural key', 'unique id',
])

CATEGORICAL_RE = re.compile(
    r'(?:^|_)(status|type|category|tier|method|mode|state|level|kind|reason|priority)$',
    re.IGNORECASE)

GRAIN_RE = re.compile(
    r'(one (?:row|record|entry|line) per|each (?:row|record) represents|grain[:\s]|unique on)\s+(.+?)(?:[.\n,;]|$)',
    re.IGNORECASE)

DOC_BLOCK_RE = re.compile(
    r'\{%[-\s]*docs\s+(\w+)\s*[-\s]*%\}(.*?)\{%[-\s]*enddocs\s*[-\s]*%\}', re.DOTALL)

DOC_REF_RE = re.compile(r"""\{\{\s*doc\s*\(\s*['"](\w+)['"]\s*\)\s*\}\}""")

# Matches single-arg `ref('model')` and two-arg `ref('project', 'model')`
# forms. Group 1 = first arg (model or project). Group 2 = second arg
# (model, present only on cross-project / dbt-mesh refs) or None.
REF_RE = re.compile(
    r"""ref\s*\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"]\s*)?\)""")
SOURCE_RE = re.compile(r"""source\s*\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""")
CONFIG_MAT_RE = re.compile(r"""config\s*\([^)]*materialized\s*=\s*['"](\w+)['"]""")

JINJA_EXPR_RE = re.compile(r'\{\{.*?\}\}', re.DOTALL)
JINJA_BLOCK_RE = re.compile(r'\{%.*?%\}', re.DOTALL)
JINJA_COMMENT_RE = re.compile(r'\{#.*?#\}', re.DOTALL)
SQL_LINE_COMMENT_RE = re.compile(r'--.*$', re.MULTILINE)
SQL_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)

LAYER_RULES = [
    (re.compile(r'(?:^|/)staging/'),       'staging'),
    (re.compile(r'(?:^|/)intermediate/'),  'intermediate'),
    (re.compile(r'(?:^|/)prep/'),          'intermediate'),
    (re.compile(r'(?:^|/)base/'),          'intermediate'),
    (re.compile(r'(?:^|/)marts/'),         'core'),
    (re.compile(r'(?:^|/)core/'),          'core'),
    (re.compile(r'(?:^|/)presentation/'),  'core'),
    (re.compile(r'(?:^|/)reporting/'),     'core'),
    (re.compile(r'(?:^|/)reference/'),     'reference'),
    (re.compile(r'(?:^|/)snapshots?/'),    'other'),
    (re.compile(r'(?:^|/)utils?/'),        'other'),
]

# ── SQL snippet extraction patterns ──────────────────────────────────────────
# All operate on Jinja-stripped SQL except IS_INCREMENTAL_RE (needs raw Jinja).

SNIPPET_MAX = 500  # truncate individual snippets

IS_INCREMENTAL_RE = re.compile(
    r'\{%-?\s*if\s+is_incremental\(\)\s*-?%\}(.*?)\{%-?\s*endif\s*-?%\}',
    re.DOTALL | re.IGNORECASE)

# ── Concept index ────────────────────────────────────────────────────────────

NOISE_STEMS = frozenset([
    'id', 'name', 'date', 'created', 'updated', 'deleted', 'is', 'has',
    'was', 'key', 'row', 'count', 'type', 'day', 'month', 'year',
    'timestamp', 'at', 'by', 'source', 'raw', 'value', 'description',
    'label', 'code', 'number', 'index', 'flag', 'note', 'comment', 'path',
    'url',
])

CONCEPT_SUFFIXES = re.compile(
    r'(_id|_at|_date|_timestamp|_count|_amount|_rate|_pct|_flag|_type'
    r'|_status|_name|_code|_key|_in_euros|_eur|_in_eur|_in_wh|_in_mw'
    r'|_in_kg|_in_degree|_in_minutes)$')

CONCEPT_PREFIXES = re.compile(r'^(is_|has_|was_|total_|nb_|num_|n_)')

# Temporal suffix conventions (for drift detection)
TEMPORAL_SUFFIX_RE = re.compile(r'_(at|date|timestamp|ts|time|datetime|on)$')

# Boolean prefix conventions (for drift detection)
BOOLEAN_PREFIX_RE = re.compile(r'^(is|has|was|can|should|will|does|did|are)_')

# Mart prefix conventions
MART_PREFIX_RE = re.compile(r'^(fct|fact|dim|rpt|report|mart|agg)_')

# Unit / currency / measurement suffixes for unit_variants catalog.
# Order matters in UNIT_SUFFIX_RE: longer variants must come first so `_in_euros`
# strips before `_euros`.
UNIT_SUFFIX_GROUPS = {
    'currency': ['_in_euros', '_in_eur', '_euros', '_eur', '_in_usd', '_usd',
                 '_in_gbp', '_gbp'],
    'energy':   ['_in_mwh', '_in_kwh', '_in_wh', '_in_mw', '_in_kw',
                 '_mwh', '_kwh', '_wh', '_mw', '_kw'],
    'duration': ['_in_ms', '_in_sec', '_in_seconds', '_in_minutes',
                 '_in_hours', '_ms', '_sec', '_seconds', '_minutes', '_hours'],
    'mass':     ['_in_kg', '_in_g', '_kg', '_g'],
    'temperature': ['_in_degree', '_in_celsius', '_celsius', '_fahrenheit'],
}
_UNIT_ALL = [(grp, sfx) for grp, lst in UNIT_SUFFIX_GROUPS.items() for sfx in lst]
UNIT_SUFFIX_RE = re.compile(
    r'(' + '|'.join(re.escape(s) for _, s in _UNIT_ALL) + r')$')

# Boolean-semantic naming patterns that commonly occur without the is_/has_
# prefix (adjectives, past participles, modal-style starts). Conservative —
# only flags names that almost certainly denote a boolean.
UNPREFIXED_BOOL_RE = re.compile(
    r'^(auto|force|allow|enable|disable|skip|block|hide|show|always|never)_'
    r'|(_disabled|_enabled|_closed|_completed|_completed_at|_active|_valid'
    r'|_verified|_required|_needed|_inconsistent|_absent|_present|_missing'
    r'|_eligible|_authorized|_deleted|_archived|_expired|_confirmed'
    r'|_approved|_rejected|_subscribed)$',
    re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def find_files(base, pattern):
    """Recursively find files, excluding build/vendor dirs."""
    return sorted(f for f in base.rglob(pattern)
                  if not EXCLUDE_DIRS.intersection(f.parts))


def trunc(text, n=200):
    return text[:n] if text else None


# ── Project config ───────────────────────────────────────────────────────────

# Map of dbt adapter type strings to sqlglot dialect identifiers. Unknown
# adapters are returned as None so callers can fall back to ANSI parsing.
_DIALECT_MAP = {
    'bigquery': 'bigquery',
    'snowflake': 'snowflake',
    'duckdb': 'duckdb',
    'redshift': 'redshift',
    'postgres': 'postgres',
    'postgresql': 'postgres',
}


def _detect_dialect(project_path):
    """Detect the sqlglot dialect for a dbt project.

    Reads `dbt_project.yml` for `profile:`, then looks up that profile's
    active target `type` in a profiles.yml (project-local first, then
    `~/.dbt/profiles.yml`). Returns one of the mapped dialect strings, or
    None if unresolvable. Never raises.
    """
    try:
        project_path = Path(project_path)
        pj = project_path / 'dbt_project.yml'
        if not pj.exists():
            return None
        with open(pj, encoding='utf-8') as f:
            pcfg = yaml.safe_load(f) or {}
        profile_name = pcfg.get('profile')
        if not profile_name:
            return None

        candidates = [
            project_path / 'profiles.yml',
            Path.home() / '.dbt' / 'profiles.yml',
        ]
        profiles = None
        for p in candidates:
            try:
                if p.is_file():
                    with open(p, encoding='utf-8') as f:
                        profiles = yaml.safe_load(f) or {}
                    break
            except Exception:
                continue
        if not profiles:
            return None

        pblock = profiles.get(profile_name)
        if not isinstance(pblock, dict):
            return None
        outputs = pblock.get('outputs') or {}
        if not isinstance(outputs, dict) or not outputs:
            return None
        target = pblock.get('target')
        chosen = outputs.get(target) if target else None
        if not isinstance(chosen, dict):
            # Fall back to first output block
            chosen = next(iter(outputs.values()), None)
        if not isinstance(chosen, dict):
            return None
        adapter = str(chosen.get('type') or '').lower().strip()
        return _DIALECT_MAP.get(adapter)
    except Exception:
        return None


def parse_project_config(project_path):
    yml = project_path / 'dbt_project.yml'
    if not yml.exists():
        return None
    with open(yml, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    model_paths = cfg.get('model-paths', cfg.get('source-paths', ['models']))
    seed_paths = cfg.get('seed-paths', ['seeds'])
    snapshot_paths = cfg.get('snapshot-paths', ['snapshots'])

    global_warn = False
    for key in ('data_tests', 'tests'):
        block = cfg.get(key, {})
        if isinstance(block, dict) and block.get('+severity') == 'warn':
            global_warn = True
            break

    return {
        'project_name': cfg.get('name', 'unknown'),
        'model_paths': [project_path / p for p in model_paths],
        'seed_paths': [project_path / p for p in seed_paths],
        'snapshot_paths': [project_path / p for p in snapshot_paths],
        'global_severity_warn': global_warn,
        'raw': cfg,
    }


# ── Doc blocks ───────────────────────────────────────────────────────────────

def build_doc_lookup(project_path):
    blocks = {}
    for md in find_files(project_path, '*.md'):
        try:
            text = md.read_text(encoding='utf-8')
        except Exception:
            continue
        for m in DOC_BLOCK_RE.finditer(text):
            blocks[m.group(1)] = m.group(2).strip()
    return blocks


def resolve_desc(raw, doc_blocks):
    if not raw:
        return raw
    m = DOC_REF_RE.match(raw.strip())
    return doc_blocks.get(m.group(1), raw) if m else raw


# ── Description quality ──────────────────────────────────────────────────────

def classify_desc(text, name=None):
    """Returns none | empty | placeholder | restates_name | good."""
    if text is None:
        return 'none'
    t = text.strip()
    if not t:
        return 'empty'
    tl = t.lower()
    if any(p in tl for p in PLACEHOLDER_WORDS):
        return 'placeholder'
    if name:
        simple = name.replace('_', ' ').lower()
        variants = {name.lower(), simple, f'the {simple}'}
        if tl.rstrip('.') in {v.rstrip('.') for v in variants}:
            return 'restates_name'
    if len(t) < 10:
        return 'restates_name'
    return 'good'


def detect_grain(text):
    if not text:
        return False, None
    m = GRAIN_RE.search(text)
    return (True, m.group(0).strip().rstrip('.,;')) if m else (False, None)


# ── Layer classification ─────────────────────────────────────────────────────

def classify_layer(name, path_str):
    p = path_str.lower()
    for regex, layer in LAYER_RULES:
        if regex.search(p):
            return layer
    if name.startswith('stg_'):
        return 'staging'
    if any(name.startswith(pfx) for pfx in ('int_', 'prep_', 'base_')):
        return 'intermediate'
    if any(name.startswith(pfx) for pfx in ('dim_', 'fct_', 'fact_')):
        return 'core'
    return 'other'


# ── Test parsing ─────────────────────────────────────────────────────────────

def parse_test(entry):
    """Normalize one test entry to a string label."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for name, cfg in entry.items():
            cfg = cfg or {}
            if 'relationships' in name:
                to_raw = cfg.get('to', '')
                field = cfg.get('field', '')
                rm = REF_RE.search(str(to_raw))
                if rm:
                    target = rm.group(2) or rm.group(1)
                else:
                    target = str(to_raw)
                return f'relationships:{target}.{field}'
            if 'accepted_values' in name:
                vals = cfg.get('values', [])
                return f'accepted_values:{",".join(str(v) for v in vals)}'
            return name
    return str(entry)


def _extract_unique_combinations(model_tests):
    """From a model-level data_tests/tests list, return the column tuples a
    model declares unique-as-a-combination.

    Recognizes `dbt_utils.unique_combination_of_columns` (and the older
    `unique_combination` name), namespaced or not. The column list lives
    under the test config either directly as `combination_of_columns`
    (classic dbt_utils) or nested under `arguments:` (newer dbt test
    argument syntax). YAML anchors/aliases (`tests: *anchor`) are already
    expanded by `yaml.safe_load`, so the merged list reaches us intact.

    Returns a list of frozensets of lowercased column names.
    """
    combos = []
    for t in model_tests or []:
        if not isinstance(t, dict):
            continue
        for name, cfg in t.items():
            base = str(name).split('.')[-1].strip().lower()
            if base not in ('unique_combination_of_columns',
                            'unique_combination'):
                continue
            cfg = cfg or {}
            cols = None
            if isinstance(cfg, dict):
                args = cfg.get('arguments')
                if isinstance(args, dict) and args.get('combination_of_columns'):
                    cols = args.get('combination_of_columns')
                else:
                    cols = cfg.get('combination_of_columns')
            if isinstance(cols, list) and cols:
                norm = frozenset(str(c).lower() for c in cols if c)
                if norm:
                    combos.append(norm)
    return combos


# ── YAML parsing ─────────────────────────────────────────────────────────────

def parse_yaml_files(yaml_files, doc_blocks):
    models = {}          # name -> dict
    columns = []         # flat list
    sources = []
    semantic_models = []
    metrics = []
    saved_queries = []
    exposures = []

    for yf in yaml_files:
        try:
            data = yaml.safe_load(yf.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        fpath = str(yf)

        # ── models ──
        for m in (data.get('models') or []):
            if not isinstance(m, dict) or not m.get('name'):
                continue
            name = m['name']
            desc = resolve_desc(m.get('description'), doc_blocks)
            grain_ok, grain_stmt = detect_grain(desc or '')
            mat = 'unknown'
            cfg = m.get('config') or {}
            if isinstance(cfg, dict):
                mat = cfg.get('materialized', 'unknown')

            model_cols = []
            col_with_desc = 0
            has_pk = False
            pk_col = None

            for c in (m.get('columns') or []):
                if not isinstance(c, dict) or not c.get('name'):
                    continue
                cname = c['name']
                cdesc = resolve_desc(c.get('description'), doc_blocks)
                tests = [parse_test(t) for t in
                         (c.get('data_tests') or c.get('tests') or [])]
                has_d = bool(cdesc and cdesc.strip())
                if has_d:
                    col_with_desc += 1
                model_cols.append({
                    'model': name, 'column': cname,
                    'has_description': has_d,
                    'description_text': trunc(cdesc),
                    'tests': tests,
                })
                tnames = {t.split(':')[0] for t in tests}
                if 'unique' in tnames and 'not_null' in tnames:
                    has_pk = True
                    pk_col = cname

            # Model-level tests (anchors already expanded by safe_load).
            # Capture tuple-uniqueness guarantees declared via
            # dbt_utils.unique_combination_of_columns.
            model_tests = m.get('data_tests') or m.get('tests') or []
            unique_combos = [sorted(c) for c in
                             _extract_unique_combinations(model_tests)]

            columns.extend(model_cols)
            models[name] = {
                'name': name, 'yaml_path': fpath, 'sql_path': None,
                'layer': None,
                'has_description': bool(desc and desc.strip()),
                'description_quality': classify_desc(desc, name),
                'description_text': trunc(desc),
                'grain_declared': grain_ok, 'grain_statement': grain_stmt,
                'column_count_yaml': len(model_cols),
                'column_count_sql': None,
                'columns_with_descriptions': col_with_desc,
                'has_pk_test': has_pk, 'pk_column': pk_col,
                'unique_combinations': unique_combos,
                'inbound_refs': 0, 'outbound_refs': [],
                'has_semantic_model': False,
                'materialization': mat,
            }

        # ── sources ──
        for s in (data.get('sources') or []):
            if not isinstance(s, dict):
                continue
            sname = s.get('name', '')
            for tbl in (s.get('tables') or []):
                if not isinstance(tbl, dict):
                    continue
                tdesc = tbl.get('description')
                sources.append({
                    'source_name': sname,
                    'table_name': tbl.get('name', ''),
                    'has_description': bool(tdesc and str(tdesc).strip()),
                    'has_freshness': bool(tbl.get('freshness') or s.get('freshness')),
                    'yaml_path': fpath,
                })

        # ── semantic_models ──
        for sm in (data.get('semantic_models') or []):
            if not isinstance(sm, dict):
                continue
            ref_raw = sm.get('model', '')
            rm = re.search(r"""ref\s*\(\s*['"]([^'"]+)""", str(ref_raw))
            model_ref = rm.group(1) if rm else str(ref_raw)
            if model_ref in models:
                models[model_ref]['has_semantic_model'] = True
            sm_desc = resolve_desc(sm.get('description'), doc_blocks)
            # Check semantic model desc for grain too
            if model_ref in models and not models[model_ref]['grain_declared']:
                gd, gs = detect_grain(sm_desc or '')
                if gd:
                    models[model_ref]['grain_declared'] = True
                    models[model_ref]['grain_statement'] = gs

            entities = [{'name': e.get('name', ''), 'type': e.get('type', ''),
                         'expr': e.get('expr')}
                        for e in (sm.get('entities') or []) if isinstance(e, dict)]
            measures = []
            for ms in (sm.get('measures') or []):
                if not isinstance(ms, dict):
                    continue
                mdesc = resolve_desc(ms.get('description'), doc_blocks)
                measures.append({
                    'name': ms.get('name', ''), 'agg': ms.get('agg', ''),
                    'expr': ms.get('expr'),
                    'has_description': bool(mdesc and mdesc.strip()),
                    'description_text': trunc(mdesc),
                })
            dimensions = [{'name': d.get('name', ''), 'type': d.get('type', ''),
                           'expr': d.get('expr')}
                          for d in (sm.get('dimensions') or []) if isinstance(d, dict)]
            semantic_models.append({
                'name': sm.get('name', ''), 'model_ref': model_ref,
                'has_description': bool(sm_desc and sm_desc.strip()),
                'entities': entities, 'measures': measures,
                'dimensions': dimensions,
            })

        # ── metrics ──
        for mt in (data.get('metrics') or []):
            if not isinstance(mt, dict):
                continue
            tp = mt.get('type_params') or {}
            mref = tp.get('measure', {})
            if isinstance(mref, dict):
                mrefs = [mref.get('name', '')]
            elif isinstance(mref, str):
                mrefs = [mref]
            else:
                mrefs = []
            if not mrefs:
                for im in (tp.get('input_measures') or []):
                    mrefs.append(im.get('name', '') if isinstance(im, dict) else str(im))
            metrics.append({
                'name': mt.get('name', ''), 'type': mt.get('type', ''),
                'measure_refs': mrefs,
                'has_description': bool(mt.get('description')),
                'has_filter': bool(mt.get('filter')),
            })

        # ── saved_queries ──
        for sq in (data.get('saved_queries') or []):
            if not isinstance(sq, dict):
                continue
            qm = (sq.get('query_params') or {}).get('metrics', [])
            saved_queries.append({
                'name': sq.get('name', ''),
                'metric_refs': qm if isinstance(qm, list) else [],
            })

        # ── exposures ──
        for ex in (data.get('exposures') or []):
            if not isinstance(ex, dict):
                continue
            deps = []
            for d in (ex.get('depends_on') or []):
                rm = re.search(r"""ref\s*\(\s*['"]([^'"]+)""", str(d))
                if rm:
                    deps.append(rm.group(1))
            exposures.append({'name': ex.get('name', ''), 'depends_on': deps})

    return {
        'models': models, 'columns': columns, 'sources': sources,
        'semantic_layer': {
            'semantic_models': semantic_models,
            'metrics': metrics,
            'saved_queries': saved_queries,
        },
        'exposures': exposures,
    }


# ── SQL parsing ──────────────────────────────────────────────────────────────

def strip_jinja_comments(sql):
    """Remove Jinja and SQL comments for structural analysis."""
    s = JINJA_COMMENT_RE.sub('', sql)
    s = JINJA_EXPR_RE.sub('__JINJA__', s)
    s = JINJA_BLOCK_RE.sub('', s)
    s = SQL_BLOCK_COMMENT_RE.sub('', s)
    s = SQL_LINE_COMMENT_RE.sub('', s)
    return s


def extract_sql_snippets(raw_sql):
    """Extract structured SQL snippets revealing business logic.

    Works on Jinja-stripped SQL for most extractions. The is_incremental
    block is extracted from raw Jinja before stripping.
    """
    # ── is_incremental: extract from raw Jinja first ──
    inc_m = IS_INCREMENTAL_RE.search(raw_sql)
    is_incremental_block = inc_m.group(1).strip()[:SNIPPET_MAX] if inc_m else None

    clean = strip_jinja_comments(raw_sql)

    # ── WHERE clauses ──
    where_clauses = []
    for m in re.finditer(
        r'\bWHERE\b\s+(.*?)(?=\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|'
        r'QUALIFY|WINDOW|UNION|INTERSECT|EXCEPT)\b|\)\s*(?:,|\bSELECT\b)|\Z)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        clause = ' '.join(m.group(1).split())[:SNIPPET_MAX]
        if clause:
            where_clauses.append(clause)

    # ── HAVING clauses ──
    having_clauses = []
    for m in re.finditer(
        r'\bHAVING\b\s+(.*?)(?=\b(?:ORDER\s+BY|LIMIT|QUALIFY|WINDOW|UNION)\b|\Z)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        clause = ' '.join(m.group(1).split())[:SNIPPET_MAX]
        if clause:
            having_clauses.append(clause)

    # ── CASE WHEN blocks ──
    case_when_blocks = []
    for m in re.finditer(r'\bCASE\b\s+(?:WHEN\b.*?END)', clean,
                         re.DOTALL | re.IGNORECASE):
        case_when_blocks.append(' '.join(m.group(0).split())[:SNIPPET_MAX])

    # ── COALESCE expressions ──
    coalesce_exprs = []
    for m in re.finditer(r'\bCOALESCE\s*\(', clean, re.IGNORECASE):
        start = m.start()
        depth, pos = 0, m.end() - 1
        while pos < len(clean):
            ch = clean[pos]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    coalesce_exprs.append(
                        ' '.join(clean[start:pos + 1].split())[:SNIPPET_MAX])
                    break
            pos += 1

    # ── JOIN conditions ──
    joins = []
    for m in re.finditer(
        r'((?:LEFT|RIGHT|INNER|FULL|CROSS)\s+(?:OUTER\s+)?)?JOIN\s+\S+\s+'
        r'(?:AS\s+\S+\s+)?ON\b\s+(.*?)(?=\b(?:LEFT|RIGHT|INNER|FULL|CROSS|'
        r'JOIN|WHERE|GROUP|ORDER|HAVING|LIMIT|QUALIFY|WINDOW|UNION|SELECT)\b|\Z)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        joins.append(' '.join(m.group(0).split())[:SNIPPET_MAX])

    # ── GROUP BY columns ──
    group_by_clauses = []
    for m in re.finditer(
        r'\bGROUP\s+BY\b\s+(.*?)(?=\b(?:HAVING|ORDER\s+BY|LIMIT|QUALIFY|'
        r'WINDOW|UNION)\b|\Z)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        group_by_clauses.append(' '.join(m.group(1).split())[:SNIPPET_MAX])

    # ── Window functions ──
    window_functions = []
    for m in re.finditer(
        r'\b(ROW_NUMBER|RANK|DENSE_RANK|LEAD|LAG|NTILE|FIRST_VALUE|'
        r'LAST_VALUE|SUM|COUNT|AVG|MIN|MAX)\s*\(.*?\)\s+OVER\s*\(.*?\)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        window_functions.append(' '.join(m.group(0).split())[:SNIPPET_MAX])

    # ── String literals (deduplicated) ──
    string_literals = sorted(set(re.findall(r"'([^']{2,})'", clean)))

    # ── DISTINCT / QUALIFY ──
    has_distinct = bool(re.search(r'\bSELECT\s+DISTINCT\b', clean,
                                  re.IGNORECASE))
    qualify_clauses = []
    for m in re.finditer(
        r'\bQUALIFY\b\s+(.*?)(?=\b(?:ORDER\s+BY|LIMIT|WINDOW|UNION)\b|\Z)',
        clean, re.DOTALL | re.IGNORECASE
    ):
        qualify_clauses.append(' '.join(m.group(1).split())[:SNIPPET_MAX])

    # ── COUNTIF (BigQuery) ──
    has_countif = bool(re.search(r'\bCOUNTIF\s*\(', clean, re.IGNORECASE))

    return {
        'where_clauses': where_clauses,
        'having_clauses': having_clauses,
        'case_when_blocks': case_when_blocks,
        'coalesce_exprs': coalesce_exprs,
        'joins': joins,
        'group_by_clauses': group_by_clauses,
        'window_functions': window_functions,
        'string_literals': string_literals,
        'has_distinct': has_distinct,
        'qualify_clauses': qualify_clauses,
        'is_incremental_block': is_incremental_block,
        'has_countif': has_countif,
    }


def parse_ctes(clean_sql):
    """Parse CTE definitions. Returns {name_lower: body_sql}."""
    ctes = {}
    if not re.match(r'\s*WITH\b', clean_sql, re.IGNORECASE):
        return ctes
    pos = re.search(r'\bWITH\b', clean_sql, re.IGNORECASE).end()
    length = len(clean_sql)
    while pos < length:
        # skip whitespace and commas
        while pos < length and clean_sql[pos] in ' \t\n\r,':
            pos += 1
        m = re.match(r'(\w+)\s+AS\s*\(', clean_sql[pos:], re.IGNORECASE)
        if not m:
            break
        cte_name = m.group(1).lower()
        pos += m.end() - 1  # at opening paren
        depth = 0
        start = pos
        while pos < length:
            ch = clean_sql[pos]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    ctes[cte_name] = clean_sql[start + 1:pos]
                    pos += 1
                    break
            pos += 1
        else:
            break
    return ctes


def split_select_cols(text):
    """Split SELECT column list by commas, respecting parentheses."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == '(':
            depth += 1; cur.append(ch)
        elif ch == ')':
            depth -= 1; cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur))
    return parts


def col_name_from_expr(expr):
    """Extract column name/alias from a SELECT expression."""
    expr = expr.strip()
    if not expr or expr == '__JINJA__':
        return None
    as_m = re.search(r'\bAS\b\s+["`]?(\w+)["`]?\s*$', expr, re.IGNORECASE)
    if as_m:
        return as_m.group(1).lower()
    idents = re.findall(r'\b(\w+)\b', expr)
    return idents[-1].lower() if idents else None


def extract_columns_from_block(sql_block):
    """Extract columns from a SELECT...FROM block. Returns (names, count) or ([], -1)."""
    m = re.search(r'\bSELECT\b\s+(.*?)\s+\bFROM\b', sql_block,
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return [], -1
    clause = m.group(1).strip()
    if re.match(r'^(\w+\.)?\*\s*$', clause):
        return [], -1
    # Check if any column part is a bare * or table.* (SELECT *, extra_col FROM ...)
    # If so, we can't determine total count without recursive CTE resolution
    parts = split_select_cols(clause)
    for part in parts:
        stripped = part.strip()
        if re.match(r'^(\w+\.)?\*$', stripped):
            return [], -1
    names = []
    for part in parts:
        n = col_name_from_expr(part)
        if n:
            names.append(n)
    return (names, len(names)) if names else ([], -1)


def _resolve_cte_columns(tree, max_depth=10, known=None):
    """Resolve output columns for every CTE in `tree` via multi-hop trace.

    Returns {cte_name_lower: [col_lower, ...]}. A CTE whose select list is
    `SELECT * FROM another_cte` or `SELECT a.* FROM another_cte a` inherits
    `another_cte`'s resolved columns. CTEs with unresolvable stars or
    computed expressions without aliases are omitted (callers treat their
    absence as "unknown shape"). Recursion is bounded by `max_depth` to
    guard against pathological or self-referential chains.

    `known` optionally seeds the resolution map with externally known
    relation shapes ({name_lower: [col_lower, ...]}), e.g. other models'
    extracted columns, so `SELECT * FROM <ref'd model>` resolves too.
    """
    from sqlglot import exp

    ctes_by_name = {}
    for cte in tree.ctes:
        nm = (cte.alias or '').lower()
        inner = cte.this
        if nm and isinstance(inner, exp.Select):
            ctes_by_name[nm] = inner

    # External shapes are consulted only when a name is NOT a local CTE —
    # a CTE named like a model must shadow the model (SQL scoping rules).
    external = {k.lower(): [c.lower() for c in v]
                for k, v in (known or {}).items()}
    resolved = {}
    resolving = set()

    def _resolve(name, depth):
        if depth > max_depth:
            return None
        if name in resolved:
            return resolved[name]
        if name in resolving:
            return None
        sel = ctes_by_name.get(name)
        if sel is None:
            return external.get(name)
        resolving.add(name)
        out = _resolve_select_columns(sel, resolved, _resolve, depth + 1)
        resolving.discard(name)
        if out is not None:
            resolved[name] = out
        return out

    for nm in ctes_by_name:
        _resolve(nm, 0)
    # Merged view for relation lookups: local CTEs win over externals.
    return {**external, **resolved}


def _resolve_select_columns(sel, resolved, recurse, depth):
    """Resolve the output column list of a Select, using known CTEs.

    `resolved` is the so-far-resolved CTE map. `recurse(name, depth)`
    attempts to resolve a still-unknown CTE referenced by this Select.
    Returns a list of lowercase column names, or None if any item cannot
    be named (unresolvable star, unaliased expression).
    """
    from sqlglot import exp

    from_clause = sel.args.get('from_') or sel.args.get('from')
    from_table = None
    if isinstance(from_clause, exp.From) and isinstance(from_clause.this, exp.Table):
        from_table = from_clause.this.name.lower()

    out = []
    for ex in sel.expressions:
        if isinstance(ex, exp.Star):
            target = from_table
            cols = resolved.get(target) if target else None
            if cols is None and target:
                cols = recurse(target, depth)
            if cols is None:
                return None
            excepts = ex.args.get('except_') or []
            excluded = {e.name.lower() for e in excepts if hasattr(e, 'name')}
            out.extend([c for c in cols if c not in excluded])
        elif isinstance(ex, exp.Column) and ex.is_star:
            qual = (ex.table or '').lower()
            target = qual or from_table
            cols = resolved.get(target) if target else None
            if cols is None and target:
                cols = recurse(target, depth)
            if cols is None:
                return None
            out.extend(cols)
        else:
            nm = _named_select_name(ex)
            if nm is None:
                return None
            nml = nm.lower()
            # A Jinja-stripped expression (`{{ macro() }}` -> `__JINJA__`) or a
            # source placeholder means the column came from code the static
            # parser cannot read. The CTE's true shape is unknown, so the whole
            # scope is unresolvable — callers must skip it rather than treat a
            # sentinel as a real column.
            if nml in ('__jinja__', '__unknown__') or nml.startswith('__source__'):
                return None
            out.append(nml)
    return out


# Literal operands that flag a likely unit / currency drift on a column
# arithmetic. 100 covers cents <-> major units; 1000 covers milli- / kilo-
# conversions (ms, grams). The fractional forms catch `col * 0.01` style.
_UNIT_DRIFT_LITERALS = frozenset(['100', '100.0', '0.01', '1000', '1000.0', '0.001'])


def _extract_unit_drift(sql, dialect=None):
    """Return unit / currency drift candidates from `col * 100` or `col / 100`.

    Walks the parsed tree for Mul/Div where one side is a Column and the
    other is a numeric Literal in `_UNIT_DRIFT_LITERALS`. Returns a list
    of {column, expression, aliased_as} dicts (model is filled in by the
    caller). Returns [] on parse failure or empty tree.
    """
    from sqlglot import exp

    clean = strip_jinja_comments(sql)
    try:
        tree = sqlglot.parse_one(clean, dialect=dialect)
    except Exception:
        return []
    if tree is None:
        return []

    hits = []
    for node in tree.walk():
        # sqlglot's walk() yields (node, parent, key) in some versions and
        # bare node in others. Normalize.
        n = node[0] if isinstance(node, tuple) else node
        if not isinstance(n, (exp.Mul, exp.Div)):
            continue
        left, right = n.left, n.right
        col = lit = None
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            col, lit = left, right
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            col, lit = right, left
        else:
            continue
        if not lit.is_number:
            continue
        if str(lit.this) not in _UNIT_DRIFT_LITERALS:
            continue
        # Find an enclosing alias if present (e.g., `col * 100 AS pct`).
        alias_name = None
        p = n.parent
        while p is not None:
            if isinstance(p, exp.Alias):
                alias_name = p.alias
                break
            # Stop climbing once we leave simple wrapping expressions.
            if isinstance(p, exp.Select):
                break
            p = p.parent
        hits.append({
            'column': col.name.lower(),
            'expression': n.sql(dialect=dialect)[:200],
            'aliased_as': alias_name.lower() if alias_name else None,
        })
    return hits


def _extract_filter_refs(sql, dialect=None):
    """Extract column references from WHERE / JOIN ON / CASE / COALESCE.

    Returns a dict with four keys:
      - where: list of {col, table, snippet}
      - joins: list of {left_col, right_col, left_table, right_table}
      - case_cols: list of column names referenced inside CASE expressions
      - coalesce_cols: list of column names referenced inside COALESCE

    Returns the empty structure on parse failure. Column names are
    lowercased; table qualifiers are preserved lowercased when available.
    """
    from sqlglot import exp

    empty = {'where': [], 'joins': [], 'case_cols': [],
             'coalesce_cols': []}

    clean = strip_jinja_comments(sql)
    try:
        tree = sqlglot.parse_one(clean, dialect=dialect)
    except Exception:
        return empty
    if tree is None:
        return empty

    def _walk(root):
        for n in root.walk():
            yield n[0] if isinstance(n, tuple) else n

    where_items = []
    joins = []
    case_cols = set()
    coalesce_cols = set()

    # WHERE: any Where node. Capture every column at top-level, plus a
    # short snippet per conjunct for hidden_filter comparison.
    for node in _walk(tree):
        if isinstance(node, exp.Where):
            body = node.this
            # Split on AND so each conjunct is its own review item.
            conjuncts = list(body.flatten()) if isinstance(body, exp.And) else [body]
            for c in conjuncts:
                for col in c.find_all(exp.Column):
                    where_items.append({
                        'col': col.name.lower(),
                        'table': (col.table or '').lower() or None,
                        'snippet': c.sql(dialect=dialect)[:200],
                    })
        elif isinstance(node, exp.Join):
            on = node.args.get('on')
            if on is None:
                continue
            # Collect equality pairs from the ON clause.
            for eq in on.find_all(exp.EQ):
                le, re_ = eq.left, eq.right
                if isinstance(le, exp.Column) and isinstance(re_, exp.Column):
                    joins.append({
                        'left_col': le.name.lower(),
                        'right_col': re_.name.lower(),
                        'left_table': (le.table or '').lower() or None,
                        'right_table': (re_.table or '').lower() or None,
                    })
        elif isinstance(node, exp.Case):
            for col in node.find_all(exp.Column):
                case_cols.add(col.name.lower())
        elif isinstance(node, exp.Coalesce):
            for col in node.find_all(exp.Column):
                coalesce_cols.add(col.name.lower())

    return {
        'where': where_items,
        'joins': joins,
        'case_cols': sorted(case_cols),
        'coalesce_cols': sorted(coalesce_cols),
    }


def _extract_columns_via_sqlglot(sql, known_ctes=None, dialect=None):
    """Extract SELECT-list columns from the outermost SELECT via sqlglot AST.

    Parses `sql` with the supplied `dialect` (ANSI when None). Returns a
    3-tuple:
        (columns, resolved, parse_method)

    - columns: lowercase column names, in source order.
    - resolved: True if every select-list item has a known name (either
      explicit alias or a known CTE column via SELECT * / SELECT t.*).
      False when a star expands against an unknown relation.
    - parse_method: 'sqlglot' on success.

    On parse failure, returns None (signals the caller should fall back
    to the regex path).

    `known_ctes` is an optional {name_lower: [col_lower, ...]} map used to
    resolve `SELECT *` / `SELECT t.*` against externally known CTE shapes.
    CTEs defined inside `sql` itself are discovered through the AST via
    recursive scope resolution (up to depth 10), so multi-hop chains like
    `base -> mid -> top` trace their output columns correctly.
    """
    from sqlglot import exp  # local import keeps module load cheap

    clean = strip_jinja_comments(sql)
    try:
        tree = sqlglot.parse_one(clean, dialect=dialect)
    except Exception:
        return None
    if tree is None or not isinstance(tree, exp.Select):
        return None

    # Resolve CTEs recursively up to a safety depth. A CTE that selects
    # from another CTE inherits its columns. Caller overrides from
    # `known_ctes` take precedence so external hints win.
    resolved_ctes = _resolve_cte_columns(tree, max_depth=10)
    if known_ctes:
        for k, v in known_ctes.items():
            resolved_ctes[k.lower()] = [c.lower() for c in v]

    # Walk the outermost select list, handling bare * and table.* expansions.
    from_table = None
    # sqlglot stores the FROM clause under the 'from_' key on Select nodes,
    # not 'from' (which is a reserved word). Use find(exp.From) scoped to
    # the outer select to avoid grabbing a CTE's inner FROM.
    from_clause = tree.args.get('from_') or tree.args.get('from')
    if isinstance(from_clause, exp.From) and isinstance(from_clause.this, exp.Table):
        from_table = from_clause.this.name.lower()

    out_cols = []
    resolved = True
    for ex in tree.expressions:
        if isinstance(ex, exp.Star):
            # SELECT * [EXCEPT (...)] FROM <something>
            target = from_table
            if target and target in resolved_ctes:
                cols = list(resolved_ctes[target])
                excepts = ex.args.get('except_') or []
                excluded = {
                    e.name.lower() for e in excepts if hasattr(e, 'name')
                }
                out_cols.extend([c for c in cols if c not in excluded])
            else:
                # Star against an unresolvable relation.
                out_cols.append('*')
                resolved = False
        elif isinstance(ex, exp.Column) and ex.is_star:
            # table.* form. Resolve against from_table alias match, or,
            # if the table-qualifier names a known CTE directly, that CTE.
            qual = (ex.table or '').lower()
            target = qual if qual in resolved_ctes else from_table
            if target and target in resolved_ctes:
                out_cols.extend(resolved_ctes[target])
            else:
                out_cols.append('*')
                resolved = False
        else:
            nm = _named_select_name(ex)
            if nm is None:
                # Unnamed expression. Match regex behavior: fall back.
                return None
            out_cols.append(nm.lower())

    return out_cols, resolved, 'sqlglot'


def _is_star_expr(ex):
    """True for bare `*` or `table.*`."""
    from sqlglot import exp
    if isinstance(ex, exp.Star):
        return True
    if isinstance(ex, exp.Column) and ex.is_star:
        return True
    return False


def _named_select_name(ex):
    """Return a name for a non-star select expression, or None."""
    from sqlglot import exp
    if isinstance(ex, exp.Alias):
        return ex.alias
    if isinstance(ex, exp.Column):
        return ex.name
    # sqlglot provides .alias_or_name on most expressions; use it as a
    # last resort but only when it yields an identifier-like string.
    name = getattr(ex, 'alias_or_name', '') or ''
    return name or None


def extract_sql_columns(sql):
    """Best-effort output column extraction with CTE tracing.

    Tries the sqlglot AST path first. Falls back to the legacy regex
    walker if sqlglot cannot parse the cleaned SQL. The regex path is
    retained unchanged for safety: WHERE/JOIN/CASE extraction still uses
    it, and this function's two-tuple return contract is preserved for
    callers such as the macro-detection test.
    """
    names, count, _method, _reason = extract_sql_columns_with_method(sql)
    return names, count


def extract_sql_columns_with_method(sql, dialect=None):
    """Like `extract_sql_columns`, but also reports the parse path.

    Returns (names, count, method, fallback_reason) where:
    - method is 'sqlglot' on AST success, 'regex' on regex fallback.
    - fallback_reason is None on success or a short string on fallback.
    - count is len(names) when every column is resolved; -1 when a star
      could not be expanded (matches the legacy regex contract).

    `dialect` is forwarded to sqlglot so adapter-specific syntax (BigQuery
    STRUCT / UNNEST, Snowflake FLATTEN, DuckDB PIVOT, etc.) parses cleanly.
    """
    # Try AST path first. known_ctes=None means sqlglot discovers CTEs
    # from the SQL itself, which matches the regex path's scope.
    try:
        ast_result = _extract_columns_via_sqlglot(sql, dialect=dialect)
    except Exception as exc:  # defensive: never let AST raise reach caller
        ast_result = None
        ast_err = f'sqlglot raised: {type(exc).__name__}: {exc}'
    else:
        ast_err = None

    if ast_result is not None:
        names, resolved, method = ast_result
        if resolved:
            return names, len(names), method, None
        # Unresolvable star. Regex path returns ([], -1) in this case
        # and we preserve that contract so downstream phantom-column
        # logic and the golden snapshot stay identical.
        return [], -1, method, None

    # Fallback to legacy regex implementation.
    names, count = _extract_sql_columns_regex(sql)
    reason = ast_err or 'sqlglot could not parse cleaned SQL'
    return names, count, 'regex', reason


def _extract_sql_columns_regex(sql):
    """Legacy regex-based column extractor. Kept as sqlglot fallback."""
    clean = strip_jinja_comments(sql)
    ctes = parse_ctes(clean)

    # Find the last SELECT in the file
    positions = [m.start() for m in re.finditer(r'\bSELECT\b', clean, re.IGNORECASE)]
    if not positions:
        return [], -1
    final = clean[positions[-1]:]

    # Check for SELECT * FROM cte_name
    star = re.match(r'SELECT\s+(\w+\.)?\*\s+FROM\s+(\w+)', final, re.IGNORECASE)
    if star:
        cte = star.group(2).lower()
        if cte in ctes:
            return extract_columns_from_block(ctes[cte])
        return [], -1

    return extract_columns_from_block(final)


def parse_sql_files(sql_files, dialect=None):
    """Parse all SQL files for refs, materialization, columns, and SQL snippets.

    `dialect` is forwarded to sqlglot so adapter-specific syntax parses
    cleanly. Unit-drift and AST-based filter references are computed per
    model and stored alongside the existing fields. When a model falls
    back to the regex column extractor, the AST-derived fields are left
    at their defaults so downstream consumers know to use the regex path.
    """
    data = {}
    for sf in sql_files:
        try:
            sql = sf.read_text(encoding='utf-8')
        except Exception:
            continue
        refs = []
        cross_project_refs = set()
        for first, second in REF_RE.findall(sql):
            if second:
                refs.append(second)
                cross_project_refs.add(second)
            else:
                refs.append(first)
        sources = SOURCE_RE.findall(sql)
        mat_m = CONFIG_MAT_RE.search(sql)
        col_names, col_count, parse_method, parse_fallback = (
            extract_sql_columns_with_method(sql, dialect=dialect))
        snippets = extract_sql_snippets(sql)
        clean = strip_jinja_comments(sql)
        aliases = extract_column_aliases(clean)
        # AST-derived extras. Only computed when the column extractor
        # reached the sqlglot path; regex-fallback models leave these
        # empty so callers can route to their own fallback.
        if parse_method == 'sqlglot':
            unit_drift = _extract_unit_drift(sql, dialect=dialect)
            filter_refs = _extract_filter_refs(sql, dialect=dialect)
        else:
            unit_drift = []
            filter_refs = {'where': [], 'joins': [], 'case_cols': [],
                           'coalesce_cols': []}
        entry = {
            'path': str(sf), 'refs': refs,
            'cross_project_refs': sorted(cross_project_refs),
            'sources': [(s, t) for s, t in sources],
            'materialization': mat_m.group(1) if mat_m else None,
            'columns': col_names, 'column_count': col_count,
            'sql_snippets': snippets,
            'column_aliases': aliases,
            'column_parse_method': parse_method,
            'unit_drift': unit_drift,
            'filter_refs': filter_refs,
        }
        if parse_fallback:
            entry['parse_fallback'] = f'sqlglot failed: {parse_fallback}'
        data[sf.stem] = entry
    return data


def _build_lineage_schema(sql_data):
    """Construct the sqlglot lineage schema from per-model column lists.

    Each model becomes a bare table with a 'UNKNOWN' type per column.
    Models whose column set is empty or contains sentinel placeholders
    are skipped because they would not help lineage resolution anyway.
    """
    schema = {}
    for name, sd in (sql_data or {}).items():
        cols = [c for c in (sd.get('columns') or [])
                if c and c not in ('__jinja_generated__', '__unknown__',
                                   '*')]
        if not cols:
            continue
        schema[name.lower()] = {c.lower(): 'UNKNOWN' for c in cols}
    return schema


def _read_model_sql(sd):
    """Return the raw SQL text for a parsed model, or None on failure."""
    try:
        return Path(sd['path']).read_text(encoding='utf-8')
    except Exception:
        return None


def _resolve_phantoms_via_lineage(model_name, candidates, sql_text, schema,
                                  dialect=None, parse_fallback=False):
    """Try to resolve each `candidate` phantom column via sqlglot lineage.

    Returns {column: 'upstream_model.col'} for columns that lineage
    traces to a known upstream. Columns lineage cannot resolve are
    omitted (caller keeps the phantom finding).

    Skips the whole model if the column parser fell back to regex, or if
    the single-model call budget exceeds the 5 second tripwire. Prints a
    one-line warning to stderr whenever a single model spends more than
    2 seconds resolving lineage.
    """
    import time

    if parse_fallback or not sql_text or not candidates:
        return {}
    try:
        from sqlglot.lineage import lineage
    except Exception:
        return {}

    resolved = {}
    start = time.perf_counter()
    cleaned = strip_jinja_comments(sql_text)
    for col in sorted(candidates):
        if time.perf_counter() - start > 5.0:
            print(f'warn: lineage budget exceeded 5s on model {model_name}; '
                  f'stopping early', file=sys.stderr)
            break
        try:
            node = lineage(col, cleaned, schema=schema, dialect=dialect)
        except Exception:
            continue
        upstream = _lineage_first_upstream_table_col(node)
        if upstream:
            resolved[col] = upstream
    elapsed = time.perf_counter() - start
    if elapsed > 2.0:
        print(f'warn: lineage took {elapsed:.2f}s on model {model_name}',
              file=sys.stderr)
    return resolved


def _lineage_first_upstream_table_col(node):
    """Walk the lineage tree and return 'table.col' for the deepest bound column.

    `node` is a `sqlglot.lineage.Node`. Returns None if lineage bottoms
    out on a literal or unresolvable expression.
    """
    from sqlglot import exp

    # BFS-ish: deepest table-qualified column in the downstream chain.
    stack = list(getattr(node, 'downstream', []) or [])
    best = None
    while stack:
        n = stack.pop()
        source = getattr(n, 'source', None)
        expr = getattr(n, 'expression', None)
        if isinstance(expr, exp.Column) and expr.table:
            best = f'{expr.table}.{expr.name}'.lower()
        elif isinstance(source, exp.Table):
            # A bound table without explicit column qualifier; use node.name.
            nm = getattr(n, 'name', '') or ''
            if nm and '.' in nm:
                best = nm.lower()
        stack.extend(getattr(n, 'downstream', []) or [])
    return best


# ── Cross-referencing ────────────────────────────────────────────────────────

def cross_reference(models, sql_data, sources, seeds, snapshots, columns,
                    dialect=None, packages_unresolved=False):
    known = set(models) | set(sql_data)
    seed_names = {s['name'] for s in seeds}
    snap_names = set(snapshots)
    source_tables = {(s['source_name'], s['table_name']) for s in sources}

    issues = {
        'broken_refs': [], 'phantom_models': [], 'phantom_columns': [],
        'duplicate_yaml_columns': [], 'copy_paste_descriptions': [],
        'source_via_ref': [],
        # refs that resolve to nothing in-project, suppressed because the
        # project declares package/extension deps that are not installed
        # (no dbt_packages/, no compiled manifest). These are package models
        # or user-supplied extension points, not broken refs — one aggregate
        # `dbt deps` notice replaces per-ref Blockers.
        'broken_refs_suppressed_no_deps': [],
        # Phantom columns that would have been flagged but were resolved
        # back to a named upstream column via sqlglot lineage.
        'phantom_columns_resolved_by_lineage': [],
    }

    # Phantom models (YAML, no SQL)
    for name, md in models.items():
        if name not in sql_data:
            issues['phantom_models'].append({
                'name': name, 'yaml_path': md.get('yaml_path', ''),
                'reason': 'YAML entry but no SQL file',
            })

    # Broken refs & source-via-ref
    # Cross-project refs (two-arg `ref('project','model')`, dbt mesh) point
    # outside this project and cannot be resolved here, so we skip them in
    # the broken-refs check rather than hard-flagging them as broken.
    for name, sd in sql_data.items():
        cross = set(sd.get('cross_project_refs') or [])
        for ref in sd['refs']:
            if ref in cross:
                continue
            if ref not in known and ref not in seed_names and ref not in snap_names:
                bucket = ('broken_refs_suppressed_no_deps'
                          if packages_unresolved else 'broken_refs')
                issues[bucket].append({
                    'model': name, 'refs': ref, 'sql_path': sd['path'],
                })
            for sn, tn in source_tables:
                if ref == tn:
                    issues['source_via_ref'].append({
                        'model': name, 'target': ref,
                        'reason': f'uses ref() for what may be source {sn}.{tn}',
                    })

    # Duplicate YAML columns
    by_model = defaultdict(list)
    for c in columns:
        by_model[c['model']].append(c)
    for mname, cols in by_model.items():
        seen = {}
        for c in cols:
            cn = c['column']
            if cn in seen:
                issues['duplicate_yaml_columns'].append({
                    'model': mname, 'column': cn,
                    'descriptions_differ': (c.get('description_text') or '') !=
                                           (seen[cn].get('description_text') or ''),
                })
            else:
                seen[cn] = c

    # Phantom columns (YAML col not in SQL output).
    # If the simple intersection flags a phantom, try resolving it
    # through sqlglot column lineage before accepting the finding. The
    # schema is built from each upstream model's already-extracted column
    # list so lineage has something to bind against. If lineage resolves
    # cleanly to a known upstream column, the phantom is dropped and a
    # row is appended to `phantom_columns_resolved_by_lineage` instead.
    lineage_schema = _build_lineage_schema(sql_data)
    for name, md in models.items():
        if name not in sql_data:
            continue
        sd = sql_data[name]
        if sd['column_count'] <= 0:
            continue
        sql_cols = {c for c in sd['columns']
                    if c not in ('__jinja_generated__', '__unknown__')}
        yaml_cols = {c['column'].lower() for c in by_model.get(name, [])}
        candidates = yaml_cols - sql_cols
        if not candidates:
            continue
        # Only parse SQL once per model when there is at least one candidate.
        sql_text = _read_model_sql(sd)
        resolved_map = _resolve_phantoms_via_lineage(
            name, candidates, sql_text, lineage_schema,
            dialect=dialect, parse_fallback=bool(sd.get('parse_fallback')))
        for phantom in candidates:
            resolved_to = resolved_map.get(phantom)
            if resolved_to:
                issues['phantom_columns_resolved_by_lineage'].append({
                    'model': name, 'column': phantom,
                    'resolved_to': resolved_to,
                })
                continue
            issues['phantom_columns'].append({
                'model': name, 'column': phantom,
                'yaml_path': md.get('yaml_path', ''),
                'reason': 'in YAML but not in SQL output',
            })

    # Copy-paste descriptions
    for mname, cols in by_model.items():
        desc_groups = defaultdict(list)
        for c in cols:
            dt = c.get('description_text')
            if dt and len(dt) > 15:
                desc_groups[dt].append(c['column'])
        for desc, cnames in desc_groups.items():
            if len(cnames) > 1:
                issues['copy_paste_descriptions'].append({
                    'model': mname, 'items': cnames,
                    'shared_description': desc[:200],
                    'why_wrong': f'{len(cnames)} columns share identical description',
                })

    return issues


def build_relationships(columns, models, semantic_models):
    declared, implicit = [], []
    declared_pairs = set()

    # From relationship tests
    for c in columns:
        for t in (c.get('tests') or []):
            if t.startswith('relationships:'):
                parts = t[len('relationships:'):].split('.')
                if len(parts) >= 2:
                    declared.append({
                        'from_model': c['model'], 'from_column': c['column'],
                        'to_model': parts[0], 'to_column': parts[1],
                        'source': 'test',
                    })
                    declared_pairs.add((c['model'], c['column']))

    # From semantic entity FKs
    for sm in semantic_models:
        for ent in sm.get('entities', []):
            if ent.get('type') == 'foreign':
                declared.append({
                    'from_model': sm['model_ref'],
                    'from_column': ent.get('expr') or ent['name'],
                    'to_model': ent['name'].replace('_id', '').replace('__', '_'),
                    'to_column': ent['name'],
                    'source': 'entity_fk',
                })
                declared_pairs.add((sm['model_ref'], ent.get('expr') or ent['name']))

    # Implicit: _id columns in multiple models without declared relationship
    fk_cols = defaultdict(list)
    for c in columns:
        if c['column'].endswith('_id'):
            fk_cols[c['column']].append(c['model'])
    for col_name, model_list in fk_cols.items():
        if len(model_list) < 2:
            continue
        for m in model_list:
            if (m, col_name) in declared_pairs:
                continue
            target = None
            for om, odata in models.items():
                if odata.get('pk_column') == col_name and om != m:
                    target = om
                    break
            if target:
                implicit.append({
                    'from_model': m, 'from_column': col_name,
                    'to_model': target, 'to_column': col_name,
                    'reason': f'{target} has PK test on {col_name}',
                })

    return {'declared': declared, 'implicit': implicit}


def build_test_summary(columns, models_dict=None):
    counts = defaultdict(int)
    models_with_tests = set()
    # Models with no YAML entry at all also have zero tests — enumerate
    # from the full model set, not just YAML-documented columns.
    all_models = set(models_dict or {})
    cat_without_av = []

    for c in columns:
        m = c['model']
        all_models.add(m)
        tests = c.get('tests') or []
        if tests:
            models_with_tests.add(m)
        for t in tests:
            base = t.split(':')[0]
            if base == 'unique':
                counts['unique'] += 1
            elif base == 'not_null':
                counts['not_null'] += 1
            elif base == 'relationships':
                counts['relationships'] += 1
            elif base == 'accepted_values':
                counts['accepted_values'] += 1
            else:
                counts['other'] += 1
        if CATEGORICAL_RE.search(c['column']):
            if not any(t.startswith('accepted_values') for t in tests):
                cat_without_av.append(f"{m}.{c['column']}")

    return {
        'unique_tests': counts['unique'],
        'not_null_tests': counts['not_null'],
        'relationship_tests': counts['relationships'],
        'accepted_values_tests': counts['accepted_values'],
        'other_tests': counts['other'],
        'models_with_zero_tests': len(all_models - models_with_tests),
        'models_with_zero_tests_list': sorted(all_models - models_with_tests),
        'categorical_columns_without_accepted_values': cat_without_av,
    }


# ── Concept index ────────────────────────────────────────────────────────────

def concept_stem(col_name):
    """Normalize a column name to its concept stem via suffix/prefix stripping.

    Does NOT apply an a priori alias map. Same-concept-different-name
    clustering across variants like cust_id/customer_id/user_id is
    evidence-based (see extract_column_aliases) — inferred from this
    project's own SQL `X as Y` renames, not assumed.
    """
    s = col_name.lower()
    s = CONCEPT_SUFFIXES.sub('', s)
    s = CONCEPT_PREFIXES.sub('', s)
    return s


def folder_from_path(path_str):
    """Extract first two path levels under models/ as folder."""
    if not path_str:
        return ''
    parts = Path(path_str).parts
    for i, p in enumerate(parts):
        if p == 'models' and i + 2 < len(parts):
            return f'{parts[i + 1]}/{parts[i + 2]}'
        if p == 'models' and i + 1 < len(parts):
            return parts[i + 1]
    return ''


def build_concept_index(models_dict, columns):
    """Group models by shared concept stems. Only keeps concepts in 2+ models.

    Returns a list of concept entries sorted by number of models (descending).
    """
    # Map: stem -> {model_name -> {columns, descriptions}}
    stem_map = defaultdict(lambda: defaultdict(lambda: {'columns': [], 'descriptions': []}))

    # Build model layer lookup
    model_layers = {n: md.get('layer', '') for n, md in models_dict.items()}

    for c in columns:
        # Skip staging models (passthrough noise)
        if model_layers.get(c['model']) == 'staging':
            continue
        stem = concept_stem(c['column'])
        if not stem or stem in NOISE_STEMS:
            continue
        model_name = c['model']
        entry = stem_map[stem][model_name]
        entry['columns'].append(c['column'])
        entry['descriptions'].append(c.get('description_text'))

    # Filter to stems appearing in 2+ distinct models
    concept_index = []
    for stem, model_entries in stem_map.items():
        if len(model_entries) < 2:
            continue
        models_list = []
        for mname, info in model_entries.items():
            md = models_dict.get(mname, {})
            snippets = md.get('sql_snippets') or {}
            models_list.append({
                'model': mname,
                'columns': info['columns'],
                'descriptions': [d for d in info['descriptions'] if d],
                'layer': md.get('layer', ''),
                'folder': folder_from_path(md.get('sql_path') or md.get('yaml_path')),
                'sql_snippets': {
                    'where_clauses': snippets.get('where_clauses', []),
                    'case_when_blocks': snippets.get('case_when_blocks', []),
                    'coalesce_exprs': snippets.get('coalesce_exprs', []),
                },
            })
        concept_index.append({
            'stem': stem,
            'model_count': len(models_list),
            'models': models_list,
        })

    concept_index.sort(key=lambda c: c['model_count'], reverse=True)
    return concept_index


# ── Review queue ─────────────────────────────────────────────────────────────

REVIEW_QUEUE_CAP = 60

# WHERE clauses that are ONLY date/null filters (low signal for hidden_filter).
# Must not contain AND/OR with additional conditions.
TRIVIAL_WHERE_RE = re.compile(
    r'^\s*\w[\w.]*\s+(?:IS\s+NOT\s+NULL|IS\s+NULL)\s*$'
    r'|^\s*\w[\w.]*\s+(?:BETWEEN\s+\S+\s+AND\s+\S+)\s*$'
    r'|^\s*\w[\w.]*\s*[<>=]+\s*(?:CURRENT_|TIMESTAMP|NOW)',
    re.IGNORECASE)


def _token_overlap(a, b):
    """Compute token overlap ratio between two strings (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    ta = set(re.findall(r'\w+', a.lower()))
    tb = set(re.findall(r'\w+', b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _flag_severity(models_list, models_dict):
    """Compute severity based on importance signals of involved models.

    Core and reference models are always high — agents query them regardless
    of inbound refs (which only measure dbt-internal consumption, not BI/agent use).
    """
    for mname in models_list:
        md = models_dict.get(mname, {})
        if md.get('layer') in ('core', 'reference'):
            return 'high'
        if md.get('inbound_refs', 0) >= 3:
            return 'high'
    return 'medium'


def build_review_queue(models_dict, columns, concept_index, sql_data=None):
    """Generate flagged items with risk hypotheses for LLM review.

    Per-model flags (skip staging):
      - hidden_filter, hidden_case_logic, hidden_coalesce,
        grain_ambiguous, incremental_asymmetry

    Cross-model flags (from concept_index):
      - concept_divergence, scope_divergence, coalesce_divergence

    Returns list of flag dicts (max REVIEW_QUEUE_CAP).
    """
    flags = []

    # Build column descriptions lookup: model -> {col_name -> desc_text}
    col_descs = defaultdict(dict)
    for c in columns:
        col_descs[c['model']][c['column']] = c.get('description_text') or ''

    # ── Per-model flags ──────────────────────────────────────────────────
    for mname, md in models_dict.items():
        if md.get('layer') == 'staging':
            continue
        snippets = md.get('sql_snippets')
        if not snippets:
            continue
        desc_text = (md.get('description_text') or '').lower()
        model_col_descs = col_descs.get(mname, {})
        all_col_desc_text = ' '.join(model_col_descs.values()).lower()

        # hidden_filter: WHERE clause not reflected in description.
        # Prefer AST-derived column references from sql_data. When
        # sqlglot parsed this model, every WHERE column reference is
        # available with a short snippet. Compare column names against
        # the model's description and its column descriptions. If none
        # of the filter's columns are mentioned anywhere, flag it.
        # Regex-fallback models (or any model without AST filter refs)
        # fall back to the legacy token-overlap path so coverage does
        # not regress.
        sd_entry = (sql_data or {}).get(mname, {})
        ast_where = (sd_entry.get('filter_refs') or {}).get('where') or []
        use_ast = bool(ast_where) and not sd_entry.get('parse_fallback')
        if use_ast:
            desc_tokens = set(re.findall(r'\w+', desc_text))
            col_desc_tokens = set(re.findall(r'\w+', all_col_desc_text))
            known_text = desc_tokens | col_desc_tokens
            # Group by snippet so each WHERE conjunct yields at most one flag.
            by_snippet = defaultdict(list)
            for item in ast_where:
                by_snippet[item['snippet']].append(item['col'])
            for snippet, cols in by_snippet.items():
                if TRIVIAL_WHERE_RE.match(snippet):
                    continue
                cols_set = {c for c in cols if c}
                if cols_set and not (cols_set & known_text):
                    flags.append({
                        'flag_type': 'hidden_filter',
                        'severity': _flag_severity([mname], models_dict),
                        'models': [mname],
                        'concept': None,
                        'evidence': f'WHERE: {snippet[:300]}  |  Description: {(desc_text or "(none)")[:200]}',
                        'question': f'Does {mname}\'s description mention this filter? Is the scope reduction documented?',
                    })
        else:
            for wc in snippets.get('where_clauses', []):
                if TRIVIAL_WHERE_RE.match(wc):
                    continue
                # Check if any significant WHERE terms appear in descriptions
                where_tokens = set(re.findall(r'\b[a-z]\w{2,}\b', wc.lower()))
                where_tokens -= {'and', 'or', 'not', 'null', 'true', 'false',
                                 'case', 'when', 'then', 'else', 'end', 'in',
                                 'select', 'from', 'where', 'group', 'order'}
                if where_tokens and not (where_tokens & set(re.findall(r'\w+', desc_text))):
                    flags.append({
                        'flag_type': 'hidden_filter',
                        'severity': _flag_severity([mname], models_dict),
                        'models': [mname],
                        'concept': None,
                        'evidence': f'WHERE: {wc[:300]}  |  Description: {(desc_text or "(none)")[:200]}',
                        'question': f'Does {mname}\'s description mention this filter? Is the scope reduction documented?',
                    })

        # hidden_case_logic: CASE WHEN with string literals not in docs
        for cb in snippets.get('case_when_blocks', []):
            case_literals = set(re.findall(r"'([^']{2,})'", cb))
            if case_literals:
                documented = set(re.findall(r"'([^']+)'", all_col_desc_text))
                undocumented = case_literals - documented
                if undocumented and len(undocumented) >= 2:
                    flags.append({
                        'flag_type': 'hidden_case_logic',
                        'severity': _flag_severity([mname], models_dict),
                        'models': [mname],
                        'concept': None,
                        'evidence': f'CASE block: {cb[:300]}  |  Undocumented values: {sorted(undocumented)[:10]}',
                        'question': f'Does {mname} document the business rules encoded in this CASE expression?',
                    })

        # hidden_coalesce: COALESCE defaulting without documentation
        for ce in snippets.get('coalesce_exprs', []):
            # Flag if COALESCE has a default literal (0, '', etc.)
            if re.search(r',\s*(?:0|\'\'|NULL|FALSE)\s*\)', ce, re.IGNORECASE):
                flags.append({
                    'flag_type': 'hidden_coalesce',
                    'severity': _flag_severity([mname], models_dict),
                    'models': [mname],
                    'concept': None,
                    'evidence': f'COALESCE: {ce[:300]}',
                    'question': f'Does {mname} document the fallback behavior in this COALESCE?',
                })

        # grain_ambiguous: core/ref model with GROUP BY but no grain declared
        if (md.get('layer') in ('core', 'reference')
                and not md.get('grain_declared')
                and (snippets.get('group_by_clauses')
                     or snippets.get('has_distinct')
                     or snippets.get('window_functions'))):
            flags.append({
                'flag_type': 'grain_ambiguous',
                'severity': _flag_severity([mname], models_dict),
                'models': [mname],
                'concept': None,
                'evidence': (f'GROUP BY: {(snippets.get("group_by_clauses") or ["(none)"])[0][:200]}'
                             f'  |  DISTINCT: {snippets.get("has_distinct")}'
                             f'  |  Window fns: {len(snippets.get("window_functions", []))}'),
                'question': f'What is the grain of {mname}? An agent needs to know what one row represents.',
            })

        # incremental_asymmetry
        if snippets.get('is_incremental_block'):
            flags.append({
                'flag_type': 'incremental_asymmetry',
                'severity': 'low',
                'models': [mname],
                'concept': None,
                'evidence': f'Incremental block: {snippets["is_incremental_block"][:300]}',
                'question': f'Could the incremental strategy in {mname} produce different results than a full refresh?',
            })

    # ── Cross-model flags (from concept index) ──────────────────────────
    # Limit to 1 flag per concept (pick the most informative type)
    for concept in concept_index:
        stem = concept['stem']
        cm = concept['models']  # list of {model, columns, descriptions, ...}
        model_names = [m['model'] for m in cm]
        concept_flag = None  # best flag for this concept

        # concept_divergence: different descriptions for same concept
        descs_by_model = {}
        for m in cm:
            if m['descriptions']:
                descs_by_model[m['model']] = ' '.join(m['descriptions'])
        if len(descs_by_model) >= 2:
            desc_list = list(descs_by_model.values())
            has_divergence = False
            for i in range(len(desc_list)):
                for j in range(i + 1, len(desc_list)):
                    if _token_overlap(desc_list[i], desc_list[j]) < 0.5:
                        has_divergence = True
                        break
                if has_divergence:
                    break
            if has_divergence:
                evidence_parts = [f'{mn}: "{d[:100]}"'
                                  for mn, d in list(descs_by_model.items())[:4]]
                concept_flag = {
                    'flag_type': 'concept_divergence',
                    'severity': _flag_severity(model_names, models_dict),
                    'models': model_names,
                    'concept': stem,
                    'evidence': ' | '.join(evidence_parts),
                    'question': f'Do these models define "{stem}" consistently? Would an agent get different answers depending on which model it queries?',
                }

        # scope_divergence: same concept, different WHERE clauses
        # (overrides concept_divergence if found -- more specific signal)
        models_with_where = []
        models_without_where = []
        for m in cm:
            where_clauses = m.get('sql_snippets', {}).get('where_clauses', [])
            significant = [w for w in where_clauses if not TRIVIAL_WHERE_RE.match(w)]
            if significant:
                models_with_where.append((m['model'], significant))
            else:
                models_without_where.append(m['model'])
        if models_with_where and models_without_where:
            concept_flag = {
                'flag_type': 'scope_divergence',
                'severity': _flag_severity(model_names, models_dict),
                'models': model_names,
                'concept': stem,
                'evidence': (f'Models WITH WHERE: {[(n, w[0][:100]) for n, w in models_with_where[:3]]}'
                             f' | Models WITHOUT WHERE: {models_without_where[:3]}'),
                'question': f'For concept "{stem}", some models filter data and others don\'t. Is this intentional scope difference documented?',
            }

        # coalesce_divergence: only if nothing else was flagged
        if concept_flag is None:
            models_with_coalesce = []
            models_without_coalesce = []
            for m in cm:
                coalesce = m.get('sql_snippets', {}).get('coalesce_exprs', [])
                if coalesce:
                    models_with_coalesce.append((m['model'], coalesce))
                else:
                    models_without_coalesce.append(m['model'])
            if models_with_coalesce and models_without_coalesce and len(cm) <= 10:
                concept_flag = {
                    'flag_type': 'coalesce_divergence',
                    'severity': _flag_severity(model_names, models_dict),
                    'models': model_names,
                    'concept': stem,
                    'evidence': (f'Models WITH COALESCE: {[(n, c[0][:100]) for n, c in models_with_coalesce[:3]]}'
                                 f' | Models WITHOUT: {models_without_coalesce[:3]}'),
                    'question': f'For concept "{stem}", some models use COALESCE fallbacks and others don\'t. Could this produce subtly different aggregates?',
                }

        if concept_flag:
            flags.append(concept_flag)

    # ── Deduplicate per-model flags (max 3 per model) ──────────────────
    per_model_counts = defaultdict(int)
    deduped = []
    # Sort per-model flags by severity first so we keep the best ones
    sev_rank = {'high': 0, 'medium': 1, 'low': 2}
    flags.sort(key=lambda f: sev_rank.get(f['severity'], 3))
    for f in flags:
        if f.get('concept') is not None:
            deduped.append(f)
            continue
        mname = f['models'][0]
        if per_model_counts[mname] < 3:
            deduped.append(f)
            per_model_counts[mname] += 1
    flags = deduped

    # ── Demote very large concept neighborhoods (low signal) ────────────
    for f in flags:
        if f.get('concept') and len(f['models']) > 15:
            f['severity'] = 'low'
        elif f.get('concept') and len(f['models']) > 8:
            if f['severity'] == 'high':
                f['severity'] = 'medium'

    # ── Sort and cap (interleave per-model and cross-model) ──────────
    sev_order = {'high': 0, 'medium': 1, 'low': 2}
    per_model = sorted(
        [f for f in flags if f.get('concept') is None],
        key=lambda f: (sev_order.get(f['severity'], 3), f['models'][0]))
    # Cross-model: sort by severity, then prefer sweet-spot size (2-6 models)
    def _concept_sort_key(f):
        n = len(f['models'])
        # Bucket: 2-8 = 0 (sweet spot), 9-15 = 1, >15 = 2
        size_bucket = 0 if n <= 8 else (1 if n <= 15 else 2)
        type_rank = {'scope_divergence': 0, 'concept_divergence': 1,
                     'coalesce_divergence': 2}.get(f['flag_type'], 3)
        return (sev_order.get(f['severity'], 3), size_bucket, type_rank)
    cross_model = sorted(
        [f for f in flags if f.get('concept') is not None],
        key=_concept_sort_key)

    # Reserve half the cap for each category (fill remaining with overflow)
    half = REVIEW_QUEUE_CAP // 2
    per_take = per_model[:half]
    cross_take = cross_model[:half]
    remaining = REVIEW_QUEUE_CAP - len(per_take) - len(cross_take)
    overflow = (per_model[half:] + cross_model[half:])
    overflow.sort(key=lambda f: (sev_order.get(f['severity'], 3),
                                 -len(f['models'])))

    result = per_take + cross_take + overflow[:remaining]
    result.sort(key=lambda f: (sev_order.get(f['severity'], 3),
                               -len(f['models'])))
    return result[:REVIEW_QUEUE_CAP]


# ── Column aliases (evidence-based, from project SQL) ───────────────────────

# Matches `X as Y` — case-insensitive, word-boundaries, skips qualified names.
ALIAS_RE = re.compile(
    r'(?<![\w.])([a-z_][a-z0-9_]*)\s+as\s+([a-z_][a-z0-9_]*)\b',
    re.IGNORECASE)

# Keywords that can never be a column alias source/target
_ALIAS_RESERVED = frozenset([
    'select', 'from', 'where', 'join', 'on', 'and', 'or', 'not', 'as',
    'case', 'when', 'then', 'else', 'end', 'in', 'is', 'null', 'true',
    'false', 'group', 'order', 'by', 'having', 'limit', 'distinct',
    'with', 'union', 'all', 'over', 'partition', 'rows', 'range',
    'between', 'like', 'ilike', 'exists', 'any', 'some',
])

# No hardcoded "generic names" list. Generic pivots are instead detected
# from the project itself: a name X is treated as a generic pivot if it
# appears as the SOURCE side of ≥2 alias edges to different targets across
# different models in this project. See _find_generic_pivots below. This
# prevents spurious unions (e.g., every `id` renamed to a different PK)
# without presuming which names are universally generic.


def find_generic_pivots(aliases_per_model):
    """Identify column names that behave as generic pivots in THIS project.

    A name X is a pivot if it appears as the SOURCE side of alias edges
    (X as Y) with ≥2 distinct targets across ≥2 distinct models. That's
    the fingerprint of a source column like `id` that every staging model
    renames to something different — useless for identity propagation.

    Evidence-based: no hardcoded list. If the project doesn't use that
    pattern, nothing is marked as a pivot.
    """
    src_to_targets = defaultdict(set)  # src -> set of (target, model)
    tgt_to_sources = defaultdict(set)  # target -> set of (src, model)
    for mname, pairs in aliases_per_model.items():
        for src, target in pairs:
            src_to_targets[src].add((target, mname))
            tgt_to_sources[target].add((src, mname))

    pivots = set()
    # Source-side pivot: one name used as source of many different renames
    # (e.g., `id` in every staging model renamed to a different PK).
    for src, tm_pairs in src_to_targets.items():
        targets = {t for t, _ in tm_pairs}
        models = {m for _, m in tm_pairs}
        if len(targets) >= 2 and len(models) >= 2:
            pivots.add(src)
    # Target-side pivot: one name used as target by many different sources
    # (e.g., `created_at` receiving first_reply_at, submitted_at, ts, etc.).
    # Threshold slightly higher here — a common target with 3+ unrelated
    # sources across 2+ models is almost certainly a generic bucket.
    for tgt, sm_pairs in tgt_to_sources.items():
        sources = {s for s, _ in sm_pairs}
        models = {m for _, m in sm_pairs}
        if len(sources) >= 3 and len(models) >= 2:
            pivots.add(tgt)
    return pivots


def extract_column_aliases(clean_sql):
    """Yield (source_col, aliased_col) pairs from SELECT clauses.

    Uses the cleaned (Jinja-stripped) SQL. Skips table aliases (FROM X as Y,
    JOIN X as Y) and CTE definitions (X as (...)) by checking the closest
    preceding SQL keyword.

    Heuristic, not a parser. Misses aliases inside nested subqueries in some
    cases, but catches the common staging-layer rename pattern that reveals
    same-concept-different-name drift.
    """
    lower = clean_sql.lower()
    pairs = []
    for m in ALIAS_RE.finditer(clean_sql):
        src = m.group(1).lower()
        alias = m.group(2).lower()
        if src == alias:
            continue
        if src in _ALIAS_RESERVED or alias in _ALIAS_RESERVED:
            continue
        # Skip CTE definitions: `name AS (` is a CTE, not a column alias.
        tail = clean_sql[m.end():m.end() + 4].lstrip()
        if tail.startswith('('):
            continue
        # Skip aliases where the source position is actually the tail of an
        # expression (e.g., `a - b as c` — `b` is not the real source of `c`).
        # Detected by an arithmetic/function-closing char just before src.
        pre_idx = m.start() - 1
        while pre_idx >= 0 and clean_sql[pre_idx] in ' \t':
            pre_idx -= 1
        if pre_idx >= 0 and clean_sql[pre_idx] in '-+*/%)':
            continue
        # Determine context: is the closest preceding keyword SELECT, or
        # FROM/JOIN (table alias)? Use a regex-based search for the last
        # occurrence of each keyword at a word boundary.
        start = m.start()
        preceding = lower[:start]
        last_select = _last_kw(preceding, 'select')
        last_from = _last_kw(preceding, 'from')
        last_join = _last_kw(preceding, 'join')
        if last_select > max(last_from, last_join):
            pairs.append((src, alias))
    return pairs


def _last_kw(text, kw):
    """Return position of last word-bounded occurrence of kw in text, or -1."""
    pat = re.compile(r'(?<!\w)' + kw + r'(?!\w)')
    last = -1
    for m in pat.finditer(text):
        last = m.start()
    return last


class _UnionFind:
    """Small union-find for grouping aliased column names."""
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            return x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Prefer the longer/more canonical name as root for readability.
            if len(rb) > len(ra) or (len(rb) == len(ra) and rb > ra):
                ra, rb = rb, ra
            self.parent[rb] = ra

    def groups(self):
        result = defaultdict(set)
        for name in self.parent:
            result[self.find(name)].add(name)
        return {root: names for root, names in result.items() if len(names) > 1}


# ── Catalog helpers ──────────────────────────────────────────────────────────

def _build_phantom_by_model(issues, sql_data=None, manifest_used=False):
    """Group issues.phantom_columns into per-model rows with counts.

    When the model's SQL uses macros (`dbt_utils.star`, `SELECT *`,
    Jinja for-loops) and no compiled manifest was used, the phantom
    finding cannot be trusted. The static extractor may have missed
    columns the macros inject. Instead of emitting a provisional row
    that synthesis will then have to discount (noisy in dbt-utils-heavy
    projects), we suppress the finding entirely and return it in a
    separate `suppressed` list so the report can emit a single notice
    pointing the user at `dbt compile`.

    Returns `(rows, suppressed)`:
    - rows: list of {model, yaml_path, phantoms, count, confidence,
      macro_signals}. All rows carry `confidence: 'high'`. Provisional
      findings are suppressed, not flagged.
    - suppressed: list of {model, yaml_path, would_be_phantoms, count,
      macro_signals}. Emit one aggregate notice in the report rather
      than per-model rows.
    """
    by_model = defaultdict(lambda: {'phantoms': [], 'yaml_path': ''})
    for p in issues.get('phantom_columns', []):
        entry = by_model[p['model']]
        entry['phantoms'].append(p['column'])
        entry['yaml_path'] = p.get('yaml_path', '')

    rows = []
    suppressed = []
    for m, d in by_model.items():
        macro_signals = []
        if not manifest_used:
            sd = (sql_data or {}).get(m)
            if sd:
                try:
                    sql_text = Path(sd['path']).read_text(encoding='utf-8')
                except Exception:
                    sql_text = ''
                columns_resolved = (
                    bool(sd.get('columns')) and sd.get('column_count', -1) > 0
                )
                uses_macros, signals = _detect_macro_column_generation(
                    sql_text, columns_resolved=columns_resolved
                )
                if uses_macros:
                    suppressed.append({
                        'model': m,
                        'yaml_path': d['yaml_path'],
                        'would_be_phantoms': sorted(d['phantoms']),
                        'count': len(d['phantoms']),
                        'macro_signals': signals,
                    })
                    continue
        rows.append({
            'model': m,
            'yaml_path': d['yaml_path'],
            'phantoms': sorted(d['phantoms']),
            'count': len(d['phantoms']),
            'confidence': 'high',
            'macro_signals': macro_signals,
        })
    rows.sort(key=lambda r: (-r['count'], r['model']))
    suppressed.sort(key=lambda r: (-r['count'], r['model']))
    return rows, suppressed


_COL_LITERAL_EQ_RE = re.compile(
    r"""\b(\w+)\s*(?:=|!=|<>)\s*'([^']{0,80})'""")
_COL_LITERAL_IN_RE = re.compile(
    r"""\b(\w+)\s+IN\s*\(([^)]{0,400})\)""", re.IGNORECASE)
_IN_LITERAL_RE = re.compile(r"""'([^']{0,80})'""")


def _collect_sql_column_values(models_dict):
    """Walk sql_snippets for each model and return {(model, col): set(values)}.

    Looks in WHERE, HAVING, and CASE WHEN blocks for `col = 'x'` and
    `col IN ('x', 'y')` patterns. Column names are lowercased. Values are
    kept as-is so casing/whitespace drift is preserved.
    """
    by_col = defaultdict(set)
    for mname, md in models_dict.items():
        snip = md.get('sql_snippets') or {}
        blobs = []
        blobs.extend(snip.get('where_clauses') or [])
        blobs.extend(snip.get('having_clauses') or [])
        blobs.extend(snip.get('case_when_blocks') or [])
        for blob in blobs:
            for cm in _COL_LITERAL_EQ_RE.finditer(blob):
                col, val = cm.group(1).lower(), cm.group(2)
                by_col[(mname, col)].add(val)
            for cm in _COL_LITERAL_IN_RE.finditer(blob):
                col = cm.group(1).lower()
                for lm in _IN_LITERAL_RE.finditer(cm.group(2)):
                    by_col[(mname, col)].add(lm.group(1))
    return by_col


def _normalize_enum(v):
    """Canonicalize an enum value for mismatch detection: case+whitespace+hyphen
    folded. Two values map to the same normalized form when they probably mean
    the same thing but differ cosmetically.
    """
    s = str(v).strip().lower()
    s = re.sub(r'[\s\-_]+', '', s)
    return s


def _build_enum_value_gaps(columns, models_dict):
    """Per column name, collect accepted_values from YAML tests + literal
    comparisons in SQL, and flag drifts.

    Emits three lists:
    - `undocumented_values`: column appears in accepted_values test AND in SQL
      literals, but SQL literal isn't in the accepted_values set
    - `casing_mismatches`: same column name across 2+ models has literal values
      that normalize the same but differ in casing/whitespace
    - `no_source_categorical`: categorical column name (by stem) has literals
      in SQL but no accepted_values test anywhere
    """
    # YAML accepted_values per (model, column)
    yaml_vals = defaultdict(set)
    for c in columns:
        for t in c.get('tests') or []:
            if t.startswith('accepted_values:'):
                vals = t[len('accepted_values:'):].split(',')
                yaml_vals[(c['model'], c['column'].lower())].update(
                    v.strip() for v in vals if v.strip())

    sql_vals = _collect_sql_column_values(models_dict)

    # Description-embedded enum values: per (model, column), scan the column's
    # description for tokens like "e.g. A, B, C" or "'Base', 'HP/HC'". These
    # count as a fourth evidence source for casing drift detection — lets us
    # catch cases where one side of the drift lives only in a description.
    desc_vals = defaultdict(set)  # (model, column_lower) -> {values}
    for c in columns:
        desc_text = c.get('description_text')
        if not desc_text:
            continue
        found = _extract_enum_values_from_description(desc_text)
        if found:
            desc_vals[(c['model'], c['column'].lower())].update(found)

    # Undocumented: SQL values not in YAML accepted_values
    undocumented = []
    for key, sqlset in sql_vals.items():
        if key in yaml_vals:
            extras = sqlset - yaml_vals[key]
            if extras:
                undocumented.append({
                    'model': key[0], 'column': key[1],
                    'documented_values': sorted(yaml_vals[key]),
                    'undocumented_sql_values': sorted(extras)[:15],
                })
    undocumented.sort(key=lambda r: (r['model'], r['column']))

    # Cross-model casing drift: same column name appearing in ≥2 models with
    # values that normalize to the same token but differ in casing/whitespace.
    # Includes YAML tests, SQL literals, AND description-extracted tokens.
    per_col = defaultdict(list)  # col_lower -> [(model, value, source)]
    for (m, col), vals in yaml_vals.items():
        for v in vals:
            per_col[col].append((m, v, 'yaml'))
    for (m, col), vals in sql_vals.items():
        for v in vals:
            per_col[col].append((m, v, 'sql'))
    for (m, col), vals in desc_vals.items():
        for v in vals:
            per_col[col].append((m, v, 'description'))

    casing = []
    for col, items in per_col.items():
        norm_to_forms = defaultdict(set)
        for m, v, _src in items:
            norm_to_forms[_normalize_enum(v)].add(v)
        drift_entries = [(norm, sorted(forms))
                         for norm, forms in norm_to_forms.items()
                         if len(forms) > 1]
        if drift_entries:
            examples = []
            for norm, forms in drift_entries[:5]:
                # Map each form back to a sample (model, source)
                form_to_loc = {}
                for m, v, src in items:
                    form_to_loc.setdefault(v, (m, src))
                examples.append({
                    'normalized': norm,
                    'variants': [{'value': f,
                                  'seen_in': form_to_loc.get(f, (None, None))[0],
                                  'source': form_to_loc.get(f, (None, None))[1]}
                                 for f in forms],
                })
            casing.append({'column': col, 'examples': examples})
    casing.sort(key=lambda r: (-len(r['examples']), r['column']))

    # Categorical-by-name with SQL literals but zero accepted_values anywhere
    cols_with_tests = {key[1] for key in yaml_vals.keys()}
    no_source = []
    cat_to_models = defaultdict(set)
    cat_to_values = defaultdict(set)
    for (m, col), vals in sql_vals.items():
        if CATEGORICAL_RE.search(col) and col not in cols_with_tests:
            cat_to_models[col].add(m)
            cat_to_values[col].update(vals)
    for col, mset in cat_to_models.items():
        no_source.append({
            'column': col, 'models': sorted(mset)[:10],
            'sql_values': sorted(cat_to_values[col])[:15],
        })
    no_source.sort(key=lambda r: (-len(r['sql_values']), r['column']))

    return {
        'undocumented_values': undocumented,
        'casing_mismatches': casing,
        'no_source_categorical': no_source,
    }


def _build_seeds_not_tested(seeds, columns, project_root):
    """Flag seeds whose values aren't referenced by any accepted_values test.

    Reads each seed CSV (best-effort, skips binary or unreadable files),
    collects the unique values per column. A seed is 'connected' if any of
    its values appears in an accepted_values test anywhere in the project.
    """
    # Gather all accepted_values from tests into one set of values
    accepted_set = set()
    for c in columns:
        for t in c.get('tests') or []:
            if t.startswith('accepted_values:'):
                for v in t[len('accepted_values:'):].split(','):
                    v = v.strip()
                    if v:
                        accepted_set.add(v.lower())

    rows = []
    for seed in seeds:
        path = Path(seed.get('path', ''))
        name = seed.get('name', '')
        row = {'seed': name, 'path': str(path),
               'row_count': None, 'columns': [],
               'connected_to_accepted_values': None,
               'matched_values': []}
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.read().splitlines()
            if not lines:
                rows.append(row)
                continue
            header = [h.strip() for h in lines[0].split(',')]
            data_lines = lines[1:]
            row['row_count'] = len(data_lines)
            row['columns'] = header
            seed_values = set()
            for ln in data_lines[:500]:
                for cell in ln.split(','):
                    v = cell.strip().strip('"').strip("'").lower()
                    if v:
                        seed_values.add(v)
            matched = sorted(seed_values & accepted_set)[:10]
            row['connected_to_accepted_values'] = bool(matched)
            row['matched_values'] = matched
        except Exception:
            pass
        rows.append(row)

    # Flag only seeds that look like reference data (≥2 rows, ≥1 non-id col)
    # AND have no connection to accepted_values tests.
    flagged = []
    for r in rows:
        rc = r.get('row_count')
        cols = r.get('columns') or []
        non_id = [c for c in cols if not c.lower().endswith('id') and
                  c.lower() not in ('id', 'key')]
        if rc and rc >= 2 and non_id and not r.get('connected_to_accepted_values'):
            flagged.append(r)
    flagged.sort(key=lambda r: (-r.get('row_count') or 0, r['seed']))
    return flagged


def _build_unit_variants(columns):
    """Group columns by stem after stripping unit/currency suffixes. Flag stems
    with 2+ distinct suffixes from the same group (e.g., `_eur` + `_euros`).
    """
    stem_to_suffixes = defaultdict(lambda: defaultdict(set))  # stem -> group -> {suffix: [col.model]}
    stem_examples = defaultdict(lambda: defaultdict(list))    # stem -> suffix -> [model.col]
    stem_group = {}  # stem -> group (from first match)

    for c in columns:
        col_l = c['column'].lower()
        m = UNIT_SUFFIX_RE.search(col_l)
        if not m:
            continue
        suffix = m.group(1)
        # Find group
        group = None
        for grp, suffixes in UNIT_SUFFIX_GROUPS.items():
            if suffix in suffixes:
                group = grp
                break
        if not group:
            continue
        stem = col_l[:-len(suffix)]
        if not stem or len(stem) < 2:
            continue
        stem_to_suffixes[stem][group].add(suffix)
        if len(stem_examples[stem][suffix]) < 3:
            stem_examples[stem][suffix].append(f"{c['model']}.{c['column']}")
        stem_group.setdefault(stem, group)

    variants = []
    for stem, groups in stem_to_suffixes.items():
        # Only flag if a single unit group has 2+ variant suffixes on this stem
        for group, suffixes in groups.items():
            if len(suffixes) < 2:
                continue
            variants.append({
                'stem': stem,
                'group': group,
                'suffixes': sorted(suffixes),
                'examples': {sfx: stem_examples[stem][sfx]
                             for sfx in sorted(suffixes)},
            })
    variants.sort(key=lambda r: (-len(r['suffixes']), r['stem']))
    return variants


# Scope/provenance qualifier tokens that mark "a variant of the same concept"
# when prepended/appended inside the same model (e.g.,
# `zone_deployment_start_date` vs `deployment_start_date`, or
# `raw_refusal_reason` vs `refusal_reason`). Intentionally excludes
# measurement-qualifier words (`min_`, `max_`, `first_`, `last_`, `initial_`,
# `current_`, `previous_`, `new_`, `old_`, `actual_`, `estimated_`, `planned_`)
# — those mark distinct measurements that legitimately coexist.
_OVERLAP_QUALIFIERS = frozenset([
    'zone', 'global', 'local', 'source', 'target',
    'raw', 'normalized', 'adjusted', 'original',
])


def _build_overlapping_concept_columns_within_model(columns):
    """Flag columns within a single model whose names strongly overlap on
    concept (e.g., `zone_deployment_start_date` vs `deployment_start_date`).

    Strategy:
    - Per model, tokenize each column name on `_`.
    - Two columns overlap if one's token list is a subset of the other's AND
      the extra tokens are all in the qualifier whitelist (`zone_`, `source_`,
      etc.). This catches qualifier-prefixed duplicates while staying
      conservative — plain pairs like `created_at` vs `updated_at` do NOT
      match.
    - Group overlapping columns by their shared "core" stem within each model.
    """
    by_model = defaultdict(list)
    for c in columns:
        by_model[c['model']].append(c['column'])

    flagged = []
    for model, cols in by_model.items():
        # Dedup while preserving order
        seen = set()
        unique_cols = []
        for col in cols:
            low = col.lower()
            if low not in seen:
                seen.add(low)
                unique_cols.append(col)

        # For each pair, check if one is a qualifier-extension of the other
        groups = defaultdict(set)  # core_tuple -> {column names}
        for col in unique_cols:
            tokens = col.lower().split('_')
            if len(tokens) < 2:
                continue
            # Try stripping each leading or trailing qualifier to derive
            # candidate "cores"
            core = tuple(tokens)
            # Strip one leading qualifier
            if tokens[0] in _OVERLAP_QUALIFIERS and len(tokens) >= 3:
                core = tuple(tokens[1:])
            # Strip one trailing qualifier
            elif tokens[-1] in _OVERLAP_QUALIFIERS and len(tokens) >= 3:
                core = tuple(tokens[:-1])
            groups[core].add(col)

        for core, members in groups.items():
            if len(members) < 2:
                continue
            flagged.append({
                'model': model,
                'core': '_'.join(core),
                'columns': sorted(members),
            })

    flagged.sort(key=lambda r: (r['model'], r['core']))
    return flagged


def _build_lineage_cycles(models_dict):
    """Detect cycles in the ref() graph. Returns a list of cycles, each a
    list of model names in traversal order.

    Uses iterative DFS with a recursion stack. Only considers refs between
    models that exist in models_dict (drops refs to sources/seeds/external).
    """
    # Build adjacency list of model-to-model refs only
    adj = {name: [r for r in (m.get('outbound_refs') or [])
                  if r in models_dict]
           for name, m in models_dict.items()}

    cycles_seen = set()
    cycles = []

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}

    for start in adj:
        if color[start] != WHITE:
            continue
        stack = [(start, iter(adj[start]))]
        path = [start]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            try:
                nxt = next(it)
            except StopIteration:
                color[node] = BLACK
                stack.pop()
                if path:
                    path.pop()
                continue
            if color.get(nxt, BLACK) == GRAY:
                # Found cycle — slice from nxt in path
                if nxt in path:
                    idx = path.index(nxt)
                    cyc = path[idx:] + [nxt]
                    key = tuple(sorted(cyc[:-1]))
                    if key not in cycles_seen:
                        cycles_seen.add(key)
                        cycles.append(cyc)
            elif color.get(nxt, WHITE) == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(adj[nxt])))
    cycles.sort(key=lambda c: (len(c), c))
    return cycles


# Description-embedded enum extraction patterns.
# Matches "e.g. X, Y, Z", "ex: X, Y, Z", "values: X, Y, Z", "par exemple X, Y, Z",
# or quoted value lists like `'X'/'Y'/'Z'` or `'X', 'Y', 'Z'`.
_DESC_ENUM_INTRO_RE = re.compile(
    r'(?:e\.g\.|ex\.|ex\s*:|values?\s*:|par\s+exemple|valeurs?\s*:)\s*'
    r'([^.\n]{1,200})',
    re.IGNORECASE)

_DESC_QUOTED_LIST_RE = re.compile(
    r"""(?:(['"])([^'"\n]{1,40})\1\s*[,/]\s*){1,}(['"])([^'"\n]{1,40})\3""")


def _extract_enum_values_from_description(text):
    """Return a set of enum-like tokens mentioned in a free-text description.

    Conservative: only extracts when a recognized introductory phrase appears,
    or when a quoted list of short tokens is present. Returns lowercased,
    whitespace-stripped strings. Empty set if nothing matches.
    """
    if not text:
        return set()
    found = set()

    # Introductory-phrase captures: split the tail on `,` or ` or ` / ` ou `
    for m in _DESC_ENUM_INTRO_RE.finditer(text):
        tail = m.group(1)
        # Break on commas, slashes, " or ", " ou "
        parts = re.split(r',|/|\bor\b|\bou\b', tail)
        for p in parts:
            tok = p.strip().strip('"').strip("'").strip()
            if tok and 1 <= len(tok) <= 40 and not tok.endswith(':'):
                found.add(tok)

    # Quoted-list captures: iterate individual quoted tokens across the whole
    # description (cheaper than the combined regex and catches more)
    for m in re.finditer(r"""(['"])([^'"\n]{1,40})\1""", text):
        tok = m.group(2).strip()
        if tok and 1 <= len(tok) <= 40:
            found.add(tok)

    return found


def _build_yaml_vs_sql_column_count_diff(models_dict):
    """Spot-check helper: compare YAML-declared column counts to SQL-extracted
    column counts. Returns the subset where YAML declares AS MANY OR MORE
    columns than SQL produces (suggesting phantom columns or stale YAML).

    Models where YAML has fewer columns than SQL are flagged elsewhere
    (phantom_columns_by_model covers YAML-column-not-in-SQL; partial YAML
    coverage is a documentation gap, not a mismatch bug).

    Only reports models where BOTH counts are present (>0) AND SQL parsing
    did not fail (column_count_sql != -1).
    """
    diffs = []
    for name, m in models_dict.items():
        y = m.get('column_count_yaml')
        s = m.get('column_count_sql')
        if not y or y <= 0:
            continue
        if s in (None, -1) or s <= 0:
            continue
        # Only flag "YAML claims >= what SQL emits" — the bug-shaped case.
        if y >= s and y != s:
            diffs.append({
                'model': name,
                'yaml_path': m.get('yaml_path'),
                'sql_path': m.get('sql_path'),
                'column_count_yaml': y,
                'column_count_sql': s,
                'diff': y - s,
            })
    diffs.sort(key=lambda r: (-r['diff'], r['model']))
    return diffs


def _build_unprefixed_booleans(columns):
    """Columns that are semantically boolean but lack is_/has_/was_ prefix.

    Heuristic: column name matches UNPREFIXED_BOOL_RE OR has an accepted_values
    test with a boolean-like value set (true/false or 0/1).
    """
    bool_vals = {'true', 'false', '0', '1', 't', 'f', 'yes', 'no'}
    rows = []
    for c in columns:
        name_lower = c['column'].lower()
        if BOOLEAN_PREFIX_RE.match(name_lower):
            continue  # properly prefixed
        reason = None
        if UNPREFIXED_BOOL_RE.search(name_lower):
            reason = 'name_pattern'
        for t in c.get('tests') or []:
            if t.startswith('accepted_values:'):
                vals = {v.strip().lower()
                        for v in t[len('accepted_values:'):].split(',')}
                if vals and vals.issubset(bool_vals):
                    reason = 'accepted_values'
                    break
        if reason:
            rows.append({
                'model': c['model'], 'column': c['column'], 'reason': reason,
            })
    rows.sort(key=lambda r: (r['model'], r['column']))
    return rows


# ── Macro / SELECT * detection ───────────────────────────────────────────────

_MACRO_PATTERNS = [
    re.compile(r'\bdbt_utils\s*\.\s*star\b', re.IGNORECASE),
    re.compile(r'\bdbt_utils\s*\.\s*pivot\b', re.IGNORECASE),
    re.compile(r'\bdbt_utils\s*\.\s*union_relations\b', re.IGNORECASE),
    re.compile(r'\bdbt_utils\s*\.\s*get_column_values\b', re.IGNORECASE),
    re.compile(r'\bdbt\s*\.\s*expand_column_types\b', re.IGNORECASE),
    # Fivetran/package staging macros inject a column set the static parser
    # cannot see (the column list comes from the warehouse relation at
    # compile time). Any model built on these has an unknowable shape
    # without a compiled manifest.
    re.compile(r'\bfivetran_utils\s*\.\s*fill_staging_columns\b', re.IGNORECASE),
    re.compile(r'\bfill_staging_columns\b', re.IGNORECASE),
    re.compile(r'\bget_columns_in_relation\b', re.IGNORECASE),
    re.compile(r'\bapply_source_relation\b', re.IGNORECASE),
    # Custom column-spreading macros: a Jinja macro call whose name ends in
    # `_columns(` (e.g. `select_extension_columns(ref(...))`) injects a column
    # list the static parser cannot see. Common in package-style projects
    # (Tuva, Fivetran) that assemble a canonical schema from a macro.
    re.compile(r'\b\w*_columns\s*\(', re.IGNORECASE),
]
_SELECT_STAR_RE = re.compile(
    r'\bSELECT\b\s+(?:/\*.*?\*/\s+)?(?:\w+\.)?\*(?:\s*,|\s+FROM)',
    re.IGNORECASE | re.DOTALL)
_JINJA_FOR_SELECT_RE = re.compile(
    r"{%\s*for\b.*?%}.*?(?:select|,\s*\w+|as\s+\w+).*?{%\s*endfor\s*%}",
    re.IGNORECASE | re.DOTALL)
# `{%- set cols -%} ... {%- endset -%}` capture blocks expanded into a SELECT
# via `{{ cols }}`. The static parser replaces the expansion with a Jinja
# placeholder, so the captured column list is invisible and the YAML-vs-SQL
# diff flags those columns as phantom when they are actually emitted. The
# `endset` keyword is the reliable tell for a captured block (vs an inline
# `{% set x = ... %}` config assignment, which has no endset).
_JINJA_SET_BLOCK_RE = re.compile(
    r"{%-?\s*set\s+\w+\s*-?%}.*?{%-?\s*endset\s*-?%}",
    re.IGNORECASE | re.DOTALL)

# SQL date-part keywords. When one of these appears as a bare, unqualified
# identifier inside DATEADD/DATEDIFF/DATE_TRUNC/TIMEADD etc. it parses as a
# pseudo-column ("DATEADD(month, -1, x)" -> Column(month)). It is a unit
# token, never a real column reference, so it is never flagged undefined.
_SQL_DATE_PARTS = frozenset([
    'day', 'days', 'dayofmonth', 'dayofweek', 'dayofweekiso', 'dayofyear',
    'week', 'weeks', 'weekofyear', 'weekiso', 'isoweek',
    'month', 'months', 'mon', 'quarter', 'quarters', 'qtr',
    'year', 'years', 'yearofweek', 'yearofweekiso', 'isoyear',
    'hour', 'hours', 'minute', 'minutes', 'second', 'seconds',
    'millisecond', 'milliseconds', 'microsecond', 'microseconds',
    'nanosecond', 'nanoseconds', 'epoch', 'epoch_second', 'epoch_millisecond',
    'dow', 'doy', 'tzh', 'tzm', 'timezone_hour', 'timezone_minute',
])

# UNPIVOT(value_col [, ...] FOR name_col IN (...)) — the value column(s) and
# the name column are produced by the unpivot, so referencing them downstream
# is valid even though no input relation lists them.
_UNPIVOT_RE = re.compile(
    r'\bunpivot\b\s*(?:include\s+nulls\s*|exclude\s+nulls\s*)?'
    r'\(\s*\(?\s*([\w\s,]+?)\s*\)?\s+for\s+(\w+)\s+in\b',
    re.IGNORECASE)

# Snowflake lateral table functions (SPLIT_TO_TABLE, FLATTEN, ...) expose
# system output columns. When a model laterals one of these, an unqualified
# reference to one of these names is the table-function output, not a typo.
_LATERAL_TABLE_FN_RE = re.compile(
    r'\b(?:lateral\b|table\s*\()|split_to_table\b|\bflatten\b|strtok_split_to_table\b',
    re.IGNORECASE)
_LATERAL_SYSTEM_COLS = frozenset(
    ['value', 'index', 'seq', 'this', 'key', 'path'])


def _unpivot_output_cols(sql):
    """Return the set of column names produced by any UNPIVOT in `sql`.

    Captures both the value column(s) and the name (category) column. Handles
    the single-value form `UNPIVOT(v FOR n IN (...))` and the comma form
    `UNPIVOT((v1, v2) FOR n IN (...))`.
    """
    out = set()
    for m in _UNPIVOT_RE.finditer(sql or ''):
        for v in m.group(1).split(','):
            v = v.strip().lower()
            if v:
                out.add(v)
        out.add(m.group(2).strip().lower())
    return out


def _model_produced_extra_cols(sql):
    """Columns a model produces that no input relation lists: UNPIVOT outputs
    and lateral table-function system columns. Used to suppress
    undefined-column false positives for these well-known constructs.
    """
    extra = _unpivot_output_cols(sql)
    if sql and _LATERAL_TABLE_FN_RE.search(sql):
        extra |= _LATERAL_SYSTEM_COLS
    return extra


def _detect_macro_column_generation(sql, columns_resolved=False):
    """Return (uses_macros: bool, signals: list[str]).

    A model whose columns are generated by macros / SELECT */Jinja loops cannot
    be reliably introspected by a static YAML-vs-SQL diff. The phantom-column
    and yaml_vs_sql_column_count_diff catalogs should be flagged as provisional
    (not authoritative) for these models.

    When `columns_resolved=True` (the static extractor successfully produced a
    concrete column list — typically via a `SELECT * FROM <local_cte>` that
    resolved through the CTE chain), the bare select_star signal is suppressed:
    the YAML-vs-SQL diff is already grounded in real column names. dbt_utils
    macros and Jinja for-loops still force provisional because they can inject
    columns the static extractor cannot see.
    """
    if not sql:
        return False, []
    signals = []
    for pat in _MACRO_PATTERNS:
        m = pat.search(sql)
        if m:
            signals.append(m.group(0))
    if _SELECT_STAR_RE.search(sql) and not columns_resolved:
        signals.append('select_star')
    if _JINJA_FOR_SELECT_RE.search(sql):
        signals.append('jinja_for_loop')
    if _JINJA_SET_BLOCK_RE.search(sql):
        signals.append('jinja_set_block')
    return (bool(signals), signals)


# ── Undefined column references & fan-out joins ─────────────────────────────

def _strip_jinja_resolving_refs(sql):
    """Like strip_jinja_comments, but keeps relation identity.

    `{{ ref('x') }}` becomes the bare identifier `x` and
    `{{ source('a', 'b') }}` becomes `__source__a__b`, so the parsed tree
    still knows which relation each FROM/JOIN points at. All other Jinja
    expressions are replaced with `__JINJA__` as before.
    """
    s = JINJA_COMMENT_RE.sub('', sql)

    def _expr_sub(m):
        text = m.group(0)
        rm = REF_RE.search(text)
        if rm:
            return (rm.group(2) or rm.group(1)).lower()
        sm = SOURCE_RE.search(text)
        if sm:
            return f'__source__{sm.group(1)}__{sm.group(2)}'.lower()
        return '__JINJA__'

    s = JINJA_EXPR_RE.sub(_expr_sub, s)
    s = JINJA_BLOCK_RE.sub('', s)
    s = SQL_BLOCK_COMMENT_RE.sub('', s)
    s = SQL_LINE_COMMENT_RE.sub('', s)
    return s


def _known_model_shapes(sql_data, manifest_used=False):
    """{model_name_lower: [col_lower, ...]} for models whose column list can
    be TRUSTED as complete, not merely non-empty.

    Excluded (their shape would make downstream undefined-column checks
    fire on real columns):
    - models whose extraction fell back to the regex path (e.g. incremental
      models where the `is_incremental()` tail subquery becomes the "last
      SELECT");
    - models with macro-generated columns (dbt_utils.star, Jinja for-loops)
      when no compiled manifest exists — the static list misses the
      injected columns.
    """
    shapes = {}
    for name, sd in (sql_data or {}).items():
        if sd.get('parse_fallback'):
            continue
        if sd.get('column_count', -1) <= 0:
            continue
        if not manifest_used:
            sql_text = _read_model_sql(sd)
            if sql_text is None:
                continue
            uses_macros, _signals = _detect_macro_column_generation(
                sql_text, columns_resolved=True)
            if uses_macros:
                continue
        cols = [c.lower() for c in (sd.get('columns') or [])
                if c and c not in ('__jinja_generated__', '__unknown__',
                                   '__jinja__', '*')]
        if cols:
            shapes[name.lower()] = cols
    return shapes


def _scope_relations(sel, resolved, shapes=None, ref_names=None):
    """Resolve every FROM/JOIN relation of one Select scope.

    Returns {alias_lower: set(columns)} when EVERY relation resolves to a
    known column set (CTE or external model shape), else None. Subqueries,
    sources, and unresolved relations make the whole scope unresolvable —
    the undefined-column check then skips it rather than guessing.

    A relation whose name is a `ref()` target (`ref_names`) is resolved
    against the external model shape, NOT a same-named local CTE. In dbt a
    `ref()` compiles to a fully qualified relation that SQL scoping does not
    let a CTE shadow, so `repo_storage_pl_daily_ext AS (... FROM
    ref('repo_storage_pl_daily'))` must read the model's columns even when a
    sibling CTE is named `repo_storage_pl_daily`. When the model's shape is
    unknown, the scope is unresolvable rather than silently resolving to the
    wrong CTE.
    """
    from sqlglot import exp

    shapes = shapes or {}
    ref_names = ref_names or set()

    tables = []
    from_clause = sel.args.get('from_') or sel.args.get('from')
    if from_clause is not None:
        tables.append(from_clause.this)
    for j in (sel.args.get('joins') or []):
        tables.append(j.this)
    if not tables:
        return None

    rels = {}
    for t in tables:
        if not isinstance(t, exp.Table):
            return None
        name = t.name.lower()
        if name in ref_names:
            cols = shapes.get(name)
        else:
            cols = resolved.get(name)
        if cols is None:
            return None
        alias = (t.alias or t.name).lower()
        rels[alias] = set(cols)
    return rels


def _build_undefined_column_refs(sql_data, manifest_used=False, dialect=None):
    """Flag columns referenced in a SELECT list or GROUP BY that no input
    relation of that scope produces.

    For each model, every Select scope (the outer query and each CTE) is
    checked independently. A scope is only checked when ALL of its
    FROM/JOIN relations resolve to a known column set — CTEs resolved
    recursively (depth 10), ref'd models resolved through their own
    extracted columns. A column reference is undefined when:
    - qualified (`t.col`): `col` is not in relation `t`'s column set;
    - unqualified: `col` is in no relation's set AND is not an explicit
      `AS` alias of another select item in the same scope (so
      self-referencing-alias bugs are not double-reported here).

    A bare select item like `has_refund` does NOT count as its own alias —
    it provides no evidence the column exists anywhere.

    Macro-suppression mirrors phantom_columns_by_model: when the model uses
    dbt_utils.star / unresolvable SELECT * / Jinja loops and no compiled
    manifest exists, the model is skipped entirely rather than emitting
    noise. All emitted rows carry confidence 'high'.
    """
    from sqlglot import exp

    shapes = _known_model_shapes(sql_data, manifest_used=manifest_used)
    rows = []

    for name, sd in sorted((sql_data or {}).items()):
        if sd.get('parse_fallback'):
            continue
        sql_text = _read_model_sql(sd)
        if not sql_text:
            continue
        if not manifest_used:
            columns_resolved = (bool(sd.get('columns'))
                                and sd.get('column_count', -1) > 0)
            uses_macros, _signals = _detect_macro_column_generation(
                sql_text, columns_resolved=columns_resolved)
            if uses_macros:
                continue

        clean = _strip_jinja_resolving_refs(sql_text)
        try:
            tree = sqlglot.parse_one(clean, dialect=dialect)
        except Exception:
            continue
        if not isinstance(tree, exp.Select):
            continue

        # Columns produced by constructs no input relation lists: UNPIVOT
        # value/name columns and lateral table-function system columns. An
        # unqualified reference to one of these is the construct's output,
        # not an undefined column.
        produced_extra = _model_produced_extra_cols(sql_text)

        # ref() targets resolve to the external model, never a same-named CTE.
        ref_names = {r.lower() for r in (sd.get('refs') or [])}

        resolved = _resolve_cte_columns(tree, max_depth=10, known=shapes)

        scopes = [('final_select', tree)]
        for cte in tree.ctes:
            nm = (cte.alias or '').lower()
            if nm and isinstance(cte.this, exp.Select):
                scopes.append((nm, cte.this))

        found = {}  # column -> {clauses, scope, relations}
        for scope_name, sel in scopes:
            rels = _scope_relations(sel, resolved, shapes=shapes,
                                    ref_names=ref_names)
            if rels is None:
                continue
            alias_set = {ex.alias.lower() for ex in sel.expressions
                         if isinstance(ex, exp.Alias) and ex.alias}

            def _check(col, clause):
                if col.is_star:
                    return
                cname = col.name.lower()
                if not cname or cname == '__jinja__':
                    return
                qual = (col.table or '').lower()
                if qual:
                    if qual not in rels:
                        return  # unknown alias context — stay conservative
                    defined = cname in rels[qual]
                else:
                    defined = (any(cname in s for s in rels.values())
                               or cname in alias_set
                               or cname in produced_extra)
                if not defined:
                    # SQL date-part keyword parsed as a pseudo-column inside a
                    # date function (DATEADD/DATE_TRUNC/...). Never a real
                    # column reference.
                    if not qual and cname in _SQL_DATE_PARTS:
                        return
                    entry = found.setdefault(cname, {
                        'clauses': set(), 'scope': scope_name,
                        'input_relations': sorted(rels.keys()),
                    })
                    entry['clauses'].add(clause)

            for ex in sel.expressions:
                for col in ex.find_all(exp.Column):
                    _check(col, 'select')
            group = sel.args.get('group')
            if group is not None:
                for col in group.find_all(exp.Column):
                    _check(col, 'group_by')

        for cname, info in sorted(found.items()):
            rows.append({
                'model': name,
                'column': cname,
                'clauses': sorted(info['clauses']),
                'scope': info['scope'],
                'input_relations': info['input_relations'],
                'sql_path': sd.get('path'),
                'confidence': 'high',
                'why_agent_fails': (
                    f'{name} references `{cname}` in '
                    f'{" and ".join(sorted(info["clauses"]))} but no input '
                    f'relation ({", ".join(info["input_relations"])}) '
                    'produces it — the query fails at compile/run time.'),
            })
    return rows


def _cte_passthrough_bases(tree, sql_data):
    """Map CTE aliases to the model they are a grain-preserving view of.

    A CTE maps to model M when its body selects FROM a single relation
    (no joins, no GROUP BY, no DISTINCT) that is M itself or another CTE
    that maps to M. Aggregating or joining CTEs map to nothing — joining
    them is not a direct fan-out on M.
    """
    from sqlglot import exp

    ctes_by_name = {}
    for cte in tree.ctes:
        nm = (cte.alias or '').lower()
        if nm and isinstance(cte.this, exp.Select):
            ctes_by_name[nm] = cte.this

    bases = {}

    def _base(nm, depth=0):
        if depth > 10:
            return None
        if nm in bases:
            return bases[nm]
        sel = ctes_by_name.get(nm)
        if sel is None:
            return nm if nm in sql_data else None
        if (sel.args.get('joins') or sel.args.get('group')
                or sel.args.get('distinct')):
            bases[nm] = None
            return None
        from_clause = sel.args.get('from_') or sel.args.get('from')
        if (not isinstance(from_clause, exp.From)
                or not isinstance(from_clause.this, exp.Table)):
            bases[nm] = None
            return None
        bases[nm] = _base(from_clause.this.name.lower(), depth + 1)
        return bases[nm]

    for nm in ctes_by_name:
        _base(nm)
    return bases


def _build_fan_out_joins(models_dict, sql_data, columns, dialect=None):
    """Flag models joined by 2+ downstream models with no unique test on
    the join key — the deterministic fan-out-risk catalog.

    For each downstream model, every JOIN target is resolved to an
    underlying model: directly (the relation IS a model after ref
    substitution) or through a grain-preserving passthrough CTE
    (`payments as (select * from <model>)`). The joined model's side of
    each ON equality gives the join key. A (model, join_column) pair is
    flagged when 2+ distinct downstream models join it and no uniqueness
    guarantee covers it.

    A join key is covered when, for every individual join clause that uses
    it, the full set of keys in that clause satisfies a uniqueness
    guarantee on the joined model:
    - a column-level `unique` test on a key in the clause, or
    - the model's tested PK is among the clause keys, or
    - a `dbt_utils.unique_combination_of_columns` tuple is a subset of the
      clause keys (joining on the whole tuple, or a superset, cannot fan
      out; joining on a strict subset still can, so it stays flagged).
    """
    from sqlglot import exp

    # (model_lower, column_lower) pairs covered by a column-level unique test
    unique_cols = set()
    for c in columns:
        for t in (c.get('tests') or []):
            if t.split(':')[0] == 'unique':
                unique_cols.add((c['model'].lower(), c['column'].lower()))

    joined_by = defaultdict(lambda: defaultdict(set))  # M -> col -> {downstream}
    join_sql = {}  # (M, col) -> sample ON snippet
    # M -> list of frozenset(keys), one per individual JOIN clause. Lets the
    # coverage test see which keys were used together in a single join.
    join_keysets = defaultdict(list)

    for dname, sd in sorted((sql_data or {}).items()):
        sql_text = _read_model_sql(sd)
        if not sql_text:
            continue
        clean = _strip_jinja_resolving_refs(sql_text)
        try:
            tree = sqlglot.parse_one(clean, dialect=dialect)
        except Exception:
            continue
        if not isinstance(tree, exp.Select):
            continue
        cte_bases = _cte_passthrough_bases(tree, sql_data)

        for node in tree.walk():
            n = node[0] if isinstance(node, tuple) else node
            if not isinstance(n, exp.Join):
                continue
            target = n.this
            if not isinstance(target, exp.Table):
                continue
            tname = target.name.lower()
            if tname in cte_bases:
                model = cte_bases[tname]  # local CTE shadows any model name
            else:
                model = tname if tname in sql_data else None
            if not model or model == dname:
                continue
            alias = (target.alias or target.name).lower()
            on = n.args.get('on')
            if on is None:
                continue
            clause_keys = set()
            for eq in on.find_all(exp.EQ):
                le, ri = eq.left, eq.right
                if not (isinstance(le, exp.Column)
                        and isinstance(ri, exp.Column)):
                    continue
                key = None
                if (le.table or '').lower() == alias:
                    key = le.name.lower()
                elif (ri.table or '').lower() == alias:
                    key = ri.name.lower()
                elif le.name.lower() == ri.name.lower():
                    key = le.name.lower()
                if key:
                    clause_keys.add(key)
                    joined_by[model][key].add(dname)
                    join_sql.setdefault((model, key),
                                        eq.sql(dialect=dialect)[:200])
            if clause_keys:
                join_keysets[model].append(frozenset(clause_keys))

    def _key_is_covered(model, col, md):
        """True when every join clause that uses `col` against `model` is
        backed by a uniqueness guarantee spanning that clause's keys."""
        pk = (md.get('pk_column') or '').lower() if md.get('has_pk_test') else None
        combos = [frozenset(c) for c in (md.get('unique_combinations') or [])]
        clauses = [ks for ks in join_keysets.get(model, []) if col in ks]
        if not clauses:
            return False
        for ks in clauses:
            covered = any((model.lower(), k) in unique_cols for k in ks)
            if not covered and pk and pk in ks:
                covered = True
            if not covered and any(combo <= ks for combo in combos):
                covered = True
            if not covered:
                return False
        return True

    rows = []
    for model, by_col in sorted(joined_by.items()):
        for col, downstreams in sorted(by_col.items()):
            if len(downstreams) < 2:
                continue
            md = models_dict.get(model, {})
            if _key_is_covered(model, col, md):
                continue
            rows.append({
                'model': model,
                'join_column': col,
                'downstream_models': sorted(downstreams),
                'inbound_join_count': len(downstreams),
                'has_unique_test': False,
                'sample_on_condition': join_sql.get((model, col)),
                'confidence': 'high',
                'verification_query': (
                    f"select {col}, count(*) as n "
                    f"from {{{{ ref('{model}') }}}} "
                    f"group by {col} having count(*) > 1 limit 20"),
                'why_agent_fails': (
                    f'{len(downstreams)} downstream models join {model} on '
                    f'`{col}` but nothing guarantees `{col}` is unique — '
                    'duplicate keys silently multiply joined rows.'),
            })
    return rows


# ── Description-contradicts-SQL catalog ──────────────────────────────────────

_DESC_ALL_RE = re.compile(
    r'\b(all rows|every row|all records|all entries|entire (?:set|table|universe)|'
    r'complete(?:ly)?\s+(?:set|list|view|data)|full(?:\s+)(?:set|table|dataset|list)|'
    r'no\s+(?:filter|exclusion|restriction)|unfiltered|'
    r'toutes? les lignes|toutes? les donn[ée]es|tout(?:e|es)? les|'
    r'l[\'e]ensemble\s+des|sans\s+filtre)\b',
    re.IGNORECASE)

# Verbs / nouns the description makes agg-level claims with
_DESC_COUNT_RE = re.compile(
    r'\b(distinct count|count of|number of|how many|nombre de)\b', re.IGNORECASE)
_DESC_SUM_RE = re.compile(
    r'\b(sum of|total of|total\s+\w+|somme de|montant total)\b', re.IGNORECASE)
_DESC_AVG_RE = re.compile(
    r'\b(average|mean|moyenne)\b', re.IGNORECASE)

# SQL keywords / operators that are not real column identifiers — stripped
# before checking whether a WHERE clause's columns are disclosed in the prose.
_WHERE_NOISE_TOKENS = frozenset([
    'and', 'or', 'not', 'null', 'is', 'in', 'like', 'ilike', 'between',
    'where', 'select', 'from', 'true', 'false', 'case', 'when', 'then',
    'else', 'end', 'exists', 'as', 'on', 'coalesce', 'cast', 'lower',
    'upper', 'current_date', 'current_timestamp', 'date', 'interval',
])
_WHERE_IDENT_RE = re.compile(r'[a-z_][a-z0-9_]{2,}')

# A SUM whose argument is a 0/1 flag or boolean IS a count, so "description says
# COUNT, SQL uses SUM" is not a real mismatch. These argument shapes mark that
# case so it is not flagged.
_FLAG_SUM_ARG_RE = re.compile(
    r'(case\s+when|then\s+1\b|1\s+else\s+0|0\s+else\s+1|::\s*int|::\s*bool|'
    r'\bcoalesce\b|\b\w*_flag\b|\bis_\w+|\bhas_\w+|\bflag\b|\bindicator\b|'
    r'\bcnt\b|\bcount\b)', re.IGNORECASE)


def _build_potential_unit_drift(sql_data):
    """Collect unit / currency drift candidates across all parsed models.

    Reads the per-model `unit_drift` rows produced by `parse_sql_files`
    and emits a flat list with the model name attached. Models that fell
    back to regex column extraction contribute nothing (their `unit_drift`
    is empty by construction). Output rows look like:
        {model, column, expression, aliased_as}
    """
    rows = []
    for name, sd in (sql_data or {}).items():
        if sd.get('parse_fallback'):
            continue
        for hit in sd.get('unit_drift') or []:
            rows.append({
                'model': name,
                'column': hit['column'],
                'expression': hit['expression'],
                'aliased_as': hit.get('aliased_as'),
            })
    rows.sort(key=lambda r: (r['model'], r['column']))
    return rows


def _build_description_contradicts_sql(models_dict, columns, sql_data, issues):
    """Catalog of descriptions that demonstrably contradict the SQL they describe.

    Returns a list of contradiction records. Each record names the exact model,
    column (or model-level), contradiction type, and the pair of evidence
    strings (what the description says vs what the SQL does). This is the
    highest-signal description-quality finding: unlike 'description is missing'
    or 'description is short', a contradiction means an agent reading the
    description and writing SQL will be actively misled.

    Types emitted:
    - copy_paste: N columns in the same model share an identical description.
      Already detected by cross_reference() as issues.copy_paste_descriptions.
      Promoted here so synthesis treats it as a Blocker, not an Appendix row.
    - model_scope_contradiction: model description says 'all / toutes /
      entire / unfiltered' but SQL has a non-trivial WHERE clause.
    - measure_agg_mismatch: column description describes a different
      aggregation than the SQL expression uses (description says 'count of
      customers' but SQL is SUM(target)).
    """
    rows = []

    # 1) Copy-paste descriptions (already computed in issues)
    for entry in (issues or {}).get('copy_paste_descriptions', []) or []:
        rows.append({
            'kind': 'copy_paste',
            'model': entry['model'],
            'columns': entry.get('items') or [],
            'evidence_description': entry.get('shared_description', ''),
            'evidence_sql': None,
            'why_agent_fails': (
                f"{len(entry.get('items') or [])} columns in "
                f"{entry['model']} share an identical description — "
                "an agent selecting by description will pick the wrong one."),
        })

    # 2) Model scope contradiction: description promises totality but SQL
    #    has a real WHERE clause.
    for mname, md in models_dict.items():
        snippets = md.get('sql_snippets') or {}
        where_clauses = [w for w in (snippets.get('where_clauses') or [])
                         if not TRIVIAL_WHERE_RE.match(w)]
        if not where_clauses:
            continue
        desc = md.get('description_text') or ''
        if not desc:
            continue
        if _DESC_ALL_RE.search(desc):
            # If the description itself names the filter (the WHERE column
            # appears in the prose), the scope is disclosed, not contradicted —
            # e.g. "...all rows where disqualified_encounter_flag = 0". Don't
            # flag a disclosed filter as a hidden scope contradiction.
            wc = where_clauses[0]
            where_idents = {t for t in _WHERE_IDENT_RE.findall(wc.lower())
                            if t not in _WHERE_NOISE_TOKENS}
            desc_l = desc.lower()
            if any(tok in desc_l for tok in where_idents):
                continue
            rows.append({
                'kind': 'model_scope_contradiction',
                'model': mname,
                'columns': [],
                'evidence_description': desc[:300],
                'evidence_sql': where_clauses[0][:300],
                'why_agent_fails': (
                    f"{mname} description claims totality but SQL filters rows. "
                    "Agent will believe the mart is the full universe and "
                    "undercount or misattribute."),
            })

    # 3) Column-level measure/agg mismatch. Only runs when SQL source is
    #    available and a column description makes an agg claim.
    # Build a lookup: model -> { column_lower -> [sql_expr_fragments] }
    # using a lightweight regex over final SELECT-ish text. Kept
    # conservative — only flags when we can tie the column name to an
    # explicit AGG(...) or SUM/COUNT/AVG usage.
    agg_by_col = defaultdict(lambda: defaultdict(list))
    agg_re = re.compile(
        r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(\s*(?:DISTINCT\s+)?([^)]{0,120})\)\s*'
        r'(?:AS\s+)?(\w+)?',
        re.IGNORECASE)
    for mname, sd in (sql_data or {}).items():
        sql_text = ''
        sql_path = sd.get('path')
        if sql_path:
            try:
                sql_text = Path(sql_path).read_text(encoding='utf-8')
            except Exception:
                sql_text = ''
        if not sql_text:
            continue
        sql_text = strip_jinja_comments(sql_text)
        for m in agg_re.finditer(sql_text):
            agg_fn = m.group(1).upper()
            alias = (m.group(3) or '').strip().lower()
            inner = (m.group(2) or '').strip()
            if alias:
                agg_by_col[mname][alias].append((agg_fn, inner))

    for c in columns:
        mname = c['model']
        col_lower = c['column'].lower()
        desc = c.get('description_text') or ''
        if not desc:
            continue
        aggs = agg_by_col.get(mname, {}).get(col_lower)
        if not aggs:
            continue
        agg_fn, _inner = aggs[0]
        d_claims_count = bool(_DESC_COUNT_RE.search(desc))
        d_claims_sum = bool(_DESC_SUM_RE.search(desc))
        d_claims_avg = bool(_DESC_AVG_RE.search(desc))
        mismatch = None
        if agg_fn == 'SUM' and d_claims_count and not d_claims_sum:
            # SUM of a 0/1 flag, boolean, or CASE WHEN ... THEN 1 IS a count,
            # so this is not a real description-vs-SQL mismatch.
            if _FLAG_SUM_ARG_RE.search(_inner):
                continue
            mismatch = 'description says COUNT, SQL uses SUM'
        elif agg_fn == 'COUNT' and d_claims_sum and not d_claims_count:
            mismatch = 'description says SUM/total, SQL uses COUNT'
        elif agg_fn == 'AVG' and (d_claims_count or d_claims_sum) and not d_claims_avg:
            mismatch = f'description says {"COUNT" if d_claims_count else "SUM"}, SQL uses AVG'
        elif agg_fn in ('SUM', 'COUNT', 'AVG') and d_claims_avg and agg_fn != 'AVG':
            mismatch = f'description says AVG, SQL uses {agg_fn}'
        if mismatch:
            rows.append({
                'kind': 'measure_agg_mismatch',
                'model': mname,
                'columns': [c['column']],
                'evidence_description': desc[:300],
                'evidence_sql': f'{agg_fn}(...) as {col_lower}',
                'why_agent_fails': mismatch
                                   + ' — agent using the description to pick a metric will compute the wrong number.',
            })

    rows.sort(key=lambda r: (
        {'copy_paste': 0, 'model_scope_contradiction': 1,
         'measure_agg_mismatch': 2}.get(r['kind'], 9),
        r['model']))
    return rows


# ── Catalogs (granular appendix data) ────────────────────────────────────────

def build_catalogs(models_dict, columns, concept_index, sql_data,
                   issues=None, seeds=None, project_root=None,
                   manifest_used=False, dialect=None):
    """Build pre-computed catalogs that synthesis can emit directly as
    appendix sections. These complement the narrative root-issue framing with
    granular reference data: every missing description, every weak one, every
    convention drift. Never a replacement for root-cause synthesis — just a
    ledger the LLM doesn't have to re-derive.
    """
    # ── missing_descriptions ───────────────────────────────────────────
    missing_col_descs = []  # [(model, column, layer)]
    weak_col_descs = []     # [(model, column, text, reason)]
    missing_model_descs = []  # [(model, layer)]

    for c in columns:
        has_d = c.get('has_description')
        mname = c['model']
        md = models_dict.get(mname, {})
        layer = md.get('layer', '')
        if not has_d:
            missing_col_descs.append({
                'model': mname, 'column': c['column'], 'layer': layer,
            })
        else:
            text = c.get('description_text') or ''
            # Use same heuristics as classify_desc but surface the reason
            tl = text.strip().lower().rstrip('.')
            reason = None
            if len(text.strip()) < 10:
                reason = 'restates_name_or_too_short'
            elif any(p in tl for p in PLACEHOLDER_WORDS):
                reason = 'placeholder'
            elif tl in GENERIC_TECH_DESCS:
                reason = 'generic_technical'
            else:
                simple = c['column'].replace('_', ' ').lower()
                variants = {c['column'].lower(), simple, f'the {simple}'}
                if tl in {v.rstrip('.') for v in variants}:
                    reason = 'restates_name'
            if reason:
                weak_col_descs.append({
                    'model': mname, 'column': c['column'], 'layer': layer,
                    'text': text, 'reason': reason,
                })

    for mname, md in models_dict.items():
        if not md.get('has_description'):
            missing_model_descs.append({
                'model': mname, 'layer': md.get('layer', ''),
                'has_yaml': bool(md.get('yaml_path')),
            })

    # ── convention_drift: suffix + prefix tallies and mixing flags ────────
    # Temporal suffix tally (on columns matching _at/_date/_timestamp)
    suffix_tally = defaultdict(list)  # suffix -> [(model, column)]
    for c in columns:
        m = TEMPORAL_SUFFIX_RE.search(c['column'].lower())
        if m:
            suffix_tally[m.group(1)].append(f"{c['model']}.{c['column']}")

    temporal_mix = None
    if len(suffix_tally) > 1:
        temporal_mix = {
            'suffixes_in_use': sorted(suffix_tally.keys()),
            'counts': {k: len(v) for k, v in suffix_tally.items()},
            'examples': {k: v[:3] for k, v in suffix_tally.items()},
        }

    # Boolean prefix tally
    bool_tally = defaultdict(list)
    for c in columns:
        m = BOOLEAN_PREFIX_RE.match(c['column'].lower())
        if m:
            bool_tally[m.group(1)].append(f"{c['model']}.{c['column']}")

    boolean_mix = None
    if len(bool_tally) > 1:
        boolean_mix = {
            'prefixes_in_use': sorted(bool_tally.keys()),
            'counts': {k: len(v) for k, v in bool_tally.items()},
            'examples': {k: v[:3] for k, v in bool_tally.items()},
        }

    # Mart prefix convention: look at core-layer model names
    mart_prefixes = defaultdict(list)
    for mname, md in models_dict.items():
        if md.get('layer') != 'core':
            continue
        pm = MART_PREFIX_RE.match(mname)
        if pm:
            mart_prefixes[pm.group(1)].append(mname)
        else:
            mart_prefixes['(none)'].append(mname)

    mart_prefix_mix = None
    # Flag if more than one prefix convention is in use AND at least one has 2+ models
    # (A project with 1 fct_ and 4 nouns is a clear mix; 1 fct_ with 1 noun is noise)
    if len(mart_prefixes) > 1:
        mart_prefix_mix = {
            'prefixes_in_use': sorted(mart_prefixes.keys()),
            'counts': {k: len(v) for k, v in mart_prefixes.items()},
            'examples': {k: v[:5] for k, v in mart_prefixes.items()},
        }

    convention_drift = {
        'temporal_suffix_mix': temporal_mix,
        'boolean_prefix_mix': boolean_mix,
        'mart_prefix_mix': mart_prefix_mix,
    }

    # ── concept_variants: same-concept-different-name clusters ──────────
    # EVIDENCE-BASED: clusters are inferred from SQL `X as Y` aliases found in
    # the project's own code. No a priori alias dictionary — we do not
    # presume "user" means "customer" etc., since whether those are the same
    # entity is project-specific. If the project never renames, this catalog
    # stays empty.
    #
    # Each alias edge is recorded with the model it was observed in so the
    # finding is citable. Clusters are emitted only when the union-find
    # group spans 2+ distinct names AND at least one name appears in >1 model
    # (otherwise it's a trivial single-hop rename with no agent-confusion
    # risk).
    aliases_per_model = {m: s.get('column_aliases', [])
                         for m, s in sql_data.items()}
    generic_pivots = find_generic_pivots(aliases_per_model)

    uf = _UnionFind()
    edge_evidence = defaultdict(list)  # frozenset({a, b}) -> [(model, raw_pair)]
    for mname, pairs in aliases_per_model.items():
        for src, alias in pairs:
            # Do not propagate identity through an evidence-based generic
            # pivot. The edge is still recorded for reference but not unioned.
            if src in generic_pivots or alias in generic_pivots:
                continue
            uf.union(src, alias)
            edge_evidence[frozenset({src, alias})].append(mname)

    # Build a column-name → set-of-models lookup for any column observed
    # anywhere (YAML columns + SQL output columns).
    name_to_models = defaultdict(set)
    for c in columns:
        name_to_models[c['column'].lower()].add(c['model'])
    for mname, sinfo in sql_data.items():
        for cn in sinfo.get('columns') or []:
            name_to_models[cn.lower()].add(mname)

    concept_variants = []
    for root, members in uf.groups().items():
        # Only keep groups where at least one member appears in a real model
        # column (filters out aliases whose sides never land in any output).
        materialized = [n for n in members if n in name_to_models]
        if len(materialized) < 2:
            continue
        evidence_pairs = []
        # Per-model alias contribution for spurious-cluster detection: if one
        # model's SELECT renames 6+ of the cluster's members, the cluster is
        # probably a catch-all (e.g., a staging model re-labeling many source
        # columns) rather than a real same-concept-different-name mapping.
        model_contributes = defaultdict(set)
        for edge, mnames in edge_evidence.items():
            if edge.issubset(members):
                a, b = sorted(list(edge))
                evidence_pairs.append({
                    'pair': [a, b],
                    'observed_in': sorted(set(mnames)),
                })
                for mname in mnames:
                    model_contributes[mname].update(edge)
        if model_contributes and max(
                len(v) for v in model_contributes.values()) >= 6:
            continue
        # Build per-model appearance list
        appearances = []
        for name in sorted(materialized):
            for mname in sorted(name_to_models[name]):
                appearances.append({'model': mname, 'column': name})
        concept_variants.append({
            'canonical': root,
            'distinct_names': sorted(materialized),
            'evidence': evidence_pairs,
            'models': appearances[:30],
        })
    concept_variants.sort(key=lambda v: (-len(v['distinct_names']),
                                         -len(v['models']), v['canonical']))

    # ── same_name_different_grain: column name shared across models with
    #    different layers (rough proxy for different grain). Not perfect but
    #    catches the amount-in-staging-vs-mart case.
    name_to_layers = defaultdict(set)
    name_to_examples = defaultdict(list)
    for c in columns:
        mname = c['model']
        md = models_dict.get(mname, {})
        layer = md.get('layer', '')
        col = c['column'].lower()
        name_to_layers[col].add(layer)
        if len(name_to_examples[col]) < 5:
            name_to_examples[col].append(f"{mname}.{c['column']}")

    same_name_diff_grain = []
    # Columns whose meaning is likely grain-sensitive (amounts, counts, rates)
    # and appear in multiple layers
    grain_sensitive_re = re.compile(
        r'(amount|count|total|sum|avg|revenue|value|price|cost|qty|quantity)',
        re.IGNORECASE)
    for col, layers in name_to_layers.items():
        if not grain_sensitive_re.search(col):
            continue
        # Drop bare 'id', single-layer cases
        if len(layers) < 2:
            continue
        # Drop staging-only passthroughs (staging + staging)
        non_staging = {l for l in layers if l != 'staging'}
        if len(layers) == 2 and 'staging' in layers and len(non_staging) == 1:
            # staging + one other is a potential grain change (aggregation).
            pass
        same_name_diff_grain.append({
            'column': col,
            'layers': sorted(layers),
            'examples': name_to_examples[col],
        })

    phantom_by_model, phantom_suppressed = _build_phantom_by_model(
        issues or {}, sql_data=sql_data, manifest_used=manifest_used)
    enum_value_gaps = _build_enum_value_gaps(columns, models_dict)
    seeds_not_tested = _build_seeds_not_tested(
        seeds or [], columns, project_root)
    unit_variants = _build_unit_variants(columns)
    unprefixed_booleans = _build_unprefixed_booleans(columns)

    overlapping_within_model = _build_overlapping_concept_columns_within_model(
        columns)
    lineage_cycles = _build_lineage_cycles(models_dict)
    yaml_vs_sql_diff = _build_yaml_vs_sql_column_count_diff(models_dict)

    description_contradicts_sql = _build_description_contradicts_sql(
        models_dict, columns, sql_data, issues or {})

    potential_unit_drift = _build_potential_unit_drift(sql_data)

    undefined_column_refs = _build_undefined_column_refs(
        sql_data, manifest_used=manifest_used, dialect=dialect)
    fan_out_joins = _build_fan_out_joins(
        models_dict, columns=columns, sql_data=sql_data, dialect=dialect)

    # Effective description coverage: raw coverage minus columns whose
    # descriptions are actively misleading (weak + copy-paste + contradicts
    # SQL) and columns that are "documented" in YAML but not emitted by SQL
    # (phantoms). The gap between raw and effective is the share of docs
    # that an agent cannot trust.
    total_cols = len(columns) or 1
    documented_cols = sum(1 for c in columns if c.get('has_description'))
    weak_set = {(r['model'], r['column']) for r in weak_col_descs}
    # Only HIGH-confidence phantoms count against effective coverage. Models
    # whose phantom findings were suppressed (macro / Jinja-set-block generated,
    # no compiled manifest) are excluded by _build_phantom_by_model — their
    # columns are documentation the static parser merely couldn't see, NOT
    # genuinely-absent columns, so counting them would deflate the number with
    # false positives on macro-heavy projects.
    phantom_set = {(r['model'], col)
                   for r in phantom_by_model
                   for col in r.get('phantoms', []) or []}
    contradicting_set = set()
    for r in description_contradicts_sql:
        if r['kind'] == 'copy_paste':
            for col in r.get('columns') or []:
                contradicting_set.add((r['model'], col))
        elif r['kind'] == 'measure_agg_mismatch':
            for col in r.get('columns') or []:
                contradicting_set.add((r['model'], col))
    untrusted = weak_set | contradicting_set | phantom_set
    effective_documented = sum(
        1 for c in columns
        if c.get('has_description')
        and (c['model'], c['column']) not in untrusted)
    effective_coverage = {
        'total_columns': total_cols,
        'documented_columns': documented_cols,
        'untrusted_columns': len(untrusted),
        'effective_documented_columns': effective_documented,
        'raw_coverage_pct': round(100 * documented_cols / total_cols, 1),
        'effective_coverage_pct': round(
            100 * effective_documented / total_cols, 1),
        'breakdown': {
            'weak_descriptions': len(weak_set),
            'phantom_documented': len(phantom_set),
            'contradicts_sql': len(contradicting_set),
        },
    }

    return {
        'missing_column_descriptions': missing_col_descs,
        'weak_column_descriptions': weak_col_descs,
        'missing_model_descriptions': missing_model_descs,
        'convention_drift': convention_drift,
        'concept_variants': concept_variants,
        'same_name_different_grain': same_name_diff_grain,
        'phantom_columns_by_model': phantom_by_model,
        'phantom_columns_suppressed_no_manifest': phantom_suppressed,
        'enum_value_gaps': enum_value_gaps,
        'seeds_not_tested': seeds_not_tested,
        'unit_variants': unit_variants,
        'unprefixed_booleans': unprefixed_booleans,
        'overlapping_concept_columns_within_model': overlapping_within_model,
        'lineage_cycles': lineage_cycles,
        'yaml_vs_sql_column_count_diff': yaml_vs_sql_diff,
        'description_contradicts_sql': description_contradicts_sql,
        'effective_description_coverage': effective_coverage,
        'potential_unit_drift': potential_unit_drift,
        'undefined_column_refs': undefined_column_refs,
        'fan_out_joins': fan_out_joins,
        'phantom_columns_resolved_by_lineage': list(
            (issues or {}).get('phantom_columns_resolved_by_lineage') or []),
    }


# ── Manifest support ─────────────────────────────────────────────────────────

def try_load_manifest(project_path):
    """Load target/manifest.json if available. Returns None silently if not."""
    manifest_path = project_path / 'target' / 'manifest.json'
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding='utf-8') as f:
            data = json.load(f)
        nodes = data.get('nodes', {})
        result = {}
        for key, node in nodes.items():
            if not key.startswith('model.'):
                continue
            name = node.get('name', key.split('.')[-1])
            result[name] = {
                'compiled_code': node.get('compiled_code'),
                'depends_on': [n.split('.')[-1]
                               for n in (node.get('depends_on', {})
                                         .get('nodes', []))],
                'columns': node.get('columns', {}),
                'materialized': (node.get('config', {})
                                 .get('materialized')),
            }
        return result
    except Exception:
        return None


# ── Assembly ─────────────────────────────────────────────────────────────────

def build_inventory(project_path):
    project_path = Path(project_path).resolve()
    config = parse_project_config(project_path)
    if not config:
        return {'error': 'no_dbt_project',
                'message': f'No dbt_project.yml at {project_path}'}

    doc_blocks = build_doc_lookup(project_path)
    dialect = _detect_dialect(project_path)
    manifest = try_load_manifest(project_path)
    # A manifest from `dbt parse` has no compiled_code and therefore no
    # resolved Jinja — the confidence check should treat it as no manifest.
    # Only `dbt compile` / `dbt run` produce compiled_code we can trust.
    manifest_has_compiled = bool(manifest) and any(
        n.get('compiled_code') for n in manifest.values())

    # Discover files
    yaml_files, sql_files = [], []
    for mp in config['model_paths']:
        yaml_files.extend(find_files(mp, '*.yml'))
        yaml_files.extend(find_files(mp, '*.yaml'))
        sql_files.extend(find_files(mp, '*.sql'))
    # Also check root for top-level YAML (metrics, semantic models)
    for f in project_path.glob('*.yml'):
        if f not in yaml_files:
            yaml_files.append(f)

    # Parse
    yd = parse_yaml_files(yaml_files, doc_blocks)
    models = yd['models']
    columns = yd['columns']
    sources = yd['sources']
    sd = parse_sql_files(sql_files, dialect=dialect)

    # Merge SQL data into models; create entries for SQL-only models
    for sf in sql_files:
        name = sf.stem
        sinfo = sd.get(name, {})

        # If manifest has compiled_code, re-extract snippets AND columns from
        # it (compiled code has Jinja resolved, so regexes and the column
        # parser work on the real output). Re-extracting columns matters for
        # phantom detection: raw SQL with a Jinja for-loop such as
        # `{{ pm }}_amount` strips to a single mangled `_amount` token, so the
        # real generated columns look phantom even though the manifest resolves
        # them. Only override when the compiled parse is confident (count > 0);
        # otherwise keep the raw result rather than nuke a usable column set.
        snippets = sinfo.get('sql_snippets')
        if manifest and name in manifest:
            compiled = manifest[name].get('compiled_code')
            if compiled:
                snippets = extract_sql_snippets(compiled)
                c_names, c_count, _cm, _cr = extract_sql_columns_with_method(
                    compiled, dialect=dialect)
                if c_count and c_count > 0:
                    sinfo['columns'] = c_names
                    sinfo['column_count'] = c_count
                    sd[name] = sinfo

        if name in models:
            m = models[name]
            m['sql_path'] = str(sf)
            m['outbound_refs'] = sinfo.get('refs', [])
            m['column_count_sql'] = sinfo.get('column_count')
            m['sql_snippets'] = snippets
            if m['materialization'] == 'unknown' and sinfo.get('materialization'):
                m['materialization'] = sinfo['materialization']
        else:
            models[name] = {
                'name': name, 'yaml_path': None, 'sql_path': str(sf),
                'layer': None,
                'has_description': False, 'description_quality': 'none',
                'description_text': None,
                'grain_declared': False, 'grain_statement': None,
                'column_count_yaml': 0,
                'column_count_sql': sinfo.get('column_count'),
                'columns_with_descriptions': 0,
                'has_pk_test': False, 'pk_column': None,
                'unique_combinations': [],
                'inbound_refs': 0,
                'outbound_refs': sinfo.get('refs', []),
                'has_semantic_model': False,
                'materialization': sinfo.get('materialization') or 'unknown',
                'sql_snippets': snippets,
            }

    # Layer classification
    for name, m in models.items():
        m['layer'] = classify_layer(name, m.get('sql_path') or m.get('yaml_path') or '')

    # Inbound refs
    ref_counts = defaultdict(int)
    for m in models.values():
        for ref in m.get('outbound_refs', []):
            ref_counts[ref] += 1
    for name, m in models.items():
        m['inbound_refs'] = ref_counts.get(name, 0)

    # Seeds and snapshots
    seeds = []
    for sp in config['seed_paths']:
        for f in find_files(sp, '*.csv'):
            seeds.append({'name': f.stem, 'path': str(f), 'appears_to_define': ''})
    snapshots = []
    for sp in config['snapshot_paths']:
        for f in find_files(sp, '*.sql'):
            snapshots.append(f.stem)

    # Package/extension deps declared but not installed: refs into those
    # packages (or user-supplied extension points the package expects the
    # consumer to define) cannot be resolved source-only, so they must not be
    # reported as broken refs. Mirrors the no-manifest phantom suppression.
    packages_present = ((project_path / 'packages.yml').exists()
                        or (project_path / 'dependencies.yml').exists())
    deps_installed = (project_path / 'dbt_packages').is_dir()
    packages_unresolved = (packages_present and not deps_installed
                           and not manifest_has_compiled)

    # Cross-reference
    issues = cross_reference(models, sd, sources, seeds, snapshots, columns,
                             dialect=dialect,
                             packages_unresolved=packages_unresolved)

    # Relationships
    sem_models = yd['semantic_layer']['semantic_models']
    relationships = build_relationships(columns, models, sem_models)

    # Test summary
    test_summary = build_test_summary(columns, models_dict=models)

    # Concept index
    concept_index = build_concept_index(models, columns)

    # Review queue
    review_queue = build_review_queue(models, columns, concept_index,
                                      sql_data=sd)

    # Catalogs (granular appendix data)
    catalogs = build_catalogs(models, columns, concept_index, sd,
                              issues=issues, seeds=seeds,
                              project_root=project_path,
                              manifest_used=manifest_has_compiled,
                              dialect=dialect)

    # Count only true schema files (models/sources/etc.), not dbt_project.yml
    # or dbt_packages.yml sitting at project root.
    schema_file_count = sum(1 for f in yaml_files if f.name != 'dbt_project.yml'
                            and f.name != 'packages.yml'
                            and f.name != 'selectors.yml')

    # Final assembly
    models_list = sorted(models.values(), key=lambda m: m['name'])
    return {
        'project_name': config['project_name'],
        'total_models': len(models_list),
        'total_schema_files': schema_file_count,
        'total_sources': len(sources),
        'global_severity_warn': config['global_severity_warn'],
        'has_semantic_layer': len(sem_models) > 0,
        'models': models_list,
        'columns': columns,
        'sources': sources,
        'semantic_layer': yd['semantic_layer'],
        'relationships': relationships,
        'issues': issues,
        'test_summary': test_summary,
        'seeds': seeds,
        'exposures': yd['exposures'],
        'concept_index': concept_index,
        'review_queue': review_queue,
        'catalogs': catalogs,
        'manifest_used': manifest_has_compiled,
        'manifest_present_without_compile': bool(manifest) and not manifest_has_compiled,
        'packages_unresolved': packages_unresolved,
        'packages_unresolved_ref_count': len(
            issues.get('broken_refs_suppressed_no_deps') or []),
        'dialect': dialect,
        'inventory_version': '1.0',
    }


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
    if not path.exists():
        json.dump({'error': 'path_not_found', 'message': str(path)}, sys.stdout)
        sys.exit(1)
    try:
        inv = build_inventory(path)
    except Exception as e:
        json.dump({'error': 'build_failed', 'message': str(e)}, sys.stdout)
        sys.exit(1)
    if 'error' in inv:
        json.dump(inv, sys.stdout)
        sys.exit(1)
    json.dump(inv, sys.stdout, indent=2, default=str)


if __name__ == '__main__':
    main()
