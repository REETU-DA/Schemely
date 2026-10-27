"""Import preparation: ordering, source->target mapping, loading guidance, staging transforms."""
from __future__ import annotations

import io

import pandas as pd

from .dialects import parse_token, resolve_type
from .identifiers import comment_safe, quote
from .models import Dataset
from .schema_designer import Schema, TableSpec, table_frame
from .sql_generator import order_tables, stg_name


def plan_orders(schema: Schema) -> dict:
    """Creation order, data-loading order and constraint order (they can differ)."""
    ordered, deferred = order_tables(schema)
    fks = schema.active_fks()
    names = {t.id: t.name for t in schema.tables}
    creation = [t.name for t in ordered]
    cyc = bool(deferred)
    if schema.dialect == "sqlite":
        deferred_effective: set[str] = set()
    elif schema.staging:
        deferred_effective = {fk.id for fk in fks}
    else:
        deferred_effective = deferred
    constraint_steps = []
    for t in ordered:
        constraint_steps.append(f"{t.name}: PRIMARY KEY / UNIQUE / CHECK are created with the table")
    for fk in fks:
        when = "after loading all data (ALTER TABLE)" if fk.id in deferred_effective else "with the child table"
        constraint_steps.append(f"{names[fk.child_table]} → {names[fk.parent_table]}: FOREIGN KEY {when}")
    explanation = ""
    if cyc:
        explanation = ("A strict linear order is impossible because some tables reference each other in a cycle. "
                       "Create all tables first, load the data (children may need NULL references first, then update), "
                       "and add the cyclic foreign keys afterwards - the generated script already does this.")
    return {"creation": creation, "loading": list(creation), "constraints": constraint_steps,
            "cycle": cyc, "explanation": explanation, "deferred_fk_ids": sorted(deferred_effective)}


def source_mapping_rows(schema: Schema, datasets: dict[str, Dataset]) -> list[dict]:
    rows = []
    names = {t.id: t.name for t in schema.tables}
    for t in schema.tables:
        ds = datasets.get(t.derived_from or t.dataset_id or "")
        for c in t.columns:
            orig = ""
            if ds is not None and c.source_column in ds.original_columns_map():
                orig = ds.original_columns_map()[c.source_column]
            if not t.include:
                status, target_t, target_c = "table removed (not imported)", "", ""
            elif c.generated:
                status, target_t, target_c = "generated", t.name, c.name
            elif c.moved_to:
                status, target_t, target_c = "moved to extracted table", names.get(c.moved_to, c.moved_to), c.name
            elif not c.include:
                status, target_t, target_c = "excluded (not imported)", "", ""
            else:
                status, target_t, target_c = "imported", t.name, c.name
            rows.append({
                "source_file": t.source_file, "worksheet": t.sheet or "", "source_column": c.source_column or "",
                "source_header_as_written": orig, "target_table": target_t, "target_column": target_c,
                "type_token": c.sql_type if status != "table removed (not imported)" else "",
                "sql_type": resolve_type(c.sql_type, schema.dialect, False, c.max_len)[0] if c.include and t.include and not c.moved_to else "",
                "status": status, "table_kind": t.kind, "note": comment_safe("; ".join(c.notes))})
    return rows


def import_ready_csvs(schema: Schema, datasets: dict[str, Dataset]) -> dict[str, bytes]:
    """CSV per table with only included columns, in target order, header = target names.
    Values are exactly as read from the source (no conversions)."""
    out = {}
    for t in schema.active_tables():
        f = table_frame(t, datasets)
        if f is None:
            continue
        cols = [c for c in t.active_columns() if c.source_column is not None and c.source_column in f.columns]
        if not cols:
            continue
        data = f[[c.source_column for c in cols]].copy()
        data.columns = [c.name for c in cols]
        buf = io.StringIO()
        data.to_csv(buf, index=False, na_rep="")
        out[f"{t.name}.csv"] = buf.getvalue().encode("utf-8")
    return out


