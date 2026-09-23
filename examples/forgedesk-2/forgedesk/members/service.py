"""Members, Qualifications, and Machine Category management services for ForgeDesk."""

import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from forgedesk.audit.service import record_audit_event
from forgedesk.db.connection import query_all, query_one, transaction
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso, parse_datetime

logger = logging.getLogger("forgedesk.members.service")

VALID_MEMBERSHIP_STATUSES = ("active", "suspended", "expired")


def validate_iso_date(date_str: Optional[str], field_name: str = "Date") -> Optional[str]:
    """Validate that a date string matches YYYY-MM-DD format."""
    if not date_str or not date_str.strip():
        return None
    clean_date = date_str.strip()
    try:
        datetime.datetime.strptime(clean_date, "%Y-%m-%d")
        return clean_date
    except ValueError:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format (got '{date_str}').")


def generate_next_member_number() -> str:
    """Generate a unique sequential member number in the format FD-MEM-XXXX."""
    row = query_one(
        """
        SELECT member_number FROM members
        WHERE member_number LIKE 'FD-MEM-%'
        ORDER BY id DESC LIMIT 1;
        """
    )
    next_seq = 1
    if row and row.get("member_number"):
        last_num = row["member_number"]
        match = re.search(r"FD-MEM-(\d+)", last_num)
        if match:
            try:
                next_seq = int(match.group(1)) + 1
            except ValueError:
                next_seq = 1

    # Verify uniqueness in a loop
    while True:
        candidate = f"FD-MEM-{next_seq:04d}"
        exists = query_one("SELECT id FROM members WHERE member_number = ?;", (candidate,))
        if not exists:
            return candidate
        next_seq += 1


# -----------------------------------------------------------------------------
# Member Query Operations
# -----------------------------------------------------------------------------

def list_members(
    status: Optional[str] = None,
    search: Optional[str] = None,
    order_by: str = "m.full_name ASC",
) -> List[Dict[str, Any]]:
    """List makerspace members with linked user details and qualification statistics."""
    query = """
        SELECT m.id, m.user_id, m.member_number, m.full_name, m.email, m.phone,
               m.membership_status, m.membership_expiry, m.notes,
               m.created_at, m.updated_at,
               u.username AS linked_username, u.role AS linked_user_role, u.is_active AS linked_user_active,
               COUNT(q.id) AS total_qualifications,
               SUM(CASE 
                   WHEN q.id IS NOT NULL 
                        AND (q.expiry_date IS NULL OR q.expiry_date >= date('now')) 
                        AND (q.issue_date <= date('now'))
                   THEN 1 ELSE 0 END) AS active_qualifications
        FROM members m
        LEFT JOIN users u ON u.id = m.user_id
        LEFT JOIN qualifications q ON q.member_id = m.id
        WHERE 1=1
    """
    params: List[Any] = []

    if status and status.strip() and status.strip().lower() in VALID_MEMBERSHIP_STATUSES:
        query += " AND m.membership_status = ?"
        params.append(status.strip().lower())

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += """ AND (
            LOWER(m.full_name) LIKE ? OR
            LOWER(m.email) LIKE ? OR
            LOWER(m.member_number) LIKE ? OR
            LOWER(COALESCE(m.phone, '')) LIKE ? OR
            LOWER(COALESCE(u.username, '')) LIKE ?
        )"""
        params.extend([term, term, term, term, term])

    query += f" GROUP BY m.id ORDER BY {order_by};"

    rows = query_all(query, tuple(params))
    now_str = now_rome_iso()[:10]

    result = []
    for r in rows:
        item = dict(r)
        # Compute effective validity
        expiry = item.get("membership_expiry")
        is_expired = bool(expiry and expiry < now_str)
        item["is_expired"] = is_expired
        item["is_active_and_valid"] = (item.get("membership_status") == "active" and not is_expired)
        result.append(item)

    return result


