"""Duplicate detection: inside one file, and against the stored catalog.

In-file duplicates keep the first occurrence of an article code and reject
every later one, so a re-imported or copy-pasted row never silently
overwrites an earlier row of the same file. Catalog duplicates are not
rejections: they are the codes already present in the catalog, reported so
the diff/apply layer can treat them as updates rather than new articles —
unless the catalog article belongs to a *different* supplier: those codes are
supplier-owned and are rejected as cross-supplier conflicts instead.
"""

from __future__ import annotations

from .models import PriceRow, Rejection

DUPLICATE_IN_FILE = "duplicate_in_file"
CROSS_SUPPLIER_CODE = "cross_supplier_code"


def split_in_file_duplicates(
    rows: list[PriceRow],
) -> tuple[list[PriceRow], list[Rejection]]:
    seen: dict[str, int] = {}
    unique: list[PriceRow] = []
    duplicates: list[Rejection] = []
    for row in rows:
        first_row = seen.get(row.article_code)
        if first_row is not None:
            duplicates.append(
                Rejection(
                    row_number=row.row_number,
                    reason_code=DUPLICATE_IN_FILE,
                    message=(
                        f"article code {row.article_code!r} already appears "
                        f"at row {first_row} of this file"
                    ),
                    raw={
                        "article_code": row.article_code,
                        "price": str(row.price),
                        "currency": row.currency,
                    },
                )
            )
        else:
            seen[row.article_code] = row.row_number
            unique.append(row)
    return unique, duplicates


def find_catalog_duplicates(rows: list[PriceRow], store) -> list[str]:
    """Return the sorted codes that already exist in the catalog."""

    return sorted({row.article_code for row in rows if store.article_exists(row.article_code)})


def split_cross_supplier_rows(
    rows: list[PriceRow], store, supplier_id: str
) -> tuple[list[PriceRow], list[Rejection]]:
    """Split off rows whose article code is owned by a different supplier.

    Article codes are supplier-scoped: a code already present in the catalog
    under another supplier can never be updated by this file. Such rows are
    rejected, so one supplier's price list can never silently rewrite another
    supplier's article.
    """

    kept: list[PriceRow] = []
    conflicts: list[Rejection] = []
    for row in rows:
        existing = store.get_article(row.article_code)
        if existing is not None and existing.supplier_id != supplier_id:
            conflicts.append(
                Rejection(
                    row_number=row.row_number,
                    reason_code=CROSS_SUPPLIER_CODE,
                    message=(
                        f"article code {row.article_code!r} belongs to supplier "
                        f"{existing.supplier_id!r}; supplier {supplier_id!r} "
                        "cannot change it"
                    ),
                    raw={
                        "article_code": row.article_code,
                        "price": str(row.price),
                        "currency": row.currency,
                    },
                )
            )
        else:
            kept.append(row)
    return kept, conflicts
