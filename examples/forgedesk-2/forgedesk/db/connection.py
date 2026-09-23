"""SQLite persistent connection and transaction manager for ForgeDesk."""

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from forgedesk.config import DB_PATH, ensure_directories

logger = logging.getLogger("forgedesk.db")


def get_connection(database_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Create and configure a robust SQLite connection with WAL mode and foreign keys enabled."""
    ensure_directories()
    target_path = Path(database_path) if database_path else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(target_path),
        timeout=10.0,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    # Performance, integrity and concurrency pragmas
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 8000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextlib.contextmanager
def transaction(
    conn: Optional[sqlite3.Connection] = None,
    database_path: Optional[Union[str, Path]] = None,
    immediate: bool = True,
) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional context that commits on clean exit or rolls back on error."""
    owns_connection = False
    if conn is None:
        conn = get_connection(database_path)
        owns_connection = True

    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE;")
        else:
            conn.execute("BEGIN;")

        yield conn
        conn.commit()
    except Exception as e:
        logger.error("Transaction rolled back due to error: %s", e)
        try:
            conn.rollback()
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        if owns_connection:
            conn.close()


def query_all(
    sql: str,
    params: Union[Tuple[Any, ...], Dict[str, Any]] = (),
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Execute a query and return all matching rows as dictionaries."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def query_one(
    sql: str,
    params: Union[Tuple[Any, ...], Dict[str, Any]] = (),
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Execute a query and return a single matching row or None."""
    owns_conn = False
    if conn is None:
        conn = get_connection()
        owns_conn = True

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        if owns_conn:
            conn.close()


def execute_write(
    sql: str,
    params: Union[Tuple[Any, ...], Dict[str, Any]] = (),
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Execute a write statement inside a transaction and return affected row count."""
    if conn is not None:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount

    with transaction() as tx_conn:
        cur = tx_conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount


def execute_insert(
    sql: str,
    params: Union[Tuple[Any, ...], Dict[str, Any]] = (),
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Execute an insert statement and return the generated lastrowid."""
    if conn is not None:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.lastrowid or 0

    with transaction() as tx_conn:
        cur = tx_conn.cursor()
        cur.execute(sql, params)
        return cur.lastrowid or 0
