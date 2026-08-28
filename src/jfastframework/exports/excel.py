"""Writing spreadsheets, including ones too large to hold in memory.

openpyxl has two modes and the difference matters at report size. The normal
one builds the whole workbook as objects and serialises at the end -- fine for
a hundred rows, fatal for two hundred thousand. **Write-only mode** streams
rows to the file as they are appended, which is what ``stream_rows`` uses.

The cost of write-only mode is real and worth stating: you cannot go back. No
reading a cell you already wrote, no auto-sizing a column from data you have
forgotten. Column widths are therefore declared up front, and the header is
whatever you pass first.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# openpyxl refuses anything else, and the failure arrives deep inside the
# serialiser rather than at the row that caused it.
_WRITABLE = (str, int, float, bool, Decimal, datetime, date, type(None))


@dataclass
class Column:
    """A column, declared before any row is written.

    Width is declared rather than measured because write-only mode cannot look
    back at the rows to size itself.
    """

    header: str
    width: int = 18
    # "0.00", "#,##0", "yyyy-mm-dd" -- openpyxl number formats.
    number_format: str = ""


def _coerce(value: Any) -> Any:
    """Make a value writable, or fail at the row rather than in the serialiser."""
    if isinstance(value, _WRITABLE):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    return str(value)


def stream_rows(
    path: str | Path,
    columns: Sequence[Column],
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "Sheet1",
    freeze_header: bool = True,
) -> int:
    """Write rows to an xlsx as they are produced. Returns the row count.

    ``rows`` is consumed lazily, so a database cursor can be handed straight to
    it and neither the rows nor the workbook are ever fully in memory.

    Requires: ``pip install jfastframework[exports]``
    """
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_name)

    for index, column in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = column.width

    if freeze_header:
        # Frozen before the header row is written, which is the only chance
        # write-only mode gives you.
        sheet.freeze_panes = "A2"

    sheet.append([c.header for c in columns])

    formats = [c.number_format for c in columns]
    written = 0
    for row in rows:
        cells: list[Any] = []
        for index, value in enumerate(row):
            coerced = _coerce(value)
            fmt = formats[index] if index < len(formats) else ""
            if fmt and coerced is not None:
                from openpyxl.cell import WriteOnlyCell

                cell = WriteOnlyCell(sheet, value=coerced)
                cell.number_format = fmt
                cells.append(cell)
            else:
                cells.append(coerced)
        sheet.append(cells)
        written += 1

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(destination))
    return written


def write_sheet(
    path: str | Path,
    columns: Sequence[Column],
    rows: Sequence[Sequence[Any]],
    *,
    sheet_name: str = "Sheet1",
) -> int:
    """The small-report version: everything in memory, editable before saving.

    Use this under a few thousand rows. Past that, ``stream_rows``.
    """
    return stream_rows(path, columns, rows, sheet_name=sheet_name)


def read_rows(path: str | Path, *, sheet_name: str | None = None) -> list[list[Any]]:
    """Read a sheet back, values only.

    ``read_only`` and ``data_only``: the first keeps a large file from being
    loaded whole, the second returns what a formula evaluated to rather than
    the formula itself -- which is almost always what a reader wants, and is
    the difference between reading ``1250.00`` and reading ``=SUM(B2:B40)``.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name] if sheet_name else workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


__all__ = ["Column", "read_rows", "stream_rows", "write_sheet"]
