import sqlite3

import pytest
from helpers import analyse_csvs, table
from modules import workflow as wf

def code(sql):
    return "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))


DATA = {
    "students.csv": "Student_ID,Student_Name,Admission_Date\nS1,A,2024-01-05\nS2,B,2024-02-05\nS3,C,2024-03-05",
    "courses.csv": "Course_ID,Course_Name,Fee\nC1,Python,1000.50\nC2,SQL,900.00",
    "enrollments.csv": "Enrollment_ID,Student_ID,Course_ID\nE1,S1,C1\nE2,S2,C1\nE3,S2,C2\nE4,S3,C2",
}


def test_postgresql_script_content_and_order():
    a = analyse_csvs(DATA)
    sql = wf.generate(a)
    assert sql.index("CREATE TABLE students") < sql.index("CREATE TABLE enrollments")
    assert sql.index("CREATE TABLE courses") < sql.index("CREATE TABLE enrollments")
    assert "student_id VARCHAR(20) NOT NULL" in sql and "admission_date DATE" in sql and "fee NUMERIC(" in sql
    assert "CONSTRAINT pk_students PRIMARY KEY (student_id)" in sql
    assert "FOREIGN KEY (student_id) REFERENCES students (student_id)" in sql
    assert sql.startswith("-- ====") and "BEGIN;" in sql and sql.rstrip().endswith("COMMIT;")
    assert "ON DELETE" not in code(sql) and "CASCADE" not in code(sql)
    assert "Add your columns" not in sql


def test_generation_is_deterministic():
    assert wf.generate(analyse_csvs(DATA)) == wf.generate(analyse_csvs(DATA))


def test_observed_patterns_do_not_become_constraints():
    sql = wf.generate(analyse_csvs(DATA))
    assert "student_name TEXT" in sql and "student_name TEXT NOT NULL" not in sql and "UNIQUE" not in code(sql)


def test_fk_to_non_key_parent_gets_required_unique_constraint():
    a = analyse_csvs({"p.csv": "Pid,Code\n1,A\n2,B\n3,C", "c.csv": "Cid,Code\n1,A\n2,B\n3,A"}, accept_all=True)
    from modules.schema_designer import set_pk
    set_pk(table(a, "p"), ["Pid"])
    for fk in a.schema.fks:
        fk.accepted = fk.parent_table == "p.csv" and fk.child_cols == ["Code"]
    sql = wf.generate(a)
    assert "UNIQUE (code)" in sql and "REFERENCES p (code)" in sql


def test_circular_dependency_uses_alter_table():
    a = analyse_csvs({"a.csv": "A_ID,B_ID\nA1,B1\nA2,B2\nA3,B1\nA4,B2", "b.csv": "B_ID,A_ID\nB1,A1\nB2,A2\nB3,A3\nB4,A4"},
                     accept_all=True)
    sql = wf.generate(a)
    assert "ALTER TABLE" in sql and "ADD CONSTRAINT" in sql


def test_surrogate_identity_per_dialect():
    csv = {"t.csv": "Name,City\nA,X\nA,X\nB,Y\nB,Y"}
    assert "GENERATED ALWAYS AS IDENTITY" in wf.generate(analyse_csvs(csv))
    assert "AUTOINCREMENT" in wf.generate(analyse_csvs(csv, dialect="sqlite"))
    assert "AUTO_INCREMENT" in wf.generate(analyse_csvs(csv, dialect="mysql"))
    assert "IDENTITY(1,1)" in wf.generate(analyse_csvs(csv, dialect="sqlserver"))


def test_dialect_specific_types_and_quoting():
    csv = {"t.csv": "ID,Order,Created\n1,a,2025-01-01 10:00:00\n2,b,2025-01-02 11:00:00"}
    pg, my, ms = (wf.generate(analyse_csvs(csv, dialect=d)) for d in ("postgresql", "mysql", "sqlserver"))
    assert '"order"' in pg and "`order`" in my and "[order]" in ms
    assert "TIMESTAMP" in pg and "DATETIME(6)" in my and "DATETIME2" in ms
    assert "ENGINE=InnoDB" in my and "BEGIN TRANSACTION" in ms


def test_mysql_text_keys_become_bounded():
    a = analyse_csvs({"t.csv": "Code,Notes\nAB,hello there\nCD,other words"}, dialect="mysql")
    table(a, "t").col("Code").sql_type = "text"
    assert "code VARCHAR(255) NOT NULL" in wf.generate(a)


def test_explicit_cascade_only_when_user_sets_it():
    a = analyse_csvs(DATA)
    fk = next(f for f in a.schema.fks if f.accepted)
    fk.on_delete = "cascade"
    assert "ON DELETE CASCADE" in wf.generate(a)
    fk.on_delete = "DROP EVERYTHING"
    assert "DROP EVERYTHING" not in code(wf.generate(a))


def test_sqlite_script_executes_and_enforces_fks():
    a = analyse_csvs(DATA, dialect="sqlite")
    sql = wf.generate(a)
    con = sqlite3.connect(":memory:")
    con.executescript(sql)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"students", "courses", "enrollments"} <= tables
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("insert into students values ('S1','A','2024-01-01')")
    con.execute("insert into courses values ('C1','x',1)")
    con.execute("insert into enrollments values ('E1','S1','C1')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("insert into enrollments values ('E2','NOPE','C1')")


def test_sqlite_cycle_script_executes():
    a = analyse_csvs({"a.csv": "A_ID,B_ID\nA1,B1\nA2,B2\nA3,B1\nA4,B2", "b.csv": "B_ID,A_ID\nB1,A1\nB2,A2\nB3,A3\nB4,A4"},
                     dialect="sqlite", accept_all=True)
    sqlite3.connect(":memory:").executescript(wf.generate(a))


def test_example_data_loads_into_sqlite_with_import_ready_csvs():
    import csv, io
    a = analyse_csvs(DATA, dialect="sqlite")
    con = sqlite3.connect(":memory:")
    con.executescript(wf.generate(a))
    from modules.import_planner import import_ready_csvs
    for name, data in import_ready_csvs(a.schema, a.datasets).items():
        rows = list(csv.reader(io.StringIO(data.decode())))
        t = name[:-4]
        con.executemany(f"insert into {t} values ({','.join('?' * len(rows[0]))})", [[v or None for v in r] for r in rows[1:]])
    assert con.execute("select count(*) from enrollments").fetchone()[0] == 4


def test_postgres_script_is_structurally_sound():
    sql = wf.generate(analyse_csvs(DATA))
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert body.count("(") == body.count(")")
    stmts = [s for s in body.split(";") if s.strip()]
    assert all(s.strip().split()[0] in ("BEGIN", "CREATE", "ALTER", "COMMIT") for s in stmts)


def test_staging_mode_defers_foreign_keys_and_builds_transform():
    from modules.import_planner import transform_sql
    a = analyse_csvs(DATA, staging=True)
    sql = wf.generate(a)
    assert "CREATE TABLE stg_students" in sql and "PART 3" in sql
    assert sql.index("PART 3") < sql.index("ALTER TABLE enrollments")
    t = transform_sql(a.schema)
    assert "INSERT INTO students" in t and "::DATE" in t and "FROM stg_students" in t


def test_unresolved_issues_are_declared_in_script_header():
    a = analyse_csvs({"t.csv": "ID,Admitted\n1,04/05/2025\n2,06/07/2025\n3,01/02/2025"})
    sql = wf.generate(a)
    assert "REVIEW REQUIRED" in sql
