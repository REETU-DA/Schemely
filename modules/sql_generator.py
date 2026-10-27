"""Deterministic, dialect-aware CREATE TABLE script generation.

Same approved schema + same settings => byte-identical script (no timestamps, no randomness).
No ON DELETE / ON UPDATE behaviour is emitted unless the user set it explicitly.
"""
from __future__ import annotations

from .dialects import DIALECTS, resolve_type
from .identifiers import LIMITS, comment_safe, make_unique, quote, shorten
from .schema_designer import ColumnSpec, ForeignKeySpec, Schema, TableSpec, SURROGATE_KEY

ALLOWED_ACTIONS = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}


def order_tables(schema: Schema):
    """-> (ordered tables, set of FK ids that must be added after all CREATE TABLEs)."""
    tables = sorted(schema.active_tables(), key=lambda t: t.name.lower())
    fks = schema.active_fks()
    parents: dict[str, set[str]] = {t.id: set() for t in tables}
    for fk in fks:
        if fk.child_table != fk.parent_table:
            parents[fk.child_table].add(fk.parent_table)
    ordered, done = [], set()
    remaining = list(tables)
    while remaining:
        ready = [t for t in remaining if parents[t.id] <= done]
        if not ready:  # cycle: place the rest alphabetically; their FKs will be deferred
            ready = remaining[:]
            ordered.extend(ready)
            done.update(t.id for t in ready)
            break
        ordered.extend(ready)
        done.update(t.id for t in ready)
        remaining = [t for t in remaining if t.id not in done]
    pos = {t.id: i for i, t in enumerate(ordered)}
    deferred = set()
    for fk in fks:
        if fk.child_table != fk.parent_table and pos[fk.parent_table] > pos[fk.child_table]:
            deferred.add(fk.id)
        elif fk.child_table != fk.parent_table and fk.parent_table in parents and fk.child_table in parents[fk.parent_table]:
            deferred.add(fk.id)  # mutual references
    return ordered, deferred


def _norm_action(a):
    a = (a or "").strip().upper()
    return a if a in ALLOWED_ACTIONS else None


class _Names:
    """Collision-free constraint/index names."""

    def __init__(self, schema: Schema):
        lim = LIMITS[schema.dialect]
        raw = []
        self.keys = []
        for t in sorted(schema.active_tables(), key=lambda t: t.name.lower()):
            if t.pk_columns and t.pk_mode != "none":
                self.keys.append(("pk", t.id, ""))
                raw.append(f"pk_{t.name}")
        for fk in schema.active_fks():
            ct, pt = schema.table(fk.child_table), schema.table(fk.parent_table)
            self.keys.append(("fk", fk.id, ""))
            raw.append(f"fk_{ct.name}_{pt.name}_{'_'.join(ct.col(k).name for k in fk.child_cols)}")
        self.map = dict(zip(self.keys, make_unique([r for r in raw], lim)))
        self.lim = lim
        self.used = {v.lower() for v in self.map.values()}

    def extra(self, prefix: str, *parts: str) -> str:
        base = shorten("_".join([prefix, *parts]), self.lim)
        cand, i = base, 2
        while cand.lower() in self.used:
            suffix = f"_{i}"
            cand, i = shorten(base, self.lim - len(suffix)) + suffix, i + 1
        self.used.add(cand.lower())
        return cand

    def get(self, kind, ident):
        return self.map[(kind, ident, "")]


def _table_constraints(schema: Schema, t: TableSpec, names: _Names, inline_fks: list[ForeignKeySpec]):
    d = schema.dialect
    q = lambda n: quote(n, d)  # noqa: E731
    out = []
    pkcols = [t.col(k) for k in t.pk_columns if t.col(k)]
    sqlite_inline_pk = d == "sqlite" and t.pk_mode == "surrogate"
    if pkcols and t.pk_mode != "none" and not sqlite_inline_pk:
        out.append(f"CONSTRAINT {q(names.get('pk', t.id))} PRIMARY KEY ({', '.join(q(c.name) for c in pkcols)})")
    groups: list[list[ColumnSpec]] = []
    seen: set[frozenset] = set()
    pk_set = frozenset(c.key for c in pkcols)

    def add_group(cols):
        fs = frozenset(c.key for c in cols)
        if fs and fs != pk_set and fs not in seen:
            seen.add(fs)
            groups.append(cols)

    for c in t.active_columns():
        if c.unique and not c.generated:
            add_group([c])
    for g in t.uniques:
        cols = [t.col(k) for k in g]
        if all(c is not None and c.active for c in cols):
            add_group(cols)
    # a FK can only reference PK/UNIQUE columns - add what is technically required
    for fk in schema.active_fks():
        if fk.parent_table == t.id:
            cols = [t.col(k) for k in fk.parent_cols]
            add_group(cols)
    for cols in groups:
        required = any(fk.parent_table == t.id and frozenset(fk.parent_cols) == frozenset(c.key for c in cols) for fk in schema.active_fks())
        nm = names.extra("uq", t.name, *[c.name for c in cols])
        tail = "  -- required so a foreign key can reference these column(s)" if required and not any(
            c.unique for c in cols) and cols not in [[t.col(k) for k in g] for g in t.uniques] else ""
        out.append(f"CONSTRAINT {q(nm)} UNIQUE ({', '.join(q(c.name) for c in cols)})" + ("\x00" + tail if tail else ""))
    for c in t.active_columns():
        if c.check and c.check.strip():
            out.append(f"CONSTRAINT {q(names.extra('ck', t.name, c.name))} CHECK ({c.check.strip()})")
    for fk in inline_fks:
        out.append(_fk_clause(schema, fk, names))
    return out


