"""Supplier mapping profiles: schema, loading and validation.

A profile is a JSON document describing how one supplier's file layout maps
onto the canonical article fields. Example:

{
  "supplier_id": "acme",
  "format": "csv",
  "csv": {"delimiter": ";", "encoding": "utf-8-sig", "has_header": true},
  "xlsx": {"sheet": "Prices", "header_row": 1},
  "columns": {
    "article_code": "Codice articolo",
    "description": "Descrizione",
    "price": "Prezzo",
    "currency": "Valuta"
  },
  "decimal": {"decimal_separator": ",", "thousands_separator": "."},
  "code_normalization": {"strip_prefix": "AC-", "strip_whitespace": true,
                          "uppercase": true},
  "code_pattern": "^[A-Z0-9-]+$",
  "defaults": {"currency": "EUR"}
}

Only "csv" options are used for CSV files and only "xlsx" options for Excel
files; the unused section may be omitted. Column values are the header names
as they appear in the supplier file; files must carry a header row.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


class ProfileError(ValueError):
    """Raised when a supplier mapping profile is structurally invalid."""


FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"

COLUMN_KEYS = frozenset({"article_code", "description", "price", "currency"})
REQUIRED_COLUMNS = frozenset({"article_code", "price"})

_SUPPLIER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class CsvOptions:
    delimiter: str = ";"
    encoding: str = "utf-8-sig"
    quotechar: str = '"'
    has_header: bool = True
    skip_rows: int = 0


@dataclass(frozen=True)
class XlsxOptions:
    sheet: str | None = None
    header_row: int = 1


@dataclass(frozen=True)
class DecimalFormat:
    decimal_separator: str = "."
    thousands_separator: str | None = None


@dataclass(frozen=True)
class CodeNormalization:
    strip_prefix: str | None = None
    strip_whitespace: bool = True
    uppercase: bool = False


@dataclass(frozen=True)
class SupplierProfile:
    supplier_id: str
    format: str
    columns: dict[str, str]
    csv: CsvOptions | None = None
    xlsx: XlsxOptions | None = None
    decimal: DecimalFormat = DecimalFormat()
    code_normalization: CodeNormalization = CodeNormalization()
    code_pattern: str | None = None
    defaults: dict[str, str] = field(default_factory=dict)


def _require_mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"{what} must be an object, got {type(value).__name__}")
    return value


def _single_char(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) != 1:
        raise ProfileError(f"{what} must be a single character")
    return value


def _build_csv(raw: dict) -> CsvOptions:
    data = _require_mapping(raw, "csv")
    delimiter = _single_char(data.get("delimiter", ";"), "csv.delimiter")
    quotechar = _single_char(data.get("quotechar", '"'), "csv.quotechar")
    encoding = data.get("encoding", "utf-8-sig")
    if not isinstance(encoding, str):
        raise ProfileError("csv.encoding must be a string")
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ProfileError(f"csv.encoding is not a known codec: {encoding!r}") from exc
    has_header = data.get("has_header", True)
    if not isinstance(has_header, bool):
        raise ProfileError("csv.has_header must be a boolean")
    skip_rows = data.get("skip_rows", 0)
    if not isinstance(skip_rows, int) or isinstance(skip_rows, bool) or skip_rows < 0:
        raise ProfileError("csv.skip_rows must be a non-negative integer")
    return CsvOptions(
        delimiter=delimiter,
        encoding=encoding,
        quotechar=quotechar,
        has_header=has_header,
        skip_rows=skip_rows,
    )


def _build_xlsx(raw: dict) -> XlsxOptions:
    data = _require_mapping(raw, "xlsx")
    sheet = data.get("sheet")
    if sheet is not None and not isinstance(sheet, str):
        raise ProfileError("xlsx.sheet must be a string or null")
    header_row = data.get("header_row", 1)
    if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
        raise ProfileError("xlsx.header_row must be an integer >= 1")
    return XlsxOptions(sheet=sheet, header_row=header_row)


def _build_decimal(raw: dict) -> DecimalFormat:
    data = _require_mapping(raw, "decimal")
    decimal_separator = _single_char(
        data.get("decimal_separator", "."), "decimal.decimal_separator"
    )
    thousands_separator = data.get("thousands_separator")
    if thousands_separator is not None:
        thousands_separator = _single_char(
            thousands_separator, "decimal.thousands_separator"
        )
        if thousands_separator == decimal_separator:
            raise ProfileError(
                "decimal.thousands_separator must differ from decimal.decimal_separator"
            )
    return DecimalFormat(
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )


def _build_normalization(raw: dict) -> CodeNormalization:
    data = _require_mapping(raw, "code_normalization")
    strip_prefix = data.get("strip_prefix")
    if strip_prefix is not None and not isinstance(strip_prefix, str):
        raise ProfileError("code_normalization.strip_prefix must be a string or null")
    for key in ("strip_whitespace", "uppercase"):
        if not isinstance(data.get(key, True if key == "strip_whitespace" else False), bool):
            raise ProfileError(f"code_normalization.{key} must be a boolean")
    return CodeNormalization(
        strip_prefix=strip_prefix,
        strip_whitespace=data.get("strip_whitespace", True),
        uppercase=data.get("uppercase", False),
    )


def profile_from_dict(raw: dict) -> SupplierProfile:
    """Validate a profile document and return the typed profile."""

    data = _require_mapping(raw, "profile")

    supplier_id = data.get("supplier_id")
    if not isinstance(supplier_id, str) or not _SUPPLIER_ID_RE.match(supplier_id):
        raise ProfileError(
            "supplier_id is required and must match ^[a-z0-9][a-z0-9_-]*$"
        )

    fmt = data.get("format")
    if fmt not in (FORMAT_CSV, FORMAT_XLSX):
        raise ProfileError(f"format must be one of: {FORMAT_CSV}, {FORMAT_XLSX}")

    columns = _require_mapping(data.get("columns"), "columns")
    unknown = set(columns) - COLUMN_KEYS
    if unknown:
        raise ProfileError(f"columns contains unknown keys: {sorted(unknown)}")
    missing = REQUIRED_COLUMNS - set(columns)
    if missing:
        raise ProfileError(f"columns is missing required keys: {sorted(missing)}")
    for key, value in columns.items():
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"columns.{key} must be a non-empty header name")

    decimal_fmt = _build_decimal(data.get("decimal", {}))
    normalization = _build_normalization(data.get("code_normalization", {}))

    code_pattern = data.get("code_pattern")
    if code_pattern is not None:
        if not isinstance(code_pattern, str):
            raise ProfileError("code_pattern must be a string or null")
        try:
            re.compile(code_pattern)
        except re.error as exc:
            raise ProfileError(f"code_pattern is not a valid regex: {exc}") from exc

    defaults = _require_mapping(data.get("defaults", {}), "defaults")
    for key, value in defaults.items():
        if not isinstance(value, str):
            raise ProfileError(f"defaults.{key} must be a string")

    csv_opts = _build_csv(data["csv"]) if "csv" in data else (CsvOptions() if fmt == FORMAT_CSV else None)
    xlsx_opts = (
        _build_xlsx(data["xlsx"]) if "xlsx" in data else (XlsxOptions() if fmt == FORMAT_XLSX else None)
    )

    return SupplierProfile(
        supplier_id=supplier_id,
        format=fmt,
        columns=dict(columns),
        csv=csv_opts,
        xlsx=xlsx_opts,
        decimal=decimal_fmt,
        code_normalization=normalization,
        code_pattern=code_pattern,
        defaults=dict(defaults),
    )


def load_profile(source: str | Path | dict) -> SupplierProfile:
    """Load a supplier profile from a JSON file path or an already-parsed dict."""

    if isinstance(source, dict):
        return profile_from_dict(source)
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read profile file {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profile file {path} is not valid JSON: {exc}") from exc
    return profile_from_dict(raw)
