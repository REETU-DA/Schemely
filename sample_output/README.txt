SmartSQL Schema Designer 1.0 - results
============================================================

Target dialect : PostgreSQL
Design mode    : direct
Staging        : no
Status         : Review required (0 error(s), 1 warning(s), 1 column(s) awaiting approval)

FILES
-----
schema.sql                       the CREATE TABLE script (Output A) - run this first
schema_report.json / .csv        everything the tool decided, per table and column
relationship_report.csv          detected foreign keys with evidence, and your decisions
validation_report.csv            every check run and every issue, with source file/row
source_mapping.csv               source file/column -> target table/column
import_preparation.txt           loading order, transformations, open problems (Output B)
output_c_loading_guidance.txt    COPY / \copy / import examples (Output C)
import_ready_csv/                one CSV per table in target column order (values untouched)
erd.svg / erd.png / erd.dot      relationship diagram (if Graphviz was available)

HOW TO RUN schema.sql
---------------------
Create (or choose) an empty database, then run:
    psql -h <host> -U <user> -d <database> -f schema.sql
Add  -v ON_ERROR_STOP=1  to stop at the first error. schema.sql is wrapped in a transaction, so a failure
leaves no partial tables behind.
Load data with the \copy commands in output_c_loading_guidance.txt (client-side) or server-side COPY.

IMPORTANT
---------
* The tool analysed your files locally and never connected to a database or sent data anywhere.
* Constraints reflect approved decisions, not just patterns seen in the data; check the 'review' comments in schema.sql.
* If validation_report.csv lists errors, constraints (primary/foreign keys, type casts) will fail on import until fixed.
* No ON DELETE / ON UPDATE cascade behaviour is included unless you chose it.