def get_member_by_id(member_id: int, include_qualifications: bool = True) -> Optional[Dict[str, Any]]:
    """Retrieve detailed member information, user account link, and qualification list."""
    row = query_one(
        """
        SELECT m.id, m.user_id, m.member_number, m.full_name, m.email, m.phone,
               m.membership_status, m.membership_expiry, m.notes,
               m.created_at, m.updated_at,
               u.username AS linked_username, u.email AS linked_user_email,
               u.role AS linked_user_role, u.is_active AS linked_user_active
        FROM members m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE m.id = ?;
        """,
        (member_id,),
    )
    if not row:
        return None

    member = dict(row)
    now_str = now_rome_iso()[:10]
    expiry = member.get("membership_expiry")
    is_expired = bool(expiry and expiry < now_str)
    member["is_expired"] = is_expired
    member["is_active_and_valid"] = (member.get("membership_status") == "active" and not is_expired)

    if include_qualifications:
        member["qualifications"] = list_member_qualifications(member_id)

    return member


def get_member_by_user_id(user_id: int, include_qualifications: bool = True) -> Optional[Dict[str, Any]]:
    """Retrieve member record associated with a system user ID."""
    row = query_one("SELECT id FROM members WHERE user_id = ?;", (user_id,))
    if not row:
        return None
    return get_member_by_id(row["id"], include_qualifications=include_qualifications)


def get_member_by_number(member_number: str) -> Optional[Dict[str, Any]]:
    """Retrieve member record by their unique member number."""
    if not member_number:
        return None
    row = query_one("SELECT id FROM members WHERE LOWER(member_number) = ?;", (member_number.strip().lower(),))
    if not row:
        return None
    return get_member_by_id(row["id"])


