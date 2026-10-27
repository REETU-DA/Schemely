"""Parse uploaded files into string-typed Datasets.

Design rules
------------
* Every cell is kept as text (or None). Type conversion is a *later, reviewable* decision.
* Nothing is silently dropped: ragged rows, bad JSON lines, duplicate/missing headers and
  blank rows all produce warnings.
* Uploaded files are untrusted: macros are never executed, XML with DTD/entities is refused,
  ZIP members are read in memory only (no extraction to disk), sizes are capped.
* New formats plug in with ``@register(label, ".ext", ...)``.
"""
from __future__ import annotations

import codecs
import csv
import datetime as dt
import decimal
import io
import json
import re
import statistics
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

import pandas as pd

from .models import Dataset, FileReport, SheetInfo

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_COLS = 2_000
MAX_ZIP_MEMBERS = 50
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
EXCEL_EXTS = (".xlsx", ".xlsm")

# ext -> (format label, reader function, tested_in_this_build)
_READERS: dict[str, tuple[str, callable]] = {}
# honest capability matrix shown in the UI / README
SUPPORT_MATRIX: dict[str, str] = {}


class ReadError(Exception):
    """A problem the user can act on (shown without traceback)."""


def register(label: str, *exts: str, status: str = "implemented and tested"):
    def deco(fn):
        for e in exts:
            _READERS[e] = (label, fn)
            SUPPORT_MATRIX[e] = status
        return fn
    return deco


def supported_extensions() -> list[str]:
    return sorted(_READERS)


