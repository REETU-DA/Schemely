"""Entity-relationship diagram as Graphviz DOT (pure Python) + optional rendering."""
from __future__ import annotations

import html
import re

from .schema_designer import Schema

MAX_OTHER_COLUMNS = 6


class ERDUnavailable(Exception):
    pass


def _port(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def build_dot(schema: Schema) -> str:
    fks = schema.active_fks()
    fk_cols = {(fk.child_table, k) for fk in fks for k in fk.child_cols}
    lines = ["digraph ERD {", '  rankdir=LR; splines=true; nodesep=0.5; ranksep=0.9;',
             '  node [shape=plaintext, fontname="Helvetica", fontsize=11];',
             '  edge [fontname="Helvetica", fontsize=9, color="#0F5B78"];']
    ids = {}
    for i, t in enumerate(schema.active_tables()):
        ids[t.id] = f"t{i}"
    for t in schema.active_tables():
        cols = t.active_columns()
        key = [c for c in cols if c.key in t.pk_columns or (t.id, c.key) in fk_cols]
        others = [c for c in cols if c not in key]
        shown = key + others[:MAX_OTHER_COLUMNS]
        rows = [f'<TR><TD COLSPAN="2" BGCOLOR="#0F5B78"><FONT COLOR="white"><B>{html.escape(t.name)}</B></FONT></TD></TR>']
        for c in shown:
            tags = []
            if c.key in t.pk_columns:
                tags.append("PK")
            if (t.id, c.key) in fk_cols:
                tags.append("FK")
            rows.append(f'<TR><TD ALIGN="LEFT" PORT="{_port(c.key)}">{html.escape(c.name)}</TD>'
                        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="9">{html.escape(" ".join(tags)) or "&nbsp;"}</FONT></TD></TR>')
        if len(others) > MAX_OTHER_COLUMNS:
            rows.append(f'<TR><TD COLSPAN="2" ALIGN="LEFT"><I>… +{len(others) - MAX_OTHER_COLUMNS} more column(s)</I></TD></TR>')
        lines.append(f'  {ids[t.id]} [label=<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" BGCOLOR="#F6FAFB">'
                     + "".join(rows) + "</TABLE>>];")
    for fk in fks:
        p, c = ids[fk.parent_table], ids[fk.child_table]
        one = fk.cardinality == "one-to-one"
        lines.append(f'  {p}:{_port(fk.parent_cols[0])} -> {c}:{_port(fk.child_cols[0])} '
                     f'[dir=both, arrowtail=tee, arrowhead={"tee" if one else "crow"}, taillabel="1", '
                     f'headlabel="{"1" if one else "N"}"];')
    lines.append("}")
    return "\n".join(lines)


def render(dot: str, fmt: str = "svg") -> bytes:
    """Render with Graphviz. Raises ERDUnavailable (never returns an empty file)."""
    try:
        import graphviz
        src = graphviz.Source(dot)
        data = src.pipe(format=fmt)
    except Exception as e:
        raise ERDUnavailable(f"Graphviz could not render the diagram ({type(e).__name__}). Install Graphviz "
                             "(https://graphviz.org/download/) and the 'graphviz' Python package; the DOT source is still available.")
    if not data:
        raise ERDUnavailable("Graphviz produced an empty diagram.")
    return data
