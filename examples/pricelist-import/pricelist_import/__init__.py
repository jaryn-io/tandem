"""Supplier price-list import tool — engine, diff/apply layer and CLI."""

from .diffapply import (
    ApplyResult,
    CatalogDiff,
    CrossSupplierConflict,
    PriceChange,
    apply_import,
    compute_diff,
)
from .duplicates import (
    CROSS_SUPPLIER_CODE,
    DUPLICATE_IN_FILE,
    find_catalog_duplicates,
    split_cross_supplier_rows,
    split_in_file_duplicates,
)
from .engine import file_sha256, parse_file
from .models import ParseReport, PriceRow, Rejection
from .profiles import ProfileError, SupplierProfile, load_profile
from .readers import (
    MAX_ROWS,
    MAX_SOURCE_BYTES,
    MAX_XLSX_CELLS,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    NumericCell,
    ReaderError,
    iter_source_rows,
)
from .store import CatalogArticle, CatalogStore, ImportRun
from .validation import (
    AMBIGUOUS_PRICE,
    AmbiguousPriceError,
    normalize_code,
    parse_price,
    validate_row,
)

__all__ = [
    "AMBIGUOUS_PRICE",
    "AmbiguousPriceError",
    "ApplyResult",
    "CROSS_SUPPLIER_CODE",
    "CatalogArticle",
    "CatalogDiff",
    "CatalogStore",
    "CrossSupplierConflict",
    "DUPLICATE_IN_FILE",
    "ImportRun",
    "MAX_ROWS",
    "MAX_SOURCE_BYTES",
    "MAX_XLSX_CELLS",
    "MAX_XLSX_UNCOMPRESSED_BYTES",
    "NumericCell",
    "ParseReport",
    "PriceChange",
    "PriceRow",
    "ProfileError",
    "ReaderError",
    "Rejection",
    "SupplierProfile",
    "apply_import",
    "compute_diff",
    "file_sha256",
    "find_catalog_duplicates",
    "iter_source_rows",
    "load_profile",
    "normalize_code",
    "parse_file",
    "parse_price",
    "split_cross_supplier_rows",
    "split_in_file_duplicates",
    "validate_row",
]
