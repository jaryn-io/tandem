"""Core import engine: parse one supplier file into a validated ParseReport.

Pipeline per file: profile-driven read (CSV or Excel) -> per-row validation
with rejection collection -> in-file duplicate split -> cross-supplier
ownership guard and catalog duplicate classification (when a store is given).
This module never writes to the catalog; the diff/apply
layer (S02) consumes ParseReport and performs the mutation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .duplicates import (
    find_catalog_duplicates,
    split_cross_supplier_rows,
    split_in_file_duplicates,
)
from .models import ParseReport, PriceRow, Rejection
from .profiles import SupplierProfile
from .readers import MAX_ROWS, ReaderError, iter_source_rows
from .validation import validate_row


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_file(
    path: str | Path,
    profile: SupplierProfile,
    store=None,
) -> ParseReport:
    """Parse and validate one supplier price list.

    ``store`` is an optional CatalogStore; when given, accepted rows are
    classified against the catalog (duplicates vs. new articles) without
    modifying anything.
    """

    source = Path(path)
    if not source.is_file():
        raise ReaderError(f"file not found: {source}")

    accepted: list[PriceRow] = []
    rejections: list[Rejection] = []
    rows_total = 0

    for row_number, raw in iter_source_rows(source, profile):
        rows_total += 1
        if rows_total > MAX_ROWS:
            raise ReaderError(
                f"{source} has more than {MAX_ROWS} data rows; "
                "refusing to process it"
            )
        row, rejection = validate_row(row_number, raw, profile)
        if rejection is not None:
            rejections.append(rejection)
        else:
            accepted.append(row)

    unique, duplicate_rejections = split_in_file_duplicates(accepted)
    rejections.extend(duplicate_rejections)

    catalog_duplicates: list[str] = []
    if store is not None:
        unique, cross_supplier_rejections = split_cross_supplier_rows(
            unique, store, profile.supplier_id
        )
        rejections.extend(cross_supplier_rejections)
        catalog_duplicates = find_catalog_duplicates(unique, store)

    rejections.sort(key=lambda r: r.row_number)

    return ParseReport(
        supplier_id=profile.supplier_id,
        file_path=str(source),
        file_sha256=file_sha256(source),
        rows_total=rows_total,
        accepted=unique,
        rejections=rejections,
        catalog_duplicates=catalog_duplicates,
    )