def _cast(colsql: str, token: str, date_order: str | None) -> str:
    base, args = parse_token(token)
    raw = f"NULLIF(BTRIM({colsql}), '')"
    if base in ("text", "varchar"):
        return colsql
    if base in ("integer", "bigint"):
        return f"{raw}::{base.upper()}"
    if base == "numeric":
        return f"{raw}::NUMERIC({','.join(map(str, args))})" if args else f"{raw}::NUMERIC"
    if base == "boolean":
        return f"{raw}::BOOLEAN"
    if base == "date":
        if date_order in ("DMY", "MDY"):
            fmt = "DD-MM-YYYY" if date_order == "DMY" else "MM-DD-YYYY"
            return f"TO_DATE(regexp_replace({raw}, '[/.]', '-', 'g'), '{fmt}')"
        return f"{raw}::DATE"
    if base == "timestamp":
        return f"{raw}::TIMESTAMP"
    if base == "timestamptz":
        return f"{raw}::TIMESTAMPTZ"
    if base == "time":
        return f"{raw}::TIME"
    if base == "uuid":
        return f"{raw}::UUID"
    return colsql


def transform_sql(schema: Schema) -> str:
    if schema.dialect != "postgresql":
        return ("-- The staging -> final transformation script is generated for PostgreSQL only.\n"
                "-- For other dialects use the staging tables with your own INSERT ... SELECT statements.\n")
    ordered, _ = order_tables(schema)
    q = lambda n: quote(n, "postgresql")  # noqa: E731
    stg_of = {t.dataset_id: t for t in schema.active_tables() if t.dataset_id}
    orders = {c.date_order for t in ordered for c in t.active_columns()
              if c.date_order and parse_token(c.sql_type)[0] in ("timestamp", "timestamptz")}
    lines = ["-- PART 2 - move validated staging rows into the final tables (PostgreSQL)",
             "-- Run after every staging table has been loaded (see import_preparation).",
             "-- Cast failures here point to values that validation_report.csv flagged: fix them in staging, then re-run.", ""]
    if orders == {"DMY"}:
        lines.append("SET datestyle = 'ISO, DMY';")
    elif orders == {"MDY"}:
        lines.append("SET datestyle = 'ISO, MDY';")
    elif len(orders) > 1:
        lines.append("-- WARNING: timestamp columns use different day/month orders; resolve before running.")
    lines += ["BEGIN;", ""]
    for t in ordered:
        src = t.derived_from or t.dataset_id
        host = stg_of.get(src)
        if host is None or t.kind == "custom":
            continue
        cols = [c for c in t.active_columns() if c.source_column is not None]
        if not cols:
            continue
        sel = ",\n       ".join(f"{_cast(q(c.name), c.sql_type, c.date_order)} AS {q(c.name)}" for c in cols)
        distinct = "DISTINCT " if t.derived_from else ""
        lines.append(f"INSERT INTO {q(t.name)} ({', '.join(q(c.name) for c in cols)})")
        lines.append(f"SELECT {distinct}{sel}")
        lines.append(f"FROM {q(stg_name(host, 'postgresql'))};")
        lines.append("")
    lines += ["COMMIT;", ""]
    return "\n".join(lines)


