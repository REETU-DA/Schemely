"""Pre-import validation of the proposed schema against the real source data.

Every check is recorded in `ValidationResult.checks` as passed / failed / skipped so a
check that did not run can never be mistaken for one that passed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .dialects import is_known_token, parse_token, resolve_type, token_group
from .identifiers import LIMITS, is_reserved, is_simple
from .models import Dataset
from .schema_designer import Schema, TableSpec, table_frame
from .type_inference import convert_check, has_leading_zero_values

MAX_DETAIL = 500
KEY_CODES = {"pk_missing", "pk_duplicate", "fk_unmatched", "pk_excluded", "fk_parent_duplicates", "pk_nullable"}


@dataclass
class Issue:
    severity: str  # error | warning | info
    code: str
    table: str
    column: str
    message: str
    resolution: str
    count: int = 0
    details: list[dict] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    status: str  # passed | failed | skipped
    issues: int = 0
    note: str = ""


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)

    def by_severity(self, sev: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == sev]

    @property
    def errors(self) -> int:
        return len(self.by_severity("error"))

    @property
    def warnings(self) -> int:
        return len(self.by_severity("warning"))


def _rows(ds: Dataset, pairs, column: str, extra: str = "") -> list[dict]:
    out = []
    for idx, val, *rest in pairs[:MAX_DETAIL]:
        out.append({"source_file": ds.source_file, "worksheet": ds.sheet or "", "source_row": ds.source_row(idx),
                    "column": column, "value": val, "problem": (rest[-1] if rest else extra)})
    return out


def _find_cycles(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan)."""
    graph: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a != b and a in graph and b in graph:
            graph[a].append(b)
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph[v]:
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for n in nodes:
        if n not in index:
            strong(n)
    return out


