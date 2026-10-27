import csv
import io
import json
import zipfile

from helpers import analyse_csvs
from modules import workflow as wf

DATA = {
    "students.csv": "Student_ID,Name\nS1,A\nS2,B\nS3,C",
    "enrollments.csv": "Enrollment_ID,Student_ID\nE1,S1\nE2,S9\nE3,S2",
}


def test_bundle_contains_all_required_outputs():
    a = analyse_csvs(DATA)
    files, failures, z = wf.outputs(a)
    for name in ["schema.sql", "schema_report.json", "schema_report.csv", "relationship_report.csv", "validation_report.csv",
                 "source_mapping.csv", "README.txt"]:
        assert name in files and len(files[name]) > 0
    names = zipfile.ZipFile(io.BytesIO(z)).namelist()
    assert "schema.sql" in names
    json.loads(files["schema_report.json"])


def test_failed_report_is_listed_not_faked(monkeypatch=None):
    from modules import reports
    a = analyse_csvs(DATA)
    wf.generate(a)
    orig = reports.render
    reports.render = lambda dot, fmt="svg": (_ for _ in ()).throw(reports.ERDUnavailable("no graphviz"))
    try:
        files, failures = reports.build_bundle(a.schema, a.datasets, a.validation, a.summary, a.sql)
    finally:
        reports.render = orig
    assert "erd.svg" in failures and "erd.svg" not in files and "REPORT_ERRORS.txt" in files


def test_validation_report_lists_rows_and_checks():
    a = analyse_csvs(DATA, accept_all=True)
    files, _, _ = wf.outputs(a)
    rows = list(csv.DictReader(io.StringIO(files["validation_report.csv"].decode("utf-8-sig"))))
    unmatched = [r for r in rows if r["code"] == "fk_unmatched"]
    assert unmatched and unmatched[0]["value"] == "S9" and unmatched[0]["source_row"] == "3"
    assert any(r["severity"] == "check" for r in rows)


def test_source_mapping_traces_every_column():
    a = analyse_csvs(DATA)
    files, _, _ = wf.outputs(a)
    rows = list(csv.DictReader(io.StringIO(files["source_mapping.csv"].decode("utf-8-sig"))))
    assert {r["source_column"] for r in rows} == {"Student_ID", "Name", "Enrollment_ID"}
    assert all(r["target_table"] for r in rows)


def test_summary_counts_come_from_the_data():
    a = analyse_csvs(DATA, accept_all=True)
    s = a.summary
    assert s["Datasets analysed"] == 2 and s["Proposed tables"] == 2 and s["Foreign key candidates"] == 1
    assert s["Unresolved key issues"] == 1 and s["Status"] == "Review required"


def test_data_never_modified_by_analysis():
    pairs = {n: t for n, t in DATA.items()}
    a = analyse_csvs(pairs)
    before = {k: d.df.copy() for k, d in a.datasets.items()}
    wf.outputs(a)
    for k, d in a.datasets.items():
        assert d.df.equals(before[k])


def test_import_guidance_explains_copy_vs_copy():
    a = analyse_csvs(DATA)
    files, _, _ = wf.outputs(a)
    g = files["output_c_loading_guidance.txt"].decode()
    assert "\\copy" in g and "Server-side COPY" in g
