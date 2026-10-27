"""Downloadable reports. A report that fails is reported as failed - never written as an empty file."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import asdict

from .dialects import DIALECTS, resolve_type
from .erd_generator import ERDUnavailable, build_dot, render
from .import_planner import (import_preparation_text, import_ready_csvs, loading_guidance, plan_orders,
                             source_mapping_rows, transform_sql)
from .models import Dataset
from .relationship_detection import detect_junctions
from .schema_designer import Schema

TOOL = "SmartSQL Schema Designer 1.0"


def _csv(rows: list[dict], fieldnames: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def schema_report_json(schema: Schema, summary: dict, validation) -> bytes:
    d = {
        "tool": TOOL, "dialect": schema.dialect, "design_mode": schema.mode, "staging": schema.staging, "summary": summary,
        "tables": [{
            "id": t.id, "name": t.name, "kind": t.kind, "included": t.include, "source_file": t.source_file, "worksheet": t.sheet,
            "primary_key": {"mode": t.pk_mode, "columns": [t.col(k).name for k in t.pk_columns if t.col(k)],
                            "approved": t.pk_approved, "explanation": t.pk_explanation},
            "unique_constraints": [[t.col(k).name for k in g if t.col(k)] for g in t.uniques],
            "indexes": [[t.col(k).name for k in g if t.col(k)] for g in t.indexes],
            "columns": [{**{k: v for k, v in asdict(c).items() if k != "source_column"},
                         "source_column": c.source_column,
                         "resolved_sql_type": resolve_type(c.sql_type, schema.dialect, c.key in t.pk_columns, c.max_len)[0]}
                        for c in t.columns]} for t in schema.tables],
        "foreign_keys": [{
            "id": fk.id, "child_table": schema.table(fk.child_table).name if schema.table(fk.child_table) else fk.child_table,
            "child_columns": fk.child_cols, "parent_table": schema.table(fk.parent_table).name if schema.table(fk.parent_table) else fk.parent_table,
            "parent_columns": fk.parent_cols, "accepted": fk.accepted, "cardinality": fk.cardinality, "status": fk.status,
            "ambiguous": fk.ambiguous, "match_pct": fk.match_pct, "unmatched_rows": fk.unmatched_rows,
            "on_delete": fk.on_delete, "on_update": fk.on_update, "evidence": fk.evidence} for fk in schema.fks],
        "proposals": [asdict(p) for p in schema.proposals],
        "validation_checks": [asdict(c) for c in validation.checks],
    }
    return json.dumps(d, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def schema_report_csv(schema: Schema, validation) -> bytes:
    fk_child = {(fk.child_table, k) for fk in schema.active_fks() for k in fk.child_cols}
    issues_by_col: dict[tuple, list[str]] = {}
    for i in validation.issues:
        issues_by_col.setdefault((i.table, i.column), []).append(i.code)
    rows = []
    for t in schema.tables:
        for c in t.columns:
            rows.append({
                "table": t.name, "table_included": t.include, "source_file": t.source_file, "worksheet": t.sheet or "",
                "source_column": c.source_column or "(generated)", "column": c.name, "type_token": c.sql_type,
                "sql_type": resolve_type(c.sql_type, schema.dialect, c.key in t.pk_columns, c.max_len)[0],
                "included": c.include and c.moved_to is None, "nullable": c.nullable and c.key not in t.pk_columns,
                "primary_key": c.key in t.pk_columns, "foreign_key": (t.id, c.key) in fk_child, "unique": c.unique,
                "check": c.check, "confidence": c.confidence, "needs_approval": c.needs_approval, "approved": c.approved,
                "observed_no_nulls": c.observed_not_null, "observed_unique": c.observed_unique, "reason": c.reason,
                "warnings": ";".join(sorted(set(issues_by_col.get((t.name, c.name), []))))})
    return _csv(rows, list(rows[0].keys()) if rows else ["table"])


def relationship_report_csv(schema: Schema, datasets: dict[str, Dataset]) -> bytes:
    rows = []
    for fk in schema.fks:
        ct, pt = schema.table(fk.child_table), schema.table(fk.parent_table)
        rows.append({
            "relationship": "foreign key", "child_table": ct.name if ct else fk.child_table,
            "child_columns": ", ".join(ct.col(k).name for k in fk.child_cols if ct and ct.col(k)),
            "parent_table": pt.name if pt else fk.parent_table,
            "parent_columns": ", ".join(pt.col(k).name for k in fk.parent_cols if pt and pt.col(k)),
            "decision": "approved" if fk.accepted else "rejected / not approved", "status": fk.status,
            "cardinality": fk.cardinality, "ambiguous": fk.ambiguous,
            "match_pct": "" if fk.match_pct is None else round(100 * fk.match_pct, 2), "unmatched_child_rows": fk.unmatched_rows,
            "on_delete": fk.on_delete or "(not set)", "evidence": " | ".join(fk.evidence)})
    return _csv(rows, ["relationship", "child_table", "child_columns", "parent_table", "parent_columns", "decision", "status",
                       "cardinality", "ambiguous", "match_pct", "unmatched_child_rows", "on_delete", "evidence"])


def validation_report_csv(validation) -> bytes:
    rows = []
    for c in validation.checks:
        rows.append({"severity": "check", "code": c.name, "table": "", "column": "", "source_file": "", "worksheet": "",
                     "source_row": "", "value": "", "message": f"{c.status.upper()}" + (f" - {c.note}" if c.note else ""),
                     "resolution": "", "total_affected": c.issues})
    for i in validation.issues:
        base = {"severity": i.severity, "code": i.code, "table": i.table, "column": i.column, "message": i.message,
                "resolution": i.resolution, "total_affected": i.count or ""}
        if i.details:
            for d in i.details:
                rows.append({**base, "source_file": d["source_file"], "worksheet": d["worksheet"], "source_row": d["source_row"],
                             "value": d["value"]})
            if i.count > len(i.details):
                rows.append({**base, "message": f"{i.message} [detail list truncated: {len(i.details)} of {i.count} rows shown]"})
        else:
            rows.append({**base, "source_file": "", "worksheet": "", "source_row": "", "value": ""})
    return _csv(rows, ["severity", "code", "table", "column", "source_file", "worksheet", "source_row", "value", "message",
                       "resolution", "total_affected"])


def source_mapping_csv(schema: Schema, datasets: dict[str, Dataset]) -> bytes:
    rows = source_mapping_rows(schema, datasets)
    return _csv(rows, ["source_file", "worksheet", "source_column", "source_header_as_written", "target_table", "target_column",
                       "type_token", "sql_type", "status", "table_kind", "note"])


def readme_text(schema: Schema, summary: dict) -> str:
    d = schema.dialect
    run = {
        "postgresql": ["Create (or choose) an empty database, then run:",
                       "    psql -h <host> -U <user> -d <database> -f schema.sql",
                       "Add  -v ON_ERROR_STOP=1  to stop at the first error. schema.sql is wrapped in a transaction, so a failure",
                       "leaves no partial tables behind.",
                       "Load data with the \\copy commands in output_c_loading_guidance.txt (client-side) or server-side COPY."],
        "sqlite": ["    sqlite3 mydatabase.sqlite < schema.sql",
                   "Foreign-key enforcement is switched on by the script's PRAGMA for that session; enable it in every later session too.",
                   "Load data with  .import --csv --skip 1  (see output_c_loading_guidance.txt)."],
        "mysql": ["EXPERIMENTAL dialect - never executed against a MySQL server by the author. Review the script first.",
                  "    mysql -u <user> -p <database> < schema.sql",
                  "DDL statements commit implicitly in MySQL, so a failure can leave earlier tables created."],
        "sqlserver": ["EXPERIMENTAL dialect - never executed against SQL Server by the author. Review the script first.",
                      "    sqlcmd -S <server> -d <database> -i schema.sql"],
    }[d]
    lines = [f"{TOOL} - results", "=" * 60, "",
             f"Target dialect : {DIALECTS[d]['label']}", f"Design mode    : {schema.mode}", f"Staging        : {'yes' if schema.staging else 'no'}",
             f"Status         : {summary['Status']} ({summary['Errors']} error(s), {summary['Warnings']} warning(s), "
             f"{summary['Columns requiring review']} column(s) awaiting approval)", "",
             "FILES", "-----",
             "schema.sql                       the CREATE TABLE script (Output A) - run this first",
             "schema_report.json / .csv        everything the tool decided, per table and column",
             "relationship_report.csv          detected foreign keys with evidence, and your decisions",
             "validation_report.csv            every check run and every issue, with source file/row",
             "source_mapping.csv               source file/column -> target table/column",
             "import_preparation.txt           loading order, transformations, open problems (Output B)",
             "output_c_loading_guidance.txt    COPY / \\copy / import examples (Output C)",
             "import_ready_csv/                one CSV per table in target column order (values untouched)",
             "erd.svg / erd.png / erd.dot      relationship diagram (if Graphviz was available)", "",
             "HOW TO RUN schema.sql", "---------------------"] + run + ["",
             "IMPORTANT", "---------",
             "* The tool analysed your files locally and never connected to a database or sent data anywhere.",
             "* Constraints reflect approved decisions, not just patterns seen in the data; check the 'review' comments in schema.sql.",
             "* If validation_report.csv lists errors, constraints (primary/foreign keys, type casts) will fail on import until fixed.",
             "* No ON DELETE / ON UPDATE cascade behaviour is included unless you chose it.", ""]
    return "\n".join(lines)


def build_bundle(schema: Schema, datasets: dict[str, Dataset], validation, summary: dict, sql: str):
    """-> (files {path: bytes}, failures {name: reason})."""
    files: dict[str, bytes] = {}
    failures: dict[str, str] = {}

    def attempt(name, fn):
        try:
            data = fn()
            if data is None or len(data) == 0:
                raise ValueError("produced no content")
            files[name] = data if isinstance(data, bytes) else data.encode("utf-8")
        except ERDUnavailable as e:
            failures[name] = str(e)
        except Exception as e:
            failures[name] = f"{type(e).__name__}: {e}"

    attempt("schema.sql", lambda: sql)
    attempt("schema_report.json", lambda: schema_report_json(schema, summary, validation))
    attempt("schema_report.csv", lambda: schema_report_csv(schema, validation))
    attempt("relationship_report.csv", lambda: relationship_report_csv(schema, datasets))
    attempt("validation_report.csv", lambda: validation_report_csv(validation))
    attempt("source_mapping.csv", lambda: source_mapping_csv(schema, datasets))
    attempt("import_preparation.txt", lambda: import_preparation_text(schema, datasets, validation, summary))
    attempt("output_c_loading_guidance.txt", lambda: loading_guidance(schema, datasets))
    attempt("README.txt", lambda: readme_text(schema, summary))
    if schema.staging:
        attempt("transform_staging_to_final.sql", lambda: transform_sql(schema))
    dot = build_dot(schema)
    attempt("erd.dot", lambda: dot)
    attempt("erd.svg", lambda: render(dot, "svg"))
    attempt("erd.png", lambda: render(dot, "png"))

    def csvs():
        for n, b in import_ready_csvs(schema, datasets).items():
            files[f"import_ready_csv/{n}"] = b
        return b"ok"
    try:
        csvs()
    except Exception as e:
        failures["import_ready_csv"] = f"{type(e).__name__}: {e}"
    if failures:
        files["REPORT_ERRORS.txt"] = ("These outputs could not be created:\n\n" + "\n".join(f"- {k}: {v}" for k, v in failures.items()) + "\n").encode()
    return files, failures


def zip_bundle(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            z.writestr(name, files[name])
    return buf.getvalue()