def loading_guidance(schema: Schema, datasets: dict[str, Dataset]) -> str:
    d = schema.dialect
    ordered, _ = order_tables(schema)
    csvs = import_ready_csvs(schema, datasets)
    out = ["OUTPUT C - DATA LOADING GUIDANCE (guidance only: this tool never connects to a database or loads data)", ""]
    out.append("Use the import-ready CSV files from the download bundle (folder import_ready_csv/). Each holds only the included")
    out.append("columns, in the table's column order, with the target column names as header. Values are exactly as read.")
    out.append("")
    if d == "postgresql":
        out += ["Server-side COPY runs on the database SERVER: the file must be readable by the server process and you need",
                "superuser or pg_read_server_files rights. Client-side \\copy (psql) reads the file from YOUR machine and needs",
                "no special rights. For most users \\copy is the right choice.", ""]
    for t in ordered:
        fname = f"{t.name}.csv"
        if fname not in csvs:
            continue
        cols = [c for c in t.active_columns() if c.source_column is not None]
        target = stg_name(t, d) if (schema.staging and not t.derived_from and t.kind != "custom") else t.name
        if schema.staging and t.derived_from:
            continue  # filled from staging by the transform script
        cl = ", ".join(quote(c.name, d) for c in cols)
        if d == "postgresql":
            out.append(f"\\copy {quote(target, d)} ({cl}) FROM '{fname}' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')")
            out.append(f"-- server-side equivalent: COPY {quote(target, d)} ({cl}) FROM '/absolute/path/on/server/{fname}' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');")
        elif d == "sqlite":
            out.append(f".import --csv --skip 1 {fname} {quote(target, d)}")
        elif d == "mysql":
            out.append(f"LOAD DATA LOCAL INFILE '{fname}' INTO TABLE {quote(target, d)} FIELDS TERMINATED BY ',' ENCLOSED BY '\"' "
                       f"LINES TERMINATED BY '\\n' IGNORE 1 LINES ({cl});  -- needs local_infile enabled; empty cells load as '' unless mapped with NULLIF")
        else:
            out.append(f"BULK INSERT {quote(target, d)} FROM 'C:\\path\\{fname}' WITH (FORMAT = 'CSV', FIRSTROW = 2, CODEPAGE = '65001');")
        out.append("")
    if schema.staging:
        out += ["After all staging tables are loaded run PART 2 (transform script), then PART 3 (foreign keys) of the SQL scripts.", ""]
    return "\n".join(out)


def import_preparation_text(schema: Schema, datasets: dict[str, Dataset], validation, summary: dict) -> str:
    plan = plan_orders(schema)
    out = ["OUTPUT B - IMPORT PREPARATION", "", f"Status: {summary['Status']}  ({summary['Errors']} error(s), {summary['Warnings']} warning(s))", ""]
    out.append("1. TABLE CREATION ORDER")
    out += [f"   {i}. {n}" for i, n in enumerate(plan["creation"], 1)]
    out += ["", "2. DATA LOADING ORDER (parents before children)"]
    out += [f"   {i}. {n}" for i, n in enumerate(plan["loading"], 1)]
    out += ["", "3. CONSTRAINT CREATION"] + [f"   - {s}" for s in plan["constraints"]]
    if plan["explanation"]:
        out += ["", "   NOTE: " + plan["explanation"]]
    out += ["", "4. REQUIRED TRANSFORMATIONS"]
    steps = []
    for t in schema.active_tables():
        for c in t.active_columns():
            base = parse_token(c.sql_type)[0]
            if c.date_order and base in ("date", "timestamp", "timestamptz"):
                steps.append(f"{t.name}.{c.name}: read dates as {c.date_order}; the loader/server must interpret them the same way.")
            if c.alternative and c.needs_approval:
                steps.append(f"{t.name}.{c.name}: loaded as {c.sql_type}; to use {c.alternative} first fix the flagged values.")
        if t.kind == "entity":
            steps.append(f"{t.name}: populate with DISTINCT rows of its columns taken from {t.source_file}.")
        if t.pk_mode == "surrogate":
            steps.append(f"{t.name}: omit the generated key column when loading; the database fills it.")
    for fname in sorted({t.source_file for t in schema.active_tables() if t.source_file.lower().endswith((".xlsx", ".xlsm", ".xls"))}):
        steps.append(f"{fname}: Excel files cannot be bulk-loaded directly - use the import-ready CSV files.")
    out += [f"   - {s}" for s in steps] or ["   - none"]
    out += ["", "5. VALIDATION BEFORE LOADING"]
    errs = [i for i in validation.issues if i.severity == "error"]
    if errs:
        out.append("   Unresolved errors - constraints WILL fail on import until these are fixed (or you use staging):")
        out += [f"   - [{i.code}] {i.table}.{i.column}: {i.message}" for i in errs[:40]]
        if len(errs) > 40:
            out.append(f"   ... and {len(errs) - 40} more (see validation_report.csv)")
    else:
        out.append("   No validation errors were found against the source data.")
    skipped = [f"   - {c.name}: {c.note}" for c in validation.checks if c.status == "skipped"]
    out += ["", "6. SKIPPED CHECKS"] + (skipped or ["   none"])
    return "\n".join(out) + "\n"
