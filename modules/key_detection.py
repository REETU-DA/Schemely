"""Primary-key candidate detection (single and composite) based on actual data."""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

import pandas as pd

from .data_profiler import DatasetProfile
from .models import Dataset

TEXTY_NAME = re.compile(r"(name|title|descr|address|comment|note|remark|city|state|country|label)", re.I)
ID_NAME = re.compile(r"(^|_)(id|code|key|no|num|number|ref|uuid|sku)$|^id", re.I)


@dataclass
class KeyCandidate:
    columns: tuple[str, ...]
    uniqueness_pct: float
    missing: int
    duplicate_rows: int
    score: int
    verdict: str  # strong | acceptable | weak | invalid
    reason: str
    duplicate_examples: list[str] = field(default_factory=list)


@dataclass
class KeyAnalysis:
    dataset_id: str
    candidates: list[KeyCandidate]
    mode: str  # existing | composite | surrogate
    columns: tuple[str, ...]
    needs_approval: bool
    explanation: str
    blockers: list[str]
    alternatives: list[str]


def _singular(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


def _name_bonus(col: str, table_name: str, position: int) -> int:
    b = 0
    lc = col.lower()
    if lc == "id" or ID_NAME.search(lc):
        b += 10
    if _singular(table_name) and _singular(table_name) in re.sub(r"[^a-z0-9]+", "_", lc):
        b += 10
    if position == 1:
        b += 5
    return b


def _type_adjust(kind: str, max_len: int) -> tuple[int, str]:
    if kind in ("integer", "bigint", "identifier", "uuid"):
        return 5, ""
    if kind in ("numeric",):
        return -30, "decimal values rarely make good keys"
    if kind in ("date", "timestamp", "timestamptz", "time"):
        return -30, "dates/times rarely identify a record by themselves"
    if kind == "boolean":
        return -50, "boolean cannot identify records"
    if max_len > 50:
        return -20, "long free text is a poor key"
    return 0, ""


def analyse_keys(ds: Dataset, prof: DatasetProfile, table_name: str) -> KeyAnalysis:
    df, n = ds.df, len(ds.df)
    cands: list[KeyCandidate] = []
    blockers: list[str] = []
    if n == 0:
        return KeyAnalysis(ds.id, [], "surrogate", (), True, "The dataset has no rows, so no key can be verified.",
                           ["No rows to analyse."], _alternatives())
    for cp in prof.columns:
        if cp.missing == n:
            continue
        valid = cp.missing == 0 and cp.duplicate_values == 0 and cp.distinct == n
        near = cp.distinct >= 0.9 * max(n - cp.missing, 1) and cp.distinct > 1
        hinted = bool(ID_NAME.search(cp.name))
        if not (valid or near or hinted):
            continue
        adj, why = _type_adjust(cp.inferred.kind, cp.max_len)
        score = 100 + _name_bonus(cp.name, table_name, cp.position) + adj
        if TEXTY_NAME.search(cp.name) and not ID_NAME.search(cp.name):
            score -= 20  # names/titles are descriptive, not identifiers
        if cp.inferred.needs_approval:
            score -= 10
        dups = []
        if cp.duplicate_values:
            dd = df[cp.name].dropna()
            dups = [str(v) for v in dd[dd.duplicated(keep=False)].unique()[:5]]
        if valid:
            verdict = "strong" if score >= 110 else ("acceptable" if score >= 85 else "weak")
            reason = "100% unique, no NULLs" + (f"; {why}" if why else "")
        else:
            verdict = "invalid"
            bits = []
            if cp.missing:
                bits.append(f"{cp.missing} NULL value(s)")
            if cp.duplicate_values:
                bits.append(f"{cp.duplicate_values} duplicate value(s)")
            reason = "; ".join(bits) or "not unique"
            blockers.append(f"{cp.name}: {reason}" + (f" (e.g. {', '.join(dups[:3])})" if dups else ""))
        cands.append(KeyCandidate((cp.name,), 100.0 * cp.distinct / max(n - cp.missing, 1), cp.missing,
                                  cp.duplicate_values, score, verdict, reason, dups))
    cands.sort(key=lambda c: (-(c.verdict in ("strong", "acceptable")), -c.score, c.columns))

    best = next((c for c in cands if c.verdict in ("strong", "acceptable")), None)
    broken_ids = [c for c in cands if c.verdict == "invalid" and ID_NAME.search(c.columns[0])]
    if best and broken_ids and not ID_NAME.search(best.columns[0]):
        best = None  # the obvious identifier column is broken; do not quietly promote an unrelated column
    if best:
        needs = best.verdict != "strong"
        expl = (f"{best.columns[0]} is a {'strong' if not needs else 'possible'} primary key: {best.reason}."
                + (" Please confirm it is the true business identifier." if needs else ""))
        return KeyAnalysis(ds.id, cands, "existing", best.columns, needs, expl, blockers, _alternatives())

    # composite search
    pool = [cp for cp in prof.columns if cp.missing == 0 and cp.distinct >= 2
            and cp.inferred.kind not in ("numeric", "boolean", "empty") and cp.max_len <= 60]
    pool.sort(key=lambda c: -c.distinct)
    pool = pool[:8]
    for size in (2, 3):
        found = []
        for combo in itertools.combinations(pool, size):
            names = [c.name for c in combo]
            if not df.duplicated(subset=names, keep=False).any():
                score = sum(_name_bonus(c.name, table_name, c.position) for c in combo)
                found.append((score, tuple(names)))
        if found:
            found.sort(key=lambda x: (-x[0], x[1]))
            names = found[0][1]
            cands.append(KeyCandidate(names, 100.0, 0, 0, 90, "acceptable",
                                      f"the combination of {', '.join(names)} is unique and has no NULLs"))
            expl = (f"No single column is a valid key, but ({', '.join(names)}) is unique together. "
                    "Use it only if business rules say that combination identifies a record.")
            return KeyAnalysis(ds.id, cands, "composite", names, True, expl, blockers, _alternatives())

    expl = ("No valid primary key exists in the data. A surrogate key is the safe default: it adds a generated identity "
            "column and keeps every original column (and original identifiers) unchanged.")
    return KeyAnalysis(ds.id, cands, "surrogate", (), True, expl, blockers, _alternatives())


def _alternatives() -> list[str]:
    return [
        "A. Review the source data and pick a valid existing key (fix duplicates / missing identifiers first).",
        "B. Define a composite key if the business rules support it.",
        "C. Generate a surrogate identity key and keep the original identifiers as ordinary columns. "
        "Other tables keep referring to the original identifier columns (made UNIQUE where a foreign key needs it).",
    ]
