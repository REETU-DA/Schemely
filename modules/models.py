"""Shared data containers used by every module."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Dataset:
    """One worksheet / file / JSON collection, loaded as strings (never auto-converted)."""

    id: str
    source_file: str
    sheet: str | None
    df: pd.DataFrame  # object dtype; every cell is str or None; index label = source row offset
    original_columns: list[str]
    warnings: list[str] = field(default_factory=list)
    header_row: int = 1  # source row number of the header (0 = no header)
    included: bool = True

    @property
    def label(self) -> str:
        return f"{self.source_file} › {self.sheet}" if self.sheet else self.source_file

    @property
    def n_rows(self) -> int:
        return len(self.df)

    @property
    def n_cols(self) -> int:
        return len(self.df.columns)

    def original_columns_map(self) -> dict:
        """Column name used by the tool -> header text exactly as written in the source."""
        return dict(zip(self.df.columns, self.original_columns))

    def source_row(self, index_label) -> int:
        """DataFrame index label -> row number in the source file."""
        return int(index_label) + 1 + self.header_row


@dataclass
class SheetInfo:
    name: str
    rows: int
    cols: int
    status: str  # loaded | empty | error
    note: str = ""


@dataclass
class FileReport:
    name: str
    fmt: str
    size: int
    datasets: list[str] = field(default_factory=list)
    sheets: list[SheetInfo] = field(default_factory=list)
    rows: int | None = None
    cols: int | None = None
    status: str = "ok"  # ok | warning | error
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def finalize(self) -> "FileReport":
        self.status = "error" if self.errors else ("warning" if self.warnings else "ok")
        return self
