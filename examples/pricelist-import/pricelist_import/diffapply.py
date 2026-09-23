"""Diff and apply layer: dry-run preview and idempotent differential import.

``compute_diff`` compares a validated ParseReport against the stored catalog
and classifies every accepted row as new / changed / description-changed /
unchanged, plus the catalog articles of the same supplier that are no longer
listed in the file. It never writes. Rows whose article code belongs to a
different supplier surface in ``cross_supplier`` instead of any change class,
and ``apply_import`` never writes them. The preview is an exact account of what
``apply_import`` will write: a description-only change appears in the preview
and is applied without touching the price history, and a combined price +
description change stays a single ``changed`` entry whose old/new descriptions
are disclosed alongside the price move, since apply performs both writes.

``apply_import`` is the only write path. It is idempotent by file content
hash: re-importing a byte-identical file records nothing and returns a
skipped result. Otherwise it writes only the differences inside a single
transaction and appends to the price history only when a price (or currency)
actually moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import ParseReport, PriceRow
from .store import CatalogArticle, CatalogStore


@dataclass(frozen=True)
class PriceChange:
    article_code: str
    description: str | None
    old_price: str
    new_price: str
    currency: str
    old_description: str | None
    new_description: str | None

    @property
    def description_updated(self) -> bool:
        """True when applying this price change also rewrites the description."""
        return self.old_description != self.new_description


@dataclass(frozen=True)
class DescriptionChange:
    article_code: str
    old_description: str | None
    new_description: str | None


@dataclass(frozen=True)
class CrossSupplierConflict:
    """An accepted row whose article code is owned by another supplier.

    Such rows are never classified as new/changed and never applied: article
    codes are supplier-owned.
    """

    article_code: str
    owner_supplier_id: str


@dataclass(frozen=True)
class CatalogDiff:
    """Dry-run result: what applying this file would change."""

    supplier_id: str
    new: list[PriceRow]
    changed: list[PriceChange]
    description_changed: list[DescriptionChange]
    unchanged: list[PriceRow]
    no_longer_listed: list[CatalogArticle]
    cross_supplier: list[CrossSupplierConflict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new or self.changed or self.description_changed
            or self.no_longer_listed
        )


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one apply. ``skipped_identical`` means the exact same file
    content was already imported: nothing was written and no new run exists."""

    run_id: int | None
    skipped_identical: bool
    previous_run_id: int | None
    rows_total: int
    rows_accepted: int
    rows_rejected: int
    inserted: int
    updated: int
    descriptions_refreshed: int
    unchanged: int
    no_longer_listed: list[str]
    cross_supplier_skipped: list[CrossSupplierConflict] = field(default_factory=list)


def _same_price(stored: str, new: Decimal) -> bool:
    try:
        return Decimal(stored) == new
    except InvalidOperation:
        return False


def compute_diff(report: ParseReport, store: CatalogStore) -> CatalogDiff:
    """Classify the accepted rows of ``report`` against the catalog.

    "No longer listed" is scoped to the supplier of the report: articles
    belonging to other suppliers are never flagged. Rows whose article code
    is owned by a different supplier are not classified at all: they surface
    in ``cross_supplier`` and are never applied. (Reports built with a store
    already carry them as rejections; this guard keeps reports parsed without
    a store safe as well.)
    """

    new: list[PriceRow] = []
    changed: list[PriceChange] = []
    description_changed: list[DescriptionChange] = []
    unchanged: list[PriceRow] = []
    cross_supplier: list[CrossSupplierConflict] = []
    listed_codes: set[str] = set()

    for row in report.accepted:
        existing = store.get_article(row.article_code)
        if existing is not None and existing.supplier_id != report.supplier_id:
            cross_supplier.append(
                CrossSupplierConflict(
                    article_code=row.article_code,
                    owner_supplier_id=existing.supplier_id,
                )
            )
            continue
        listed_codes.add(row.article_code)
        if existing is None:
            new.append(row)
        elif not _same_price(existing.current_price, row.price) or existing.currency != row.currency:
            changed.append(
                PriceChange(
                    article_code=row.article_code,
                    description=row.description,
                    old_price=existing.current_price,
                    new_price=str(row.price),
                    currency=row.currency,
                    old_description=existing.description,
                    new_description=row.description,
                )
            )
        elif row.description != existing.description:
            description_changed.append(
                DescriptionChange(
                    article_code=row.article_code,
                    old_description=existing.description,
                    new_description=row.description,
                )
            )
        else:
            unchanged.append(row)

    no_longer_listed = [
        article
        for article in store.list_articles(supplier_id=report.supplier_id)
        if article.article_code not in listed_codes
    ]

    return CatalogDiff(
        supplier_id=report.supplier_id,
        new=new,
        changed=changed,
        description_changed=description_changed,
        unchanged=unchanged,
        no_longer_listed=no_longer_listed,
        cross_supplier=cross_supplier,
    )


def apply_import(report: ParseReport, store: CatalogStore) -> ApplyResult:
    """Apply a validated report to the catalog, writing only differences.

    Rejected rows never reach the catalog. Articles no longer listed are
    reported but never deleted, so their price history stays intact. The
    whole import runs in one transaction: on any failure it is rolled back
    and the exception propagates.
    """

    existing_run = store.find_run_by_hash(report.file_sha256)
    if existing_run is not None:
        return ApplyResult(
            run_id=None,
            skipped_identical=True,
            previous_run_id=existing_run.id,
            rows_total=report.rows_total,
            rows_accepted=report.rows_accepted,
            rows_rejected=report.rows_rejected,
            inserted=0,
            updated=0,
            descriptions_refreshed=0,
            unchanged=0,
            no_longer_listed=[],
        )

    diff = compute_diff(report, store)
    changed_codes = {c.article_code for c in diff.changed}
    foreign_codes = {c.article_code for c in diff.cross_supplier}

    run_id = store.begin_run(
        report.file_sha256, Path(report.file_path).name, report.supplier_id
    )
    try:
        for row in report.accepted:
            if row.article_code in foreign_codes:
                continue
            store.upsert_article(
                run_id,
                row.article_code,
                row.description,
                str(row.price),
                row.currency,
                changed=row.article_code in changed_codes,
                supplier_id=report.supplier_id,
            )
        store.finish_run(
            run_id, report.rows_total, report.rows_accepted, report.rows_rejected
        )
        store.commit()
    except BaseException:
        store.rollback()
        raise

    return ApplyResult(
        run_id=run_id,
        skipped_identical=False,
        previous_run_id=None,
        rows_total=report.rows_total,
        rows_accepted=report.rows_accepted,
        rows_rejected=report.rows_rejected,
        inserted=len(diff.new),
        updated=len(diff.changed),
        descriptions_refreshed=len(diff.description_changed),
        unchanged=len(diff.unchanged),
        no_longer_listed=[a.article_code for a in diff.no_longer_listed],
        cross_supplier_skipped=diff.cross_supplier,
    )
