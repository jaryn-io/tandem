"""Profile-driven readers for CSV and Excel price lists.

Both readers yield ``(row_number, raw_fields)`` pairs where ``row_number`` is
the physical row in the source file (CSV line number or Excel sheet row) and
``raw_fields`` maps canonical field names (article_code, description, price,
currency) to the raw string value, or None when the column is absent from the
profile or the cell is empty. Completely empty rows are skipped.

Excel support requires the optional ``openpyxl`` dependency; it is imported
lazily so that CSV-only usage works with the standard library alone.

Every source passes fixed input bounds before and during reading: a source
file larger than ``MAX_SOURCE_BYTES`` is refused up front, more than
``MAX_ROWS`` rows aborts the read, and an .xlsx archive whose declared
uncompressed payload exceeds ``MAX_XLSX_UNCOMPRESSED_BYTES`` (or yields more
than ``MAX_XLSX_CELLS`` cells) is rejected as a file-level error instead of
being allowed to exhaust local resources.
"""

from __future__ import annotations

import csv
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

from .profiles import FORMAT_CSV, FORMAT_XLSX, SupplierProfile

# Input bounds protecting the local machine from oversized or hostile
# supplier files. They are deliberately generous for legitimate price lists;
# a file that exceeds them is rejected with a file-level error.
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 200_000
MAX_XLSX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_XLSX_CELLS = 2_000_000


class ReaderError(RuntimeError):
    """Raised for file-level problems: unreadable file, wrong structure,
    missing mapped header columns, unsupported sheet, decode failures."""


class NumericCell(str):
    """A raw value that came from a numeric Excel cell.

    The readers convert numeric cells to plain machine-formatted text; the
    marker lets price validation trust that conversion while still treating
    supplier-authored text strictly by the profile's separators.
    """


RawRow = tuple[int, dict[str, str | None]]


def iter_source_rows(path: str | Path, profile: SupplierProfile) -> Iterator[RawRow]:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ReaderError(f"cannot stat {source}: {exc}") from exc
    if size > MAX_SOURCE_BYTES:
        raise ReaderError(
            f"{source} is {size} bytes, over the {MAX_SOURCE_BYTES}-byte "
            "source limit; refusing to process it"
        )
    if profile.format == FORMAT_CSV:
        yield from _iter_csv(source, profile)
    elif profile.format == FORMAT_XLSX:
        yield from _iter_xlsx(source, profile)
    else:  # pragma: no cover - load_profile rejects other formats
        raise ReaderError(f"unsupported format: {profile.format!r}")


def _column_indexes(header: list[str | None], profile: SupplierProfile) -> dict[str, int]:
    normalized = [h.strip() if isinstance(h, str) else None for h in header]
    indexes: dict[str, int] = {}
    for field_name, header_name in profile.columns.items():
        try:
            indexes[field_name] = normalized.index(header_name.strip())
        except ValueError:
            if field_name in ("article_code", "price"):
                raise ReaderError(
                    f"required column {header_name!r} (mapped to {field_name!r}) "
                    f"not found in header row: {normalized}"
                )
    return indexes


def _extract(
    values: list[str | None], indexes: dict[str, int]
) -> dict[str, str | None]:
    raw: dict[str, str | None] = {}
    for field_name in ("article_code", "description", "price", "currency"):
        idx = indexes.get(field_name)
        if idx is None or idx >= len(values):
            raw[field_name] = None
        else:
            raw[field_name] = values[idx]
    return raw


def _is_empty(values: list[str | None]) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def _iter_csv(path: Path, profile: SupplierProfile) -> Iterator[RawRow]:
    opts = profile.csv
    assert opts is not None
    try:
        handle = path.open("r", encoding=opts.encoding, newline="")
    except OSError as exc:
        raise ReaderError(f"cannot open {path}: {exc}") from exc
    with handle:
        reader = csv.reader(handle, delimiter=opts.delimiter, quotechar=opts.quotechar)
        try:
            for _ in range(opts.skip_rows):
                next(reader, None)
            header = next(reader, None)
            if header is None:
                raise ReaderError(f"{path} is empty: no header row")
            indexes = _column_indexes(header, profile)
            for record in reader:
                if not record or _is_empty(record):
                    continue
                yield reader.line_num, _extract(record, indexes)
        except UnicodeDecodeError as exc:
            raise ReaderError(
                f"{path} cannot be decoded with encoding {opts.encoding!r}: {exc}"
            ) from exc
        except csv.Error as exc:
            raise ReaderError(f"{path} is not well-formed CSV: {exc}") from exc


def _cell_to_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return NumericCell(str(value))
    if isinstance(value, float):
        # repr() is the shortest decimal string that round-trips to the same
        # binary float; formatting it as a Decimal with "f" expands any
        # scientific notation, so the text keeps the cell's available
        # precision and stays a plain number for validation.
        return NumericCell(format(Decimal(repr(value)), "f"))
    return str(value)


def _check_xlsx_archive(path: Path) -> None:
    """Reject .xlsx archives whose declared uncompressed payload is abusive.

    An .xlsx is a ZIP; a tiny compressed file can expand into gigabytes of
    XML. The declared member sizes are summed and bounded *before* any
    integrity check runs, because ``testzip()`` itself reads and decompresses
    every member and would consume the very resources the bound exists to
    protect. openpyxl is only invoked once both checks have passed.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
            if total > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ReaderError(
                    f"{path} expands to {total} bytes, over the "
                    f"{MAX_XLSX_UNCOMPRESSED_BYTES}-byte decompression limit; "
                    "refusing to process it"
                )
            bad = archive.testzip()
    except zipfile.BadZipFile as exc:
        raise ReaderError(f"{path} is not a readable .xlsx archive: {exc}") from exc
    if bad is not None:
        raise ReaderError(f"{path} has a corrupt archive member: {bad!r}")


def _iter_xlsx(path: Path, profile: SupplierProfile) -> Iterator[RawRow]:
    opts = profile.xlsx
    assert opts is not None
    try:
        import openpyxl
    except ImportError as exc:
        raise ReaderError(
            "reading .xlsx files requires the 'openpyxl' package; "
            "install the dependency listed in requirements.txt"
        ) from exc
    _check_xlsx_archive(path)
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except OSError as exc:
        raise ReaderError(f"cannot open {path}: {exc}") from exc
    except Exception as exc:
        raise ReaderError(f"{path} is not a readable .xlsx workbook: {exc}") from exc
    try:
        if opts.sheet is not None:
            if opts.sheet not in workbook.sheetnames:
                raise ReaderError(
                    f"sheet {opts.sheet!r} not found in {path}; "
                    f"available: {workbook.sheetnames}"
                )
            sheet = workbook[opts.sheet]
        else:
            sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header: list[str | None] | None = None
        indexes: dict[str, int] = {}
        cells_seen = 0
        for row_number, cells in enumerate(rows, start=1):
            if row_number > MAX_ROWS:
                raise ReaderError(
                    f"{path} has more than {MAX_ROWS} sheet rows; "
                    "refusing to process it"
                )
            cells_seen += len(cells)
            if cells_seen > MAX_XLSX_CELLS:
                raise ReaderError(
                    f"{path} has more than {MAX_XLSX_CELLS} cells; "
                    "refusing to process it"
                )
            values = [_cell_to_text(c) for c in cells]
            if row_number < opts.header_row:
                continue
            if row_number == opts.header_row:
                header = values
                indexes = _column_indexes(header, profile)
                continue
            if _is_empty(values):
                continue
            yield row_number, _extract(values, indexes)
    finally:
        workbook.close()
