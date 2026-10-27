"""Column and dataset profiling from actual values."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .models import Dataset
from .type_inference import (RE_DEC, RE_INT, TypeInference, date_class, infer_column, parse_temporal)

NULL_LIKE = {"null", "none", "nan", "n/a", "na", "#n/a", "nil", "-", "--", "?", "undefined", "missing"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class ColumnProfile:
    name: str
    position: int
    n_rows: int
    missing: int
    missing_pct: float
    distinct: int
    duplicate_values: int  # non-null values that repeat an earlier value
    is_unique: bool  # every non-null value distinct
    candidate_key: bool  # unique AND no NULLs
    constant: bool
    samples: list[str]
    min_value: str | None
    max_value: str | None
    min_len: int
    max_len: int
    outlier_count: int
    outlier_examples: list[str]
    mixed: str | None
    suspicious: list[str]
    date_patterns: dict[str, int]
    inferred: TypeInference

    def to_row(self) -> dict:
        return {
            "Column": self.name,
            "Detected type": self.inferred.kind,
            "Missing": self.missing,
            "Missing %": round(self.missing_pct, 2),
            "Distinct": self.distinct,
            "Duplicate values": self.duplicate_values,
            "Candidate key": "Yes" if self.candidate_key else "No",
            "Min": self.min_value,
            "Max": self.max_value,
            "Samples": ", ".join(self.samples),
            "Needs review": "Yes" if (self.inferred.needs_approval or self.mixed or self.suspicious) else "No",
        }


@dataclass
class DatasetProfile:
    dataset_id: str
    n_rows: int
    n_cols: int
    duplicate_rows: int
    empty_rows: int
    columns: list[ColumnProfile] = field(default_factory=list)

    def col(self, name: str) -> ColumnProfile:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)


def _value_class(v: str) -> str:
    t = v.strip()
    if RE_INT.match(t):
        return "integer"
    if RE_DEC.match(t):
        return "decimal"
    if t.lower() in {"true", "false", "yes", "no", "t", "f", "y", "n"}:
        return "boolean"
    k = parse_temporal(t)[0]
    if k != "invalid" and (date_class(t) or ":" in t):
        return "date/time"
    return "text"


def profile_column(name: str, position: int, series: pd.Series) -> ColumnProfile:
    n = len(series)
    s = series.dropna()
    nn = len(s)
    missing = n - nn
    vc = s.value_counts()
    distinct = int(len(vc))
    inferred = infer_column(name, series)
    lengths = s.map(len) if nn else pd.Series([], dtype=int)
    samples = [str(v) for v in list(vc.index[:5])] if nn else []
    # --- min / max where meaningful
    min_v = max_v = None
    out_cnt, out_ex = 0, []
    if nn and inferred.kind in ("integer", "bigint", "numeric"):
        nums = pd.to_numeric(s, errors="coerce").dropna()
        if len(nums):
            min_v, max_v = f"{nums.min():g}", f"{nums.max():g}"
            if distinct >= 8:
                q1, q3 = nums.quantile(0.25), nums.quantile(0.75)
                iqr = q3 - q1
                if iqr > 0:
                    mask = (nums < q1 - 1.5 * iqr) | (nums > q3 + 1.5 * iqr)
                    out_cnt = int(mask.sum())
                    out_ex = [f"{x:g}" for x in nums[mask].unique()[:5]]
    elif nn and inferred.kind in ("date", "timestamp", "timestamptz"):
        keys = []
        for v in vc.index:
            k, d, tm, tz, st = parse_temporal(v, inferred.date_order)
            if d is not None:
                keys.append((d, tm.isoformat() if tm else "", v))
        if keys:
            keys.sort(key=lambda x: (x[0], x[1]))
            min_v, max_v = keys[0][2], keys[-1][2]
    # --- date patterns
    patterns: dict[str, int] = {}
    if inferred.kind in ("date", "timestamp", "timestamptz", "time") or (nn and inferred.alternative in ("date", "timestamp", "timestamptz")):
        for v, c in vc.items():
            sig = re.sub(r"[A-Za-z]+", "Mon", re.sub(r"\d", "9", v.strip()))
            patterns[sig] = patterns.get(sig, 0) + int(c)
    # --- mixed types
    mixed = None
    if nn:
        classes: dict[str, int] = {}
        for v, c in vc.items():
            k = _value_class(v)
            classes[k] = classes.get(k, 0) + int(c)
        groups = {("number" if k in ("integer", "decimal") else k): 0 for k in classes}
        for k, c in classes.items():
            groups["number" if k in ("integer", "decimal") else k] += c
        if len(groups) > 1:
            dominant = max(groups, key=groups.get)
            if dominant != "text" and groups[dominant] / nn >= 0.5:
                mixed = ", ".join(f"{k}: {v}" for k, v in sorted(groups.items(), key=lambda x: -x[1]))
    # --- suspicious formats
    sus = []
    if nn:
        ws = int((s != s.str.strip()).sum())
        if ws:
            sus.append(f"{ws} value(s) have leading/trailing whitespace")
        nl = int(s.str.strip().str.lower().isin(NULL_LIKE).sum())
        if nl:
            sus.append(f"{nl} value(s) look like NULL placeholders (e.g. 'N/A', '-')")
        cc = int(s.map(lambda x: bool(CONTROL.search(x))).sum())
        if cc:
            sus.append(f"{cc} value(s) contain control characters")
        if inferred.kind == "text" and 1 < distinct <= 200:
            fold = pd.Series(list(vc.index)).str.strip().str.casefold()
            collide = int(len(fold) - fold.nunique())
            if collide:
                sus.append(f"{collide} value(s) differ only by case/spacing (e.g. 'Delhi' vs 'delhi')")
        if len(patterns) > 1 and inferred.kind in ("date", "timestamp", "timestamptz", "time"):
            sus.append(f"{len(patterns)} different date/time layouts in one column")
        if inferred.has_leading_zeros:
            sus.append("numeric-looking values with leading zeros (kept as text)")
    return ColumnProfile(
        name=name, position=position, n_rows=n, missing=missing, missing_pct=(100.0 * missing / n if n else 0.0),
        distinct=distinct, duplicate_values=nn - distinct, is_unique=(nn > 0 and distinct == nn),
        candidate_key=(n > 0 and missing == 0 and distinct == n), constant=(nn > 0 and distinct == 1),
        samples=samples, min_value=min_v, max_value=max_v,
        min_len=int(lengths.min()) if nn else 0, max_len=int(lengths.max()) if nn else 0,
        outlier_count=out_cnt, outlier_examples=out_ex, mixed=mixed, suspicious=sus,
        date_patterns=patterns, inferred=inferred)


def profile_dataset(ds: Dataset) -> DatasetProfile:
    df = ds.df
    n = len(df)
    dup = int(df.duplicated(keep="first").sum()) if n else 0
    empty_rows = int(df.isna().all(axis=1).sum()) if n else 0
    cols = [profile_column(c, i, df[c]) for i, c in enumerate(df.columns, 1)]
    return DatasetProfile(ds.id, n, len(df.columns), dup, empty_rows, cols)


def profile_all(datasets: list[Dataset]) -> dict[str, DatasetProfile]:
    return {d.id: profile_dataset(d) for d in datasets}
