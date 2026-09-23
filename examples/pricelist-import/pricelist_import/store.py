"""SQLite-backed article catalog, price history and import ledger.

One local database file holds the catalog (current article state), the
price history (append-only audit trail) and the import ledger (one row per
import run, keyed by file content hash so re-importing the identical file
is detectable as a no-op).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    file_name TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    rows_total INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_rejected INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
    article_code TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    description TEXT,
    currency TEXT NOT NULL,
    current_price TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL REFERENCES import_runs(id),
    last_changed_run INTEGER NOT NULL REFERENCES import_runs(id)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_code TEXT NOT NULL REFERENCES articles(article_code),
    price TEXT NOT NULL,
    currency TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES import_runs(id),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_article
    ON price_history (article_code);
CREATE INDEX IF NOT EXISTS idx_import_runs_hash
    ON import_runs (file_sha256);
"""


@dataclass(frozen=True)
class CatalogArticle:
    article_code: str
    supplier_id: str
    description: str | None
    currency: str
    current_price: str
    first_seen_run: int
    last_changed_run: int


@dataclass(frozen=True)
class ImportRun:
    id: int
    imported_at: str
    file_sha256: str
    file_name: str
    supplier_id: str
    rows_total: int
    rows_accepted: int
    rows_rejected: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogStore:
    """Local catalog database. Usage::

        with CatalogStore("catalog.db") as store:
            run_id = store.begin_run(...)
            store.upsert_article(...)
            store.finish_run(run_id, ...)

    All writes happen inside one transaction per import run; nothing is
    committed if the run aborts.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- read side ---------------------------------------------------------

    def article_exists(self, article_code: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM articles WHERE article_code = ?", (article_code,)
        ).fetchone()
        return row is not None

    def get_article(self, article_code: str) -> CatalogArticle | None:
        row = self._conn.execute(
            "SELECT * FROM articles WHERE article_code = ?", (article_code,)
        ).fetchone()
        return CatalogArticle(**dict(row)) if row else None

    def list_articles(self, supplier_id: str | None = None) -> list[CatalogArticle]:
        if supplier_id is None:
            rows = self._conn.execute(
                "SELECT * FROM articles ORDER BY article_code"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM articles WHERE supplier_id = ? ORDER BY article_code",
                (supplier_id,),
            ).fetchall()
        return [CatalogArticle(**dict(r)) for r in rows]

    def price_history(self, article_code: str) -> list[tuple[str, str, str, int]]:
        rows = self._conn.execute(
            "SELECT price, currency, recorded_at, run_id FROM price_history "
            "WHERE article_code = ? ORDER BY id",
            (article_code,),
        ).fetchall()
        return [(r["price"], r["currency"], r["recorded_at"], r["run_id"]) for r in rows]

    def find_run_by_hash(self, file_sha256: str) -> ImportRun | None:
        row = self._conn.execute(
            "SELECT * FROM import_runs WHERE file_sha256 = ? ORDER BY id",
            (file_sha256,),
        ).fetchone()
        return ImportRun(**dict(row)) if row else None

    # -- write side --------------------------------------------------------

    def begin_run(self, file_sha256: str, file_name: str, supplier_id: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO import_runs "
            "(imported_at, file_sha256, file_name, supplier_id, "
            " rows_total, rows_accepted, rows_rejected) "
            "VALUES (?, ?, ?, ?, 0, 0, 0)",
            (_utcnow(), file_sha256, file_name, supplier_id),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, rows_total: int, rows_accepted: int,
                   rows_rejected: int) -> None:
        self._conn.execute(
            "UPDATE import_runs SET rows_total = ?, rows_accepted = ?, "
            "rows_rejected = ? WHERE id = ?",
            (rows_total, rows_accepted, rows_rejected, run_id),
        )

    def upsert_article(
        self,
        run_id: int,
        article_code: str,
        description: str | None,
        price: str,
        currency: str,
        changed: bool,
        supplier_id: str,
    ) -> None:
        """Insert a new article or update an existing one.

        ``changed`` records whether the price actually moved: an unchanged
        re-import appends no history row. A description that differs while
        the price stands still is refreshed without touching the history.
        """
        existing = self.get_article(article_code)
        if existing is None:
            self._conn.execute(
                "INSERT INTO articles "
                "(article_code, supplier_id, description, currency, current_price, "
                " first_seen_run, last_changed_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (article_code, supplier_id, description, currency, price, run_id, run_id),
            )
            self._append_history(run_id, article_code, price, currency)
        elif changed:
            self._conn.execute(
                "UPDATE articles SET description = ?, currency = ?, "
                "current_price = ?, last_changed_run = ? WHERE article_code = ?",
                (description, currency, price, run_id, article_code),
            )
            self._append_history(run_id, article_code, price, currency)
        elif description != existing.description:
            self._conn.execute(
                "UPDATE articles SET description = ? WHERE article_code = ?",
                (description, article_code),
            )

    def _append_history(self, run_id: int, article_code: str,
                        price: str, currency: str) -> None:
        self._conn.execute(
            "INSERT INTO price_history "
            "(article_code, price, currency, run_id, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (article_code, price, currency, run_id, _utcnow()),
        )

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()
