"""Foreign-key / relationship detection using several kinds of evidence.

A matching name or overlapping values is *evidence*, never proof. Every candidate
carries its evidence, a status, and defaults to "not accepted" unless the evidence is
strong and complete (name match + 100% of non-NULL child values found in a unique parent key).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import pandas as pd

from .data_profiler import DatasetProfile
from .dialects import token_group
from .models import Dataset

MEASURE_WORDS = re.compile(r"(age|qty|quantity|price|amount|marks?|score|year|count|total|fee|salary|rate|percent|height|weight|"
                           r"balance|rank|number_of|duration|day|month)", re.I)
MAX_PAIRS = 40_000


@dataclass
class Relationship:
    id: str
    child_dataset: str
    child_cols: tuple[str, ...]
    parent_dataset: str
    parent_cols: tuple[str, ...]
    name_score: float
    child_non_null: int
    matched_rows: int
    match_pct: float
    unmatched_rows: int
    unmatched_distinct: int
    parent_unique: bool
    parent_missing: int
    child_unique: bool
    cardinality: str  # one-to-one | one-to-many
    status: str  # strong | candidate | weak
    ambiguous: bool = False
    evidence: list[str] = field(default_factory=list)
    accepted: bool = False  # default decision; user can change

    def summary(self) -> str:
        return (f"{self.child_dataset}.{self.child_cols[0]} → {self.parent_dataset}.{self.parent_cols[0]}")


def _tokens(name: str) -> list[str]:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    toks = [t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t]
    return [t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks]


def _norm(name: str) -> str:
    return "".join(_tokens(name))


def table_stem(ds: Dataset) -> str:
    base = ds.sheet if ds.sheet and not re.match(r"(?i)^sheet\d*$", ds.sheet) else ds.source_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return base


def name_score(child_col: str, parent_col: str, parent_table: str) -> tuple[float, str]:
    c, p = _norm(child_col), _norm(parent_col)
    pt = _norm(parent_table)
    if c == "id" and p == "id":
        return 0.0, ""  # two tables' own 'id' columns are not related by name
    if c == p and c != "":
        return 1.0, "identical column names"
    if p == "id" and pt and c in (pt + "id", pt + "key", pt + "code"):
        return 0.9, f"column name '{child_col}' = parent table '{parent_table}' + '{parent_col}'"
    if pt and c == pt + p:
        return 0.9, f"column name '{child_col}' = parent table '{parent_table}' + '{parent_col}'"
    if pt and pt in c and p and (p in c or c.endswith("id")):
        return 0.7, f"column name contains parent table name '{parent_table}'"
    ratio = difflib.SequenceMatcher(None, c, p).ratio()
    if ratio >= 0.75:
        return round(ratio * 0.8, 2), f"similar column names ({ratio:.0%} similarity)"
    return 0.0, ""


def _eligible_parent(cp) -> bool:
    return cp.distinct > 0 and cp.duplicate_values == 0 and cp.inferred.kind not in ("numeric", "boolean", "empty", "date", "timestamp", "timestamptz", "time")


def detect_relationships(datasets: list[Dataset], profiles: dict[str, DatasetProfile],
                         table_names: dict[str, str] | None = None) -> list[Relationship]:
    table_names = table_names or {d.id: table_stem(d) for d in datasets}
    rels: list[Relationship] = []
    pairs = 0
    parents = []
    for pd_ in datasets:
        for cp in profiles[pd_.id].columns:
            if _eligible_parent(cp):
                parents.append((pd_, cp))
    for cd in datasets:
        cprof = profiles[cd.id]
        for cc in cprof.columns:
            if cc.missing == cd.n_rows or cc.inferred.kind in ("numeric", "boolean", "empty"):
                continue
            cgroup = token_group(cc.inferred.token)
            child_series = cd.df[cc.name].dropna()
            best_for_col: list[Relationship] = []
            for pd_, pc in parents:
                if pd_.id == cd.id and pc.name == cc.name:
                    continue
                if token_group(pc.inferred.token) != cgroup:
                    continue
                ns, why = name_score(cc.name, pc.name, table_names.get(pd_.id, pd_.id))
                if ns < 0.5:
                    if (cc.is_unique or cgroup not in ("int", "text", "uuid") or cc.distinct < 3 or pc.distinct < 3
                            or MEASURE_WORDS.search(cc.name) or cc.inferred.kind in ("text",) and cc.max_len > 40):
                        continue
                pairs += 1
                if pairs > MAX_PAIRS:
                    break
                pvals = pd_.df[pc.name].dropna().unique()
                mask = child_series.isin(pvals)
                matched = int(mask.sum())
                total = len(child_series)
                pct = matched / total if total else 0.0
                if pct < 0.5:
                    continue
                if ns < 0.5 and pct < 1.0:
                    continue
                unmatched = total - matched
                un_distinct = int(child_series[~mask].nunique())
                child_unique = cc.is_unique and cc.missing == 0 and total >= 2
                ev = [f"Name evidence: {why}." if why else "No name similarity (value overlap only).",
                      f"{pct:.1%} of the {total} non-NULL child values exist in the parent column.",
                      f"Parent column uniqueness: {'passed' if pc.duplicate_values == 0 else 'FAILED'}"
                      + (f" ({pc.missing} NULL parent value(s))" if pc.missing else "") + ".",
                      f"Child column has {cc.duplicate_values} repeated value(s) -> {'one-to-one' if child_unique else 'one-to-many'}."]
                if unmatched:
                    ev.append(f"{unmatched} child row(s) ({un_distinct} distinct value(s)) have no matching parent.")
                if ns < 0.5:
                    ev.append("Value-only evidence: integer/ID ranges often overlap by coincidence - treat as a hint.")
                status = ("strong" if ns >= 0.9 and pct >= 0.95 else "candidate" if ns >= 0.5 else "weak")
                accepted = status == "strong" and unmatched == 0 and pc.missing == 0 and cd.id != pd_.id
                rels.append(Relationship(
                    id="", child_dataset=cd.id, child_cols=(cc.name,), parent_dataset=pd_.id, parent_cols=(pc.name,),
                    name_score=ns, child_non_null=total, matched_rows=matched, match_pct=pct, unmatched_rows=unmatched,
                    unmatched_distinct=un_distinct, parent_unique=pc.duplicate_values == 0, parent_missing=pc.missing,
                    child_unique=child_unique, cardinality="one-to-one" if child_unique else "one-to-many",
                    status=status, evidence=ev, accepted=accepted))
                best_for_col.append(rels[-1])
            # one child column that fits several parents equally well -> ambiguity
            if len(best_for_col) > 1:
                top = max(best_for_col, key=lambda r: (r.name_score, r.match_pct))
                close = [r for r in best_for_col if r.name_score >= top.name_score - 0.1 and r.match_pct >= 0.95]
                if len(close) > 1:
                    for r in close:
                        r.ambiguous = True
                        r.accepted = False
                        r.evidence.append("Ambiguous: this child column fits more than one parent table equally well - choose the correct one.")
    # reverse duplicates (A.x->B.y and B.y->A.x): both columns are unique. Pick the direction where the
    # parent table "owns" the column (e.g. users.user_id), otherwise keep one and flag the ambiguity.
    keep: list[Relationship] = []
    groups: dict[frozenset, list[Relationship]] = {}
    for r in rels:
        key = frozenset([(r.child_dataset, r.child_cols), (r.parent_dataset, r.parent_cols)])
        groups.setdefault(key, []).append(r)
    for key, members in groups.items():
        if len(members) == 1:
            keep.append(members[0])
            continue

        def owns(r):
            pt = _norm(table_names.get(r.parent_dataset, r.parent_dataset))
            return 1 if pt and pt in _norm(r.parent_cols[0]) else 0

        members.sort(key=lambda r: (-owns(r), r.child_dataset, r.parent_dataset))
        chosen = members[0]
        if owns(members[0]) == owns(members[1]):
            chosen.ambiguous = True
            chosen.accepted = False
            chosen.evidence.append("Ambiguous direction: both columns are unique and hold the same kind of values. "
                                   "Confirm which table is the parent.")
        keep.append(chosen)
    keep.sort(key=lambda r: (r.child_dataset, r.child_cols, -r.name_score, r.parent_dataset))
    for i, r in enumerate(keep, 1):
        r.id = f"rel_{i}"
    return keep


def unmatched_references(rel: Relationship, datasets: dict[str, Dataset]) -> pd.DataFrame:
    """Exact unmatched child values with their source location."""
    cd, pdset = datasets[rel.child_dataset], datasets[rel.parent_dataset]
    col, pcol = rel.child_cols[0], rel.parent_cols[0]
    s = cd.df[col].dropna()
    bad = s[~s.isin(pdset.df[pcol].dropna().unique())]
    return pd.DataFrame({
        "source_file": cd.source_file, "worksheet": cd.sheet or "", "source_row": [cd.source_row(i) for i in bad.index],
        "column": col, "unmatched_value": bad.values, "expected_in": f"{pdset.label}.{pcol}"})


@dataclass
class JunctionCandidate:
    dataset_id: str
    parents: list[str]
    fk_columns: list[str]
    extra_columns: list[str]
    composite_unique: bool
    evidence: str


def detect_junctions(datasets: list[Dataset], rels: list[Relationship]) -> list[JunctionCandidate]:
    """Tables that are link tables between two parents (many-to-many resolved by a junction table)."""
    by_child: dict[str, list[Relationship]] = {}
    for r in rels:
        if r.accepted and r.child_dataset != r.parent_dataset:
            by_child.setdefault(r.child_dataset, []).append(r)
    dmap = {d.id: d for d in datasets}
    out = []
    for cid, rs in by_child.items():
        parents = sorted({r.parent_dataset for r in rs})
        if len(parents) < 2:
            continue
        fkc = [r.child_cols[0] for r in rs]
        d = dmap[cid]
        extra = [c for c in d.df.columns if c not in fkc]
        comp = not d.df.duplicated(subset=fkc, keep=False).any() if len(d.df) else False
        non_unique_each = all(not r.child_unique for r in rs)
        if comp and non_unique_each:
            out.append(JunctionCandidate(
                cid, parents, fkc, extra, True,
                f"Each ({', '.join(fkc)}) combination appears once while each column repeats on its own: "
                f"a many-to-many link between {' and '.join(parents)}."))
    return out
