"""Thin orchestration used by the UI (and tests): files -> analysis -> validation -> outputs."""
from __future__ import annotations

from dataclasses import dataclass, field

from .data_profiler import DatasetProfile, profile_all
from .file_reader import read_files
from .key_detection import KeyAnalysis
from .models import Dataset, FileReport
from .relationship_detection import Relationship, detect_junctions, detect_relationships
from .reports import build_bundle, zip_bundle
from .schema_designer import Schema, build_schema, dataset_table_names
from .schema_validator import ValidationResult, summarize, validate
from .sql_generator import generate_schema_sql


@dataclass
class Analysis:
    datasets: dict[str, Dataset]
    profiles: dict[str, DatasetProfile]
    relationships: list[Relationship]
    key_analyses: dict[str, KeyAnalysis]
    schema: Schema
    validation: ValidationResult | None = None
    summary: dict = field(default_factory=dict)
    sql: str = ""


def load(files: list[tuple[str, bytes]], has_header: bool = True) -> tuple[list[Dataset], list[FileReport]]:
    return read_files(files, has_header)


def profile(datasets: list[Dataset]) -> dict[str, DatasetProfile]:
    return profile_all([d for d in datasets if d.included])


def analyse(datasets: list[Dataset], profiles: dict[str, DatasetProfile] | None = None, dialect: str = "postgresql",
            mode: str = "direct", staging: bool = False) -> Analysis:
    chosen = [d for d in datasets if d.included]
    if not chosen:
        raise ValueError("No datasets are selected.")
    profiles = {d.id: (profiles or {}).get(d.id) or profile_all([d])[d.id] for d in chosen}
    names = dataset_table_names(chosen, dialect)
    rels = detect_relationships(chosen, profiles, names)
    schema, keys = build_schema(chosen, profiles, rels, dialect, mode, staging)
    a = Analysis({d.id: d for d in chosen}, profiles, rels, keys, schema)
    revalidate(a)
    return a


def revalidate(a: Analysis) -> Analysis:
    a.validation = validate(a.schema, a.datasets)
    a.summary = summarize(a.schema, a.datasets, a.relationships, a.validation)
    return a


def generate(a: Analysis) -> str:
    revalidate(a)
    a.sql = generate_schema_sql(a.schema, a.summary)
    return a.sql


def junctions(a: Analysis):
    """Link tables between two parents, judged on the relationships the user currently accepts."""
    import copy
    accepted = {fk.id: fk.accepted for fk in a.schema.fks}
    rels = []
    for r in a.relationships:
        r2 = copy.copy(r)
        r2.accepted = accepted.get(r.id, r.accepted)
        rels.append(r2)
    return detect_junctions(list(a.datasets.values()), rels)


def outputs(a: Analysis):
    """-> (files, failures, zip bytes). Always regenerates SQL from the current decisions."""
    generate(a)
    files, failures = build_bundle(a.schema, a.datasets, a.validation, a.summary, a.sql)
    return files, failures, zip_bundle(files)
