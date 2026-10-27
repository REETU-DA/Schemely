"""SQL identifier hygiene: naming, reserved words, quoting, length limits."""
from __future__ import annotations

import hashlib
import re

LIMITS = {"postgresql": 63, "mysql": 64, "sqlite": 255, "sqlserver": 128}

RESERVED = frozenset(
    """add all alter analyse analyze and any array as asc authorization between binary bit both by case cast char
    character check collate column commit constraint create cross current_date current_time current_timestamp
    current_user database date decimal default deferrable delete desc distinct do double drop else end except
    exists false fetch float for foreign from full function grant group having if in index initially inner insert
    int integer intersect into is join key leading left level like limit natural not null numeric offset on only
    option or order outer over partition placing primary procedure public range references return returns revoke
    right role rollback row rows schema select session_user set some table tablespace then time timestamp to
    trailing transaction trigger true union unique unknown update user using values varchar variadic verbose view
    when where window with comment source type""".split()
)

_SIMPLE = re.compile(r"^[a-z_][a-z0-9_]*$")


def to_snake(name: str) -> str:
    s = str(name).strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s


def sanitize(name: str, fallback: str, prefix: str = "c_") -> str:
    s = to_snake(name)
    if not s:
        return fallback
    if s[0].isdigit():
        s = prefix + s
    return s


def shorten(name: str, limit: int) -> str:
    if len(name) <= limit:
        return name
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    return f"{name[: limit - 7]}_{h}"


def is_reserved(name: str) -> bool:
    return name.lower() in RESERVED


def is_simple(name: str) -> bool:
    return bool(_SIMPLE.match(name))


def make_unique(names: list[str], limit: int = 63) -> list[str]:
    seen: set[str] = set()
    out = []
    for n in names:
        base = shorten(n, limit)
        cand, i = base, 2
        while cand.lower() in seen:
            suffix = f"_{i}"
            cand = shorten(base, limit - len(suffix)) + suffix
            i += 1
        seen.add(cand.lower())
        out.append(cand)
    return out


def quote(name: str, dialect: str) -> str:
    """Quote only when needed (reserved word or non-simple name). Always escapes safely."""
    if is_simple(name) and not is_reserved(name):
        return name
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if dialect == "sqlserver":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def comment_safe(text: str) -> str:
    """Make untrusted text safe for inclusion in a `--` SQL comment."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()
