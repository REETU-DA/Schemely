"""Evidence-based SQL type inference (dialect-neutral type tokens).

Principles: never silently discard or reinterpret values. If some values cannot be
converted the *safe* type (text) is suggested and the better-typed alternative is
offered for approval. Ambiguous dates are never resolved silently.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

import pandas as pd

from .dialects import is_known_token, parse_token

RE_INT = re.compile(r"^[+-]?\d+$")
RE_DEC = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)$")
RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RE_LEADING_ZERO = re.compile(r"^[+-]?0\d")
RE_FORMATTED_NUM = re.compile(r"^[^\d]*\d[\d,. ]*[^\d]*$")

_FULL_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december"]
MONTHS = {m[:3]: i for i, m in enumerate(_FULL_MONTHS, 1)}
_MONTH_WORDS = set(_FULL_MONTHS) | set(MONTHS) | {"sept"}

RE_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
RE_XYZ = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")
RE_NAMED1 = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3,9})\.?,?[ -](\d{2}|\d{4})$")
RE_NAMED2 = re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$")
_TIME_SPLIT = re.compile(
    r"(?:^|[T\s])(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[AaPp][Mm])?)\s*(Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?$")
RE_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?\s*([AaPp][Mm])?$")

ORDER_LABELS = {"DMY": "DD/MM/YYYY (day first)", "MDY": "MM/DD/YYYY (month first)"}

NAME_PHONE = re.compile(r"(phone|mobile|\btel\b|telephone|fax|whatsapp|contact_?(no|num))", re.I)
NAME_POSTAL = re.compile(r"(zip|postal|post_?code|pin_?code|\bpin\b)", re.I)
NAME_TEXT_ID = re.compile(r"(aadhaar|aadhar|ssn|passport|barcode|isbn|iban|account_?(no|num)|licen[cs]e|ifsc|\bpan\b)", re.I)
NAME_ID = re.compile(r"(^|_|[a-z])(id|code|key|no|num|number|ref)$", re.I)
NAME_TEXTY = re.compile(r"(name|title|descr|address|comment|note|remark|city|state|country|email|url|label)", re.I)
NAME_BOOL = re.compile(r"(^is_|^has_|^can_|flag|active|enabled|deleted|verified|paid)", re.I)
NAME_MONEY = re.compile(r"(price|amount|fee|cost|salary|total|balance|revenue|tax|discount|payment)", re.I)

BOOL_WORDS = {"true", "false", "t", "f", "yes", "no", "y", "n"}


@dataclass
class TypeInference:
    token: str
    kind: str  # integer bigint numeric boolean date timestamp timestamptz time uuid identifier phone postal text empty
    reason: str
    confidence: str = "high"  # high | medium | low
    exceptions: list[str] = field(default_factory=list)
    exception_values: list[tuple] = field(default_factory=list)  # (index, value)
    needs_approval: bool = False
    alternative: str | None = None
    alternative_reason: str = ""
    date_order: str | None = None  # 'DMY' | 'MDY' | None (unresolved or not applicable)
    date_ambiguous: int = 0
    max_len: int = 0
    has_leading_zeros: bool = False
    n_values: int = 0


# ----------------------------------------------------------------------------- temporal parsing
def _mk(y, mo, d):
    try:
        return dt.date(y, mo, d), "ok"
    except ValueError:
        return None, "invalid"


def _month_num(word: str):
    w = word.lower().rstrip(".")
    if w not in _MONTH_WORDS:
        return None
    return MONTHS.get(w[:3])


def date_class(part: str) -> str | None:
    if RE_YMD.match(part):
        return "ymd"
    if RE_XYZ.match(part):
        return "xyz"
    if RE_NAMED1.match(part) or RE_NAMED2.match(part):
        return "named"
    return None


def parse_date_part(p: str, order: str | None):
    """-> (date|None, status) status in ok | ambiguous | invalid"""
    m = RE_YMD.match(p)
    if m:
        y, mo, d = map(int, m.groups())
        return _mk(y, mo, d)
    m = RE_XYZ.match(p)
    if m:
        a, b, y = map(int, m.groups())
        if order == "DMY":
            return _mk(y, b, a)
        if order == "MDY":
            return _mk(y, a, b)
        if a > 12 and b > 12:
            return None, "invalid"
        if a > 12:
            return _mk(y, b, a)
        if b > 12:
            return _mk(y, a, b)
        if a == b:
            return _mk(y, b, a)
        return None, "ambiguous"
    m = RE_NAMED1.match(p)
    if m:
        d, mon, yy = m.groups()
        mo = _month_num(mon)
        if not mo:
            return None, "invalid"
        y = int(yy) if len(yy) == 4 else (2000 + int(yy) if int(yy) < 70 else 1900 + int(yy))
        return _mk(y, mo, int(d))
    m = RE_NAMED2.match(p)
    if m:
        mon, d, y = m.groups()
        mo = _month_num(mon)
        if not mo:
            return None, "invalid"
        return _mk(int(y), mo, int(d))
    return None, "invalid"


def _parse_time(t: str):
    m = RE_TIME_ONLY.match(t.strip())
    if not m:
        return None
    h, mi, s, frac, ap = m.groups()
    h, mi, s = int(h), int(mi), int(s or 0)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.lower() == "pm" else 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    micro = int((frac or "0")[:6].ljust(6, "0"))
    return dt.time(h, mi, s, micro)


def parse_temporal(s: str, order: str | None = None):
    """-> (kind, date|None, time|None, has_tz, status). kind: date | datetime | time | invalid"""
    s = s.strip()
    tpart, tz, dpart = None, False, s
    m = _TIME_SPLIT.search(s)
    if m:
        tpart = _parse_time(m.group(1))
        if tpart is None:
            return "invalid", None, None, False, "invalid"
        tz = bool(m.group(2))
        dpart = s[: m.start()].strip().rstrip("Tt").strip()
    if dpart == "":
        if tpart is None:
            return "invalid", None, None, False, "invalid"
        return "time", None, tpart, tz, "ok"
    d, status = parse_date_part(dpart, order)
    if status == "invalid":
        return "invalid", None, None, False, "invalid"
    return ("datetime" if tpart is not None else "date"), d, tpart, tz, status


# ----------------------------------------------------------------------------- helpers
def _digits(v: str):
    v = v.lstrip("+-")
    ip, _, fp = v.partition(".")
    ip = ip.lstrip("0") or "0"
    return len(ip), len(fp)


def _size_varchar(max_len: int) -> int:
    return max(20, int(math.ceil(max_len * 2 / 10.0) * 10))


def _pattern_signature(v: str) -> str:
    return re.sub(r"[A-Za-z]+", "A", re.sub(r"\d+", "9", v))


def _cap(lst, n=50):
    return lst[:n]


def _rows_for(series: pd.Series, bad: set, n=50):
    out = []
    for idx, v in series.items():
        if v in bad:
            out.append((idx, v))
            if len(out) >= n:
                break
    return out


def infer_column(name: str, series: pd.Series) -> TypeInference:
    s = series.dropna()
    n = len(s)
    if n == 0:
        return TypeInference("text", "empty", "The column has no values, so no type can be inferred.",
                             confidence="low", needs_approval=True)
    vc = s.value_counts()
    values = list(vc.index)
    max_len = int(max(len(v) for v in values))
    base = dict(n_values=n, max_len=max_len)
    lname = str(name)

    def finish(inf: TypeInference) -> TypeInference:
        inf.n_values, inf.max_len = n, max_len
        return inf

    # --- identifier-like name hints that must stay text, whatever the digits look like
    if NAME_PHONE.search(lname):
        return finish(TypeInference(f"varchar({max(20, _size_varchar(max_len))})", "phone",
                      "Phone numbers are identifiers, not quantities: leading zeros, '+' and spacing must be preserved.",
                      "high"))
    if NAME_POSTAL.search(lname):
        return finish(TypeInference(f"varchar({_size_varchar(max_len)})", "postal",
                      "Postal codes can have leading zeros or letters, so they are stored as text.", "high"))
    if NAME_TEXT_ID.search(lname):
        return finish(TypeInference(f"varchar({_size_varchar(max_len)})", "identifier",
                      "Government/account identifiers must keep every character, so they are stored as text.", "high"))

    # --- UUID
    if all(RE_UUID.match(v) for v in values):
        return finish(TypeInference("uuid", "uuid", f"All {n} values match the UUID format.", "high"))

    # --- boolean
    low = {v.strip().lower() for v in values}
    if low <= BOOL_WORDS and len(low) >= 2:
        return finish(TypeInference("boolean", "boolean", f"All values are true/false-style words ({', '.join(sorted(low))}).", "high"))
    if low <= {"0", "1"}:
        if NAME_BOOL.search(lname):
            return finish(TypeInference("boolean", "boolean",
                          "Only 0/1 values and the column name suggests a flag.", "medium", needs_approval=True,
                          alternative="integer", alternative_reason="Keep as a number if 0/1 are counts, not flags."))
        return finish(TypeInference("integer", "integer", "Only 0/1 values; kept numeric because the name does not say it is a flag.",
                      "medium", alternative="boolean", alternative_reason="Choose boolean if this is a yes/no flag."))

    # --- numeric
    num_vals = [v for v in values if RE_DEC.match(v.strip())]
    num_rows = int(sum(vc[v] for v in num_vals))
    if num_vals and num_rows / n >= 0.9:
        bad = [v for v in values if not RE_DEC.match(v.strip())]
        leading = any(RE_LEADING_ZERO.match(v.strip()) for v in num_vals)
        if leading:
            return finish(TypeInference(f"varchar({_size_varchar(max_len)})", "identifier",
                          "Some numeric-looking values have leading zeros (for example 00125); a number type would silently "
                          "drop them, so text is used.", "high" if not bad else "medium", has_leading_zeros=True))
        is_int_all = all(RE_INT.match(v.strip()) for v in num_vals)
        id_dec = max(_digits(v.strip())[0] for v in num_vals)
        max_scale = max(_digits(v.strip())[1] for v in num_vals)
        if is_int_all:
            big = max(abs(int(v)) for v in num_vals)
            if big <= 2_147_483_647:
                tok, kind = "integer", "integer"
            elif big <= 9_223_372_036_854_775_807:
                tok, kind = "bigint", "bigint"
            else:
                tok, kind = f"numeric({id_dec},0)", "numeric"
            why = f"All numeric values are whole numbers (largest magnitude {big:,})."
            if NAME_ID.search(lname) and not re.search(r"(^|_)id$", lname.lower()):
                why += " The name looks like a code; switch to text if codes may gain letters or leading zeros."
        else:
            scale = max(max_scale, 2) if NAME_MONEY.search(lname) and max_scale <= 2 else max_scale
            prec = id_dec + 4 + scale
            tok, kind = f"numeric({prec},{scale})", "numeric"
            why = (f"Decimal values; up to {id_dec} digit(s) before and {max_scale} after the point. Precision gives 4 spare "
                   f"integer digits of headroom (an assumption - adjust if the business range is larger).")
        if not bad:
            return finish(TypeInference(tok, kind, why, "high" if n >= 5 else "medium"))
        bad_rows = int(sum(vc[v] for v in bad))
        ex = [f"{vc[v]} row(s) contain '{v}'" for v in bad[:5]]
        hint = ""
        if any(RE_FORMATTED_NUM.match(v) and re.search(r"\d", v) for v in bad):
            hint = " Some look like formatted numbers (currency symbols or thousands separators) - clean them before import."
        return finish(TypeInference(
            "text", "text",
            f"{num_rows} of {n} values are numeric but {bad_rows} value(s) are not ({', '.join(repr(v) for v in bad[:3])}). "
            f"Text is the safe default because converting would lose or reject those values.{hint}",
            "low", exceptions=ex, exception_values=_rows_for(s, set(bad)), needs_approval=True,
            alternative=tok, alternative_reason="Use only after the non-numeric values are fixed or intentionally mapped to NULL."))

    # --- temporal
    t = _infer_temporal(lname, s, vc, values, n)
    if t is not None:
        return finish(t)

    # --- text-like
    sigs = {_pattern_signature(v) for v in values}
    uniq_ratio = len(values) / n
    id_like = bool(NAME_ID.search(lname)) or (not NAME_TEXTY.search(lname) and len(sigs) == 1 and uniq_ratio > 0.5 and max_len <= 30 and any(ch.isdigit() for ch in values[0]))
    if id_like and max_len <= 100:
        return finish(TypeInference(f"varchar({_size_varchar(max_len)})", "identifier",
                      f"Identifier-like text (max observed length {max_len}). Length is sized with 2x headroom (minimum 20) "
                      f"so longer future IDs are not truncated.", "high" if len(sigs) <= 2 else "medium"))
    return finish(TypeInference("text", "text", "Free text of variable length; no length limit is imposed so values are never truncated.",
                  "high"))


def _infer_temporal(lname, s, vc, values, n):
    parsed = {}
    for v in values:
        parsed[v] = parse_temporal(v, None)
    good = [v for v in values if parsed[v][4] != "invalid"]
    good_rows = int(sum(vc[v] for v in good))
    if not good or good_rows / n < 0.9:
        return None
    # at least one separator-bearing date/time pattern; guard against plain small ints being 'times'
    kinds = {parsed[v][0] for v in good}
    if kinds == {"time"} and not all(":" in v for v in good):
        return None
    if "time" in kinds and kinds != {"time"}:
        return None
    # order evidence from slash-style dates
    dmy = mdy = amb = 0
    for v in good:
        k, d, tm, tz, st = parsed[v]
        part = re.sub(_TIME_SPLIT, "", v.strip()).strip().rstrip("Tt").strip()
        if date_class(part) == "xyz":
            a, b = (int(x) for x in RE_XYZ.match(part).groups()[:2])
            if a > 12 >= b:
                dmy += vc[v]
            elif b > 12 >= a:
                mdy += vc[v]
            elif st == "ambiguous":
                amb += vc[v]
    exceptions: list[str] = []
    order = None
    if dmy and mdy:
        return TypeInference("text", "text",
                             f"Dates mix day-first ({dmy} values) and month-first ({mdy} values) formats, so the column cannot be read safely as dates.",
                             "low", exceptions=["Conflicting DD/MM and MM/DD evidence in the same column."],
                             needs_approval=True, alternative="date",
                             alternative_reason="Standardise the source format first, then choose a date type.")
    if dmy:
        order = "DMY"
    elif mdy:
        order = "MDY"
    # re-parse with chosen order to find invalid / still-ambiguous values
    bad_vals, final = [], {}
    for v in values:
        k, d, tm, tz, st = parse_temporal(v, order)
        final[v] = (k, d, tm, tz, st)
        if st == "invalid":
            bad_vals.append(v)
    ambiguous_rows = int(sum(vc[v] for v in values if final[v][4] == "ambiguous"))
    ok_kinds = {final[v][0] for v in values if final[v][4] != "invalid"}
    tzs = {final[v][3] for v in values if final[v][4] != "invalid"}
    date_only = ok_kinds == {"date"}
    has_time_vals = any(final[v][2] is not None and final[v][2] != dt.time(0, 0) for v in values if final[v][4] != "invalid")
    if ok_kinds == {"time"}:
        tok, kind = "time", "time"
        why = "All values are clock times."
    elif tzs == {True}:
        tok, kind, why = "timestamptz", "timestamptz", "All values carry a time-zone offset."
    elif len(tzs) > 1:
        return TypeInference("text", "text", "Some timestamps carry a time-zone offset and some do not.", "low",
                             exceptions=["Mixed time-zone information."], needs_approval=True, alternative="timestamptz",
                             alternative_reason="Decide which zone offset-less values belong to, then convert.")
    elif date_only or not has_time_vals:
        tok, kind = "date", "date"
        why = ("All values are calendar dates." if date_only else
               "Values are timestamps whose time part is always midnight, so DATE is sufficient.")
    else:
        tok, kind = "timestamp", "timestamp"
        why = "Values include a time of day."
    conf = "high"
    needs = False
    if ambiguous_rows:
        conf, needs = "low", True
        sample = next(v for v in values if final[v][4] == "ambiguous")
        exceptions.append(f"{ambiguous_rows} row(s) are ambiguous, e.g. '{sample}' could be day-first or month-first.")
        why += f" {ambiguous_rows} value(s) are ambiguous (day and month both <= 12) and NO order was assumed - choose DD/MM or MM/DD."
    elif order:
        why += f" Day/month order {order} was inferred because other values in the column can only be read that way."
        conf = "medium"
    if bad_vals:
        bad_rows = int(sum(vc[v] for v in bad_vals))
        if bad_rows / n > 0.1:
            return None
        exceptions += [f"{vc[v]} row(s) contain '{v}'" for v in bad_vals[:5]]
        return TypeInference("text", "text",
                             f"Most values look like {tok}s but {bad_rows} do not parse ({', '.join(repr(v) for v in bad_vals[:3])}). "
                             "Text is the safe default.", "low", exceptions=exceptions,
                             exception_values=_rows_for(s, set(bad_vals)), needs_approval=True, alternative=tok,
                             alternative_reason="Use after fixing or intentionally blanking the invalid values.", date_order=order,
                             date_ambiguous=ambiguous_rows)
    inf = TypeInference(tok, kind, why, conf, exceptions=exceptions, needs_approval=needs, date_order=order,
                        date_ambiguous=ambiguous_rows)
    if ambiguous_rows:
        amb_set = {v for v in values if final[v][4] == "ambiguous"}
        inf.exception_values = _rows_for(s, amb_set)
    return inf


# ----------------------------------------------------------------------------- conversion check
def _checker(base: str, args: tuple, date_order: str | None):
    if base in ("integer", "bigint"):
        lim = 2_147_483_647 if base == "integer" else 9_223_372_036_854_775_807

        def f(v):
            t = v.strip()
            if not RE_INT.match(t):
                return ("type", f"'{v}' is not a whole number")
            if abs(int(t)) > lim:
                return ("type", f"'{v}' is out of range for {base}")
            return None
        return f
    if base == "numeric":
        p, s = (args + (18, 2))[:2] if len(args) == 2 else ((args[0], 0) if len(args) == 1 else (18, 2))

        def f(v):
            t = v.strip()
            if not RE_DEC.match(t):
                return ("type", f"'{v}' is not a decimal number")
            il, fl = _digits(t)
            if fl > s:
                return ("precision", f"'{v}' has {fl} decimal places but the type keeps {s}; it would be rounded")
            if il > p - s:
                return ("type", f"'{v}' has too many integer digits for numeric({p},{s})")
            return None
        return f
    if base == "boolean":
        return lambda v: None if v.strip().lower() in BOOL_WORDS | {"0", "1"} else ("type", f"'{v}' is not a boolean value")
    if base == "uuid":
        return lambda v: None if RE_UUID.match(v.strip()) else ("type", f"'{v}' is not a UUID")
    if base == "varchar" and args:
        n = args[0]
        return lambda v: None if len(v) <= n else ("truncation", f"value of length {len(v)} exceeds varchar({n})")
    if base in ("date", "timestamp", "timestamptz", "time"):
        def f(v):
            k, d, tm, tz, st = parse_temporal(v, date_order)
            if st == "invalid":
                return ("type", f"'{v}' is not a valid {base}")
            if st == "ambiguous":
                return ("ambiguous", f"'{v}' is ambiguous: day-first or month-first? Choose a date order.")
            if base == "time" and k != "time":
                return ("type", f"'{v}' is not a time of day")
            if base != "time" and k == "time":
                return ("type", f"'{v}' is a time, not a {base}")
            if base == "date" and k == "datetime" and tm != dt.time(0, 0):
                return ("type", f"'{v}' has a time part that DATE would discard")
            if base == "timestamptz" and not tz:
                return ("type", f"'{v}' has no time-zone offset")
            return None
        return f
    return None  # text / unknown: nothing to verify


def convert_check(series: pd.Series, token: str, date_order: str | None = None):
    """Values that would NOT load cleanly as `token`: list of (index, value, code, reason)."""
    base, args = parse_token(token)
    checker = _checker(base, args, date_order)
    if checker is None:
        return []
    s = series.dropna()
    bad = {}
    for v in s.unique():
        r = checker(v)
        if r:
            bad[v] = r
    if not bad:
        return []
    mask = s.isin(list(bad))
    return [(idx, v, bad[v][0], bad[v][1]) for idx, v in s[mask].items()]


def has_leading_zero_values(series: pd.Series) -> bool:
    return any(RE_LEADING_ZERO.match(v.strip()) for v in series.dropna().unique())
