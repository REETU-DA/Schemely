import io
import json
import zipfile

from helpers import csv_bytes, xlsx_bytes
from modules.file_reader import read_file, read_files


def test_simple_csv_with_valid_pk():
    ds, rep = read_file("people.csv", csv_bytes("id,name\n1,A\n2,B\n3,C"))
    assert rep.status == "ok" and rep.rows == 3 and rep.cols == 2
    assert list(ds[0].df.columns) == ["id", "name"]
    assert ds[0].source_row(ds[0].df.index[0]) == 2  # header is row 1


def test_values_stay_text_and_leading_zeros_survive():
    ds, _ = read_file("a.csv", csv_bytes("code,n\n00125,1\n00047,2"))
    assert list(ds[0].df["code"]) == ["00125", "00047"]


def test_multiple_excel_worksheets_and_empty_sheet():
    data = xlsx_bytes({"Students": [["id", "name"], [1, "A"], [2, "B"]], "Empty": [], "Courses": [["cid", "t"], ["C1", "x"]]})
    ds, rep = read_file("book.xlsx", data)
    assert [d.sheet for d in ds] == ["Students", "Courses"]
    assert [s.status for s in rep.sheets] == ["loaded", "empty", "loaded"]
    assert any("empty" in w for w in rep.warnings)


def test_empty_workbook_is_an_error_not_a_crash():
    ds, rep = read_file("e.xlsx", xlsx_bytes({"S": []}))
    assert ds == [] and rep.status == "error"


def test_duplicate_and_missing_headers_are_renamed_with_warning():
    ds, rep = read_file("d.csv", csv_bytes("a,a,\n1,2,3"))
    assert list(ds[0].df.columns) == ["a", "a_dup2", "column_3"]
    assert any("Duplicate column" in w for w in rep.warnings) and any("no header" in w for w in rep.warnings)


def test_semicolon_and_tab_delimiters_detected():
    ds, _ = read_file("s.csv", csv_bytes("a;b\n1;2\n3;4"))
    assert ds[0].n_cols == 2
    ds, _ = read_file("t.tsv", csv_bytes("a\tb\n1\t2"))
    assert ds[0].n_cols == 2


def test_latin1_encoding_warns():
    ds, rep = read_file("l.csv", "name,city\nJosé,Zürich\nRenée,Köln".encode("cp1252"))
    assert ds[0].df["name"].iloc[0] == "José" and any("not UTF-8" in w for w in rep.warnings)


def test_ragged_rows_are_kept_and_warned():
    ds, rep = read_file("r.csv", csv_bytes("a,b\n1\n2,3,4"))
    assert ds[0].n_rows == 2 and ds[0].n_cols == 3
    assert any("fewer cells" in w for w in rep.warnings) and any("more cells" in w for w in rep.warnings)


def test_unsupported_empty_and_corrupt_inputs_give_messages():
    assert read_file("x.docx", b"abc")[1].status == "error"
    assert read_file("x.csv", b"")[1].status == "error"
    ds, rep = read_file("x.xlsx", b"not a zip at all")
    assert ds == [] and "corrupt" in rep.errors[0].lower()


def test_json_jsonl_and_bad_json():
    ds, _ = read_file("j.json", json.dumps([{"id": 1, "a": {"b": "x"}}, {"id": 2, "a": {"b": "y"}}]).encode())
    assert "a_b" in ds[0].df.columns
    ds, rep = read_file("j.jsonl", b'{"id":1}\nnot json\n{"id":3}')
    assert ds[0].n_rows == 2 and any("NOT loaded" in w for w in rep.warnings)
    assert read_file("bad.json", b"{oops")[1].status == "error"


def test_json_multiple_collections():
    ds, _ = read_file("m.json", json.dumps({"users": [{"id": 1}], "orders": [{"oid": 1, "uid": 1}]}).encode())
    assert sorted(d.sheet for d in ds) == ["orders", "users"]


def test_xml_records_and_dtd_refusal():
    xml = b"<r><p id='1'><n>A</n></p><p id='2'><n>B</n></p></r>"
    ds, _ = read_file("x.xml", xml)
    assert ds[0].n_rows == 2 and "n" in ds[0].df.columns
    bad = b'<!DOCTYPE x [<!ENTITY a "b">]><r><p>1</p></r>'
    assert read_file("evil.xml", bad)[1].status == "error"


def test_zip_with_supported_and_unsupported_members():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.csv", "id\n1\n2")
        z.writestr("notes.pdf", "x")
    ds, rep = read_file("bundle.zip", buf.getvalue())
    assert len(ds) == 1 and any("unsupported" in w for w in rep.warnings)


def test_no_header_option_keeps_first_row_as_data():
    ds, _ = read_file("n.csv", csv_bytes("1,2\n3,4"), has_header=False)
    assert ds[0].n_rows == 2 and ds[0].source_row(ds[0].df.index[0]) == 1


def test_same_name_uploaded_twice_gets_unique_ids():
    ds, _ = read_files([("a.csv", b"id\n1"), ("a.csv", b"id\n2")])
    assert len({d.id for d in ds}) == 2


def test_parquet_roundtrip_if_pyarrow_available():
    import pytest
    pytest.importorskip("pyarrow")
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({"id": [1, 2], "n": ["a", None]}).to_parquet(buf)
    ds, rep = read_file("p.parquet", buf.getvalue())
    assert rep.status != "error" and ds[0].n_rows == 2
