from helpers import analyse_csvs, issues, table
from modules import workflow as wf


def test_ambiguous_date_is_an_error_until_user_chooses():
    a = analyse_csvs({"t.csv": "ID,Admitted\n1,04/05/2025\n2,06/07/2025\n3,01/02/2025"})
    assert issues(a, "ambiguous_date") and a.summary["Ambiguous date columns"] == 1
    c = table(a, "t").col("Admitted")
    c.date_order = "DMY"
    c.approved = True
    wf.revalidate(a)
    assert not issues(a, "ambiguous_date")


def test_leading_zeros_lost_when_user_forces_numeric():
    a = analyse_csvs({"t.csv": "ID,Code\n1,00125\n2,00047\n3,00300"})
    assert table(a, "t").col("Code").sql_type.startswith("varchar")
    table(a, "t").col("Code").sql_type = "integer"
    wf.revalidate(a)
    assert issues(a, "leading_zeros_lost")


def test_mixed_numeric_column_defaults_to_safe_text_and_warns_when_forced():
    csv = "ID,Fee\n" + "\n".join(f"{i},{i * 100}" for i in range(1, 15)) + "\n15,Pending"
    a = analyse_csvs({"t.csv": csv})
    c = table(a, "t").col("Fee")
    assert c.sql_type == "text" and c.needs_approval and not c.approved
    assert a.summary["Columns requiring review"] == 1
    c.sql_type = "numeric(10,2)"
    wf.revalidate(a)
    bad = issues(a, "type_conversion")
    assert bad and bad[0].details[0]["value"] == "Pending" and bad[0].details[0]["source_row"] == 16


def test_truncation_detected():
    a = analyse_csvs({"t.csv": "ID,Name\n1,Alexander\n2,Bo"})
    table(a, "t").col("Name").sql_type = "varchar(4)"
    wf.revalidate(a)
    assert issues(a, "truncation")


def test_reserved_keywords_are_flagged_and_quoted():
    a = analyse_csvs({"t.csv": "ID,Order,User,Group\n1,a,b,c\n2,d,e,f"})
    assert {i.column for i in issues(a, "reserved_keyword")} >= {"order", "user", "group"}
    sql = wf.generate(a)
    assert '"order"' in sql and '"user"' in sql and '"group"' in sql


def test_naming_conflicts_detected():
    a = analyse_csvs({"t.csv": "ID,A,B\n1,x,y\n2,z,w"})
    t = table(a, "t")
    t.col("B").name = t.col("A").name
    wf.revalidate(a)
    assert issues(a, "name_conflict")


def test_excluded_column_is_reported_as_destructive():
    a = analyse_csvs({"t.csv": "ID,A\n1,x\n2,z"})
    table(a, "t").col("A").include = False
    wf.revalidate(a)
    assert issues(a, "column_excluded")


def test_duplicate_rows_reported():
    a = analyse_csvs({"t.csv": "A,B\n1,x\n1,x\n2,y"})
    assert issues(a, "duplicate_records")


def test_circular_dependency_reported_and_deferred():
    a = analyse_csvs({"a.csv": "A_ID,B_ID\nA1,B1\nA2,B2\nA3,B1\nA4,B2", "b.csv": "B_ID,A_ID\nB1,A1\nB2,A2\nB3,A3\nB4,A4"},
                     accept_all=True)
    assert issues(a, "circular_dependency")


def test_checks_are_all_recorded_and_skips_are_explicit():
    a = analyse_csvs({"t.csv": "ID,A\n1,x\n2,z"})
    names = {c.name: c for c in a.validation.checks}
    assert names["Foreign-key references and parent keys"].status == "skipped"
    assert names["Primary key completeness and uniqueness"].status == "passed"


def test_unsafe_check_expression_rejected():
    a = analyse_csvs({"t.csv": "ID,A\n1,5\n2,6"})
    table(a, "t").col("A").check = "A > 0; DROP TABLE t"
    wf.revalidate(a)
    assert issues(a, "unsafe_check")


def test_normalisation_proposal_and_application():
    from modules.schema_designer import apply_extract_entity
    rows = ["Order_ID,Customer_ID,Customer_Name,Customer_City,Amount"]
    for i in range(1, 13):
        c = (i % 3) + 1
        rows.append(f"O{i},C{c},Name {c},City {c},{i * 10}.50")
    a = analyse_csvs({"orders.csv": "\n".join(rows)}, mode="relational")
    props = [p for p in a.schema.proposals if p.kind == "extract_entity"]
    assert props and props[0].determinant == "Customer_ID" and set(props[0].dependents) == {"Customer_Name", "Customer_City"}
    apply_extract_entity(a.schema, props[0])
    wf.revalidate(a)
    sql = wf.generate(a)
    assert "CREATE TABLE customer" in sql and "FOREIGN KEY (customer_id) REFERENCES customer (customer_id)" in sql
    ent = [t for t in a.schema.tables if t.kind == "entity"][0]
    assert not issues(a, "pk_duplicate") and ent.pk_columns == ["Customer_ID"]
    assert "customer_name" not in sql.split("CREATE TABLE orders")[1].split(");")[0]  # moved, not duplicated