def get_unlinked_users(current_member_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Get list of users who are not yet linked to any member profile (or linked to current member)."""
    if current_member_user_id:
        rows = query_all(
            """
            SELECT u.id, u.username, u.full_name, u.email, u.role
            FROM users u
            LEFT JOIN members m ON m.user_id = u.id
            WHERE m.id IS NULL OR u.id = ?
            ORDER BY u.full_name ASC;
            """,
            (current_member_user_id,),
        )
    else:
        rows = query_all(
            """
            SELECT u.id, u.username, u.full_name, u.email, u.role
            FROM users u
            LEFT JOIN members m ON m.user_id = u.id
            WHERE m.id IS NULL
            ORDER BY u.full_name ASC;
            """
        )
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Member Mutation Operations
# -----------------------------------------------------------------------------

def create_member(
    full_name: str,
    email: str,
    phone: Optional[str] = None,
    user_id: Optional[int] = None,
    member_number: Optional[str] = None,
    membership_status: str = "active",
    membership_expiry: Optional[str] = None,
    notes: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
) -> int:
    """Create a new makerspace member record inside an atomic transaction."""
    name_clean = full_name.strip()
    email_clean = email.strip().lower()

    if not name_clean:
        raise ValueError("Member full name is required.")
    if not email_clean or "@" not in email_clean:
        raise ValueError("A valid email address is required.")

    status_clean = membership_status.strip().lower() if membership_status else "active"
    if status_clean not in VALID_MEMBERSHIP_STATUSES:
        raise ValueError(f"Invalid membership status '{membership_status}'. Allowed: {', '.join(VALID_MEMBERSHIP_STATUSES)}")

    expiry_clean = validate_iso_date(membership_expiry, "Membership Expiry Date")
    mem_no_clean = member_number.strip().upper() if member_number and member_number.strip() else generate_next_member_number()

    # Check member number uniqueness
    existing_no = query_one("SELECT id FROM members WHERE UPPER(member_number) = ?;", (mem_no_clean,))
    if existing_no:
        raise ValueError(f"Member number '{mem_no_clean}' is already in use by another member.")

    # Check user account linkage uniqueness
    if user_id:
        existing_user_link = query_one("SELECT id FROM members WHERE user_id = ?;", (user_id,))
        if existing_user_link:
            raise ValueError(f"User account ID {user_id} is already linked to member ID {existing_user_link['id']}.")

    now_iso = now_rome_iso()

    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO members (user_id, member_number, full_name, email, phone,
                                membership_status, membership_expiry, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                user_id,
                mem_no_clean,
                name_clean,
                email_clean,
                phone.strip() if phone else None,
                status_clean,
                expiry_clean,
                notes.strip() if notes else None,
                now_iso,
                now_iso,
            ),
        )
        new_member_id = cursor.lastrowid

        record_audit_event(
            action="member.created",
            object_type="member",
            object_id=new_member_id,
            actor=actor_user,
            after={
                "member_id": new_member_id,
                "member_number": mem_no_clean,
                "full_name": name_clean,
                "email": email_clean,
                "phone": phone,
                "membership_status": status_clean,
                "membership_expiry": expiry_clean,
                "user_id": user_id,
            },
            conn=conn,
        )

    logger.info("Member created: ID %d (%s, %s)", new_member_id, mem_no_clean, name_clean)
    return new_member_id


def update_member(
    member_id: int,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    user_id: Optional[Any] = None,  # Can be None or int or False to keep unchanged
    membership_status: Optional[str] = None,
    membership_expiry: Optional[str] = None,
    notes: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Update an existing member profile, recording before and after audit state."""
    current = get_member_by_id(member_id, include_qualifications=False)
    if not current:
        return False, f"Member with ID {member_id} does not exist."

    updates: Dict[str, Any] = {}
    before_state: Dict[str, Any] = {}

    if full_name is not None:
        name_clean = full_name.strip()
        if not name_clean:
            return False, "Full name cannot be empty."
        if name_clean != current["full_name"]:
            before_state["full_name"] = current["full_name"]
            updates["full_name"] = name_clean

    if email is not None:
        email_clean = email.strip().lower()
        if not email_clean or "@" not in email_clean:
            return False, "A valid email address is required."
        if email_clean != current["email"].lower():
            before_state["email"] = current["email"]
            updates["email"] = email_clean

    if phone is not None:
        phone_clean = phone.strip() if phone.strip() else None
        if phone_clean != current.get("phone"):
            before_state["phone"] = current.get("phone")
            updates["phone"] = phone_clean

    if membership_status is not None:
        status_clean = membership_status.strip().lower()
        if status_clean not in VALID_MEMBERSHIP_STATUSES:
            return False, f"Invalid status '{membership_status}'. Allowed: {', '.join(VALID_MEMBERSHIP_STATUSES)}"
        if status_clean != current["membership_status"]:
            before_state["membership_status"] = current["membership_status"]
            updates["membership_status"] = status_clean

    if membership_expiry is not None:
        try:
            expiry_clean = validate_iso_date(membership_expiry, "Membership Expiry Date")
        except ValueError as e:
            return False, str(e)
        if expiry_clean != current.get("membership_expiry"):
            before_state["membership_expiry"] = current.get("membership_expiry")
            updates["membership_expiry"] = expiry_clean

    if notes is not None:
        notes_clean = notes.strip() if notes.strip() else None
        if notes_clean != current.get("notes"):
            before_state["notes"] = current.get("notes")
            updates["notes"] = notes_clean

    if user_id is not False:  # None means unlink, int means link
        target_uid = int(user_id) if user_id else None
        if target_uid != current.get("user_id"):
            if target_uid:
                existing_link = query_one("SELECT id FROM members WHERE user_id = ? AND id != ?;", (target_uid, member_id))
                if existing_link:
                    return False, f"User ID {target_uid} is already linked to another member (ID {existing_link['id']})."
            before_state["user_id"] = current.get("user_id")
            updates["user_id"] = target_uid

    if not updates:
        return True, "No changes detected."

    now_iso = now_rome_iso()
    updates["updated_at"] = now_iso

    set_clauses = [f"{col} = ?" for col in updates.keys()]
    values = list(updates.values()) + [member_id]

    with transaction() as conn:
        conn.execute(f"UPDATE members SET {', '.join(set_clauses)} WHERE id = ?;", tuple(values))

        record_audit_event(
            action="member.updated",
            object_type="member",
            object_id=member_id,
            actor=actor_user,
            before=before_state,
            after=updates,
            conn=conn,
        )

    logger.info("Member updated: ID %d changes: %s", member_id, list(updates.keys()))
    return True, "Member profile successfully updated."


def change_membership_status(
    member_id: int,
    new_status: str,
    actor_user: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Tuple[bool, str]:
    """Change the membership status of a member (active, suspended, expired)."""
    current = get_member_by_id(member_id, include_qualifications=False)
    if not current:
        return False, f"Member with ID {member_id} does not exist."

    clean_status = new_status.strip().lower()
    if clean_status not in VALID_MEMBERSHIP_STATUSES:
        return False, f"Invalid membership status '{new_status}'. Allowed: {', '.join(VALID_MEMBERSHIP_STATUSES)}"

    if current["membership_status"] == clean_status:
        return True, f"Member status is already '{clean_status}'."

    now_iso = now_rome_iso()
    with transaction() as conn:
        conn.execute(
            "UPDATE members SET membership_status = ?, updated_at = ? WHERE id = ?;",
            (clean_status, now_iso, member_id),
        )

        record_audit_event(
            action="member.status_changed",
            object_type="member",
            object_id=member_id,
            actor=actor_user,
            before={"membership_status": current["membership_status"]},
            after={"membership_status": clean_status},
            details={"notes": notes} if notes else None,
            conn=conn,
        )

    logger.info("Member %d status changed from %s to %s", member_id, current["membership_status"], clean_status)
    return True, f"Member status successfully changed to '{clean_status}'."


def delete_member(member_id: int, actor_user: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Delete a member record if no reservations/charges are linked, or suspend them if history exists."""
    current = get_member_by_id(member_id, include_qualifications=False)
    if not current:
        return False, f"Member with ID {member_id} does not exist."

    # Check for linked historical records
    res_count_row = query_one("SELECT COUNT(*) AS c FROM reservations WHERE member_id = ?;", (member_id,))
    res_count = res_count_row["c"] if res_count_row else 0

    charges_count_row = query_one("SELECT COUNT(*) AS c FROM usage_charges WHERE member_id = ?;", (member_id,))
    charges_count = charges_count_row["c"] if charges_count_row else 0

    if res_count > 0 or charges_count > 0:
        # Prevent hard deletion to maintain audit integrity; soft-suspend instead
        change_membership_status(member_id, "suspended", actor_user=actor_user, notes="Auto-suspended due to deletion attempt on active record history.")
        return False, f"Cannot delete member with existing reservation ({res_count}) or billing ({charges_count}) history. Member has been suspended instead."

    with transaction() as conn:
        # Delete qualifications first
        conn.execute("DELETE FROM qualifications WHERE member_id = ?;", (member_id,))
        conn.execute("DELETE FROM members WHERE id = ?;", (member_id,))

        record_audit_event(
            action="member.deleted",
            object_type="member",
            object_id=member_id,
            actor=actor_user,
            before=current,
            conn=conn,
        )

    logger.info("Member ID %d (%s) deleted.", member_id, current["member_number"])
    return True, f"Member {current['member_number']} successfully deleted."


# -----------------------------------------------------------------------------
# Machine Categories Operations
# -----------------------------------------------------------------------------

def list_machine_categories() -> List[Dict[str, Any]]:
    """List all defined machine categories with current machine counts."""
    rows = query_all(
        """
        SELECT mc.id, mc.code, mc.name, mc.description, mc.created_at,
               COUNT(m.id) AS machine_count
        FROM machine_categories mc
        LEFT JOIN machines m ON m.category_id = mc.id AND m.state != 'retired'
        GROUP BY mc.id
        ORDER BY mc.name ASC;
        """
    )
    return [dict(r) for r in rows]


def get_machine_category_by_id(category_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve machine category by its ID."""
    row = query_one("SELECT id, code, name, description, created_at FROM machine_categories WHERE id = ?;", (category_id,))
    return dict(row) if row else None


def get_machine_category_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Retrieve machine category by unique category code."""
    if not code:
        return None
    row = query_one("SELECT id, code, name, description, created_at FROM machine_categories WHERE LOWER(code) = ?;", (code.strip().lower(),))
    return dict(row) if row else None


def create_machine_category(
    code: str,
    name: str,
    description: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
) -> int:
    """Create a new machine category record."""
    clean_code = code.strip().lower()
    clean_name = name.strip()

    if not clean_code or not clean_name:
        raise ValueError("Category code and name are required.")

    existing = query_one("SELECT id FROM machine_categories WHERE code = ?;", (clean_code,))
    if existing:
        raise ValueError(f"Category with code '{clean_code}' already exists.")

    now_iso = now_rome_iso()
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO machine_categories (code, name, description, created_at) VALUES (?, ?, ?, ?);",
            (clean_code, clean_name, description.strip() if description else None, now_iso),
        )
        cat_id = cursor.lastrowid

        record_audit_event(
            action="machine_category.created",
            object_type="machine_category",
            object_id=cat_id,
            actor=actor_user,
            after={"code": clean_code, "name": clean_name, "description": description},
            conn=conn,
        )

    return cat_id


# -----------------------------------------------------------------------------
# Qualifications Management
# -----------------------------------------------------------------------------

def list_member_qualifications(member_id: int) -> List[Dict[str, Any]]:
    """List all qualifications assigned to a member, computing live validity."""
    rows = query_all(
        """
        SELECT q.id, q.member_id, q.category_id, q.qualification_name,
               q.issue_date, q.expiry_date, q.verified_by_user_id, q.notes, q.created_at,
               mc.code AS category_code, mc.name AS category_name,
               u.full_name AS verified_by_name, u.username AS verified_by_username
        FROM qualifications q
        JOIN machine_categories mc ON mc.id = q.category_id
        LEFT JOIN users u ON u.id = q.verified_by_user_id
        WHERE q.member_id = ?
        ORDER BY mc.name ASC, q.issue_date DESC;
        """,
        (member_id,),
    )

    now_str = now_rome_iso()[:10]
    result = []
    for r in rows:
        item = dict(r)
        issue = item["issue_date"]
        expiry = item.get("expiry_date")

        if issue > now_str:
            item["status"] = "pending_start"
            item["is_valid"] = False
        elif expiry and expiry < now_str:
            item["status"] = "expired"
            item["is_valid"] = False
        else:
            item["status"] = "valid"
            item["is_valid"] = True

        result.append(item)

    return result


def get_qualification_by_id(qualification_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a single qualification by ID."""
    row = query_one(
        """
        SELECT q.id, q.member_id, q.category_id, q.qualification_name,
               q.issue_date, q.expiry_date, q.verified_by_user_id, q.notes, q.created_at,
               mc.code AS category_code, mc.name AS category_name,
               m.full_name AS member_name, m.member_number,
               u.full_name AS verified_by_name
        FROM qualifications q
        JOIN machine_categories mc ON mc.id = q.category_id
        JOIN members m ON m.id = q.member_id
        LEFT JOIN users u ON u.id = q.verified_by_user_id
        WHERE q.id = ?;
        """,
        (qualification_id,),
    )
    if not row:
        return None

    item = dict(row)
    now_str = now_rome_iso()[:10]
    issue = item["issue_date"]
    expiry = item.get("expiry_date")
    if issue > now_str:
        item["status"] = "pending_start"
        item["is_valid"] = False
    elif expiry and expiry < now_str:
        item["status"] = "expired"
        item["is_valid"] = False
    else:
        item["status"] = "valid"
        item["is_valid"] = True
    return item


def grant_qualification(
    member_id: int,
    category_id: int,
    qualification_name: str,
    issue_date: str,
    expiry_date: Optional[str] = None,
    verified_by_user_id: Optional[int] = None,
    notes: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
) -> int:
    """Grant or recertify a machine qualification for a member."""
    # Verify member and category exist
    member = get_member_by_id(member_id, include_qualifications=False)
    if not member:
        raise ValueError(f"Member ID {member_id} does not exist.")

    category = get_machine_category_by_id(category_id)
    if not category:
        raise ValueError(f"Machine Category ID {category_id} does not exist.")

    qual_name_clean = qualification_name.strip()
    if not qual_name_clean:
        qual_name_clean = f"{category['name']} Certified Operator"

    issue_clean = validate_iso_date(issue_date, "Qualification Issue Date")
    if not issue_clean:
        issue_clean = now_rome_iso()[:10]

    expiry_clean = validate_iso_date(expiry_date, "Qualification Expiry Date")
    if expiry_clean and expiry_clean < issue_clean:
        raise ValueError(f"Expiry date ({expiry_clean}) cannot precede issue date ({issue_clean}).")

    verifier_id = verified_by_user_id
    if verifier_id is None and actor_user:
        verifier_id = actor_user.get("id")

    now_iso = now_rome_iso()

    with transaction() as conn:
        # Check if qualification for (member_id, category_id) already exists
        existing = conn.execute(
            "SELECT id, qualification_name, issue_date, expiry_date, notes FROM qualifications WHERE member_id = ? AND category_id = ?;",
            (member_id, category_id),
        ).fetchone()

        if existing:
            # Update/recertify existing qualification
            qid = existing["id"]
            conn.execute(
                """
                UPDATE qualifications
                SET qualification_name = ?, issue_date = ?, expiry_date = ?,
                    verified_by_user_id = ?, notes = ?
                WHERE id = ?;
                """,
                (qual_name_clean, issue_clean, expiry_clean, verifier_id, notes.strip() if notes else None, qid),
            )
            record_audit_event(
                action="qualification.recertified",
                object_type="qualification",
                object_id=qid,
                actor=actor_user,
                before=dict(existing),
                after={
                    "qualification_name": qual_name_clean,
                    "issue_date": issue_clean,
                    "expiry_date": expiry_clean,
                    "verified_by_user_id": verifier_id,
                    "notes": notes,
                },
                conn=conn,
            )
            logger.info("Qualification %d recertified for member %d in category %d", qid, member_id, category_id)
            return qid
        else:
            # Insert new qualification
            cursor = conn.execute(
                """
                INSERT INTO qualifications (member_id, category_id, qualification_name,
                                           issue_date, expiry_date, verified_by_user_id, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    member_id,
                    category_id,
                    qual_name_clean,
                    issue_clean,
                    expiry_clean,
                    verifier_id,
                    notes.strip() if notes else None,
                    now_iso,
                ),
            )
            qid = cursor.lastrowid
            record_audit_event(
                action="qualification.granted",
                object_type="qualification",
                object_id=qid,
                actor=actor_user,
                after={
                    "member_id": member_id,
                    "category_id": category_id,
                    "category_code": category["code"],
                    "qualification_name": qual_name_clean,
                    "issue_date": issue_clean,
                    "expiry_date": expiry_clean,
                    "verified_by_user_id": verifier_id,
                },
                conn=conn,
            )
            logger.info("Qualification granted: ID %d to member %d in category %d", qid, member_id, category_id)
            return qid


def revoke_qualification(
    qualification_id: int,
    reason: Optional[str] = None,
    actor_user: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Revoke a member's qualification with audit tracking."""
    current = get_qualification_by_id(qualification_id)
    if not current:
        return False, f"Qualification ID {qualification_id} does not exist."

    with transaction() as conn:
        conn.execute("DELETE FROM qualifications WHERE id = ?;", (qualification_id,))
        record_audit_event(
            action="qualification.revoked",
            object_type="qualification",
            object_id=qualification_id,
            actor=actor_user,
            before=current,
            details={"reason": reason} if reason else None,
            conn=conn,
        )

    logger.info("Qualification ID %d revoked for member %d (%s)", qualification_id, current["member_id"], current["qualification_name"])
    return True, f"Qualification '{current['qualification_name']}' successfully revoked."


# -----------------------------------------------------------------------------
# Time-Aware Qualification & Eligibility Verification
# -----------------------------------------------------------------------------

def check_member_qualification(
    member_id: int,
    required_category_id: Optional[int],
    target_datetime: Optional[Any] = None,
) -> Dict[str, Any]:
    """Verify if a member is authorized to reserve or operate a machine in a category at a specific target datetime.
    
    Brief Contract:
    - Member must be active and membership not expired at target_datetime.
    - If machine requires qualification (required_category_id is not None), member must possess a valid
      qualification record covering target_datetime (issue_date <= target_date <= expiry_date).
    - Accurately identifies qualifications valid at reservation creation time that expire before check-in time.
    """
    # Parse target date
    target_dt: datetime.datetime
    if target_datetime is None:
        target_dt = now_rome()
    elif isinstance(target_datetime, datetime.datetime):
        target_dt = target_datetime
    elif isinstance(target_datetime, str):
        try:
            target_dt = parse_datetime(target_datetime)
        except Exception:
            try:
                target_dt = datetime.datetime.strptime(target_datetime[:10], "%Y-%m-%d")
            except Exception:
                target_dt = now_rome()
    else:
        target_dt = now_rome()

    target_date_str = target_dt.strftime("%Y-%m-%d")
    target_time_str = target_dt.strftime("%Y-%m-%d %H:%M")

    # 1. Fetch member
    member = get_member_by_id(member_id, include_qualifications=False)
    if not member:
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Member record #{member_id} not found.",
            "member_id": member_id,
            "membership_status": "not_found",
            "membership_valid": False,
            "qualification": None,
        }

    # 2. Check membership status
    if member["membership_status"] != "active":
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Membership is currently {member['membership_status'].upper()} for {member['full_name']} ({member['member_number']}).",
            "member_id": member_id,
            "membership_status": member["membership_status"],
            "membership_valid": False,
            "qualification": None,
        }

    # 3. Check membership expiry date against target date
    if member.get("membership_expiry") and member["membership_expiry"] < target_date_str:
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Membership for {member['full_name']} expires on {member['membership_expiry']}, which is before the scheduled operation date {target_date_str}.",
            "member_id": member_id,
            "membership_status": "expired",
            "membership_valid": False,
            "qualification": None,
        }

    # 4. Check machine category qualification requirement
    if required_category_id is None:
        # No qualification needed for this machine
        return {
            "eligible": True,
            "qualified": True,
            "reason": "Machine requires no specific qualification.",
            "member_id": member_id,
            "membership_status": member["membership_status"],
            "membership_valid": True,
            "qualification": None,
        }

    # Fetch qualification for this member and required category
    qual_row = query_one(
        """
        SELECT q.id, q.qualification_name, q.issue_date, q.expiry_date,
               mc.id AS category_id, mc.name AS category_name, mc.code AS category_code
        FROM qualifications q
        JOIN machine_categories mc ON mc.id = q.category_id
        WHERE q.member_id = ? AND q.category_id = ?;
        """,
        (member_id, required_category_id),
    )

    if not qual_row:
        cat_info = get_machine_category_by_id(required_category_id)
        cat_name = cat_info["name"] if cat_info else f"Category #{required_category_id}"
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Member {member['full_name']} has no qualification record for machine category '{cat_name}'.",
            "member_id": member_id,
            "membership_status": member["membership_status"],
            "membership_valid": True,
            "qualification": None,
        }

    qual = dict(qual_row)

    # Check issue date
    if qual["issue_date"] > target_date_str:
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Qualification '{qual['qualification_name']}' is not yet effective (issue date is {qual['issue_date']}, target is {target_date_str}).",
            "member_id": member_id,
            "membership_status": member["membership_status"],
            "membership_valid": True,
            "qualification": qual,
        }

    # Check expiry date
    if qual.get("expiry_date") and qual["expiry_date"] < target_date_str:
        return {
            "eligible": False,
            "qualified": False,
            "reason": f"Qualification '{qual['qualification_name']}' expired on {qual['expiry_date']} (prior to scheduled time {target_time_str}). Recertification required.",
            "member_id": member_id,
            "membership_status": member["membership_status"],
            "membership_valid": True,
            "qualification": qual,
        }

    # All checks passed
    return {
        "eligible": True,
        "qualified": True,
        "reason": f"Qualification '{qual['qualification_name']}' is valid for scheduled time {target_time_str}.",
        "member_id": member_id,
        "membership_status": member["membership_status"],
        "membership_valid": True,
        "qualification": qual,
    }
