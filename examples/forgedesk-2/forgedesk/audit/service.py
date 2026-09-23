"""Audit Service - Append-Only Audit Logging & Searchable History for ForgeDesk."""

import csv
import io
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.utils.datetime_tz import format_display, now_rome_iso

logger = logging.getLogger("forgedesk.audit.service")


def diff_values(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    ignored_keys: Optional[set] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compute structured key-level differences between before and after states.
    
    Returns a dict mapping field names to:
    {
        "before": old_value,
        "after": new_value,
        "change_type": "added" | "removed" | "modified"
    }
    """
    if ignored_keys is None:
        ignored_keys = {"password_hash", "password_salt", "session_token"}

    diffs: Dict[str, Dict[str, Any]] = {}
    before_dict = before if isinstance(before, dict) else {}
    after_dict = after if isinstance(after, dict) else {}

    all_keys = (set(before_dict.keys()) | set(after_dict.keys())) - ignored_keys

    for key in sorted(all_keys):
        in_before = key in before_dict
        in_after = key in after_dict

        if in_before and not in_after:
            diffs[key] = {
                "before": before_dict[key],
                "after": None,
                "change_type": "removed",
            }
        elif not in_before and in_after:
            diffs[key] = {
                "before": None,
                "after": after_dict[key],
                "change_type": "added",
            }
        else:
            val_b = before_dict[key]
            val_a = after_dict[key]
            if val_b != val_a:
                diffs[key] = {
                    "before": val_b,
                    "after": val_a,
                    "change_type": "modified",
                }

    return diffs


def record_audit_event(
    action: str,
    object_type: str,
    object_id: Union[str, int],
    actor: Optional[Dict[str, Any]] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Record an append-only audit event in the database.
    
    If `conn` is provided, executes within the caller's ongoing transaction,
    guaranteeing atomic failure-safety for multi-object business operations.
    """
    # Resolve actor information
    resolved_actor_id = actor_id
    resolved_actor_name = actor_name

    if actor and isinstance(actor, dict):
        if resolved_actor_id is None:
            resolved_actor_id = actor.get("id")
        if not resolved_actor_name:
            resolved_actor_name = actor.get("full_name") or actor.get("username") or actor.get("name")

    if not resolved_actor_name:
        resolved_actor_name = "system" if resolved_actor_id is None else f"user_{resolved_actor_id}"

    # Serialize JSON fields safely
    def _to_json(val: Any) -> Optional[str]:
        if val is None:
            return None
        if isinstance(val, str):
            return val
        try:
            return json.dumps(val, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning("Failed to serialize audit payload field to JSON: %s", e)
            return json.dumps({"raw_str": str(val)})

    details_json = _to_json(details)
    before_json = _to_json(before)
    after_json = _to_json(after)
    created_at = now_rome_iso()
    obj_id_str = str(object_id)

    sql = """
        INSERT INTO audit_log (
            actor_id, actor_name, action, object_type, object_id,
            details_json, before_json, after_json, ip_address, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    params = (
        resolved_actor_id,
        resolved_actor_name,
        action,
        object_type,
        obj_id_str,
        details_json,
        before_json,
        after_json,
        ip_address[:128] if ip_address else None,
        created_at,
    )

    if conn is not None:
        cursor = conn.execute(sql, params)
        event_id = cursor.lastrowid
    else:
        with transaction() as tx_conn:
            cursor = tx_conn.execute(sql, params)
            event_id = cursor.lastrowid

    logger.debug(
        "Audit event #%d recorded: action='%s', object='%s:%s', actor='%s' (id=%s)",
        event_id,
        action,
        object_type,
        obj_id_str,
        resolved_actor_name,
        resolved_actor_id,
    )
    return event_id


def query_audit_logs(
    search_query: Optional[str] = None,
    action: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    actor_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "DESC",
) -> Tuple[List[Dict[str, Any]], int]:
    """Search and paginate audit logs with comprehensive filters.
    
    Returns a tuple of (records_list, total_count).
    """
    where_clauses: List[str] = []
    params: List[Any] = []

    if action:
        where_clauses.append("a.action = ?")
        params.append(action.strip())

    if object_type:
        where_clauses.append("a.object_type = ?")
        params.append(object_type.strip())

    if object_id:
        where_clauses.append("a.object_id = ?")
        params.append(str(object_id).strip())

    if actor_id is not None:
        where_clauses.append("a.actor_id = ?")
        params.append(actor_id)

    if start_date:
        # Support YYYY-MM-DD or full ISO
        clean_start = start_date.strip()
        if len(clean_start) == 10:
            clean_start = f"{clean_start}T00:00:00"
        where_clauses.append("a.created_at >= ?")
        params.append(clean_start)

    if end_date:
        clean_end = end_date.strip()
        if len(clean_end) == 10:
            clean_end = f"{clean_end}T23:59:59"
        where_clauses.append("a.created_at <= ?")
        params.append(clean_end)

    if search_query:
        query_pattern = f"%{search_query.strip()}%"
        where_clauses.append(
            """(
                a.action LIKE ? OR
                a.object_type LIKE ? OR
                a.object_id LIKE ? OR
                a.actor_name LIKE ? OR
                a.details_json LIKE ? OR
                a.before_json LIKE ? OR
                a.after_json LIKE ? OR
                a.ip_address LIKE ?
            )"""
        )
        params.extend([query_pattern] * 8)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # 1. Count Total
    count_sql = f"SELECT COUNT(*) AS total FROM audit_log a {where_sql};"
    count_row = query_one(count_sql, tuple(params))
    total_count = count_row["total"] if count_row else 0

    # 2. Fetch Paginated Records
    order_dir = "ASC" if order.upper() == "ASC" else "DESC"
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)

    fetch_sql = f"""
        SELECT a.id, a.actor_id, a.actor_name, a.action, a.object_type, a.object_id,
               a.details_json, a.before_json, a.after_json, a.ip_address, a.created_at,
               u.username AS actor_username, u.email AS actor_email, u.role AS actor_role
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_id
        {where_sql}
        ORDER BY a.id {order_dir}
        LIMIT ? OFFSET ?;
    """
    fetch_params = list(params) + [safe_limit, safe_offset]
    rows = query_all(fetch_sql, tuple(fetch_params))

    records: List[Dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        # Parse JSON fields safely
        details = None
        if rec["details_json"]:
            try:
                details = json.loads(rec["details_json"])
            except Exception:
                details = {"raw": rec["details_json"]}
        rec["details"] = details

        before = None
        if rec["before_json"]:
            try:
                before = json.loads(rec["before_json"])
            except Exception:
                before = {"raw": rec["before_json"]}
        rec["before"] = before

        after = None
        if rec["after_json"]:
            try:
                after = json.loads(rec["after_json"])
            except Exception:
                after = {"raw": rec["after_json"]}
        rec["after"] = after

        # Compute field diffs
        rec["diffs"] = diff_values(before, after)
        records.append(rec)

    return records, total_count


def get_audit_event_by_id(audit_id: int) -> Optional[Dict[str, Any]]:
    """Fetch single audit log record by ID with parsed JSON payloads and field diffs."""
    row = query_one(
        """
        SELECT a.id, a.actor_id, a.actor_name, a.action, a.object_type, a.object_id,
               a.details_json, a.before_json, a.after_json, a.ip_address, a.created_at,
               u.username AS actor_username, u.email AS actor_email, u.role AS actor_role
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_id
        WHERE a.id = ?;
        """,
        (audit_id,),
    )
    if not row:
        return None

    rec = dict(row)
    for json_field in ("details_json", "before_json", "after_json"):
        parsed = None
        if rec[json_field]:
            try:
                parsed = json.loads(rec[json_field])
            except Exception:
                parsed = {"raw": rec[json_field]}
        rec[json_field[:-5]] = parsed  # details, before, after

    rec["diffs"] = diff_values(rec["before"], rec["after"])
    return rec


def get_audit_filter_options() -> Dict[str, List[Any]]:
    """Retrieve distinct actions, object types, and actors for search filters."""
    actions_rows = query_all("SELECT DISTINCT action FROM audit_log ORDER BY action ASC;")
    objects_rows = query_all("SELECT DISTINCT object_type FROM audit_log ORDER BY object_type ASC;")
    actors_rows = query_all("SELECT DISTINCT actor_id, actor_name FROM audit_log ORDER BY actor_name ASC;")

    return {
        "actions": [r["action"] for r in actions_rows],
        "object_types": [r["object_type"] for r in objects_rows],
        "actors": [{"id": r["actor_id"], "name": r["actor_name"]} for r in actors_rows],
    }


def export_audit_logs_csv(
    search_query: Optional[str] = None,
    action: Optional[str] = None,
    object_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 2000,
) -> str:
    """Generate RFC 4180 CSV export of filtered audit logs."""
    records, _ = query_audit_logs(
        search_query=search_query,
        action=action,
        object_type=object_type,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID",
        "Timestamp (Europe/Rome)",
        "Actor ID",
        "Actor Name",
        "Actor Role",
        "Action",
        "Object Type",
        "Object ID",
        "Details JSON",
        "Before JSON",
        "After JSON",
        "IP Address",
    ])

    for r in records:
        writer.writerow([
            r["id"],
            r["created_at"],
            r["actor_id"] or "",
            r["actor_name"] or "",
            r.get("actor_role") or "",
            r["action"],
            r["object_type"],
            r["object_id"],
            r["details_json"] or "",
            r["before_json"] or "",
            r["after_json"] or "",
            r["ip_address"] or "",
        ])

    return output.getvalue()


def export_audit_logs_json(
    search_query: Optional[str] = None,
    action: Optional[str] = None,
    object_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 2000,
) -> str:
    """Generate structured JSON export of filtered audit logs."""
    records, total = query_audit_logs(
        search_query=search_query,
        action=action,
        object_type=object_type,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    payload = {
        "app": "ForgeDesk",
        "exported_at": now_rome_iso(),
        "total_records": len(records),
        "total_matched": total,
        "records": records,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
