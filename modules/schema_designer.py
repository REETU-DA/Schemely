"""Schema model (tables, columns, keys, foreign keys) and the builder that proposes it.

The schema is dialect-neutral: column types are *type tokens* (see dialects.py). All
decisions that the user can override live on these dataclasses; the validator and the
SQL generator only read them.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import pandas as pd

from .data_profiler import DatasetProfile
from .identifiers import LIMITS, make_unique, sanitize
from .key_detection import KeyAnalysis, analyse_keys
from .models import Dataset
from .relationship_detection import Relationship, table_stem

SURROGATE_KEY = "__sk__"


@dataclass
class ColumnSpec:
    source_column: str | None  # None => generated column (surrogate key)
    name: str
    sql_type: str
    nullable: bool = True
    unique: bool = False
    check: str = ""
    include: bool = True
    approved: bool = False
    needs_approval: bool = False
    date_order: str | None = None
    confidence: str = "high"
    reason: str = ""
    exceptions: list[str] = field(default_factory=list)
    alternative: str | None = None
    max_len: int = 0
    observed_not_null: bool = False
    observed_unique: bool = False
    generated: bool = False
    moved_to: str | None = None  # table id this column was extracted to (normalisation)
    notes: list[str] = field(default_factory=list)
    original_name: str = ""

    @property
    def key(self) -> str:
        return self.source_column if self.source_column is not None else SURROGATE_KEY

    @property
    def active(self) -> bool:
        return self.include and self.moved_to is None


@dataclass
class TableSpec:
    id: str
    name: str
    dataset_id: str | None
    source_file: str
    sheet: str | None
    columns: list[ColumnSpec]
    pk_mode: str = "existing"  # existing | composite | surrogate | none
    pk_columns: list[str] = field(default_factory=list)  # ColumnSpec.key values
    pk_approved: bool = True
    pk_needs_approval: bool = False
    pk_explanation: str = ""
    uniques: list[list[str]] = field(default_factory=list)
    indexes: list[list[str]] = field(default_factory=list)
    include: bool = True
    kind: str = "table"  # table | entity | custom
    derived_from: str | None = None
    notes: list[str] = field(default_factory=list)

    def col(self, key: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.key == key), None)

    def active_columns(self) -> list[ColumnSpec]:
        return [c for c in self.columns if c.active]


@dataclass
class ForeignKeySpec:
    id: str
    child_table: str
    child_cols: list[str]
    parent_table: str
    parent_cols: list[str]
    accepted: bool = False
    cardinality: str = "one-to-many"
    status: str = "candidate"
    evidence: list[str] = field(default_factory=list)
    ambiguous: bool = False
    match_pct: float | None = None
    unmatched_rows: int = 0
    on_delete: str | None = None  # never inferred; only explicit user choice
    on_update: str | None = None
    relationship_id: str | None = None
    index_child: bool = False


@dataclass
class Proposal:
    id: str
    kind: str  # extract_entity | multivalue | redundant
    table_id: str
    title: str
    detail: str
    determinant: str | None = None
    dependents: list[str] = field(default_factory=list)
    new_table_name: str | None = None
    rows_in_new_table: int = 0
    assumptions: list[str] = field(default_factory=list)
    applicable: bool = False
    applied: bool = False


@dataclass
class Schema:
    dialect: str = "postgresql"
    mode: str = "direct"  # direct | relational
    staging: bool = False
    tables: list[TableSpec] = field(default_factory=list)
    fks: list[ForeignKeySpec] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)

    def table(self, table_id: str) -> TableSpec | None:
        return next((t for t in self.tables if t.id == table_id), None)

    def active_tables(self) -> list[TableSpec]:
        return [t for t in self.tables if t.include]

    def active_fks(self) -> list[ForeignKeySpec]:
        """Accepted FKs whose both tables and columns still exist."""
        out = []
        for fk in self.fks:
            if not fk.accepted:
                continue
            ct, pt = self.table(fk.child_table), self.table(fk.parent_table)
            if not ct or not pt or not ct.include or not pt.include:
                continue
            ok = all((ct.col(k) and ct.col(k).active) for k in fk.child_cols) and \
                 all((pt.col(k) and pt.col(k).active) for k in fk.parent_cols)
            if ok:
                out.append(fk)
        return out


# ----------------------------------------------------------------------------- naming
def table_base_name(ds: Dataset, multi_sheet_files: set[str]) -> str:
    stem = ds.source_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if ds.sheet and (ds.source_file in multi_sheet_files):
        if re.match(r"(?i)^sheet\d*$", ds.sheet):
            return f"{stem}_{ds.sheet}"
        return ds.sheet
    return stem


def dataset_table_names(datasets: list[Dataset], dialect: str = "postgresql") -> dict[str, str]:
    counts: dict[str, int] = {}
    for d in datasets:
        counts[d.source_file] = counts.get(d.source_file, 0) + 1
    multi = {f for f, c in counts.items() if c > 1}
    raw = [sanitize(table_base_name(d, multi), f"table_{i}", "t_") for i, d in enumerate(datasets, 1)]
    names = make_unique(raw, LIMITS[dialect])
    return {d.id: n for d, n in zip(datasets, names)}


# ----------------------------------------------------------------------------- builder
def column_spec_from_profile(ds_col: str, cp, name: str) -> ColumnSpec:
    inf = cp.inferred
    return ColumnSpec(
        source_column=ds_col, name=name, sql_type=inf.token, nullable=True, include=True,
        approved=not inf.needs_approval, needs_approval=inf.needs_approval, date_order=inf.date_order,
        confidence=inf.confidence, reason=inf.reason, exceptions=list(inf.exceptions), alternative=inf.alternative,
        max_len=inf.max_len, observed_not_null=(cp.missing == 0), observed_unique=cp.is_unique, original_name=ds_col)


def build_schema(datasets: list[Dataset], profiles: dict[str, DatasetProfile], rels: list[Relationship],
                 dialect: str = "postgresql", mode: str = "direct", staging: bool = False,
                 ) -> tuple[Schema, dict[str, KeyAnalysis]]:
    from .normalization import detect_proposals  # local import avoids cycle

    schema = Schema(dialect=dialect, mode=mode, staging=staging)
    names = dataset_table_names(datasets, dialect)
    analyses: dict[str, KeyAnalysis] = {}
    limit = LIMITS[dialect]
    for ds in datasets:
        prof = profiles[ds.id]
        tname = names[ds.id]
        raw_cols = [sanitize(c, f"column_{i}") for i, c in enumerate(ds.df.columns, 1)]
        col_names = make_unique(raw_cols, limit)
        cols = [column_spec_from_profile(c, prof.col(c), n) for c, n in zip(ds.df.columns, col_names)]
        ka = analyse_keys(ds, prof, tname)
        analyses[ds.id] = ka
        t = TableSpec(id=ds.id, name=tname, dataset_id=ds.id, source_file=ds.source_file, sheet=ds.sheet, columns=cols,
                      pk_mode=ka.mode, pk_needs_approval=ka.needs_approval, pk_approved=not ka.needs_approval,
                      pk_explanation=ka.explanation)
        if ka.mode in ("existing", "composite"):
            t.pk_columns = list(ka.columns)
            for k in t.pk_columns:
                t.col(k).nullable = False
        else:
            set_surrogate(t)
        schema.tables.append(t)
    for r in rels:
        schema.fks.append(ForeignKeySpec(
            id=r.id, child_table=r.child_dataset, child_cols=list(r.child_cols), parent_table=r.parent_dataset,
            parent_cols=list(r.parent_cols), accepted=r.accepted, cardinality=r.cardinality, status=r.status,
            evidence=list(r.evidence), ambiguous=r.ambiguous, match_pct=r.match_pct, unmatched_rows=r.unmatched_rows,
            relationship_id=r.id))
    if mode == "relational":
        schema.proposals = detect_proposals(datasets, profiles, schema, names)
    return schema, analyses


def set_surrogate(t: TableSpec) -> None:
    """Switch table to a generated identity key; original columns are kept untouched."""
    for c in list(t.columns):
        if c.generated:
            t.columns.remove(c)
    taken = {c.name.lower() for c in t.columns}
    base = f"{t.name}_sk"
    name, i = base, 2
    while name.lower() in taken:
        name, i = f"{base}{i}", i + 1
    t.columns.insert(0, ColumnSpec(source_column=None, name=name, sql_type="bigint", nullable=False, generated=True,
                                   approved=True, reason="Generated surrogate key; original identifiers are preserved as normal columns.",
                                   notes=["generated - no source column"]))
    t.pk_mode, t.pk_columns = "surrogate", [SURROGATE_KEY]


def set_pk(t: TableSpec, columns: list[str]) -> None:
    """Use existing column(s) as the key (single or composite)."""
    for c in list(t.columns):
        if c.generated:
            t.columns.remove(c)
    t.pk_columns = list(columns)
    t.pk_mode = "existing" if len(columns) == 1 else "composite"
    t.pk_approved = True
    for k in columns:
        c = t.col(k)
        if c:
            c.nullable = False


def table_frame(t: TableSpec, datasets: dict[str, Dataset]) -> pd.DataFrame | None:
    """The source rows that populate this table (distinct rows for extracted entities)."""
    ds = datasets.get(t.derived_from or t.dataset_id or "")
    if ds is None:
        return None
    cols = [c.source_column for c in t.columns if c.source_column is not None and c.active and c.source_column in ds.df.columns]
    df = ds.df[cols]
    return df.drop_duplicates() if t.derived_from else df


# ----------------------------------------------------------------------------- relational-mode operations
def apply_extract_entity(schema: Schema, prop: Proposal) -> None:
    src = schema.table(prop.table_id)
    if prop.applied or src is None:
        return
    taken = {t.name.lower() for t in schema.tables}
    base, name, i = prop.new_table_name, prop.new_table_name, 2
    while name.lower() in taken:
        name, i = f"{base}_{i}", i + 1
    tid = f"{src.id}::entity::{prop.determinant}"
    cols = []
    for key in [prop.determinant] + prop.dependents:
        c = copy.deepcopy(src.col(key))
        c.moved_to, c.include, c.unique = None, True, False
        c.nullable = key != prop.determinant and c.nullable
        cols.append(c)
    new_t = TableSpec(id=tid, name=name, dataset_id=None, source_file=src.source_file, sheet=src.sheet, columns=cols,
                      pk_mode="existing", pk_columns=[prop.determinant], pk_approved=False, pk_needs_approval=True,
                      pk_explanation="Proposed entity table: assumes each value of the key column describes exactly one entity.",
                      kind="entity", derived_from=src.dataset_id,
                      notes=[f"Extracted from {src.name}: {', '.join(prop.dependents)} depend on {prop.determinant}."])
    schema.tables.append(new_t)
    for key in prop.dependents:
        src.col(key).moved_to = tid
    schema.fks.append(ForeignKeySpec(
        id=f"fk_{prop.id}", child_table=src.id, child_cols=[prop.determinant], parent_table=tid,
        parent_cols=[prop.determinant], accepted=True, cardinality="one-to-many", status="candidate",
        evidence=[f"Created by normalisation proposal: {prop.title}"]))
    prop.applied = True


def add_custom_table(schema: Schema, name: str, columns: list[tuple[str, str]]) -> TableSpec:
    tid = f"custom::{name}"
    t = TableSpec(id=tid, name=sanitize(name, "new_table", "t_"), dataset_id=None, source_file="(user defined)", sheet=None,
                  columns=[ColumnSpec(source_column=c, name=sanitize(c, f"column_{i}"), sql_type=tok, approved=True,
                                      reason="Defined by you.", original_name=c) for i, (c, tok) in enumerate(columns, 1)],
                  kind="custom", pk_mode="none", pk_approved=True, notes=["User-defined table: no source data to validate."])
    if t.columns:
        set_pk(t, [t.columns[0].key])
    schema.tables.append(t)
    return t


def add_dataset_table(schema: Schema, ds: Dataset, prof: DatasetProfile, dialect: str) -> TableSpec:
    """Bring a dataset that was left out back in as a table."""
    existing = {t.name.lower() for t in schema.tables}
    tname = make_unique([sanitize(table_stem(ds), "table", "t_")] + [], LIMITS[dialect])[0]
    base, i = tname, 2
    while tname.lower() in existing:
        tname, i = f"{base}_{i}", i + 1
    col_names = make_unique([sanitize(c, f"column_{k}") for k, c in enumerate(ds.df.columns, 1)], LIMITS[dialect])
    cols = [column_spec_from_profile(c, prof.col(c), n) for c, n in zip(ds.df.columns, col_names)]
    ka = analyse_keys(ds, prof, tname)
    t = TableSpec(id=ds.id, name=tname, dataset_id=ds.id, source_file=ds.source_file, sheet=ds.sheet, columns=cols,
                  pk_mode=ka.mode, pk_needs_approval=ka.needs_approval, pk_approved=not ka.needs_approval,
                  pk_explanation=ka.explanation)
    if ka.mode in ("existing", "composite"):
        set_pk(t, list(ka.columns))
        t.pk_approved = not ka.needs_approval
    else:
        set_surrogate(t)
    schema.tables.append(t)
    return t
