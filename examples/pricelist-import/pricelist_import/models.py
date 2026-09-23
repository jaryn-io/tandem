"""Domain model for the supplier price-list import tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class PriceRow:
    """One validated price-list row, ready for the diff/apply layer."""

    row_number: int
    article_code: str
    description: str | None
    price: Decimal
    currency: str


@dataclass(frozen=True)
class Rejection:
    """One rejected row: machine-readable reason plus the raw source values."""

    row_number: int
    reason_code: str
    message: str
    raw: dict[str, str | None] = field(default_factory=dict)


@dataclass
class ParseReport:
    """Result of parsing and validating one supplier file."""

    supplier_id: str
    file_path: str
    file_sha256: str
    rows_total: int
    accepted: list[PriceRow]
    rejections: list[Rejection]
    catalog_duplicates: list[str]

    @property
    def rows_accepted(self) -> int:
        return len(self.accepted)

    @property
    def rows_rejected(self) -> int:
        return len(self.rejections)

    def validation_rejections(self) -> list[Rejection]:
        return [
            r
            for r in self.rejections
            if r.reason_code not in ("duplicate_in_file", "cross_supplier_code")
        ]

    def duplicate_rejections(self) -> list[Rejection]:
        return [r for r in self.rejections if r.reason_code == "duplicate_in_file"]

    def cross_supplier_rejections(self) -> list[Rejection]:
        return [r for r in self.rejections if r.reason_code == "cross_supplier_code"]
