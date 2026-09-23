"""Database package for ForgeDesk: connection, migrations, transactions, and seeding."""

import logging
from typing import Any, Dict

from forgedesk.db.connection import (
    execute_insert,
    execute_write,
    get_connection,
    query_all,
    query_one,
    transaction,
)
from forgedesk.db.migrations import (
    apply_migrations,
    get_applied_migrations,
    reset_database,
)
from forgedesk.db.seed import seed_database

logger = logging.getLogger("forgedesk.db")


def init_db(seed_if_empty: bool = True) -> Dict[str, Any]:
    """Initialize database by running all pending migrations and seeding initial data if needed."""
    logger.info("Initializing ForgeDesk database...")
    applied = apply_migrations()
    result: Dict[str, Any] = {
        "applied_migrations": applied,
        "seed_result": None,
    }
    if seed_if_empty:
        result["seed_result"] = seed_database()
    return result


__all__ = [
    "get_connection",
    "transaction",
    "query_all",
    "query_one",
    "execute_write",
    "execute_insert",
    "apply_migrations",
    "get_applied_migrations",
    "reset_database",
    "seed_database",
    "init_db",
]
