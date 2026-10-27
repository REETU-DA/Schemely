"""SmartSQL Schema Designer - Streamlit UI.

All logic lives in modules/; this file only wires widgets to it.
Run:  streamlit run app.py
"""
from __future__ import annotations

import glob
import hashlib
import os

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from modules import workflow as wf
from modules.dialects import DIALECTS, TOKEN_CHOICES, resolve_type
from modules.erd_generator import ERDUnavailable, build_dot, render
from modules.file_reader import SUPPORT_MATRIX, supported_extensions
from modules.import_planner import plan_orders
from modules.relationship_detection import unmatched_references
from modules.schema_designer import add_custom_table, apply_extract_entity, set_pk, set_surrogate
from modules.type_inference import ORDER_LABELS

st.set_page_config(page_title="SmartSQL Schema Designer", layout="wide")

STEPS = ["Upload files", "Inspect data", "Database & mode", "Review tables", "Resolve issues",
         "Relationships", "Generate SQL", "Download & use"]
HERE = os.path.dirname(os.path.abspath(__file__))

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;600;700&display=swap');
html, body, [class*="css"], .stApp { font-family: 'Schibsted Grotesk', 'Segoe UI', system-ui, sans-serif; }
.stApp { background: #F4F7F9; }
h1, h2, h3 { color: #12303F; letter-spacing: -0.01em; }
section[data-testid="stSidebar"] { background: #12303F; }
section[data-testid="stSidebar"] * { color: #E6F0F4 !important; }
section[data-testid="stSidebar"] .stButton button { background: transparent; border: 1px solid #2C5568; text-align: left; width: 100%; }
section[data-testid="stSidebar"] .stButton button:hover { border-color: #7FC4DB; }
.step-done { color: #7FC4DB; }
div[data-testid="stMetric"] { background: #FFFFFF; border: 1px solid #D5E1E7; border-radius: 6px; padding: 10px 14px; }
.callout { border-left: 4px solid #0F6E8C; background: #FFFFFF; padding: 10px 14px; margin: 6px 0 14px; border-radius: 0 6px 6px 0; }
.callout.warn { border-left-color: #C77D0A; }
.callout.bad { border-left-color: #B3372D; }
.callout.ok { border-left-color: #2F7D4F; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------------- state
def init_state():
    ss = st.session_state
    defaults = {"step": 1, "max_step": 1, "files": [], "file_sig": None, "datasets": [], "reports": [],
                "has_header": True, "analysis": None, "dialect": "postgresql", "mode": "direct", "staging": False,
                "dirty": True, "outputs": None}
    for k, v in defaults.items():
        if k not in ss:
            ss[k] = v


def goto(n: int):
    st.session_state.step = n
    st.session_state.max_step = max(st.session_state.max_step, n)


def callout(text: str, kind: str = ""):
    st.markdown(f'<div class="callout {kind}">{text}</div>', unsafe_allow_html=True)


def need_analysis() -> bool:
    if st.session_state.analysis is None:
        callout("Run the analysis first (step 3).", "warn")
        st.button("Go to step 3", on_click=goto, args=(3,), key="need_an")
        return False
    return True


def ensure_validated(force: bool = False):
    a = st.session_state.analysis
    if a is not None and (force or st.session_state.dirty or a.validation is None):
        wf.revalidate(a)
        st.session_state.dirty = False


def nav_buttons(prev: int | None, nxt: int | None, next_label: str = "Continue", disabled: bool = False):
    c1, c2, _ = st.columns([1, 1, 4])
    if prev:
        c1.button("Back", on_click=goto, args=(prev,), key=f"back{prev}")
    if nxt:
        c2.button(next_label, on_click=goto, args=(nxt,), disabled=disabled, type="primary", key=f"next{nxt}")


# ----------------------------------------------------------------------------- sidebar
def sidebar():
    with st.sidebar:
        st.markdown("### SmartSQL\nSchema Designer")
        st.caption("Files are processed locally. Nothing is uploaded to a third party or a database.")
        for i, name in enumerate(STEPS, 1):
            locked = i > st.session_state.max_step
            mark = "✓ " if i < st.session_state.max_step and i != st.session_state.step else ""
            st.button(f"{i}. {mark}{name}", key=f"nav{i}", on_click=goto, args=(i,), disabled=locked)
        st.caption(f"Target: {DIALECTS[st.session_state.dialect]['label']}")


# ----------------------------------------------------------------------------- step 1
def step_upload():
    st.header("1 · Upload files")
    st.write("Upload one or more files that belong to the same database. Related files are analysed together.")
    exts = [e.lstrip(".") for e in supported_extensions()]
    up = st.file_uploader("Data files", type=exts, accept_multiple_files=True, key="uploader")
    st.session_state.has_header = st.checkbox("First row is a header", value=st.session_state.has_header)
    c1, c2 = st.columns([1, 3])
    if c1.button("Load the example files"):
        paths = sorted(glob.glob(os.path.join(HERE, "examples", "*")))
        set_files([(os.path.basename(p), open(p, "rb").read()) for p in paths])
    if up:
        set_files([(f.name, f.getvalue()) for f in up])
    with st.expander("Which formats are supported?"):
        st.table(pd.DataFrame({"Extension": list(SUPPORT_MATRIX), "Status": list(SUPPORT_MATRIX.values())}))
    reports = st.session_state.reports
    if not reports:
        return
    st.subheader("Files")
    st.dataframe(pd.DataFrame([{
        "File": r.name, "Format": r.fmt, "Size (KB)": round(r.size / 1024, 1), "Datasets": len(r.datasets),
        "Rows": r.rows, "Columns": r.cols, "Status": r.status, "Warnings": len(r.warnings), "Errors": len(r.errors)}
        for r in reports]), use_container_width=True, hide_index=True)
    for r in reports:
        for e in r.errors:
            st.error(f"{r.name}: {e}")
        if r.warnings:
            with st.expander(f"{r.name}: {len(r.warnings)} warning(s)"):
                for w in r.warnings:
                    st.write("• " + w)
        if len(r.sheets) > 1:
            st.caption(f"{r.name} worksheets: " + ", ".join(f"{s.name} ({s.status})" for s in r.sheets))
    ds = st.session_state.datasets
    if ds:
        st.subheader("Datasets to analyse")
        st.caption("Each worksheet/file is a *potential* table. Untick the ones that are not relevant.")
        edit = st.data_editor(pd.DataFrame({"Include": [d.included for d in ds], "Dataset": [d.label for d in ds],
                                            "Rows": [d.n_rows for d in ds], "Columns": [d.n_cols for d in ds]}),
                              disabled=["Dataset", "Rows", "Columns"], hide_index=True, key="ds_editor", use_container_width=True)
        for d, inc in zip(ds, edit["Include"]):
            if d.included != bool(inc):
                d.included = bool(inc)
                st.session_state.analysis = None
    nav_buttons(None, 2, disabled=not any(d.included for d in st.session_state.datasets))


def set_files(files):
    sig = hashlib.md5(b"".join(n.encode() + hashlib.md5(b).digest() for n, b in files)).hexdigest() + str(st.session_state.has_header)
    if sig == st.session_state.file_sig:
        return
    with st.spinner("Reading files…"):
        ds, reps = wf.load(files, st.session_state.has_header)
    ss = st.session_state
    ss.update(file_sig=sig, files=files, datasets=ds, reports=reps, analysis=None, outputs=None, dirty=True)
    ss.profiles = None


# ----------------------------------------------------------------------------- step 2
def get_profiles():
    ss = st.session_state
    if ss.get("profiles") is None:
        with st.spinner("Profiling data…"):
            ss.profiles = wf.profile(ss.datasets)
    return ss.profiles


def step_inspect():
    st.header("2 · Inspect data")
    chosen = [d for d in st.session_state.datasets if d.included]
    if not chosen:
        callout("No datasets selected. Go back to step 1.", "warn")
        return
    profiles = get_profiles()
    label = st.selectbox("Dataset", [d.id for d in chosen], format_func=lambda i: next(d.label for d in chosen if d.id == i))
    ds = next(d for d in chosen if d.id == label)
    p = profiles[ds.id]
    m = st.columns(4)
    m[0].metric("Rows", f"{p.n_rows:,}")
    m[1].metric("Columns", p.n_cols)
    m[2].metric("Duplicate rows", p.duplicate_rows)
    m[3].metric("Fully empty rows", p.empty_rows)
    st.caption("Duplicate rows = complete copies of an earlier row. Duplicate values in a single column are shown per column below.")
    st.subheader("Column profile")
    st.dataframe(pd.DataFrame([c.to_row() for c in p.columns]), use_container_width=True, hide_index=True)
    st.subheader("Type recommendations")
    rows = []
    for c in p.columns:
        i = c.inferred
        rows.append({"Source column": c.name, "Suggested type": i.token,
                     f"In {DIALECTS[st.session_state.dialect]['label'].split(' ')[0]}": resolve_type(i.token, st.session_state.dialect, False, i.max_len)[0],
                     "Confidence": i.confidence, "Exceptions": "; ".join(i.exceptions), "Needs approval": "Yes" if i.needs_approval else "No",
                     "Reason": i.reason})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    for c in p.columns:
        flags = c.suspicious + ([f"mixed values ({c.mixed})"] if c.mixed else []) + (
            [f"{c.outlier_count} potential outlier(s): {', '.join(c.outlier_examples)}"] if c.outlier_count else []) + (
            ["constant column"] if c.constant else [])
        if flags:
            with st.expander(f"{c.name}: {len(flags)} observation(s)"):
                for f in flags:
                    st.write("• " + f)
                if c.date_patterns:
                    st.write("Date/time layouts:", c.date_patterns)
    with st.expander("Sample rows"):
        st.dataframe(ds.df.head(50), use_container_width=True)
    nav_buttons(1, 3)


# ----------------------------------------------------------------------------- step 3
def step_mode():
    st.header("3 · Database and design mode")
    keys = list(DIALECTS)
    dialect = st.selectbox("Target database", keys, index=keys.index(st.session_state.dialect), format_func=lambda k: DIALECTS[k]["label"])
    st.session_state.dialect = dialect
    callout(DIALECTS[dialect]["status"], "" if DIALECTS[dialect]["stable"] else "warn")
    mode = st.radio("Design mode", ["direct", "relational"], index=["direct", "relational"].index(st.session_state.mode),
                    format_func=lambda m: {"direct": "A · Direct import schema - one table per worksheet/file, columns preserved",
                                           "relational": "B · Relational schema design - also propose entities, junctions and normalisation"}[m])
    st.session_state.mode = mode
    staging = st.checkbox("Use staging tables (raw text copies → validated final tables)", value=st.session_state.staging)
    st.session_state.staging = staging
    st.caption("Staging helps when data is dirty (unmatched references, ambiguous dates, mixed types). Direct import is fine when "
               "validation shows no errors. With staging, foreign keys are added only after the data is loaded and checked.")
    if st.button("Analyse and propose a schema", type="primary"):
        with st.spinner("Detecting keys, relationships and types…"):
            st.session_state.analysis = wf.analyse(st.session_state.datasets, get_profiles(), dialect, mode, staging)
            st.session_state.dirty = True
            st.session_state.outputs = None
        goto(4)
        st.rerun()
    a = st.session_state.analysis
    if a is not None:
        callout("An analysis exists. Re-running it replaces the edits you made in step 4. "
                "Changing only the database does not need a re-run.", "warn")
        a.schema.dialect = dialect
    nav_buttons(2, 4 if a is not None else None)


# ----------------------------------------------------------------------------- step 4
def column_frame(a, t):
    rows = []
    for c in t.columns:
        rows.append({
            "Include": c.include, "Source column": c.source_column or "(generated)", "Column name": c.name, "SQL type": c.sql_type,
            "Nullable": c.nullable and c.key not in t.pk_columns, "Unique": c.unique, "CHECK": c.check,
            "Date order": c.date_order or "", "Approved": c.approved or not c.needs_approval,
            "Confidence": c.confidence, f"{DIALECTS[a.schema.dialect]['label'].split(' ')[0]} type": resolve_type(
                c.sql_type, a.schema.dialect, c.key in t.pk_columns, c.max_len)[0],
            "Why": c.reason})
    return pd.DataFrame(rows)


def apply_column_edits(t, edited: pd.DataFrame):
    for c, (_, row) in zip(t.columns, edited.iterrows()):
        if c.generated:
            c.name = str(row["Column name"]).strip() or c.name
            continue
        c.include = bool(row["Include"])
        c.name = str(row["Column name"]).strip()
        c.sql_type = str(row["SQL type"]).strip() or c.sql_type
        c.unique = bool(row["Unique"])
        c.check = str(row["CHECK"] or "").strip()
        c.date_order = str(row["Date order"]).strip() or None
        c.approved = bool(row["Approved"])
        c.nullable = False if c.key in t.pk_columns else bool(row["Nullable"])


def step_review():
    st.header("4 · Review tables")
    if not need_analysis():
        return
    a = st.session_state.analysis
    s = a.schema
    st.session_state.dirty = True
    tabs = st.tabs(["Tables & columns", "Foreign keys", "Normalisation", "Add / remove tables"])
    with tabs[0]:
        ids = [t.id for t in s.tables]
        tid = st.selectbox("Table", ids, format_func=lambda i: s.table(i).name + ("" if s.table(i).include else "  (removed)"))
        t = s.table(tid)
        c1, c2 = st.columns(2)
        t.name = c1.text_input("Table name", t.name, key=f"tn_{tid}").strip() or t.name
        t.include = c2.checkbox("Include this table", t.include, key=f"ti_{tid}")
        st.caption(f"Source: {t.source_file}" + (f" › {t.sheet}" if t.sheet else "") + f"  ·  {t.kind}")
        st.markdown("**Primary key**")
        callout(t.pk_explanation or "User-defined table.", "warn" if t.pk_needs_approval and not t.pk_approved else "")
        ka = a.key_analyses.get(t.dataset_id or "")
        if ka and ka.candidates:
            st.dataframe(pd.DataFrame([{"Columns": ", ".join(c.columns), "Unique %": round(c.uniqueness_pct, 1), "Missing": c.missing,
                                        "Duplicate values": c.duplicate_rows, "Verdict": c.verdict, "Why": c.reason}
                                       for c in ka.candidates]), hide_index=True, use_container_width=True)
        if ka and ka.blockers:
            st.warning("Key problems in the data: " + "; ".join(ka.blockers))
        modes = ["existing", "composite", "surrogate"]
        cur = t.pk_mode if t.pk_mode in modes else "surrogate"
        pm = st.radio("Key strategy", modes, index=modes.index(cur), horizontal=True, key=f"pm_{tid}",
                      format_func=lambda m: {"existing": "Existing column", "composite": "Composite (several columns)",
                                             "surrogate": "Generated surrogate key"}[m])
        src_cols = [c for c in t.columns if not c.generated]
        if pm in ("existing", "composite"):
            default = [k for k in t.pk_columns if t.col(k) and not t.col(k).generated][: 1 if pm == "existing" else 3]
            picked = st.multiselect("Key column(s)", [c.key for c in src_cols], default=default, key=f"pk_{tid}",
                                    format_func=lambda k: t.col(k).name, max_selections=1 if pm == "existing" else 4)
            if picked and (picked != t.pk_columns or pm != t.pk_mode):
                set_pk(t, picked)
        elif t.pk_mode != "surrogate":
            set_surrogate(t)
        if t.pk_needs_approval:
            t.pk_approved = st.checkbox("I approve this key choice", t.pk_approved, key=f"pa_{tid}")
        st.markdown("**Columns**")
        edited = st.data_editor(column_frame(a, t), hide_index=True, use_container_width=True, key=f"cols_{tid}",
                                disabled=["Source column", "Confidence", "Why"] + [c for c in column_frame(a, t).columns if c.endswith(" type") and c != "SQL type"],
                                column_config={"Date order": st.column_config.SelectboxColumn(options=["", "DMY", "MDY"],
                                                                                              help="DMY = DD/MM/YYYY, MDY = MM/DD/YYYY"),
                                               "SQL type": st.column_config.TextColumn(help="e.g. " + ", ".join(TOKEN_CHOICES[:6]))})
        apply_column_edits(t, edited)
        alts = [c for c in t.columns if c.alternative and c.sql_type != c.alternative]
        if alts:
            pick = st.selectbox("Better type available once the data is clean", [c.key for c in alts], key=f"alt_{tid}",
                                format_func=lambda k: f"{t.col(k).name}: use {t.col(k).alternative}")
            if st.button("Use that type", key=f"altb_{tid}"):
                t.col(pick).sql_type = t.col(pick).alternative
                st.rerun()
        with st.expander("Extra unique constraints and indexes"):
            names = [c.key for c in t.active_columns() if not c.generated]
            g = st.multiselect("Columns that must be unique together", names, key=f"ug_{tid}", format_func=lambda k: t.col(k).name)
            if st.button("Add unique constraint", key=f"uga_{tid}") and g and g not in t.uniques:
                t.uniques.append(g)
            ix = st.multiselect("Create an index on", names, key=f"ix_{tid}", format_func=lambda k: t.col(k).name)
            if st.button("Add index", key=f"ixa_{tid}") and ix and ix not in t.indexes:
                t.indexes.append(ix)
            if t.uniques or t.indexes:
                st.write("Unique:", [[t.col(k).name for k in g] for g in t.uniques], " Indexes:", [[t.col(k).name for k in g] for g in t.indexes])
                if st.button("Clear extra unique constraints and indexes", key=f"clr_{tid}"):
                    t.uniques, t.indexes = [], []
                    st.rerun()
    with tabs[1]:
        fk_tab(a)
    with tabs[2]:
        norm_tab(a)
    with tabs[3]:
        add_remove_tab(a)
    nav_buttons(3, 5)


def fk_label(s, fk):
    ct, pt = s.table(fk.child_table), s.table(fk.parent_table)
    return (f"{ct.name}.{', '.join(ct.col(k).name for k in fk.child_cols)} → {pt.name}.{', '.join(pt.col(k).name for k in fk.parent_cols)}")


def fk_tab(a):
    s = a.schema
    if not s.fks:
        st.info("No relationships were detected between the datasets. Matching names or values are only treated as evidence, never forced.")
        return
    st.write("Tick **Approve** only for relationships that are true in your business. Unapproved candidates are left out of the SQL.")
    df = pd.DataFrame([{"Approve": fk.accepted, "Relationship": fk_label(s, fk), "Status": fk.status + (" · ambiguous" if fk.ambiguous else ""),
                        "Match %": None if fk.match_pct is None else round(100 * fk.match_pct, 1), "Unmatched rows": fk.unmatched_rows,
                        "Cardinality": fk.cardinality, "Index child": fk.index_child, "ON DELETE": fk.on_delete or ""} for fk in s.fks])
    ed = st.data_editor(df, hide_index=True, use_container_width=True, key="fk_editor",
                        disabled=["Relationship", "Status", "Match %", "Unmatched rows", "Cardinality"],
                        column_config={"ON DELETE": st.column_config.SelectboxColumn(
                            options=["", "CASCADE", "SET NULL", "RESTRICT", "NO ACTION"],
                            help="Never inferred. Leave empty unless you have an explicit business rule.")})
    for fk, (_, r) in zip(s.fks, ed.iterrows()):
        fk.accepted, fk.index_child, fk.on_delete = bool(r["Approve"]), bool(r["Index child"]), (r["ON DELETE"] or None)
    for fk in s.fks:
        with st.expander(fk_label(s, fk)):
            for e in fk.evidence:
                st.write("• " + e)
            if fk.unmatched_rows:
                rel = next((r for r in a.relationships if r.id == fk.relationship_id), None)
                if rel:
                    um = unmatched_references(rel, a.datasets)
                    st.warning(f"{len(um)} child row(s) have no parent. The constraint cannot be enforced until they are resolved.")
                    st.dataframe(um, hide_index=True, use_container_width=True)
                    st.download_button("Download unmatched references (CSV)", um.to_csv(index=False).encode("utf-8-sig"),
                                       f"unmatched_{fk.id}.csv", "text/csv", key=f"um_{fk.id}")


def norm_tab(a):
    s = a.schema
    j = wf.junctions(a)
    if j:
        st.markdown("**Many-to-many link tables detected**")
        for x in j:
            st.write(f"• `{s.table(x.dataset_id).name}` links {' and '.join(s.table(p).name for p in x.parents)} - {x.evidence}")
    if s.mode != "relational":
        st.info("Switch to *Relational schema design* in step 3 for normalisation proposals.")
        return
    if not s.proposals:
        st.success("No repeated entities or multi-valued fields were found.")
    for p in s.proposals:
        with st.container():
            st.markdown(f"**{p.title}**")
            st.write(p.detail)
            for x in p.assumptions:
                st.caption("Assumption: " + x)
            if p.applicable and not p.applied:
                if st.button("Apply this proposal", key=f"ap_{p.id}"):
                    apply_extract_entity(s, p)
                    st.rerun()
            elif p.applied:
                st.success("Applied")
            else:
                st.caption("Report only - needs a transformation during loading, so it is not applied automatically.")


def add_remove_tab(a):
    s = a.schema
    st.write("Remove a table with *Include this table* on the Tables tab. Add a table of your own here:")
    name = st.text_input("New table name", key="newt_name")
    spec = st.text_area("Columns, one per line:  name  type", "id integer\nname text", key="newt_cols")
    if st.button("Add table") and name.strip():
        cols = []
        for line in spec.splitlines():
            bits = line.split()
            if bits:
                cols.append((bits[0], " ".join(bits[1:]) or "text"))
        if cols:
            add_custom_table(s, name.strip(), cols)
            st.success(f"Table '{name}' added. It has no source data, so it cannot be validated.")


# ----------------------------------------------------------------------------- step 5
def find_fks(a, table_name, column):
    s = a.schema
    return [fk for fk in s.fks if s.table(fk.child_table) and s.table(fk.child_table).name == table_name
            and any(s.table(fk.child_table).col(k).name == column for k in fk.child_cols)]


def find_col(a, table_name, column):
    t = next((x for x in a.schema.tables if x.name == table_name), None)
    return t, (next((c for c in t.columns if c.name == column), None) if t else None)


def step_issues():
    st.header("5 · Resolve issues")
    if not need_analysis():
        return
    a = st.session_state.analysis
    ensure_validated()
    if st.button("Re-run validation"):
        ensure_validated(force=True)
    v, summ = a.validation, a.summary
    cols = st.columns(5)
    for col, key in zip(cols, ["Datasets analysed", "Proposed tables", "Primary key candidates", "Foreign key candidates", "Confirmed by user (accepted)"]):
        col.metric(key, summ[key])
    cols = st.columns(5)
    for col, key in zip(cols, ["Unresolved key issues", "Columns requiring review", "Ambiguous date columns", "Errors", "Warnings"]):
        col.metric(key, summ[key])
    callout(f"Status: <b>{summ['Status']}</b>", "ok" if summ["Status"] == "Ready" else "warn")
    with st.expander("Every check that was run"):
        st.dataframe(pd.DataFrame([{"Check": c.name, "Result": c.status, "Findings": c.issues, "Note": c.note} for c in v.checks]),
                     hide_index=True, use_container_width=True)
    sev = st.multiselect("Show", ["error", "warning", "info"], default=["error", "warning"])
    shown = [i for i in v.issues if i.severity in sev]
    if not shown:
        st.success("Nothing to show for this filter.")
    for n, i in enumerate(shown):
        icon = {"error": "🔴", "warning": "🟠", "info": "ℹ️"}[i.severity]
        with st.expander(f"{icon} {i.table}.{i.column} · {i.code}" if i.column else f"{icon} {i.table} · {i.code}"):
            st.write(i.message)
            st.caption("How to resolve: " + i.resolution)
            if i.details:
                st.dataframe(pd.DataFrame(i.details), hide_index=True, use_container_width=True)
                if i.count > len(i.details):
                    st.caption(f"Showing {len(i.details)} of {i.count} affected rows. The CSV report has the same cap.")
            issue_actions(a, i, n)
    nav_buttons(4, 6)


def issue_actions(a, i, n):
    key = f"act{n}"
    if i.code == "fk_unmatched" and st.button("Reject this foreign key", key=key):
        for fk in find_fks(a, i.table, i.column):
            fk.accepted = False
        st.session_state.dirty = True
        st.rerun()
    elif i.code in ("type_conversion", "truncation", "leading_zeros_lost", "column_unapproved") and st.button(
            "Store this column as text (safe) and approve", key=key):
        t, c = find_col(a, i.table, i.column)
        if c:
            c.sql_type, c.approved = "text", True
            st.session_state.dirty = True
            st.rerun()
    elif i.code == "ambiguous_date":
        order = st.radio("These dates are…", ["DMY", "MDY"], key=key + "r", format_func=lambda o: ORDER_LABELS[o], horizontal=True)
        if st.button("Apply this date order", key=key):
            t, c = find_col(a, i.table, i.column)
            if c:
                c.date_order, c.approved = order, True
                st.session_state.dirty = True
                st.rerun()
    elif i.code in ("pk_missing", "pk_duplicate") and st.button("Use a generated surrogate key instead", key=key):
        t = next((x for x in a.schema.tables if x.name == i.table), None)
        if t:
            set_surrogate(t)
            st.session_state.dirty = True
            st.rerun()
    elif i.code == "pk_unapproved" and st.button("Approve key", key=key):
        t = next((x for x in a.schema.tables if x.name == i.table), None)
        if t:
            t.pk_approved = True
            st.session_state.dirty = True
            st.rerun()


# ----------------------------------------------------------------------------- step 6
def step_erd():
    st.header("6 · Relationships")
    if not need_analysis():
        return
    a = st.session_state.analysis
    dot = build_dot(a.schema)
    st.graphviz_chart(dot, use_container_width=True)
    st.download_button("Download diagram source (DOT)", dot.encode(), "erd.dot", key="dl_dot")
    plan = plan_orders(a.schema)
    c1, c2, c3 = st.columns(3)
    c1.markdown("**Table creation order**")
    c1.write("\n".join(f"{i}. {n}" for i, n in enumerate(plan["creation"], 1)))
    c2.markdown("**Data loading order**")
    c2.write("\n".join(f"{i}. {n}" for i, n in enumerate(plan["loading"], 1)))
    c3.markdown("**Constraint creation**")
    c3.write("\n".join("• " + x for x in plan["constraints"]))
    if plan["cycle"]:
        callout(plan["explanation"], "warn")
    j = wf.junctions(a)
    for x in j:
        st.info(f"Many-to-many: `{a.schema.table(x.dataset_id).name}` is a link table. {x.evidence}")
    nav_buttons(5, 7)


# ----------------------------------------------------------------------------- step 7
def copy_button(sql: str):
    import json
    payload = json.dumps(sql)
    components.html(f"""
    <button id="b" style="padding:8px 14px;border:1px solid #0F6E8C;background:#0F6E8C;color:#fff;border-radius:6px;cursor:pointer;font-family:sans-serif">Copy SQL to clipboard</button>
    <span id="m" style="font-family:sans-serif;margin-left:10px;color:#2F7D4F"></span>
    <script>
    const t = {payload};
    document.getElementById('b').onclick = async () => {{
      try {{ await navigator.clipboard.writeText(t); }}
      catch (e) {{ const a=document.createElement('textarea'); a.value=t; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); }}
      document.getElementById('m').textContent = 'Copied';
    }};
    </script>""", height=50)


def step_sql():
    st.header("7 · Generate SQL")
    if not need_analysis():
        return
    a = st.session_state.analysis
    with st.spinner("Validating and generating…"):
        sql = wf.generate(a)
        st.session_state.dirty = False
    summ = a.summary
    if summ["Status"] == "Ready":
        callout("All checks passed and every decision is approved.", "ok")
    else:
        callout(f"<b>Review required</b>: {summ['Errors']} error(s), {summ['Columns requiring review']} unapproved column decision(s). "
                "The script is generated anyway and says so in its header; read step 5 before importing.", "warn")
    st.download_button("Download schema.sql", sql.encode("utf-8"), "schema.sql", "text/plain", type="primary", key="dl_sql7")
    copy_button(sql)
    st.code(sql, language="sql")
    nav_buttons(6, 8)


# ----------------------------------------------------------------------------- step 8
def step_download():
    st.header("8 · Download and use")
    if not need_analysis():
        return
    a = st.session_state.analysis
    with st.spinner("Building reports…"):
        files, failures, z = wf.outputs(a)
    st.session_state.dirty = False
    st.download_button("Download schema.sql", files["schema.sql"], "schema.sql", "text/plain", type="primary", key="dl_sql8")
    st.download_button("Download everything (ZIP)", z, "smartsql_output.zip", "application/zip", key="dl_zip")
    for name, why in failures.items():
        st.error(f"{name} could not be created: {why}")
    st.subheader("Individual files")
    for name in [n for n in files if "/" not in n and n != "schema.sql"]:
        st.download_button(name, files[name], name, key=f"dl_{name}")
    csvs = [n for n in files if n.startswith("import_ready_csv/")]
    if csvs:
        with st.expander(f"import_ready_csv/ ({len(csvs)} files, included in the ZIP)"):
            for n in csvs:
                st.download_button(n.split("/")[-1], files[n], n.split("/")[-1], key=f"dl_{n}")
    if "erd.svg" in files:
        st.subheader("Entity relationship diagram")
        st.image(files["erd.svg"].decode("utf-8"), use_container_width=True)
    t1, t2, t3, t4 = st.tabs(["Output A · Schema SQL", "Output B · Import preparation", "Output C · Loading guidance", "README"])
    t1.code(files["schema.sql"].decode(), language="sql")
    t2.text(files["import_preparation.txt"].decode())
    t3.text(files["output_c_loading_guidance.txt"].decode())
    t4.text(files["README.txt"].decode())
    nav_buttons(7, None)


# ----------------------------------------------------------------------------- main
def main():
    init_state()
    sidebar()
    {1: step_upload, 2: step_inspect, 3: step_mode, 4: step_review, 5: step_issues, 6: step_erd, 7: step_sql, 8: step_download}[
        st.session_state.step]()


main()