def validate(schema: Schema, datasets: dict[str, Dataset]) -> ValidationResult:
    res = ValidationResult()
    tables = schema.active_tables()
    tname = {t.id: t.name for t in schema.tables}
    limit = LIMITS[schema.dialect]

    def run(name: str, fn, skip_reason: str | None = None):
        if skip_reason:
            res.checks.append(CheckResult(name, "skipped", 0, skip_reason))
            return
        before = len(res.issues)
        fn()
        added = res.issues[before:]
        bad = [i for i in added if i.severity in ("error", "warning")]
        res.checks.append(CheckResult(name, "failed" if bad else "passed", len(added)))

    def add(sev, code, table, column, msg, fix, count=0, details=None):
        res.issues.append(Issue(sev, code, table, column, msg, fix, count, details or []))

    frames = {t.id: table_frame(t, datasets) for t in tables}

    # 1/2 primary keys ---------------------------------------------------------------------
    def check_pk():
        for t in tables:
            if t.pk_mode not in ("existing", "composite"):
                continue
            specs = [t.col(k) for k in t.pk_columns]
            if any(s is None or not s.active for s in specs):
                add("error", "pk_excluded", t.name, ", ".join(t.pk_columns),
                    "A primary-key column is excluded or missing.", "Re-include the column or choose another key.")
                continue
            for s in specs:
                if s.nullable:
                    add("error", "pk_nullable", t.name, s.name, "Primary-key column is marked nullable.",
                        "Untick Nullable for key columns.")
            f = frames[t.id]
            ds = datasets.get(t.derived_from or t.dataset_id or "")
            if f is None or ds is None:
                continue
            src = [s.source_column for s in specs]
            miss = f[f[src].isna().any(axis=1)]
            if len(miss):
                add("error", "pk_missing", t.name, ", ".join(src),
                    f"{len(miss)} row(s) have a NULL primary-key value.",
                    "Fill the identifiers in the source, exclude the rows, or use a surrogate key.", len(miss),
                    _rows(ds, [(i, None, "NULL key") for i in miss.index], ", ".join(src)))
            present = f.dropna(subset=src)
            dup = present[present.duplicated(subset=src, keep=False)]
            if len(dup):
                vals = [(i, " | ".join(str(v) for v in r), "duplicate key") for i, r in zip(dup.index, dup[src].values)]
                add("error", "pk_duplicate", t.name, ", ".join(src),
                    f"{len(dup)} row(s) share {dup.drop_duplicates(subset=src).shape[0]} duplicated key value(s).",
                    "Correct the duplicates in the source, pick another key, or use a surrogate key.", len(dup),
                    _rows(ds, vals, ", ".join(src)))
    run("Primary key completeness and uniqueness", check_pk)

    # 3 duplicate records ------------------------------------------------------------------
    def check_dup_rows():
        for t in tables:
            f = frames[t.id]
            ds = datasets.get(t.derived_from or t.dataset_id or "")
            if f is None or ds is None or t.derived_from or len(f) == 0:
                continue
            dup = f[f.duplicated(keep="first")]
            if len(dup):
                add("warning", "duplicate_records", t.name, "", f"{len(dup)} row(s) are exact copies of an earlier row.",
                    "Remove the repeats before import, or keep them if repeats are legitimate.", len(dup),
                    _rows(ds, [(i, "(entire row)", "duplicate row") for i in dup.index], "(all columns)"))
    run("Duplicate records", check_dup_rows)

    # 4 type conversion, ambiguity, truncation, precision --------------------------------------
    def check_types():
        for t in tables:
            f = frames[t.id]
            ds = datasets.get(t.derived_from or t.dataset_id or "")
            if f is None or ds is None:
                continue
            for c in t.active_columns():
                if c.source_column is None or c.source_column not in f.columns:
                    continue
                if not is_known_token(c.sql_type):
                    add("warning", "unknown_type", t.name, c.name, f"Type '{c.sql_type}' is custom and cannot be verified against the data.",
                        "Check the values manually.")
                    continue
                bad = convert_check(f[c.source_column], c.sql_type, c.date_order)
                groups: dict[str, list] = {}
                for idx, v, code, why in bad:
                    groups.setdefault(code, []).append((idx, v, why))
                for code, items in groups.items():
                    if code == "ambiguous":
                        add("error", "ambiguous_date", t.name, c.name,
                            f"{len(items)} date value(s) can be read as day-first or month-first (e.g. '{items[0][1]}').",
                            "Choose DD/MM/YYYY or MM/DD/YYYY for this column (Review Tables).", len(items), _rows(ds, items, c.name))
                    elif code == "truncation":
                        add("error", "truncation", t.name, c.name, f"{len(items)} value(s) are longer than {c.sql_type} allows.",
                            "Increase the length or use text.", len(items), _rows(ds, items, c.name))
                    elif code == "precision":
                        add("warning", "precision_loss", t.name, c.name,
                            f"{len(items)} value(s) have more decimal places than {c.sql_type} keeps and would be rounded.",
                            "Increase the scale.", len(items), _rows(ds, items, c.name))
                    else:
                        add("error", "type_conversion", t.name, c.name,
                            f"{len(items)} value(s) cannot be loaded as {c.sql_type} (e.g. {items[0][2]}).",
                            "Use text (safe), fix the values, or load via a staging table.", len(items), _rows(ds, items, c.name))
    run("Data-type compatibility, ambiguous dates, truncation", check_types)

    # leading zeros --------------------------------------------------------------------------
    def check_zeros():
        for t in tables:
            f = frames[t.id]
            ds = datasets.get(t.derived_from or t.dataset_id or "")
            if f is None or ds is None:
                continue
            for c in t.active_columns():
                if c.source_column in (f.columns if f is not None else []) and token_group(c.sql_type) in ("int", "num") \
                        and has_leading_zero_values(f[c.source_column]):
                    s = f[c.source_column].dropna()
                    hit = s[s.str.strip().str.match(r"^[+-]?0\d")]
                    add("error", "leading_zeros_lost", t.name, c.name,
                        f"{len(hit)} value(s) have leading zeros that a numeric type would drop (e.g. '{hit.iloc[0]}').",
                        "Change the type to text/varchar.", len(hit), _rows(ds, [(i, v, "leading zeros") for i, v in hit.items()], c.name))
    run("Lost leading zeros", check_zeros)

    # 6 foreign keys --------------------------------------------------------------------------
    fks = schema.active_fks()

    def check_fk():
        for fk in fks:
            ct, pt = schema.table(fk.child_table), schema.table(fk.parent_table)
            label = f"{ct.name}.{','.join(ct.col(k).name for k in fk.child_cols)} → {pt.name}.{','.join(pt.col(k).name for k in fk.parent_cols)}"
            for ck, pk_ in zip(fk.child_cols, fk.parent_cols):
                a, b = ct.col(ck), pt.col(pk_)
                if token_group(a.sql_type) != token_group(b.sql_type):
                    add("error", "fk_type_mismatch", ct.name, a.name,
                        f"{label}: child type {a.sql_type} is not compatible with parent type {b.sql_type}.",
                        "Give both columns the same type.")
                elif a.sql_type.replace(" ", "") != b.sql_type.replace(" ", "") and token_group(a.sql_type) == "text":
                    pass  # differing varchar lengths are allowed
            cf, pf = frames.get(ct.id), frames.get(pt.id)
            dsc = datasets.get(ct.derived_from or ct.dataset_id or "")
            if cf is None or pf is None or dsc is None:
                continue
            csrc = [ct.col(k).source_column for k in fk.child_cols]
            psrc = [pt.col(k).source_column for k in fk.parent_cols]
            if len(csrc) == 1:
                s = cf[csrc[0]].dropna()
                parent_vals = pf[psrc[0]].dropna().unique()
                bad = s[~s.isin(parent_vals)]
                pser = pf[psrc[0]].dropna()
                if pser.duplicated().any():
                    add("error", "fk_parent_duplicates", pt.name, pt.col(fk.parent_cols[0]).name,
                        f"{label}: the referenced parent column contains duplicate values, so a foreign key cannot reference it.",
                        "Fix duplicates in the parent or reject this relationship.", int(pser.duplicated().sum()))
                if len(bad):
                    pairs = [(i, v, f"no matching {pt.name}.{psrc[0]}") for i, v in bad.items()]
                    add("error", "fk_unmatched", ct.name, ct.col(fk.child_cols[0]).name,
                        f"{label}: {len(bad)} row(s) ({bad.nunique()} distinct value(s)) have no parent record, so the constraint "
                        "would be violated on import.",
                        "Add the missing parent rows, correct the values, load via staging and add the FK afterwards, or reject the FK.",
                        len(bad), _rows(dsc, pairs, csrc[0]))
            else:
                merged = cf[csrc].dropna().merge(pf[psrc].dropna().drop_duplicates(), left_on=csrc, right_on=psrc, how="left", indicator=True)
                miss = int((merged["_merge"] == "left_only").sum())
                if miss:
                    add("error", "fk_unmatched", ct.name, ", ".join(csrc), f"{label}: {miss} composite reference(s) have no parent.",
                        "Fix or reject the FK.", miss)
    run("Foreign-key references and parent keys", check_fk, None if fks else "No foreign keys are accepted.")

    # 7 uniqueness assumptions ----------------------------------------------------------------
    def check_unique():
        for t in tables:
            f = frames[t.id]
            ds = datasets.get(t.derived_from or t.dataset_id or "")
            if f is None or ds is None:
                continue
            groups = [[c.key] for c in t.active_columns() if c.unique] + [g for g in t.uniques]
            for g in groups:
                cols = [t.col(k) for k in g]
                if any(c is None or c.source_column is None or not c.active for c in cols):
                    continue
                src = [c.source_column for c in cols]
                present = f.dropna(subset=src)
                dup = present[present.duplicated(subset=src, keep=False)]
                if len(dup):
                    add("error", "unique_violation", t.name, ", ".join(c.name for c in cols),
                        f"UNIQUE was requested but {len(dup)} row(s) contain repeated values.", "Remove the UNIQUE setting or fix the data.",
                        len(dup), _rows(ds, [(i, " | ".join(map(str, r)), "repeated value") for i, r in zip(dup.index, dup[src].values)], ", ".join(src)))
    run("Uniqueness constraints", check_unique)

    # unsupported / risky combinations ---------------------------------------------------------
    def check_combos():
        for t in tables:
            for c in t.active_columns():
                if re.search(r";|--|/\*|\*/", c.check or ""):
                    add("error", "unsafe_check", t.name, c.name, "The CHECK expression contains ';', '--' or '/*', which is not allowed.",
                        "Write a single expression such as  price >= 0.")
                if c.check and len(c.check) > 500:
                    add("error", "unsafe_check", t.name, c.name, "The CHECK expression is too long.", "Shorten it.")
            keycols = {k for k in t.pk_columns} | {k for g in t.uniques for k in g} | {c.key for c in t.active_columns() if c.unique}
            keycols |= {k for fk in fks if fk.child_table == t.id for k in fk.child_cols}
            keycols |= {k for fk in fks if fk.parent_table == t.id for k in fk.parent_cols}
            for k in sorted(keycols):
                c = t.col(k)
                if c and c.active and not c.generated and schema.dialect in ("mysql", "sqlserver") and parse_token(c.sql_type)[0] == "text":
                    _, note = resolve_type(c.sql_type, schema.dialect, True, c.max_len)
                    add("info", "key_text_type", t.name, c.name, note or "Text key converted to a bounded type.", "No action needed.")
            if t.pk_mode == "none" and t.kind != "custom":
                add("warning", "no_primary_key", t.name, "", "The table has no primary key.", "Choose a key or a surrogate key.")
    run("Unsupported constraint combinations", check_combos)

    # 9 cycles --------------------------------------------------------------------------------
    def check_cycles():
        for comp in _find_cycles([t.id for t in tables], [(fk.child_table, fk.parent_table) for fk in fks]):
            add("warning", "circular_dependency", ", ".join(tname[i] for i in comp), "",
                "These tables reference each other in a cycle, so no single creation order satisfies every foreign key.",
                "The generated script creates the tables first and adds the cyclic foreign keys afterwards (ALTER TABLE). "
                "Load data in two passes or defer the constraints.")
        for fk in fks:
            if fk.child_table == fk.parent_table:
                add("info", "self_reference", tname[fk.child_table], "", "Self-referencing foreign key (hierarchy).",
                    "Load parent rows before children, or NULL the reference first.")
    run("Circular dependencies", check_cycles, None if fks else "No foreign keys are accepted.")

    # 10 identifiers / reserved words / naming ---------------------------------------------------
    def check_names():
        seen_t: dict[str, str] = {}
        for t in tables:
            low = t.name.lower()
            if not t.name.strip():
                add("error", "invalid_identifier", t.name, "", "Table name is empty.", "Enter a name.")
            if low in seen_t:
                add("error", "name_conflict", t.name, "", f"Table name '{t.name}' is used by more than one table.", "Rename one table.")
            seen_t[low] = t.id
            for nm, kind, col in [(t.name, "table", "")] + [(c.name, "column", c.name) for c in t.active_columns()]:
                if len(nm) > limit:
                    add("error", "identifier_too_long", t.name, col, f"{kind.capitalize()} name '{nm}' exceeds {limit} characters.", "Shorten it.")
                if not nm.strip():
                    add("error", "invalid_identifier", t.name, col, "Name is empty.", "Enter a name.")
                elif is_reserved(nm):
                    add("info", "reserved_keyword", t.name, col, f"'{nm}' is a SQL reserved word; it will be quoted in the script.",
                        "Optionally rename it to avoid quoting in queries.")
                elif not is_simple(nm):
                    add("info", "identifier_quoted", t.name, col, f"'{nm}' contains unusual characters; it will be quoted.", "Optionally rename it.")
            seen_c: dict[str, str] = {}
            for c in t.active_columns():
                if c.name.lower() in seen_c:
                    add("error", "name_conflict", t.name, c.name, f"Column name '{c.name}' appears twice in {t.name}.", "Rename one column.")
                seen_c[c.name.lower()] = c.key
    run("Identifiers, reserved keywords and naming conflicts", check_names)

    # 12/13 mapping conflicts, destructive changes ------------------------------------------------
    def check_mapping():
        for t in tables:
            if not t.active_columns():
                add("error", "empty_table", t.name, "", "The table has no included columns.", "Include columns or remove the table.")
        for fk in schema.fks:
            if not fk.accepted:
                continue
            ct, pt = schema.table(fk.child_table), schema.table(fk.parent_table)
            if ct and pt and ct.include and pt.include:
                bad = [k for k in fk.child_cols if not (ct.col(k) and ct.col(k).active)] + [k for k in fk.parent_cols if not (pt.col(k) and pt.col(k).active)]
                if bad:
                    add("error", "fk_excluded_column", ct.name, ", ".join(bad), "An accepted foreign key uses an excluded column.",
                        "Reject the foreign key or include the column again.")
            elif ct and pt:
                add("warning", "fk_excluded_table", ct.name, "", "An accepted foreign key involves a removed table and was left out of the SQL.", "Reject it explicitly.")
    run("Source-to-target mapping conflicts", check_mapping)

    def check_destructive():
        for t in schema.tables:
            ds = datasets.get(t.dataset_id or "")
            if not t.include and ds is not None and len(ds.df):
                add("warning", "table_excluded", t.name, "", f"Table removed: its {len(ds.df)} row(s) will not be imported.",
                    "Add the table back if the data is needed.")
                continue
            if not t.include or ds is None:
                continue
            for c in t.columns:
                if c.source_column and not c.include and c.moved_to is None and c.source_column in ds.df.columns:
                    n = int(ds.df[c.source_column].notna().sum())
                    if n:
                        add("warning", "column_excluded", t.name, c.name, f"Column excluded: {n} non-empty value(s) will not be imported.",
                            "Tick Include if the data is needed.", n)
                if c.moved_to:
                    add("info", "column_moved", t.name, c.name, f"Column moves to table '{tname.get(c.moved_to, c.moved_to)}' (normalisation).",
                        "Reject the proposal by re-running analysis if unwanted.")
            if t.pk_mode == "surrogate":
                add("info", "surrogate_key", t.name, "", "A generated surrogate key is added; all original columns are preserved.", "Keep original identifiers for references.")
    run("Potentially destructive changes", check_destructive)

    def check_empty():
        for t in tables:
            ds = datasets.get(t.dataset_id or "")
            if ds is not None and len(ds.df) == 0:
                add("warning", "empty_dataset", t.name, "", "The source has no data rows, so nothing could be verified.", "Check the file.")
    run("Empty sources", check_empty)

    def check_review():
        for t in tables:
            if t.pk_needs_approval and not t.pk_approved:
                add("warning", "pk_unapproved", t.name, ", ".join(t.pk_columns), "The primary-key choice needs your confirmation.",
                    "Approve it on the Review Tables step.")
            for c in t.active_columns():
                if c.needs_approval and not c.approved:
                    add("warning", "column_unapproved", t.name, c.name, f"Needs review ({c.confidence} confidence): {c.reason[:160]}",
                        "Approve the type or change it.")
        for fk in schema.fks:
            if fk.accepted and fk.ambiguous:
                add("info", "fk_ambiguous_accepted", tname.get(fk.child_table, ""), ",".join(fk.child_cols),
                    "You accepted a relationship that was flagged ambiguous.", "Double-check the parent table.")
    run("Open decisions awaiting approval", check_review)

    return res


def summarize(schema: Schema, datasets: dict[str, Dataset], rels, res: ValidationResult) -> dict:
    tables = schema.active_tables()
    key_issue_groups = {(i.table, i.column, i.code) for i in res.issues if i.severity == "error" and i.code in KEY_CODES}
    review_cols = sum(1 for t in tables for c in t.active_columns() if c.needs_approval and not c.approved)
    amb = len({(i.table, i.column) for i in res.issues if i.code == "ambiguous_date"})
    pk_cands = sum(1 for t in tables if t.pk_mode in ("existing", "composite"))
    ready = res.errors == 0 and review_cols == 0 and not any(i.code == "pk_unapproved" for i in res.issues)
    return {
        "Datasets analysed": len(datasets),
        "Proposed tables": len(tables),
        "Primary key candidates": pk_cands,
        "Foreign key candidates": len(schema.fks),
        "Confirmed by user (accepted)": len(schema.active_fks()),
        "Unresolved key issues": len(key_issue_groups),
        "Columns requiring review": review_cols,
        "Ambiguous date columns": amb,
        "Errors": res.errors,
        "Warnings": res.warnings,
        "Status": "Ready" if ready else "Review required",
    }