# ----------------------------------------------------------------------------- helpers
def to_text(v):
    """Convert any parsed cell to text without losing information."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (bytes, bytearray)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, float):
        if v != v:
            return None
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return format(v, ".15g")
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            return v.isoformat(sep=" ")
        if v.hour == v.minute == v.second == v.microsecond == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S") + (f".{v.microsecond:06d}" if v.microsecond else "")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.isoformat()
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v)
    return s if s != "" else None


def _build_columns(header: list, warnings: list[str]):
    cols, originals, seen = [], [], set()
    for i, h in enumerate(header, 1):
        orig = "" if h is None else str(h).strip()
        name = orig or f"column_{i}"
        if not orig:
            warnings.append(f"Column {i} has no header; it was named '{name}'.")
        cand, n = name, 1
        while cand.casefold() in seen:
            n += 1
            cand = f"{name}_dup{n}"
        if cand != name:
            warnings.append(f"Duplicate column name '{name}' - the repeat was renamed '{cand}'.")
        seen.add(cand.casefold())
        cols.append(cand)
        originals.append(orig)
    return cols, originals


_NUMLIKE = re.compile(r"^[+-]?\d+([.,]\d+)?$")


def _header_looks_like_data(header: list) -> bool:
    cells = [str(h).strip() for h in header if h is not None and str(h).strip()]
    return len(cells) >= 2 and all(_NUMLIKE.match(c) for c in cells)


def _make_dataset(file_name: str, sheet: str | None, header: list, records: list, has_header: bool,
                  header_row: int, warnings: list[str], ds_id: str) -> Dataset:
    """records: list of (source_row_number, list_of_cells)."""
    width = max([len(header)] + [len(c) for _, c in records]) if records or header else 0
    if width > MAX_COLS:
        raise ReadError(f"{width} columns exceed the limit of {MAX_COLS}.")
    if len(records) > MAX_ROWS:
        raise ReadError(f"{len(records):,} rows exceed the limit of {MAX_ROWS:,}. Split the file or sample it.")
    if has_header:
        if _header_looks_like_data(header):
            warnings.append("The header row looks like data (all numeric). If the file has no header row, "
                            "untick 'First row is a header' and re-upload.")
        header = list(header) + [None] * (width - len(header))
    else:
        header = [None] * width
    cols, originals = _build_columns(header, warnings)
    # ragged rows
    short = [n for n, c in records if len(c) < width]
    if short:
        warnings.append(f"{len(short)} row(s) had fewer cells than the header; missing cells were treated as NULL "
                        f"(source rows: {', '.join(map(str, short[:10]))}{'…' if len(short) > 10 else ''}).")
    rows = [list(c) + [None] * (width - len(c)) for _, c in records]
    index = [n - header_row - 1 for n, _ in records]
    df = pd.DataFrame(rows, columns=cols, index=index, dtype=object) if rows else pd.DataFrame(columns=cols, dtype=object)
    return Dataset(id=ds_id, source_file=file_name, sheet=sheet, df=df, original_columns=originals,
                   warnings=list(warnings), header_row=header_row)


# ----------------------------------------------------------------------------- text / csv
def decode_bytes(data: bytes, warnings: list[str]) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        import chardet  # optional

        guess = chardet.detect(data[:200_000])
        enc, conf = guess.get("encoding"), guess.get("confidence") or 0
        if enc and conf >= 0.6:
            try:
                text = data.decode(enc)
                warnings.append(f"File is not UTF-8; decoded as {enc} (confidence {conf:.0%}). Check accented characters.")
                return text
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass
    try:
        text = data.decode("cp1252")
        warnings.append("File is not UTF-8; decoded as Windows-1252. Check accented characters.")
        return text
    except UnicodeDecodeError:
        warnings.append("File encoding could not be determined; decoded as Latin-1. Some characters may be wrong.")
        return data.decode("latin-1")


def sniff_delimiter(text: str, forced: str | None = None) -> str | None:
    if forced:
        return forced
    sample = text[:50_000]
    lines = [ln for ln in sample.splitlines() if ln.strip()][:30]
    if not lines:
        return None
    try:
        d = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|").delimiter
    except csv.Error:
        d = None
    best = None
    for cand in [d] + [c for c in (",", ";", "\t", "|") if c != d]:
        if not cand:
            continue
        counts = [ln.count(cand) for ln in lines]
        common, freq = Counter(counts).most_common(1)[0]
        if common >= 1 and freq / len(counts) >= 0.8:
            best = cand
            break
    return best


def _read_delimited(name: str, data: bytes, has_header: bool, forced: str | None, require_delim: bool):
    warnings: list[str] = []
    text = decode_bytes(data, warnings)
    delim = sniff_delimiter(text, forced)
    if delim is None:
        if require_delim:
            raise ReadError("No delimiter (comma, semicolon, tab or pipe) could be detected consistently in this text file.")
        delim = ","
        warnings.append("No delimiter detected; treated as a single-column CSV.")
    csv.field_size_limit(20 * 1024 * 1024)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    records, header, header_row, blank, longer = [], None, 0, 0, []
    try:
        for cells in reader:
            if not cells:
                blank += 1
                continue
            vals = [c if c != "" else None for c in cells]
            if header is None and has_header:
                header, header_row = vals, reader.line_num
                continue
            if header is None:
                header, header_row = [], 0
            if len(records) >= MAX_ROWS + 1:
                break
            if len(vals) > max(len(header), 1) and header:
                longer.append(reader.line_num)
            records.append((reader.line_num, vals))
    except csv.Error as e:
        raise ReadError(f"Malformed delimited text near line {reader.line_num}: {e}")
    if header is None:
        raise ReadError("The file contains no rows.")
    if blank:
        warnings.append(f"{blank} blank line(s) were skipped (they contain no data).")
    if longer:
        warnings.append(f"{len(longer)} row(s) had more cells than the header; extra cells were kept in generated "
                        f"'column_N' columns (source rows: {', '.join(map(str, longer[:10]))}{'…' if len(longer) > 10 else ''}).")
    if not has_header:
        header_row = records[0][0] - 1 if records else 0
    ds = _make_dataset(name, None, header, records, has_header, header_row, warnings, name)
    rep_note = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}.get(delim, repr(delim))
    return [ds], [SheetInfo(name, ds.n_rows, ds.n_cols, "loaded", f"delimiter: {rep_note}")], warnings


@register("CSV", ".csv")
def read_csv(name, data, has_header=True):
    return _read_delimited(name, data, has_header, None, False)


@register("TSV", ".tsv", ".tab")
def read_tsv(name, data, has_header=True):
    return _read_delimited(name, data, has_header, "\t", False)


@register("Delimited text", ".txt", ".dat")
def read_txt(name, data, has_header=True):
    return _read_delimited(name, data, has_header, None, True)


# ----------------------------------------------------------------------------- excel
@register("Excel workbook", ".xlsx", ".xlsm")
def read_xlsx(name, data, has_header=True):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_vba=False)
    except zipfile.BadZipFile:
        raise ReadError("The workbook is corrupted or is not a real .xlsx/.xlsm file.")
    except ImportError:
        raise ReadError("openpyxl is not installed (pip install openpyxl).")
    except Exception as e:  # openpyxl raises many types for damaged files
        raise ReadError(f"The workbook could not be opened ({type(e).__name__}). It may be corrupted or password-protected.")
    datasets, sheets, file_warnings = [], [], []
    multi = len(wb.worksheets) > 1
    for ws in wb.worksheets:
        try:
            raw = [(i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)]
        except Exception as e:
            sheets.append(SheetInfo(ws.title, 0, 0, "error", f"unreadable: {type(e).__name__}"))
            file_warnings.append(f"Worksheet '{ws.title}' could not be read and was skipped.")
            continue
        rows = [(i, [to_text(c) for c in r]) for i, r in raw]
        nonempty = [(i, r) for i, r in rows if any(c is not None for c in r)]
        if not nonempty:
            sheets.append(SheetInfo(ws.title, 0, 0, "empty", "worksheet has no data"))
            file_warnings.append(f"Worksheet '{ws.title}' is empty and was skipped.")
            continue
        width = max(max(j for j, c in enumerate(r) if c is not None) + 1 for _, r in nonempty)
        nonempty = [(i, r[:width]) for i, r in nonempty]
        warnings = []
        if ws.sheet_state != "visible":
            warnings.append(f"Worksheet '{ws.title}' is {ws.sheet_state} in the workbook.")
        skipped = len(rows) - len(nonempty)
        ds_id = f"{name}::{ws.title}"
        if has_header:
            header_row, header = nonempty[0]
            body = nonempty[1:]
            interior_blank = (body[-1][0] - header_row) - len(body) if body else 0
            if interior_blank > 0:
                warnings.append(f"{interior_blank} blank row(s) inside the data were skipped (row numbers still match the sheet).")
        else:
            header, header_row = [], nonempty[0][0] - 1
            body = nonempty
        ds = _make_dataset(name, ws.title, header, body, has_header, header_row, warnings, ds_id)
        datasets.append(ds)
        sheets.append(SheetInfo(ws.title, ds.n_rows, ds.n_cols, "loaded"))
    if not datasets:
        raise ReadError("The workbook contains no worksheets with data.")
    return datasets, sheets, file_warnings


@register("Legacy Excel", ".xls", status="implemented, NOT tested in the build environment (xlrd unavailable offline)")
def read_xls(name, data, has_header=True):
    try:
        import xlrd
    except ImportError:
        raise ReadError("Reading .xls needs the 'xlrd' package (pip install xlrd). Alternatively save the file as .xlsx.")
    try:
        wb = xlrd.open_workbook(file_contents=data)
    except Exception as e:
        raise ReadError(f"The legacy Excel file could not be opened ({type(e).__name__}); it may be corrupted or protected.")
    datasets, sheets, file_warnings = [], [], []
    for sh in wb.sheets():
        rows = []
        for r in range(sh.nrows):
            cells = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        cells.append(to_text(xlrd.xldate.xldate_as_datetime(cell.value, wb.datemode)))
                    except Exception:
                        cells.append(to_text(cell.value))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    cells.append(None)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    cells.append("true" if cell.value else "false")
                else:
                    cells.append(to_text(cell.value))
            rows.append((r + 1, cells))
        nonempty = [(i, r) for i, r in rows if any(c is not None for c in r)]
        if not nonempty:
            sheets.append(SheetInfo(sh.name, 0, 0, "empty", "worksheet has no data"))
            file_warnings.append(f"Worksheet '{sh.name}' is empty and was skipped.")
            continue
        warnings: list[str] = []
        if has_header:
            header_row, header = nonempty[0]
            body = nonempty[1:]
        else:
            header, header_row, body = [], nonempty[0][0] - 1, nonempty
        ds = _make_dataset(name, sh.name, header, body, has_header, header_row, warnings, f"{name}::{sh.name}")
        datasets.append(ds)
        sheets.append(SheetInfo(sh.name, ds.n_rows, ds.n_cols, "loaded"))
    if not datasets:
        raise ReadError("The workbook contains no worksheets with data.")
    return datasets, sheets, file_warnings


# ----------------------------------------------------------------------------- json
def _flatten(obj, prefix="", out=None, nested=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}_{k}" if prefix else str(k)
            if isinstance(v, dict):
                _flatten(v, key, out, nested)
            else:
                if isinstance(v, list) and nested is not None and any(isinstance(x, (dict, list)) for x in v):
                    nested.add(key)
                out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


def _records_to_dataset(name, sheet, records: list[dict], warnings: list[str], line_numbers=None):
    nested: set[str] = set()
    flat = [_flatten(r, nested=nested) for r in records]
    if nested:
        warnings.append("Nested lists inside records were kept as JSON text in columns: "
                        + ", ".join(sorted(nested)) + ". Consider splitting them into child tables (Relational mode).")
    cols: list[str] = []
    seen = set()
    for r in flat:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    recs = []
    for i, r in enumerate(flat):
        n = line_numbers[i] if line_numbers else i + 1
        recs.append((n, [to_text(r.get(c)) for c in cols]))
    ds_id = f"{name}::{sheet}" if sheet else name
    return _make_dataset(name, sheet, cols, recs, True, 0, warnings, ds_id)


@register("JSON", ".json")
def read_json(name, data, has_header=True):
    warnings: list[str] = []
    text = decode_bytes(data, warnings)
    try:
        obj = json.loads(text, parse_float=str, parse_int=str)
    except json.JSONDecodeError as e:
        raise ReadError(f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}.")
    datasets, sheets = [], []

    def add(sheet, recs, extra_warn=()):
        w = list(extra_warn)
        ds = _records_to_dataset(name, sheet, recs, w)
        datasets.append(ds)
        sheets.append(SheetInfo(sheet or name, ds.n_rows, ds.n_cols, "loaded"))

    def is_recs(v):
        return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)

    if is_recs(obj):
        add(None, obj)
    elif isinstance(obj, dict):
        tables = {k: v for k, v in obj.items() if is_recs(v)}
        if tables:
            others = [k for k in obj if k not in tables]
            for k, v in tables.items():
                add(k, v, [f"Top-level keys ignored (not tables): {', '.join(others)}."] if others else [])
        else:
            add(None, [obj], ["The JSON object was loaded as a single record."])
    else:
        raise ReadError("JSON must be an array of objects or an object containing arrays of objects.")
    return datasets, sheets, warnings


@register("JSON Lines", ".jsonl", ".ndjson")
def read_jsonl(name, data, has_header=True):
    warnings: list[str] = []
    text = decode_bytes(data, warnings)
    recs, nums, bad = [], [], []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line, parse_float=str, parse_int=str)
        except json.JSONDecodeError:
            bad.append(n)
            continue
        recs.append(obj if isinstance(obj, dict) else {"value": obj})
        nums.append(n)
    if bad:
        warnings.append(f"{len(bad)} line(s) are not valid JSON and were NOT loaded (lines: "
                        f"{', '.join(map(str, bad[:10]))}{'…' if len(bad) > 10 else ''}). Fix them to avoid missing rows.")
    if not recs:
        raise ReadError("No valid JSON objects were found in the file.")
    ds = _records_to_dataset(name, None, recs, warnings, nums)
    return [ds], [SheetInfo(name, ds.n_rows, ds.n_cols, "loaded")], warnings


# ----------------------------------------------------------------------------- parquet
@register("Parquet", ".parquet", status="implemented, NOT tested in the build environment (pyarrow unavailable offline)")
def read_parquet(name, data, has_header=True):
    try:
        df = pd.read_parquet(io.BytesIO(data))
    except ImportError:
        raise ReadError("Reading Parquet needs 'pyarrow' (pip install pyarrow).")
    except Exception as e:
        raise ReadError(f"The Parquet file could not be read ({type(e).__name__}): {e}")
    warnings: list[str] = []
    cols = [str(c) for c in df.columns]
    header = cols
    recs = [(i + 1, [to_text(v) for v in row]) for i, row in enumerate(df.astype(object).where(df.notna(), None).itertuples(index=False, name=None))]
    ds = _make_dataset(name, None, header, recs, True, 0, warnings, name)
    return [ds], [SheetInfo(name, ds.n_rows, ds.n_cols, "loaded")], warnings


# ----------------------------------------------------------------------------- xml
@register("XML", ".xml")
def read_xml(name, data, has_header=True):
    head = data[:200_000].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ReadError("XML with a DOCTYPE/ENTITY declaration is refused for safety (untrusted input).")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ReadError(f"Invalid XML: {e}")
    kids = list(root)
    if not kids:
        raise ReadError("The XML root element has no child elements to treat as records.")
    tag, freq = Counter(k.tag for k in kids).most_common(1)[0]
    records = [k for k in kids if k.tag == tag]
    warnings: list[str] = []
    if len(records) == 1 and len(list(records[0])) and all(len(list(c)) for c in records[0]):
        # <root><items><item>..</item></items></root>-style wrapper: descend one level
        inner = list(records[0])
        tag2, _ = Counter(k.tag for k in inner).most_common(1)[0]
        records = [k for k in inner if k.tag == tag2]
        warnings.append(f"Records were read from nested <{tag}>/<{tag2}> elements.")
    nested: set[str] = set()

    def flat(el, prefix=""):
        out = {}
        for a, v in el.attrib.items():
            out[f"{prefix}{a}"] = v
        children = list(el)
        if not children:
            text = (el.text or "").strip()
            if text:
                out[prefix.rstrip("_") or el.tag] = text
            return out
        counts = Counter(c.tag for c in children)
        for c in children:
            key = f"{prefix}{c.tag}"
            if counts[c.tag] > 1:
                nested.add(key)
                out[key] = (out.get(key, "") + "|" if key in out else "") + (c.text or "").strip()
            elif list(c) or c.attrib:
                out.update(flat(c, key + "_"))
            else:
                t = (c.text or "").strip()
                out[key] = t if t else None
        return out

    rows = [flat(r) for r in records]
    if nested:
        warnings.append("Repeated child elements were joined with '|' in columns: " + ", ".join(sorted(nested)) + ".")
    ds = _records_to_dataset(name, None, rows, warnings)
    return [ds], [SheetInfo(name, ds.n_rows, ds.n_cols, "loaded")], warnings


# ----------------------------------------------------------------------------- zip
@register("ZIP archive", ".zip")
def read_zip(name, data, has_header=True):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ReadError("The ZIP archive is corrupted.")
    members = [i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX")
               and not i.filename.rsplit("/", 1)[-1].startswith(".")]
    if len(members) > MAX_ZIP_MEMBERS:
        raise ReadError(f"The archive has {len(members)} files; the limit is {MAX_ZIP_MEMBERS}.")
    if sum(m.file_size for m in members) > MAX_ZIP_UNCOMPRESSED:
        raise ReadError("The archive expands beyond the safety limit (possible zip bomb).")
    datasets, sheets, warnings = [], [], []
    for m in members:
        ext = "." + m.filename.rsplit(".", 1)[-1].lower() if "." in m.filename else ""
        label = f"{name}/{m.filename}"
        if ext == ".zip":
            warnings.append(f"Nested archive '{m.filename}' was skipped (nested ZIPs are not supported).")
            continue
        if ext not in _READERS:
            warnings.append(f"'{m.filename}' skipped: unsupported format '{ext or 'none'}'.")
            continue
        with zf.open(m) as fh:
            payload = fh.read(MAX_FILE_BYTES + 1)
        ds_list, rep = read_file(label, payload, has_header)
        datasets.extend(ds_list)
        sheets.extend(rep.sheets)
        warnings.extend(f"{m.filename}: {w}" for w in rep.warnings)
        warnings.extend(f"{m.filename}: ERROR {e}" for e in rep.errors)
    if not datasets:
        raise ReadError("The archive contains no readable data files.")
    return datasets, sheets, warnings


# ----------------------------------------------------------------------------- public API
def read_file(name: str, data: bytes, has_header: bool = True):
    """-> (datasets, FileReport). Never raises for bad input; errors land in the report."""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    label = _READERS[ext][0] if ext in _READERS else (ext.lstrip(".").upper() or "unknown")
    rep = FileReport(name=name, fmt=label, size=len(data))
    if ext not in _READERS:
        rep.errors.append(f"Unsupported format '{ext or 'no extension'}'. Supported: {', '.join(supported_extensions())}.")
        return [], rep.finalize()
    if not data:
        rep.errors.append("The file is empty.")
        return [], rep.finalize()
    if len(data) > MAX_FILE_BYTES:
        rep.errors.append(f"The file is {len(data) / 1e6:.1f} MB; the limit is {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        return [], rep.finalize()
    try:
        datasets, sheets, warnings = _READERS[ext][1](name, data, has_header)
    except ReadError as e:
        rep.errors.append(str(e))
        return [], rep.finalize()
    except Exception as e:  # defensive: never show a traceback
        rep.errors.append(f"Unexpected problem while reading this file ({type(e).__name__}: {e}).")
        return [], rep.finalize()
    rep.datasets = [d.id for d in datasets]
    rep.sheets = sheets
    rep.rows = sum(d.n_rows for d in datasets)
    rep.cols = max((d.n_cols for d in datasets), default=0) if len(datasets) == 1 else None
    rep.warnings = list(warnings) + [w for d in datasets for w in d.warnings]
    # de-duplicate while keeping order
    rep.warnings = list(dict.fromkeys(rep.warnings))
    return datasets, rep.finalize()


def read_files(files: list[tuple[str, bytes]], has_header: bool = True):
    """Read many uploads; keeps dataset ids unique even if two uploads share a name."""
    all_ds: list[Dataset] = []
    reports: list[FileReport] = []
    used: dict[str, int] = {}
    for fname, data in files:
        used[fname] = used.get(fname, 0) + 1
        if used[fname] == 1 or "." not in fname:
            shown = fname if used[fname] == 1 else f"{fname} ({used[fname]})"
        else:
            stem, ext = fname.rsplit(".", 1)
            shown = f"{stem} ({used[fname]}).{ext}"  # keep the extension so the format is still recognised
        ds_list, rep = read_file(shown, data, has_header)
        all_ds.extend(ds_list)
        reports.append(rep)
    return all_ds, reports