def _fk_clause(schema: Schema, fk: ForeignKeySpec, names: _Names) -> str:
    d = schema.dialect
    q = lambda n: quote(n, d)  # noqa: E731
    ct, pt = schema.table(fk.child_table), schema.table(fk.parent_table)
    s = (f"CONSTRAINT {q(names.get('fk', fk.id))} FOREIGN KEY ({', '.join(q(ct.col(k).name) for k in fk.child_cols)}) "
         f"REFERENCES {q(pt.name)} ({', '.join(q(pt.col(k).name) for k in fk.parent_cols)})")
    if _norm_action(fk.on_delete):
        s += f" ON DELETE {_norm_action(fk.on_delete)}"
    if _norm_action(fk.on_update):
        s += f" ON UPDATE {_norm_action(fk.on_update)}"
    return s


def _key_column_keys(schema: Schema, t: TableSpec) -> set[str]:
    keys = set(t.pk_columns) | {k for g in t.uniques for k in g} | {c.key for c in t.active_columns() if c.unique}
    for fk in schema.active_fks():
        if fk.child_table == t.id:
            keys |= set(fk.child_cols)
        if fk.parent_table == t.id:
            keys |= set(fk.parent_cols)
    return keys


def _column_def(schema: Schema, t: TableSpec, c: ColumnSpec, keycols: set[str]) -> str:
    d = schema.dialect
    n = quote(c.name, d)
    if c.generated:
        return {"postgresql": f"{n} BIGINT GENERATED ALWAYS AS IDENTITY",
                "mysql": f"{n} BIGINT NOT NULL AUTO_INCREMENT",
                "sqlserver": f"{n} BIGINT IDENTITY(1,1) NOT NULL",
                "sqlite": f"{n} INTEGER PRIMARY KEY AUTOINCREMENT"}[d]
    typ, _ = resolve_type(c.sql_type, d, c.key in keycols, c.max_len)
    not_null = (not c.nullable) or (c.key in t.pk_columns)
    return f"{n} {typ}" + (" NOT NULL" if not_null else "")


def _wrap(dialect: str, body: list[str]) -> list[str]:
    if dialect == "mysql":
        return ["-- MySQL implicitly commits DDL statements, so no explicit transaction is used."] + body
    if dialect == "sqlserver":
        return ["BEGIN TRANSACTION;", ""] + body + ["", "COMMIT TRANSACTION;"]
    return ["BEGIN;", ""] + body + ["", "COMMIT;"]


