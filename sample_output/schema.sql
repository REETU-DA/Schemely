-- ============================================================================
-- SmartSQL Schema Designer - schema script
-- Target dialect : PostgreSQL
-- Design mode    : Direct import schema
-- Review status  : REVIEW REQUIRED (0 validation error(s), 1 column decision(s) awaiting approval)
-- WARNING        : unresolved issues remain - read validation_report.csv before importing data.
-- Constraints are limited to what was approved; observed data patterns are NOT turned into
-- NOT NULL / UNIQUE constraints automatically. No ON DELETE / ON UPDATE behaviour is assumed.
-- ============================================================================

-- Assumptions and open items:
--   * students.admission_date: dates read as DMY (day/month order)
--   * payments.amount: stored as text; better type numeric(10,2) needs clean data first

BEGIN;

-- Table: courses   (source: courses.csv)
CREATE TABLE courses (
    course_id VARCHAR(20) NOT NULL,
    course_name TEXT,
    fee NUMERIC(11,2),
    CONSTRAINT pk_courses PRIMARY KEY (course_id)
);

-- Table: enrollments   (source: enrollments.csv)
CREATE TABLE enrollments (
    enrollment_id VARCHAR(20) NOT NULL,
    student_id VARCHAR(20),
    course_id VARCHAR(20),
    is_active BOOLEAN,
    CONSTRAINT pk_enrollments PRIMARY KEY (enrollment_id)
);

-- Table: students   (source: students.csv)
CREATE TABLE students (
    student_id VARCHAR(20) NOT NULL,
    student_name TEXT,
    phone VARCHAR(20),
    admission_date DATE,
    city TEXT,
    postal_code VARCHAR(20),
    CONSTRAINT pk_students PRIMARY KEY (student_id)
);

-- Table: batches   (source: payments_and_batches.xlsx / Batches)
CREATE TABLE batches (
    batch_id VARCHAR(20) NOT NULL,
    course_id VARCHAR(20),
    start_date DATE,
    CONSTRAINT pk_batches PRIMARY KEY (batch_id),
    CONSTRAINT fk_batches_courses_course_id FOREIGN KEY (course_id) REFERENCES courses (course_id)
);

-- Table: payments   (source: payments_and_batches.xlsx / Payments)
CREATE TABLE payments (
    payment_id INTEGER NOT NULL,
    enrollment_id VARCHAR(20),
    -- review: 60 of 61 values are numeric but 1 value(s) are not ('Pending'). Text is the safe default b...
    amount TEXT,
    paid_on DATE,
    mode TEXT,
    CONSTRAINT pk_payments PRIMARY KEY (payment_id),
    CONSTRAINT fk_payments_enrollments_enrollment_id FOREIGN KEY (enrollment_id) REFERENCES enrollments (enrollment_id)
);

COMMIT;
