# SmartSQL Schema Designer

Upload CSV / Excel / JSON / Parquet files → the tool profiles them, infers SQL types, detects primary and foreign
keys, lets you review every decision, and generates an executable `CREATE TABLE` script **before** you import any data.
Everything runs locally; the tool never connects to a database and never sends your data anywhere.

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# optional, for the ER diagram image: install the Graphviz program (https://graphviz.org/download/)
streamlit run app.py
```

Run the tests: `pytest`

Try it with the files in `examples/` (button "Load the example files" in step 1). `sample_output/` is what the tool
produced for them with default decisions (PostgreSQL).

## Short user guide

1. **Upload files** – drop several files at once; related files are analysed together. Untick worksheets you do not need.
2. **Inspect data** – per-column profile (missing, distinct, duplicates, min/max, outliers, mixed types, date layouts)
   and the suggested SQL type with reason and confidence.
3. **Database & mode** – pick PostgreSQL / SQLite / MySQL / SQL Server and *Direct import* (one table per sheet) or
   *Relational design* (also proposes extracted entities and flags link tables). Optionally use staging tables.
4. **Review tables** – rename tables/columns, change types, choose keys, approve/reject foreign keys, exclude columns,
   set NULL/UNIQUE/CHECK, add tables. Sensible defaults are pre-filled.
5. **Resolve issues** – every failed check lists the table, column, value and source row, with one-click fixes
   (reject FK, store as text, choose DD/MM or MM/DD, use surrogate key).
6. **Relationships** – ER diagram, creation / loading / constraint order, junction tables.
7. **Generate SQL** – view, copy, or download `schema.sql`.
8. **Download & use** – ZIP with all reports, import-ready CSVs, loading guidance and `README.txt` for running the script.

## Design principles that shape the behaviour

* **Nothing is silently changed.** Values are read as text; invalid values, ambiguous dates and leading zeros are
  reported, never converted away. A column that is *mostly* numeric but contains `Pending` is suggested as `text`
  (safe) with the numeric type offered as an approvable alternative.
* **Ambiguous dates** such as `04/05/2025` are never resolved silently; you must choose DD/MM or MM/DD.
* **Evidence, not guesses.** A foreign key is only a *candidate* built from name evidence + value overlap + parent
  uniqueness; it is pre-approved only when the name matches, 100 % of child values exist and the parent key is unique.
  Candidates with orphan rows are never pre-approved and the exact unmatched values and rows are listed.
* **No invented constraints.** Observed "no NULLs / all unique" does not become NOT NULL / UNIQUE. No `ON DELETE` /
  `ON UPDATE` is emitted unless you choose it. A UNIQUE constraint is added automatically only where a foreign key
  technically needs one on its parent column (the script says so).
* **Honest status.** The script header states whether review is still required; every validation check is recorded
  as passed / failed / skipped.

## What is implemented (and how well it is verified)

| Area | Status |
|---|---|
| CSV, TSV, delimited text, XLSX/XLSM (macros never executed), JSON, JSONL, XML, ZIP | implemented and unit-tested |
| Legacy `.xls` (needs `xlrd`), Parquet (needs `pyarrow`) | implemented; **not tested in the author's offline environment** – the Parquet test runs automatically if `pyarrow` is installed, there is no `.xls` fixture |
| Profiling, type inference, primary keys (single/composite/surrogate), foreign keys, junction tables | implemented and unit-tested |
| Relational mode: extract repeated entity, redundant-attribute and multi-value reports | entity extraction is applied on approval; the others are reported only |
| PostgreSQL SQL | fully implemented; checked structurally – **not executed against a live PostgreSQL server** by the author |
| SQLite SQL | implemented; the tests execute it on a real SQLite engine, including foreign-key enforcement and cycles |
| MySQL, SQL Server SQL | **experimental** – implemented, never executed against those servers |
| Staging workflow (`stg_*` tables, deferred FKs, staging→final `INSERT … SELECT`) | implemented; transform script is PostgreSQL only |
| Streamlit UI | every step is exercised by an automated smoke test with a stand-in `streamlit` module; it has **not been looked at in a real browser** by the author |
| ER diagram | DOT always; SVG/PNG when Graphviz is installed |

## Known limitations

* Foreign keys are detected on single columns; composite foreign keys are not auto-detected (composite primary keys are).
* Header is assumed to be the first non-empty row; merged cells and multi-row headers are not interpreted.
* Excel formulas are read as their last cached values.
* JSON/XML nested lists are kept as text and reported; they are not split into child tables automatically.
* Row numbers in reports are the source row (CSV line / Excel row / JSON record number). Detail lists are capped at
  500 rows per issue (the total count is always shown).
* Limits: 50 MB per file, 1 M rows, 2,000 columns, 50 files per ZIP.
* Natural-language business rules (for example "fee must be ≥ 0") are not inferred; add them as CHECK expressions.

## Project layout

```
app.py                      Streamlit UI (thin)
modules/
  file_reader.py            parsers (pluggable via @register), encoding/delimiter detection, limits
  data_profiler.py          column and dataset profiles
  type_inference.py         type tokens, date logic, conversion checks
  key_detection.py          primary-key candidates
  relationship_detection.py foreign keys, cardinality, junction tables, unmatched-reference report
  normalization.py          relational-mode proposals
  schema_designer.py        schema model + builder + edits
  schema_validator.py       pre-import validation and summary
  dialects.py, identifiers.py   dialect types, quoting, reserved words
  sql_generator.py          deterministic CREATE TABLE script
  import_planner.py         orders, source mapping, loading guidance, staging transform
  erd_generator.py          DOT / SVG / PNG
  reports.py                downloadable files
  workflow.py               glue used by the UI and tests
tests/                      pytest suite
examples/                   synthetic demo data (with deliberate problems)
sample_output/              output for the examples
```

To add a file format: write `def read_xyz(name, data, has_header=True)` returning `(datasets, sheets, warnings)` and
decorate it with `@register("Label", ".xyz")` in `modules/file_reader.py`.
