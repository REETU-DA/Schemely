from helpers import analyse_csvs, table, xlsx_bytes
from modules import workflow as wf
from modules.relationship_detection import unmatched_references

STUD = "Student_ID,Name\nS1,A\nS2,B\nS3,C\nS4,D"
ENR = "Enrollment_ID,Student_ID,Course_ID\nE1,S1,C1\nE2,S2,C1\nE3,S2,C2\nE4,S3,C2"
COURSES = "Course_ID,Title\nC1,Python\nC2,SQL"


def rel(a, child, parent):
    return next((r for r in a.relationships if r.child_dataset.startswith(child) and r.parent_dataset.startswith(parent)), None)


def test_basic_fk_detection_with_evidence():
    a = analyse_csvs({"students.csv": STUD, "enrollments.csv": ENR, "courses.csv": COURSES})
    r = rel(a, "enrollments", "students")
    assert r and r.match_pct == 1.0 and r.unmatched_rows == 0 and r.parent_unique
    assert r.cardinality == "one-to-many" and r.accepted and r.status == "strong"
    assert rel(a, "enrollments", "courses")
    assert any("exist in the parent" in e for e in r.evidence)


def test_no_relationship_from_similar_names_alone():
    a = analyse_csvs({"a.csv": "Item_ID,x\nI1,1\nI2,2\nI3,3\nI4,4", "b.csv": "Order_ID,Item_ID,y\nO1,Z9,1\nO2,Z8,2\nO3,Z7,3\nO4,Z6,4"})
    assert rel(a, "b", "a") is None  # same name but no value overlap -> not a candidate


def test_matching_values_with_different_names_is_candidate_not_auto_accepted():
    a = analyse_csvs({"customers.csv": "Customer_ID,Name\nC1,A\nC2,B\nC3,C\nC4,D",
                      "orders.csv": "Order_ID,CustID\nO1,C1\nO2,C2\nO3,C2\nO4,C3"})
    r = rel(a, "orders", "customers")
    assert r and r.status == "candidate" and not r.accepted


def test_unmatched_references_are_listed_with_locations():
    enr = ENR + "\nE5,S99,C1"
    a = analyse_csvs({"students.csv": STUD, "enrollments.csv": enr, "courses.csv": COURSES})
    r = rel(a, "enrollments", "students")
    assert r.unmatched_rows == 1 and not r.accepted  # never auto-accept with orphans
    df = unmatched_references(r, a.datasets)
    assert list(df["unmatched_value"]) == ["S99"] and list(df["source_row"]) == [6]
    for fk in a.schema.fks:
        fk.accepted = True
    wf.revalidate(a)
    errs = [i for i in a.validation.issues if i.code == "fk_unmatched"]
    assert errs and errs[0].details[0]["value"] == "S99"


def test_excel_worksheets_produce_fk():
    data = xlsx_bytes({"Students": [["id", "n"], [1, "a"], [2, "b"], [3, "c"]],
                       "Marks": [["mark_id", "student_id", "score"], [1, 1, 50.5], [2, 2, 60.5], [3, 2, 70.5]]})
    ds, _ = wf.load([("school.xlsx", data)])
    a = wf.analyse(ds)
    r = rel(a, "school.xlsx::Marks", "school.xlsx::Students")
    assert r and r.name_score >= 0.9


def test_many_to_many_junction_detected():
    a = analyse_csvs({"students.csv": STUD, "courses.csv": COURSES,
                      "enrollments.csv": "Student_ID,Course_ID\nS1,C1\nS1,C2\nS2,C1\nS3,C2"})
    for fk in a.schema.fks:
        if fk.child_table.startswith("enrollments"):
            fk.accepted = True
    j = wf.junctions(a)
    assert j and set(j[0].fk_columns) == {"Student_ID", "Course_ID"} and j[0].composite_unique


def test_one_to_one_cardinality():
    a = analyse_csvs({"users.csv": "User_ID,Name\nU1,A\nU2,B\nU3,C", "profiles.csv": "Profile_ID,User_ID\nP1,U1\nP2,U2\nP3,U3"})
    r = rel(a, "profiles", "users")
    assert r and r.cardinality == "one-to-one"


def test_ids_in_both_tables_named_id_are_not_related():
    a = analyse_csvs({"a.csv": "id,x\n1,a\n2,b\n3,c\n4,d", "b.csv": "id,y\n1,a\n2,b\n3,c\n4,d"})
    assert a.relationships == []
