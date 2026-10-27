"""Relational-mode analysis: repeated entities, redundant attributes, multi-valued fields.

Nothing here changes the schema by itself. Proposals are shown with their assumptions;
only the user's explicit approval applies them (schema_designer.apply_extract_entity).
"""
from __future__ import annotations

import re

from .data_profiler import DatasetProfile
from .identifiers import sanitize
from .models import Dataset

SEPARATORS = re.compile(r"\s*[;,|]\s*")


def _entity_name(col: str) -> str:
    base = re.sub(r"(?i)[_ ]?(id|code|key|no|number)$", "", col).strip("_ ") or col
    return sanitize(base, "entity", "t_")


def detect_proposals(datasets: list[Dataset], profiles: dict[str, DatasetProfile], schema, names: dict[str, str]):
    from .schema_designer import Proposal  # avoid import cycle

    out: list[Proposal] = []
    n_id = 0
    fk_child_cols = {(fk.child_table, c) for fk in schema.fks if fk.accepted for c in fk.child_cols}
    for ds in datasets:
        prof = profiles[ds.id]
        table = schema.table(ds.id)
        df = ds.df
        n = len(df)
        if n < 4:
            continue
        pk = set(table.pk_columns)
        for cp in prof.columns:
            x = cp.name
            if x in pk or cp.missing or cp.distinct < 2 or cp.distinct > 0.7 * n:
                continue
            if cp.inferred.kind not in ("integer", "bigint", "identifier", "text", "uuid"):
                continue
            idish = bool(re.search(r"(?i)(^|_)(id|code|key|no|number)$", x))
            if not idish:
                continue
            deps = []
            grp = df.groupby(x, dropna=False)
            for other in prof.columns:
                y = other.name
                if y == x or y in pk:
                    continue
                if (grp[y].nunique(dropna=False) <= 1).all():
                    deps.append(y)
            if not deps:
                continue
            n_id += 1
            if (ds.id, x) in fk_child_cols:
                out.append(Proposal(
                    id=f"prop_{n_id}", kind="redundant", table_id=ds.id,
                    title=f"{names[ds.id]}: {', '.join(deps)} repeat data already reachable through {x}",
                    detail=(f"{x} already references another table, and {', '.join(deps)} are fully determined by it. "
                            "They are probably duplicated copies of that table's columns. Review before keeping them.")))
                continue
            new_name = _entity_name(x)
            out.append(Proposal(
                id=f"prop_{n_id}", kind="extract_entity", table_id=ds.id,
                title=f"Extract '{new_name}' from {names[ds.id]} ({x} → {', '.join(deps)})",
                detail=(f"{cp.distinct} distinct {x} values repeat across {n} rows, and {', '.join(deps)} never vary for the same "
                        f"{x}. Moving them to their own table removes repeated data. Source rows and values stay traceable."),
                determinant=x, dependents=deps, new_table_name=new_name, rows_in_new_table=cp.distinct,
                assumptions=[f"Each {x} value identifies exactly one {new_name}.",
                             f"{', '.join(deps)} describe the {new_name}, not the individual {names[ds.id]} row.",
                             "The new table is populated with DISTINCT rows of these columns; no information is dropped."],
                applicable=True))
        # multi-valued text columns (report only)
        for cp in prof.columns:
            if cp.inferred.kind != "text" and cp.inferred.kind != "identifier":
                continue
            s = df[cp.name].dropna()
            if len(s) < 4:
                continue
            multi = s[s.str.contains(SEPARATORS)]
            if len(multi) / len(s) < 0.3:
                continue
            tokens = [t for v in multi for t in SEPARATORS.split(v.strip()) if t]
            if not tokens or max(len(t) for t in tokens) > 40 or len(set(tokens)) > 0.6 * len(tokens):
                continue
            n_id += 1
            out.append(Proposal(
                id=f"prop_{n_id}", kind="multivalue", table_id=ds.id,
                title=f"{names[ds.id]}.{cp.name} may hold several values per cell",
                detail=(f"{len(multi)} of {len(s)} values contain separators and the pieces repeat ({len(set(tokens))} distinct among "
                        f"{len(tokens)}). A one-to-many or many-to-many child table would normally store them. "
                        "This needs a transformation during loading, so it is reported but not applied automatically."),
                assumptions=["Separators ',' ';' '|' split values.", "Each piece is a separate fact about the row."]))
    return out
