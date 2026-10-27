"""SQL dialect metadata and semantic-type -> dialect-type resolution.

The rest of the app works with dialect-neutral *type tokens* such as ``integer``,
``numeric(12,2)`` or ``varchar(40)``. Only this module turns a token into the SQL
type of a particular database, so switching dialect never loses user edits.
"""
from __future__ import annotations

import re

DIALECTS = {
    "postgresql": {
        "label": "PostgreSQL",
        "status": "Primary target. SQL generation is implemented and covered by structural tests; "
                  "it was not executed against a live PostgreSQL server in the build environment.",
        "stable": True,
    },
    "sqlite": {
        "label": "SQLite",
        "status": "Implemented. Generated SQL is executed on a real SQLite engine in the test-suite.",
        "stable": True,
    },
    "mysql": {
        "label": "MySQL (experimental)",
        "status": "Experimental. Implemented but never executed against a MySQL server - review before use.",
        "stable": False,
    },
    "sqlserver": {
        "label": "SQL Server (experimental)",
        "status": "Experimental. Implemented but never executed against SQL Server - review before use.",
        "stable": False,
    },
}

_TOKEN = re.compile(r"^\s*([a-zA-Z_ ]+?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$")
_ALIASES = {
    "int": "integer", "int4": "integer", "int8": "bigint", "smallint": "integer",
    "decimal": "numeric", "bool": "boolean", "char": "varchar", "character varying": "varchar",
    "datetime": "timestamp", "string": "text",
}
KNOWN = {"text", "varchar", "integer", "bigint", "numeric", "boolean", "date", "timestamp",
         "timestamptz", "time", "uuid"}
TOKEN_CHOICES = ["text", "varchar(50)", "integer", "bigint", "numeric(12,2)", "boolean", "date",
                 "timestamp", "timestamptz", "time", "uuid"]


def parse_token(token: str):
    """-> (base, args tuple). Unknown tokens come back with their own lowercase base."""
    m = _TOKEN.match(token or "")
    if not m:
        return (str(token).strip().lower(), ())
    base = m.group(1).strip().lower()
    base = _ALIASES.get(base, base)
    args = tuple(int(x) for x in m.groups()[1:] if x is not None)
    return base, args


def canonical_token(token: str) -> str:
    base, args = parse_token(token)
    if base in KNOWN:
        if args and base in ("varchar", "numeric"):
            return f"{base}({','.join(map(str, args))})"
        return base
    return (token or "").strip()


def is_known_token(token: str) -> bool:
    return parse_token(token)[0] in KNOWN


def token_group(token: str) -> str:
    base, _ = parse_token(token)
    return {
        "integer": "int", "bigint": "int", "numeric": "num", "text": "text", "varchar": "text",
        "boolean": "bool", "date": "date", "timestamp": "ts", "timestamptz": "ts", "time": "time",
        "uuid": "uuid",
    }.get(base, "other")


def resolve_type(token: str, dialect: str, is_key: bool = False, max_len: int = 0):
    """Return (sql_type, note | None). Unknown tokens are passed through verbatim."""
    base, args = parse_token(token)
    note = None
    if base not in KNOWN:
        return (token or "").strip(), "Custom type passed through unchanged; not verified against the data."
    p = s = 0
    if base == "numeric":
        if len(args) == 2:
            p, s = args
        elif len(args) == 1:
            p, s = args[0], 0
        else:
            p, s = 18, 2
        cap = {"mysql": 65, "sqlserver": 38}.get(dialect)
        if cap and p > cap:
            note = f"Precision {p} exceeds the {dialect} limit; clamped to {cap}."
            p = cap
            s = min(s, p)
    if dialect == "postgresql":
        m = {"text": "TEXT", "integer": "INTEGER", "bigint": "BIGINT", "boolean": "BOOLEAN", "date": "DATE",
             "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ", "time": "TIME", "uuid": "UUID"}
        if base == "varchar":
            return (f"VARCHAR({args[0]})" if args else "VARCHAR"), None
        if base == "numeric":
            return f"NUMERIC({p},{s})", note
        return m[base], None
    if dialect == "sqlite":
        m = {"text": "TEXT", "integer": "INTEGER", "bigint": "INTEGER", "boolean": "BOOLEAN", "date": "DATE",
             "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMP", "time": "TIME", "uuid": "TEXT"}
        if base == "varchar":
            return (f"VARCHAR({args[0]})" if args else "TEXT"), None
        if base == "numeric":
            return f"NUMERIC({p},{s})", "SQLite NUMERIC has flexible affinity; precision is advisory."
        return m[base], ("SQLite has no time-zone type; store UTC." if base == "timestamptz" else None)
    if dialect == "mysql":
        m = {"integer": "INT", "bigint": "BIGINT", "boolean": "BOOLEAN", "date": "DATE",
             "timestamp": "DATETIME(6)", "timestamptz": "DATETIME(6)", "time": "TIME", "uuid": "CHAR(36)"}
        if base == "text":
            if is_key:
                n = min(max(255, max_len * 2), 768)
                return f"VARCHAR({n})", "TEXT cannot be indexed without a prefix in MySQL; VARCHAR used for this key column."
            return "TEXT", None
        if base == "varchar":
            return (f"VARCHAR({args[0]})" if args else "VARCHAR(255)"), None
        if base == "numeric":
            return f"DECIMAL({p},{s})", note
        return m[base], ("MySQL has no time-zone type; store UTC." if base == "timestamptz" else None)
    if dialect == "sqlserver":
        m = {"integer": "INT", "bigint": "BIGINT", "boolean": "BIT", "date": "DATE", "timestamp": "DATETIME2",
             "timestamptz": "DATETIMEOFFSET", "time": "TIME", "uuid": "UNIQUEIDENTIFIER"}
        if base == "text":
            if is_key:
                n = min(max(255, max_len * 2), 450)
                return f"NVARCHAR({n})", "NVARCHAR(MAX) cannot be a key in SQL Server; sized NVARCHAR used for this key column."
            return "NVARCHAR(MAX)", None
        if base == "varchar":
            return (f"NVARCHAR({args[0]})" if args else "NVARCHAR(255)"), None
        if base == "numeric":
            return f"DECIMAL({p},{s})", note
        return m[base], None
    raise ValueError(f"Unknown dialect: {dialect}")
