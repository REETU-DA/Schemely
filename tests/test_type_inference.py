import pandas as pd

from modules.type_inference import convert_check, infer_column


def inf(name, values):
    return infer_column(name, pd.Series(values, dtype=object))


def test_integer_decimal_text():
    assert inf("qty", ["1", "2", "3"]).token == "integer"
    d = inf("fee", ["1000", "2500.50"])
    assert d.token.startswith("numeric(") and d.token.endswith(",2)")
    assert inf("notes", ["hello there", "other words"]).token == "text"


def test_big_integers_use_bigint():
    assert inf("n", ["5000000000", "1"]).token == "bigint"


def test_leading_zeros_force_text():
    r = inf("code", ["00125", "00047", "00300"])
    assert r.token.startswith("varchar") and r.has_leading_zeros


def test_phone_and_postal_are_never_numbers():
    assert inf("phone", ["9876543210", "9876543211"]).kind == "phone"
    assert inf("pin_code", ["302001", "110001"]).kind == "postal"


def test_mixed_numeric_column_is_text_with_alternative_and_exceptions():
    r = inf("fee", [str(i * 100) for i in range(1, 12)] + ["Pending"])
    assert r.token == "text" and r.alternative in ("integer", "bigint") or r.alternative.startswith("numeric") and r.needs_approval
    assert any("Pending" in e for e in r.exceptions)
    assert r.exception_values[0][1] == "Pending"


def test_ambiguous_dates_are_not_silently_resolved():
    r = inf("admitted", ["04/05/2025", "06/07/2025", "01/02/2025"])
    assert r.kind == "date" and r.date_order is None and r.needs_approval and r.date_ambiguous == 3
    assert any(code == "ambiguous" for _, _, code, _ in convert_check(pd.Series(["04/05/2025"], dtype=object), "date", None))


def test_unambiguous_dates_infer_order_from_evidence():
    r = inf("d", ["04/05/2025", "25/07/2025"])
    assert r.date_order == "DMY" and not r.date_ambiguous
    assert convert_check(pd.Series(["04/05/2025", "25/07/2025"], dtype=object), "date", "DMY") == []


def test_mixed_day_month_orders_fall_back_to_text():
    r = inf("d", ["25/07/2025", "07/25/2025"])
    assert r.token == "text" and r.needs_approval


def test_iso_dates_timestamps_and_timezones():
    assert inf("d", ["2025-01-01", "2025-02-01"]).token == "date"
    assert inf("t", ["2025-01-01 10:30:00", "2025-01-02 11:00:00"]).token == "timestamp"
    assert inf("t", ["2025-01-01T10:30:00Z", "2025-01-02T11:00:00+05:30"]).token == "timestamptz"
    assert inf("t", ["10:30", "23:15:10"]).token == "time"


def test_booleans_uuid_and_empty():
    assert inf("paid", ["yes", "no", "yes"]).token == "boolean"
    assert inf("u", ["123e4567-e89b-12d3-a456-426614174000"]).token == "uuid"
    assert inf("x", [None, None]).needs_approval


def test_invalid_calendar_dates_are_flagged():
    bad = convert_check(pd.Series(["2025-02-30"], dtype=object), "date")
    assert bad and bad[0][2] == "type"


def test_varchar_truncation_and_numeric_precision_checks():
    assert convert_check(pd.Series(["abcdef"], dtype=object), "varchar(3)")[0][2] == "truncation"
    assert convert_check(pd.Series(["1.239"], dtype=object), "numeric(5,2)")[0][2] == "precision"