def generate_schema_sql(schema: Schema, summary: dict | None = None) -> str:
    d = schema.dialect
    q = lambda n: quote(n, d)  # noqa: E731
    ordered, deferred = order_tables(schema)
    names = _Names(schema)
    fks = schema.active_fks()
    staging = schema.staging
    after_all = {fk.id for fk in fks} if (staging and d != "sqlite") else (set() if d == "sqlite" else deferred)

    lines: list[str] = [
        "-- ============================================================================",
        "-- SmartSQL Schema Designer - schema script",
        f"-- Target dialect : {DIALECTS[d]['label']}",
        f"-- Design mode    : {'Relational schema design' if schema.mode == 'relational' else 'Direct import schema'}"
        + ("  (with staging tables)" if staging else ""),
    ]
    if summary:
        lines.append(f"-- Review status  : {summary['Status'].upper()} ({summary['Errors']} validation error(s), "
                     f"{summary['Columns requiring review']} column decision(s) awaiting approval)")
        if summary["Status"] != "Ready":
            lines.append("-- WARNING        : unresolved issues remain - read validation_report.csv before importing data.")
    lines += ["-- Constraints are limited to what was approved; observed data patterns are NOT turned into",
              "-- NOT NULL / UNIQUE constraints automatically. No ON DELETE / ON UPDATE behaviour is assumed.",
              "-- ============================================================================", ""]
    notes = []
    for t in ordered:
        for c in t.active_columns():
            if c.needs_approval and c.sql_type and c.alternative:
                notes.append(f"{t.name}.{c.name}: stored as {c.sql_type}; better type {c.alternative} needs clean data first")
            if c.date_order:
                notes.append(f"{t.name}.{c.name}: dates read as {c.date_order} (day/month order)")
        if t.pk_mode == "surrogate":
            notes.append(f"{t.name}: generated surrogate key; original identifiers kept as normal columns")
    if notes:
        lines.append("-- Assumptions and open items:")
        lines += [f"--   * {comment_safe(n)}" for n in notes]
        lines.append("")

    body: list[str] = []
    if staging:
        body += ["-- ---------------------------------------------------------------------------",
                 "-- PART 1a - staging tables: raw text copies of the source columns (no constraints)",
                 "-- ---------------------------------------------------------------------------"]
        for t in ordered:
            if t.derived_from or t.kind == "custom":
                continue
            cols = [c for c in t.columns if c.source_column is not None and c.include]
            stg = quote(stg_name(t, d), d)
            typ = resolve_type("text", d)[0]
            body.append(f"CREATE TABLE {stg} (")
            body.append(",\n".join(f"    {q(c.name)} {typ}" for c in cols))
            body.append(");")
            body.append("")
        body += ["-- ---------------------------------------------------------------------------",
                 "-- PART 1b - final tables",
                 "-- ---------------------------------------------------------------------------"]
    for t in ordered:
        inline = [fk for fk in fks if fk.child_table == t.id and fk.id not in after_all]
        keycols = _key_column_keys(schema, t)
        cons = _table_constraints(schema, t, names, inline)
        body.append(f"-- Table: {comment_safe(t.name)}   (source: {comment_safe(t.source_file)}"
                    + (f" / {comment_safe(t.sheet)}" if t.sheet else "") + (", extracted entity" if t.kind == "entity" else "") + ")")
        parts = []
        for c in t.active_columns():
            note = ""
            if c.needs_approval and not c.approved:
                note = f"    -- review: {comment_safe(c.reason)[:90]}...\n"
            parts.append(note + "    " + _column_def(schema, t, c, keycols))
        col_block = ",\n".join(parts)
        cons_lines = []
        for x in cons:
            text, _, tail = x.partition("\x00")
            cons_lines.append(("    " + text, tail))
        body.append(f"CREATE TABLE {q(t.name)} (")
        all_items = [col_block] if col_block else []
        rendered_cons = []
        for i, (text, tail) in enumerate(cons_lines):
            rendered_cons.append(text + ("," if i < len(cons_lines) - 1 else "") + tail)
        if rendered_cons:
            body.append(col_block + ",")
            body.extend(rendered_cons)
        else:
            body.append(col_block)
        body.append(")" + (" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if d == "mysql" else "") + ";")
        body.append("")
    # indexes explicitly requested
    idx_lines = []
    for t in ordered:
        groups = [[t.col(k) for k in g] for g in t.indexes]
        groups += [[t.col(k) for k in fk.child_cols] for fk in fks if fk.child_table == t.id and fk.index_child]
        seen = set()
        for cols in groups:
            if not cols or any(c is None or not c.active for c in cols):
                continue
            sig = tuple(c.key for c in cols)
            if sig in seen:
                continue
            seen.add(sig)
            nm = names.extra("idx", t.name, *[c.name for c in cols])
            idx_lines.append(f"CREATE INDEX {q(nm)} ON {q(t.name)} ({', '.join(q(c.name) for c in cols)});")
    if idx_lines:
        body += ["-- Indexes requested by you"] + idx_lines + [""]
    while body and body[-1] == "":
        body.pop()
    lines += _wrap(d, body) if body else []

    late = [fk for fk in fks if fk.id in after_all]
    if late:
        lines.append("")
        if staging:
            lines += ["-- ---------------------------------------------------------------------------",
                      "-- PART 3 - foreign keys: run AFTER the staging data has been validated and loaded into the final tables",
                      "-- ---------------------------------------------------------------------------"]
        else:
            lines += ["-- Foreign keys that close a dependency cycle are added after all tables exist."]
        alter: list[str] = []
        for fk in sorted(late, key=lambda f: (schema.table(f.child_table).name, f.id)):
            ct = schema.table(fk.child_table)
            alter.append(f"ALTER TABLE {q(ct.name)} ADD {_fk_clause(schema, fk, names)};")
        lines += _wrap(d, alter)
    return "\n".join(lines).rstrip() + "\n"


def stg_name(t: TableSpec, dialect: str) -> str:
    return shorten("stg_" + t.name, LIMITS[dialect])
