from helpers import analyse_csvs, issues, table


def test_valid_pk_is_strong_and_needs_no_approval():
    a = analyse_csvs({"students.csv": "Student_ID,Name\nST001,A\nST002,B\nST003,C"})
    ka = a.key_analyses["students.csv"]
    assert ka.mode == "existing" and ka.columns == ("Student_ID",) and not ka.needs_approval


def test_duplicate_key_is_reported_and_not_nominated():
    a = analyse_csvs({"t.csv": "Student_ID,Name\nST001,A\nST001,B\nST003,C\nST004,D"})
    ka = a.key_analyses["t.csv"]
    assert ka.mode != "existing" and any("duplicate" in b for b in ka.blockers)
    assert any(c.verdict == "invalid" for c in ka.candidates)
    assert len(ka.alternatives) == 3


def test_null_key_candidate_is_rejected():
    a = analyse_csvs({"t.csv": "Student_ID,Name\nST001,A\n,B\nST003,C\nST004,D"})
    ka = a.key_analyses["t.csv"]
    assert ka.mode != "existing" and any("NULL" in b for b in ka.blockers)


def test_forced_duplicate_pk_surfaces_in_validation():
    from modules import workflow as wf
    from modules.schema_designer import set_pk
    a = analyse_csvs({"t.csv": "Student_ID,Name\nST001,A\nST001,B\nST003,C\nST004,D"})
    set_pk(table(a, "t"), ["Student_ID"])
    wf.revalidate(a)
    dup = issues(a, "pk_duplicate")
    assert dup and dup[0].details[0]["source_row"] in (2, 3) and dup[0].details[0]["value"] == "ST001"


def test_composite_key_found_when_no_single_key():
    a = analyse_csvs({"t.csv": "Order_No,Line_No,Qty\nO1,1,5\nO1,2,5\nO2,1,5\nO2,2,5"})
    ka = a.key_analyses["t.csv"]
    assert ka.mode == "composite" and set(ka.columns) == {"Order_No", "Line_No"} and ka.needs_approval


def test_surrogate_key_preserves_original_columns():
    a = analyse_csvs({"t.csv": "Name,City\nA,X\nA,X\nB,Y\nB,Y"})
    t = table(a, "t")
    assert t.pk_mode == "surrogate" and t.columns[0].generated
    assert [c.source_column for c in t.columns[1:]] == ["Name", "City"]
