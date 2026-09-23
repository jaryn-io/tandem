"""Comprehensive unit and integration tests for Machines, Operating Hours, and Maintenance Windows."""

import datetime
import json
import unittest
from typing import Any, Dict, Optional

from app import create_application_router
from forgedesk.audit.service import query_audit_logs
from forgedesk.auth.middleware import COOKIE_SESSION_NAME
from forgedesk.auth.permissions import ROLE_ADMIN, ROLE_MEMBER, ROLE_OPERATOR, ROLE_VIEWER
from forgedesk.auth.service import authenticate_user, create_user_session
from forgedesk.config import UPLOAD_DIR
from forgedesk.core.http import Request, Response, UploadedFile
from forgedesk.db import init_db, reset_database, seed_database
from forgedesk.db.connection import get_connection, query_all, query_one, transaction
from forgedesk.machines.service import (
    VALID_INCIDENT_SEVERITIES,
    VALID_INCIDENT_STATUSES,
    VALID_MACHINE_STATES,
    VALID_MAINTENANCE_JOB_STATUSES,
    VALID_MAINTENANCE_PRIORITIES,
    VALID_MAINTENANCE_STATUSES,
    add_incident_attachment,
    change_incident_status,
    change_machine_state,
    change_maintenance_job_status,
    change_maintenance_window_status,
    check_machine_availability,
    create_incident,
    create_machine,
    create_maintenance_job,
    create_maintenance_window,
    delete_incident,
    delete_incident_attachment,
    delete_machine,
    delete_maintenance_job,
    delete_maintenance_window,
    get_incident_attachment_by_id,
    get_incident_by_id,
    get_machine_by_code,
    get_machine_by_id,
    get_machines_summary_stats,
    get_maintenance_job_by_id,
    get_maintenance_window_by_id,
    list_incident_attachments,
    list_incidents,
    list_machines,
    list_maintenance_jobs,
    list_maintenance_windows,
    retire_machine,
    update_incident,
    update_machine,
    update_maintenance_job,
    update_maintenance_window,
)
from forgedesk.members.service import (
    create_machine_category,
    list_machine_categories,
)
from forgedesk.utils.datetime_tz import now_rome, now_rome_iso


class TestMachinesDomain(unittest.TestCase):
    """Test suite for machines, operating hours, categories, and maintenance windows."""

    @classmethod
    def setUpClass(cls):
        """Set up database with migrations and seeds."""
        reset_database()
        init_db(seed_if_empty=False)
        seed_database(force=True)

    def setUp(self):
        """Configure test application router and demo users."""
        self.router = create_application_router()

        # Cache demo users
        self.admin_user = authenticate_user("admin", "admin123")
        self.operator_user = authenticate_user("operator", "operator123")
        self.member_user = authenticate_user("member_alice", "member123")
        self.viewer_user = authenticate_user("viewer", "viewer123")

        # Create sessions
        self.admin_session = create_user_session(self.admin_user["id"])
        self.operator_session = create_user_session(self.operator_user["id"])
        self.member_session = create_user_session(self.member_user["id"])
        self.viewer_session = create_user_session(self.viewer_user["id"])

    def _make_request(
        self,
        method: str,
        path: str,
        session_token: str = None,
        bearer_token: str = None,
        json_data: dict = None,
        form_data: dict = None,
        headers: dict = None,
    ) -> Response:
        """Helper to construct and dispatch HTTP requests through Router."""
        req_headers = headers.copy() if headers else {}
        body_bytes = b""

        if bearer_token:
            req_headers["Authorization"] = f"Bearer {bearer_token}"
        elif session_token:
            req_headers["Cookie"] = f"{COOKIE_SESSION_NAME}={session_token}"

        if json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
            req_headers["Content-Length"] = str(len(body_bytes))
        elif form_data is not None:
            from urllib.parse import urlencode
            encoded = urlencode(form_data)
            body_bytes = encoded.encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
            req_headers["Content-Length"] = str(len(body_bytes))

        req = Request(
            method=method,
            path=path,
            headers=req_headers,
            body=body_bytes,
        )
        return self.router.dispatch(req)

    def test_01_list_and_query_machines(self):
        """Test listing machines, filtering by category and state, and searching."""
        machines = list_machines()
        self.assertGreaterEqual(len(machines), 6)

        # Check seeded machines exist
        codes = [m["code"] for m in machines]
        self.assertIn("PRUSA-MK4-01", codes)
        self.assertIn("EPILOG-FUSION-PRO", codes)
        self.assertIn("HAAS-MINI-MILL", codes)
        self.assertIn("ROLAND-MDX-40", codes)

        # Filter by state
        avail_machines = list_machines(state="available")
        for m in avail_machines:
            self.assertEqual(m["state"], "available")

        maint_machines = list_machines(state="under_maintenance")
        self.assertTrue(any(m["code"] == "HAAS-MINI-MILL" for m in maint_machines))

        retired_machines = list_machines(state="retired")
        self.assertTrue(any(m["code"] == "ROLAND-MDX-40" for m in retired_machines))

        # Filter excluding retired
        active_fleet = list_machines(include_retired=False)
        self.assertFalse(any(m["code"] == "ROLAND-MDX-40" for m in active_fleet))

        # Search query
        search_res = list_machines(search="Epilog")
        self.assertEqual(len(search_res), 1)
        self.assertEqual(search_res[0]["code"], "EPILOG-FUSION-PRO")

        # Summary statistics
        stats = get_machines_summary_stats()
        self.assertGreaterEqual(stats["total"], 6)
        self.assertGreaterEqual(stats["available"], 4)
        self.assertGreaterEqual(stats["under_maintenance"], 1)
        self.assertGreaterEqual(stats["retired"], 1)

    def test_02_create_machine_success_and_validations(self):
        """Test machine creation with validation rules, operating hours, and rates."""
        categories = list_machine_categories()
        cat_3d = next(c for c in categories if c["code"] == "3d_printers")
        cat_laser = next(c for c in categories if c["code"] == "laser_cutters")

        # 1. Valid Creation
        m = create_machine(
            code="RESIN-SATURN-01",
            name="Elegoo Saturn 4 Ultra SLA 3D Printer",
            category_id=cat_3d["id"],
            required_qualification_category_id=cat_3d["id"],
            capacity=1,
            state="available",
            operating_hours_start="09:00",
            operating_hours_end="21:00",
            hourly_rate_cents=450,  # €4.50
            minimum_charge_cents=200,  # €2.00
            peak_hourly_rate_cents=600,  # €6.00
            peak_hours_start="18:00",
            peak_hours_end="21:00",
            location="Resin Room Lab 3",
            description="12K mono LCD SLA resin printer for high resolution miniatures.",
            actor=self.admin_user,
        )

        self.assertIsNotNone(m["id"])
        self.assertEqual(m["code"], "RESIN-SATURN-01")
        self.assertEqual(m["hourly_rate_cents"], 450)
        self.assertEqual(m["minimum_charge_cents"], 200)
        self.assertEqual(m["peak_hourly_rate_cents"], 600)
        self.assertEqual(m["operating_hours_start"], "09:00")
        self.assertEqual(m["operating_hours_end"], "21:00")

        # 2. Duplicate Code validation
        with self.assertRaises(ValueError) as ctx:
            create_machine(
                code="RESIN-SATURN-01",
                name="Duplicate Machine",
                category_id=cat_3d["id"],
            )
        self.assertIn("already exists", str(ctx.exception))

        # 3. Invalid operating hours (start >= end)
        with self.assertRaises(ValueError) as ctx:
            create_machine(
                code="BAD-HOURS-01",
                name="Bad Hours Machine",
                category_id=cat_3d["id"],
                operating_hours_start="22:00",
                operating_hours_end="08:00",
            )
        self.assertIn("strictly earlier", str(ctx.exception))

        # 4. Invalid capacity
        with self.assertRaises(ValueError) as ctx:
            create_machine(
                code="BAD-CAP-01",
                name="Bad Cap Machine",
                category_id=cat_3d["id"],
                capacity=0,
            )
        self.assertIn("at least 1", str(ctx.exception))

        # 5. Invalid State
        with self.assertRaises(ValueError) as ctx:
            create_machine(
                code="BAD-STATE-01",
                name="Bad State Machine",
                category_id=cat_3d["id"],
                state="exploded",
            )
        self.assertIn("Invalid machine state", str(ctx.exception))

    def test_03_update_machine_and_state_transitions(self):
        """Test updating machine specifications and operational state changes."""
        m = get_machine_by_code("PRUSA-MK4-01")
        self.assertIsNotNone(m)

        # Update rates and location
        updated = update_machine(
            machine_id=m["id"],
            name="Prusa MK4 FDM #1 (Upgraded Nozzle)",
            hourly_rate_cents=250,
            location="3D Print Lab, Bench A1-Modified",
            actor=self.operator_user,
        )
        self.assertEqual(updated["name"], "Prusa MK4 FDM #1 (Upgraded Nozzle)")
        self.assertEqual(updated["hourly_rate_cents"], 250)
        self.assertEqual(updated["location"], "3D Print Lab, Bench A1-Modified")

        # State transition to temporarily_unavailable
        changed = change_machine_state(
            machine_id=m["id"],
            new_state="temporarily_unavailable",
            reason="Awaiting Nextruder 0.4mm brass nozzle replacement",
            actor=self.operator_user,
        )
        self.assertEqual(changed["state"], "temporarily_unavailable")

        # Restore to available
        restored = change_machine_state(
            machine_id=m["id"],
            new_state="available",
            reason="Nozzle replaced and test print calibrated",
            actor=self.operator_user,
        )
        self.assertEqual(restored["state"], "available")

    def test_04_machine_retirement_and_deletion_preservation(self):
        """Test that machines with historical reservations cannot be deleted, but can be retired."""
        # 1. Attempt to delete Prusa MK4 (which has historical reservations in seed)
        prusa = get_machine_by_code("PRUSA-MK4-01")
        self.assertIsNotNone(prusa)

        with self.assertRaises(ValueError) as ctx:
            delete_machine(prusa["id"], actor=self.admin_user)
        self.assertIn("historical reservation", str(ctx.exception))
        self.assertIn("retire", str(ctx.exception))

        # 2. Retire Prusa MK4
        retired = retire_machine(
            machine_id=prusa["id"],
            reason="Decommissioned for next-gen printer rollout",
            actor=self.admin_user,
        )
        self.assertEqual(retired["state"], "retired")

        # Verify historical reservations remain intact
        res_count = query_one("SELECT COUNT(*) AS c FROM reservations WHERE machine_id = ?;", (prusa["id"],))["c"]
        self.assertGreater(res_count, 0)

        # Restore Prusa MK4 to available for other tests
        change_machine_state(prusa["id"], "available", actor=self.admin_user)

        # 3. Create a temporary machine with NO reservations and delete it
        cats = list_machine_categories()
        temp_m = create_machine(
            code="TEMP-DRILL-01",
            name="Temporary Benchtop Pillar Drill",
            category_id=cats[0]["id"],
            actor=self.admin_user,
        )
        self.assertIsNotNone(temp_m["id"])

        deleted = delete_machine(temp_m["id"], actor=self.admin_user)
        self.assertTrue(deleted)
        self.assertIsNone(get_machine_by_id(temp_m["id"]))

    def test_05_maintenance_windows_lifecycle(self):
        """Test scheduling, transitioning, and closing maintenance windows with machine state sync."""
        categories = list_machine_categories()
        bambu = get_machine_by_code("BAMBU-X1C-01")
        self.assertIsNotNone(bambu)

        now = now_rome()
        win_start = (now + datetime.timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
        win_end = win_start + datetime.timedelta(hours=3)

        # 1. Create scheduled maintenance window
        mw = create_maintenance_window(
            machine_id=bambu["id"],
            title="Carbon Rod Cleaning & Extruder Inspection",
            start_time=win_start.isoformat(),
            end_time=win_end.isoformat(),
            status="scheduled",
            created_by_user_id=self.operator_user["id"],
            notes="Use isopropyl alcohol on carbon rails",
            actor=self.operator_user,
        )
        self.assertIsNotNone(mw["id"])
        self.assertEqual(mw["status"], "scheduled")
        self.assertEqual(mw["machine_code"], "BAMBU-X1C-01")

        # Machine is still available while window is in 'scheduled'
        bambu_state = get_machine_by_id(bambu["id"])["state"]
        self.assertEqual(bambu_state, "available")

        # 2. Transition maintenance window to 'in_progress' -> automatically sets machine state to 'under_maintenance'
        in_prog_mw = change_maintenance_window_status(
            window_id=mw["id"],
            new_status="in_progress",
            notes="Maintenance started by operator",
            actor=self.operator_user,
        )
        self.assertEqual(in_prog_mw["status"], "in_progress")
        bambu_state = get_machine_by_id(bambu["id"])["state"]
        self.assertEqual(bambu_state, "under_maintenance")

        # 3. Transition maintenance window to 'completed' -> automatically restores machine state to 'available'
        comp_mw = change_maintenance_window_status(
            window_id=mw["id"],
            new_status="completed",
            notes="Maintenance completed successfully",
            actor=self.operator_user,
        )
        self.assertEqual(comp_mw["status"], "completed")
        bambu_state = get_machine_by_id(bambu["id"])["state"]
        self.assertEqual(bambu_state, "available")

        # 4. Delete maintenance window
        del_res = delete_maintenance_window(mw["id"], actor=self.admin_user)
        self.assertTrue(del_res)
        self.assertIsNone(get_maintenance_window_by_id(mw["id"]))

    def test_06_check_machine_availability_engine(self):
        """Test machine availability, operating hours validation, and conflict detection."""
        epilog = get_machine_by_code("EPILOG-FUSION-PRO")
        self.assertIsNotNone(epilog)

        now = now_rome()
        target_day = (now + datetime.timedelta(days=3))

        # A. Normal valid operating hours (10:00 - 12:00 within 08:00 - 22:00)
        valid_start = target_day.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
        valid_end = target_day.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        avail, msg, conflicts = check_machine_availability(epilog["id"], valid_start, valid_end)
        self.assertTrue(avail)
        self.assertEqual(len(conflicts), 0)

        # B. Outside daily operating hours (06:00 - 07:30 before 08:00)
        early_start = target_day.replace(hour=6, minute=0, second=0, microsecond=0).isoformat()
        early_end = target_day.replace(hour=7, minute=30, second=0, microsecond=0).isoformat()
        avail_early, msg_early, conflicts_early = check_machine_availability(epilog["id"], early_start, early_end)
        self.assertFalse(avail_early)
        self.assertIn("operating hours", msg_early.lower())

        # C. Conflict with Maintenance Window
        maint_start = target_day.replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
        maint_end = target_day.replace(hour=17, minute=0, second=0, microsecond=0).isoformat()
        mw = create_maintenance_window(
            machine_id=epilog["id"],
            title="Laser Optics Alignment",
            start_time=maint_start,
            end_time=maint_end,
            status="scheduled",
            actor=self.operator_user,
        )

        # Request overlapping 15:00 - 16:00
        req_start = target_day.replace(hour=15, minute=0, second=0, microsecond=0).isoformat()
        req_end = target_day.replace(hour=16, minute=0, second=0, microsecond=0).isoformat()
        avail_maint, msg_maint, conflicts_maint = check_machine_availability(epilog["id"], req_start, req_end)
        self.assertFalse(avail_maint)
        self.assertIn("scheduled maintenance", msg_maint.lower())
        self.assertEqual(len(conflicts_maint), 1)
        self.assertEqual(conflicts_maint[0]["id"], mw["id"])

        # Clean up window
        delete_maintenance_window(mw["id"])

        # D. Retired machine
        roland = get_machine_by_code("ROLAND-MDX-40")
        avail_ret, msg_ret, conflicts_ret = check_machine_availability(roland["id"], valid_start, valid_end)
        self.assertFalse(avail_ret)
        self.assertIn("retired", msg_ret.lower())

    def test_07_rbac_enforcement_on_api_and_html(self):
        """Test RBAC: Admins & Operators can manage, Members & Viewers are read-only / blocked with 403."""
        cats = list_machine_categories()

        # 1. Viewer can view /api/machines
        resp_viewer_get = self._make_request("GET", "/api/machines", bearer_token=self.viewer_session)
        self.assertEqual(resp_viewer_get.status_code, 200)

        # 2. Member can view /api/machines/{id}
        m = get_machine_by_code("PRUSA-MK4-01")
        resp_mem_get = self._make_request("GET", f"/api/machines/{m['id']}", bearer_token=self.member_session)
        self.assertEqual(resp_mem_get.status_code, 200)

        # 3. Member attempts to create machine via POST /api/machines -> 403 Forbidden
        resp_mem_post = self._make_request(
            "POST",
            "/api/machines",
            bearer_token=self.member_session,
            json_data={
                "code": "ILLEGAL-MACHINE",
                "name": "Member Unauthorized Machine",
                "category_id": cats[0]["id"],
            },
        )
        self.assertEqual(resp_mem_post.status_code, 403)

        # 4. Viewer attempts to change machine state -> 403 Forbidden
        resp_viewer_patch = self._make_request(
            "PATCH",
            f"/api/machines/{m['id']}/state",
            bearer_token=self.viewer_session,
            json_data={"state": "retired"},
        )
        self.assertEqual(resp_viewer_patch.status_code, 403)

        # 5. Operator creates machine via POST /api/machines -> 201 Created
        resp_op_post = self._make_request(
            "POST",
            "/api/machines",
            bearer_token=self.operator_session,
            json_data={
                "code": "OPERATOR-SAW-01",
                "name": "Operator Band Saw",
                "category_id": cats[3]["id"],  # woodworking
                "capacity": 1,
                "operating_hours_start": "08:30",
                "operating_hours_end": "20:00",
            },
        )
        self.assertEqual(resp_op_post.status_code, 201)
        created_id = json.loads(resp_op_post.body.decode("utf-8"))["machine"]["id"]

        # 6. Admin updates created machine -> 200 OK
        resp_admin_put = self._make_request(
            "PUT",
            f"/api/machines/{created_id}",
            bearer_token=self.admin_session,
            json_data={"name": "Operator Band Saw (Pro Model)"},
        )
        self.assertEqual(resp_admin_put.status_code, 200)

        # 7. Admin deletes created machine -> 200 OK
        resp_admin_del = self._make_request("DELETE", f"/api/machines/{created_id}", bearer_token=self.admin_session)
        self.assertEqual(resp_admin_del.status_code, 200)

    def test_08_audit_trail_logging(self):
        """Verify that all machine and maintenance mutations create append-only audit events."""
        events, total = query_audit_logs(object_type="machine", limit=20)
        self.assertGreater(len(events), 0)

        actions = [e["action"] for e in events]
        self.assertTrue(
            any(a in ("machine.created", "machine.updated", "machine.state_changed") for a in actions),
            f"Expected machine audit events in: {actions}",
        )

    def test_09_maintenance_jobs_lifecycle(self):
        """Verify maintenance jobs creation, assignment, priority, status workflow, and audit trail."""
        machines = list_machines()
        self.assertGreater(len(machines), 0)
        target_m = machines[0]

        # 1. Create a maintenance job via service
        job = create_maintenance_job(
            machine_id=target_m["id"],
            title="Calibrate Z-axis Lead Screws",
            description="Perform precision dial indicator calibration on dual Z lead screws.",
            priority="high",
            status="open",
            assigned_to_user_id=self.operator_user["id"],
            opened_by_user_id=self.admin_user["id"],
            scheduled_date="2026-09-15",
            notes="Initial inspection note.",
            actor=self.admin_user,
        )
        self.assertIsNotNone(job["id"])
        self.assertEqual(job["title"], "Calibrate Z-axis Lead Screws")
        self.assertEqual(job["priority"], "high")
        self.assertEqual(job["status"], "open")
        self.assertEqual(job["assigned_to_user_id"], self.operator_user["id"])
        self.assertIsNone(job.get("completed_at"))

        # 2. Retrieve job by ID
        fetched = get_maintenance_job_by_id(job["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["title"], "Calibrate Z-axis Lead Screws")
        self.assertEqual(fetched["machine_code"], target_m["code"])

        # 3. Update job details
        updated = update_maintenance_job(
            job_id=job["id"],
            title="Calibrate Dual Z-axis Lead Screws (Updated)",
            priority="critical",
            notes="Updated priority due to bed leveling drift.",
            actor=self.operator_user,
        )
        self.assertEqual(updated["title"], "Calibrate Dual Z-axis Lead Screws (Updated)")
        self.assertEqual(updated["priority"], "critical")

        # 4. Status workflow: open -> in_progress
        in_prog = change_maintenance_job_status(job["id"], "in_progress", notes="Technician began calibration.", actor=self.operator_user)
        self.assertEqual(in_prog["status"], "in_progress")
        self.assertIsNone(in_prog.get("completed_at"))

        # 5. Status workflow: in_progress -> completed (completed_at set automatically)
        completed = change_maintenance_job_status(job["id"], "completed", notes="Calibration finished within 0.01mm tolerance.", actor=self.operator_user)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNotNone(completed.get("completed_at"))

        # 6. List and filter maintenance jobs
        all_jobs = list_maintenance_jobs(machine_id=target_m["id"])
        self.assertTrue(any(j["id"] == job["id"] for j in all_jobs))

        completed_jobs = list_maintenance_jobs(status="completed")
        self.assertTrue(any(j["id"] == job["id"] for j in completed_jobs))

        open_jobs = list_maintenance_jobs(status="open")
        self.assertFalse(any(j["id"] == job["id"] for j in open_jobs))

        # 7. Delete maintenance job
        del_result = delete_maintenance_job(job["id"], actor=self.admin_user)
        self.assertTrue(del_result)
        self.assertIsNone(get_maintenance_job_by_id(job["id"]))

        # 8. Verify audit events recorded for maintenance jobs
        job_events, _ = query_audit_logs(object_type="maintenance_job", object_id=job["id"])
        actions = [e["action"] for e in job_events]
        self.assertIn("maintenance_job.created", actions)
        self.assertIn("maintenance_job.status_changed", actions)
        self.assertIn("maintenance_job.deleted", actions)

    def test_10_incidents_lifecycle_and_machine_outage(self):
        """Verify incident reporting, severity, machine availability impact, and resolution."""
        machines = list_machines()
        # Find an available machine
        avail_m = next(m for m in machines if m["state"] == "available")

        # 1. Report minor incident (should NOT place machine out of service)
        inc_minor = create_incident(
            machine_id=avail_m["id"],
            title="Minor cosmetic scratch on build plate",
            description="Scratch on corner, does not affect print center.",
            severity="minor",
            status="reported",
            reported_by_user_id=self.member_user["id"],
            takes_machine_out_of_service=False,
            actor=self.member_user,
        )
        self.assertEqual(inc_minor["severity"], "minor")
        self.assertEqual(inc_minor["status"], "reported")
        self.assertEqual(inc_minor["takes_machine_out_of_service"], 0)

        # Verify machine remains available
        m_after_minor = get_machine_by_id(avail_m["id"])
        self.assertEqual(m_after_minor["state"], "available")

        # 2. Report critical incident with takes_machine_out_of_service=True
        inc_critical = create_incident(
            machine_id=avail_m["id"],
            title="Laser cooling pump failure",
            description="Cooling pump stopped circulating chilled water. Risk of tube thermal shock.",
            severity="critical",
            status="reported",
            reported_by_user_id=self.operator_user["id"],
            takes_machine_out_of_service=True,
            actor=self.operator_user,
        )
        self.assertEqual(inc_critical["severity"], "critical")
        self.assertEqual(inc_critical["takes_machine_out_of_service"], 1)

        # Verify machine state transitioned to under_maintenance without deleting historical reservations
        m_after_crit = get_machine_by_id(avail_m["id"])
        self.assertIn(m_after_crit["state"], ("under_maintenance", "temporarily_unavailable"))

        # Verify availability check reflects conflict / out-of-service
        avail, msg, conflicts = check_machine_availability(
            machine_id=avail_m["id"],
            start_time="2026-09-20T10:00:00+02:00",
            end_time="2026-09-20T12:00:00+02:00",
        )
        self.assertFalse(avail)
        self.assertTrue(any(c.get("type") in ("critical_incident", "machine_under_maintenance", "machine_temporarily_unavailable") for c in conflicts))

        # 3. Status progression: reported -> investigating -> resolved -> closed
        inv = change_incident_status(inc_critical["id"], "investigating", notes="Technician inspecting chiller flow meter.", actor=self.operator_user)
        self.assertEqual(inv["status"], "investigating")
        self.assertIsNone(inv.get("resolved_at"))

        res = change_incident_status(inc_critical["id"], "resolved", notes="Replaced blown 12V DC pump fuse. Flow verified.", actor=self.operator_user)
        self.assertEqual(res["status"], "resolved")
        self.assertIsNotNone(res.get("resolved_at"))

        closed = change_incident_status(inc_critical["id"], "closed", notes="Post-fix test cycle completed.", actor=self.operator_user)
        self.assertEqual(closed["status"], "closed")

        # 4. List and filter incidents
        all_incs = list_incidents(machine_id=avail_m["id"])
        self.assertTrue(any(i["id"] == inc_critical["id"] for i in all_incs))
        self.assertTrue(any(i["id"] == inc_minor["id"] for i in all_incs))

        crit_incs = list_incidents(severity="critical")
        self.assertTrue(any(i["id"] == inc_critical["id"] for i in crit_incs))

        # 5. Delete incident
        delete_incident(inc_minor["id"], actor=self.admin_user)
        self.assertIsNone(get_incident_by_id(inc_minor["id"]))

        # Restore machine state for subsequent tests
        change_machine_state(avail_m["id"], "available", reason="Test cleanup restoration", actor=self.admin_user)

    def test_11_incident_attachments_and_security(self):
        """Verify secure file upload, extension whitelist, size enforcement, traversal defense, and download handler."""
        machines = list_machines()
        target_m = machines[0]

        # 1. Create an incident to hold attachments
        inc = create_incident(
            machine_id=target_m["id"],
            title="Spindle bearing vibration diagnostic",
            description="Vibration spectrogram captured during high RPM milling test.",
            severity="major",
            reported_by_user_id=self.operator_user["id"],
            actor=self.operator_user,
        )

        # 2. Upload valid PNG image attachment
        fake_png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256  # Minimal valid PNG header + dummy payload
        valid_upload = UploadedFile(
            filename="bearing_vibration_spectrogram.png",
            content_type="image/png",
            data=fake_png_data,
        )
        att = add_incident_attachment(
            incident_id=inc["id"],
            uploaded_file=valid_upload,
            uploaded_by_user_id=self.operator_user["id"],
            actor=self.operator_user,
        )
        self.assertIsNotNone(att["id"])
        self.assertEqual(att["original_filename"], "bearing_vibration_spectrogram.png")
        self.assertTrue(att["stored_filename"].startswith(f"inc_{inc['id']}_"))
        self.assertTrue(att["stored_filename"].endswith(".png"))
        self.assertEqual(att["file_size"], len(fake_png_data))

        # Verify file is physically saved under UPLOAD_DIR
        import os
        from pathlib import Path
        stored_path = Path(att["file_path"]).resolve()
        self.assertTrue(stored_path.is_file())
        self.assertTrue(str(stored_path).startswith(str(UPLOAD_DIR)))

        # 3. Retrieve attachment and download via API
        fetched_att = get_incident_attachment_by_id(att["id"])
        self.assertIsNotNone(fetched_att)

        resp_download = self._make_request(
            "GET",
            f"/api/incidents/{inc['id']}/attachments/{att['id']}/download",
            bearer_token=self.operator_session,
        )
        self.assertEqual(resp_download.status_code, 200)
        self.assertEqual(resp_download.body, fake_png_data)
        self.assertEqual(resp_download.headers.get("Content-Type"), "image/png")

        # 4. Upload valid log file
        log_data = b"2026-09-01 12:00:00 [ERROR] Axis Z motor stall detected at step 4200.\n"
        log_upload = UploadedFile(
            filename="grbl_controller.log",
            content_type="text/plain",
            data=log_data,
        )
        att_log = add_incident_attachment(
            incident_id=inc["id"],
            uploaded_file=log_upload,
            uploaded_by_user_id=self.operator_user["id"],
            actor=self.operator_user,
        )
        self.assertEqual(att_log["original_filename"], "grbl_controller.log")

        # Verify incident attachments count
        inc_with_atts = get_incident_by_id(inc["id"])
        self.assertEqual(inc_with_atts["attachments_count"], 2)

        # 5. Security: Reject executable and dangerous script extensions
        forbidden_extensions = [".exe", ".sh", ".py", ".php", ".js", ".html", ".svg", ".bat", ".elf"]
        for bad_ext in forbidden_extensions:
            bad_upload = UploadedFile(
                filename=f"malicious_payload{bad_ext}",
                content_type="application/octet-stream",
                data=b"echo 'malicious code'",
            )
            with self.assertRaises(ValueError, msg=f"Should reject forbidden extension: {bad_ext}"):
                add_incident_attachment(
                    incident_id=inc["id"],
                    uploaded_file=bad_upload,
                    actor=self.operator_user,
                )

        # 6. Security: Reject empty file (0 bytes)
        empty_upload = UploadedFile(
            filename="empty_file.txt",
            content_type="text/plain",
            data=b"",
        )
        with self.assertRaises(ValueError, msg="Should reject 0-byte empty file"):
            add_incident_attachment(
                incident_id=inc["id"],
                uploaded_file=empty_upload,
                actor=self.operator_user,
            )

        # 7. Security: Reject oversized file (> 10MB)
        huge_data = b"X" * (11 * 1024 * 1024)
        huge_upload = UploadedFile(
            filename="oversized_archive.zip",
            content_type="application/zip",
            data=huge_data,
        )
        with self.assertRaises(ValueError, msg="Should reject file exceeding 10MB limit"):
            add_incident_attachment(
                incident_id=inc["id"],
                uploaded_file=huge_upload,
                actor=self.operator_user,
            )

        # 8. Security: Path traversal in filename is sanitized
        traversal_upload = UploadedFile(
            filename="../../../etc/shadow.png",
            content_type="image/png",
            data=fake_png_data,
        )
        att_traversal = add_incident_attachment(
            incident_id=inc["id"],
            uploaded_file=traversal_upload,
            actor=self.operator_user,
        )
        self.assertEqual(att_traversal["original_filename"], "shadow.png")  # Basename extracted
        self.assertTrue(str(Path(att_traversal["file_path"]).resolve()).startswith(str(UPLOAD_DIR)))

        # 9. Delete attachment and verify physical file is unlinked
        del_att_res = delete_incident_attachment(att["id"], actor=self.admin_user)
        self.assertTrue(del_att_res)
        self.assertIsNone(get_incident_attachment_by_id(att["id"]))
        self.assertFalse(stored_path.exists(), "Attachment file should be deleted from disk upon deletion")

        # Cleanup remaining incident and files
        delete_incident(inc["id"], actor=self.admin_user)

    def test_12_rbac_maintenance_and_incidents(self):
        """Verify server-side RBAC on Maintenance Jobs and Incidents HTTP and REST API endpoints."""
        machines = list_machines()
        m_id = machines[0]["id"]

        # 1. Admin: Create Maintenance Job -> 201 Created
        resp_adm_job = self._make_request(
            "POST",
            "/api/maintenance-jobs",
            bearer_token=self.admin_session,
            json_data={
                "machine_id": m_id,
                "title": "Admin Lubrication Task",
                "priority": "medium",
            },
        )
        self.assertEqual(resp_adm_job.status_code, 201)
        job_id = json.loads(resp_adm_job.body.decode("utf-8"))["maintenance_job"]["id"]

        # 2. Operator: Update Maintenance Job -> 200 OK
        resp_op_job = self._make_request(
            "PUT",
            f"/api/maintenance-jobs/{job_id}",
            bearer_token=self.operator_session,
            json_data={"notes": "Operator applied high-temp grease."},
        )
        self.assertEqual(resp_op_job.status_code, 200)

        # 3. Member: List Maintenance Jobs -> 200 OK
        resp_mem_get = self._make_request("GET", "/api/maintenance-jobs", bearer_token=self.member_session)
        self.assertEqual(resp_mem_get.status_code, 200)

        # 4. Member: Attempt to Create Maintenance Job -> 403 Forbidden
        resp_mem_post_job = self._make_request(
            "POST",
            "/api/maintenance-jobs",
            bearer_token=self.member_session,
            json_data={
                "machine_id": m_id,
                "title": "Unauthorized Member Maintenance",
            },
        )
        self.assertEqual(resp_mem_post_job.status_code, 403)

        # 5. Member: Report Incident -> 201 Created (Members are authorized to report issues!)
        resp_mem_inc = self._make_request(
            "POST",
            "/api/incidents",
            bearer_token=self.member_session,
            json_data={
                "machine_id": m_id,
                "title": "Member Observed Belt Tension Squeak",
                "severity": "minor",
                "description": "Squeaking sound when Y carriage accelerates.",
            },
        )
        self.assertEqual(resp_mem_inc.status_code, 201)
        inc_id = json.loads(resp_mem_inc.body.decode("utf-8"))["incident"]["id"]

        # 6. Member: Attempt to update/resolve incident -> 403 Forbidden
        resp_mem_put_inc = self._make_request(
            "PUT",
            f"/api/incidents/{inc_id}",
            bearer_token=self.member_session,
            json_data={"severity": "critical"},
        )
        self.assertEqual(resp_mem_put_inc.status_code, 403)

        # 7. Operator: Resolve Incident -> 200 OK
        resp_op_patch_inc = self._make_request(
            "PATCH",
            f"/api/incidents/{inc_id}/status",
            bearer_token=self.operator_session,
            json_data={"status": "resolved", "notes": "Adjusted idler pulley tension."},
        )
        self.assertEqual(resp_op_patch_inc.status_code, 200)

        # 8. Viewer: Read-only access to incidents and jobs -> 200 OK
        resp_view_get_jobs = self._make_request("GET", "/api/maintenance-jobs", bearer_token=self.viewer_session)
        self.assertEqual(resp_view_get_jobs.status_code, 200)
        resp_view_get_incs = self._make_request("GET", "/api/incidents", bearer_token=self.viewer_session)
        self.assertEqual(resp_view_get_incs.status_code, 200)

        # 9. Viewer: Attempt to report incident -> 403 Forbidden
        resp_view_post_inc = self._make_request(
            "POST",
            "/api/incidents",
            bearer_token=self.viewer_session,
            json_data={
                "machine_id": m_id,
                "title": "Viewer Unauthorized Incident",
            },
        )
        self.assertEqual(resp_view_post_inc.status_code, 403)

        # Cleanup
        delete_maintenance_job(job_id, actor=self.admin_user)
        delete_incident(inc_id, actor=self.admin_user)

    def test_13_incident_and_maintenance_window_state_sync_symmetry(self):
        """Verify symmetrical state synchronization between incidents and maintenance windows (CHAL-S09-001)."""
        m = create_machine(
            code="SYNC-TEST-01",
            name="Symmetry Sync Test Machine",
            category_id=1,
            state="available",
            actor=self.admin_user,
        )
        m_id = m["id"]

        # 1. Critical incident sets machine to under_maintenance
        inc = create_incident(
            machine_id=m_id,
            title="Drive Motor Failure",
            severity="critical",
            takes_machine_out_of_service=True,
            actor=self.operator_user,
        )
        self.assertEqual(get_machine_by_id(m_id)["state"], "under_maintenance")

        # 2. Add maintenance window in_progress
        mw = create_maintenance_window(
            machine_id=m_id,
            title="Motor Replacement Window",
            start_time="2026-10-01T10:00:00+02:00",
            end_time="2026-10-01T14:00:00+02:00",
            status="in_progress",
            actor=self.operator_user,
        )
        self.assertEqual(get_machine_by_id(m_id)["state"], "under_maintenance")

        # 3. Completing maintenance window while critical incident is still open must NOT mark machine as available!
        change_maintenance_window_status(mw["id"], "completed", actor=self.operator_user)
        self.assertEqual(get_machine_by_id(m_id)["state"], "under_maintenance")

        # 4. Now resolving the critical incident restores machine to available
        change_incident_status(inc["id"], "resolved", actor=self.operator_user)
        self.assertEqual(get_machine_by_id(m_id)["state"], "available")

        # Cleanup
        delete_incident(inc["id"], actor=self.admin_user)
        delete_machine(m_id, actor=self.admin_user)

    def test_14_windows_backslash_attachment_filename_sanitization(self):
        """Verify Windows-style backslash filenames are cleanly sanitized to basename (CHAL-S09-002)."""
        m = list_machines()[0]
        inc = create_incident(
            machine_id=m["id"],
            title="Windows Path Traversal Test",
            actor=self.operator_user,
        )

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        win_upload = UploadedFile(
            filename="C:\\Users\\Operator\\Desktop\\diagnostics\\schema_v2.png",
            content_type="image/png",
            data=fake_png,
        )
        att = add_incident_attachment(
            incident_id=inc["id"],
            uploaded_file=win_upload,
            actor=self.operator_user,
        )
        self.assertEqual(att["original_filename"], "schema_v2.png")
        self.assertNotIn("\\", att["original_filename"])
        self.assertNotIn(":", att["original_filename"])

        delete_incident(inc["id"], actor=self.admin_user)

    def test_15_maintenance_job_in_progress_coordination_and_machine_state(self):
        """Verify high/critical priority maintenance job in_progress syncs machine state and conflicts (CHAL-S09-003)."""
        m = create_machine(
            code="JOB-SYNC-01",
            name="Job Coordination Machine",
            category_id=1,
            state="available",
            actor=self.admin_user,
        )
        m_id = m["id"]

        # Create critical priority job
        job = create_maintenance_job(
            machine_id=m_id,
            title="Emergency Spindle Bearing Replacement",
            priority="critical",
            status="open",
            actor=self.operator_user,
        )
        self.assertEqual(get_machine_by_id(m_id)["state"], "available")

        # Start job (in_progress) -> sets machine to under_maintenance
        change_maintenance_job_status(job["id"], "in_progress", actor=self.operator_user)
        self.assertEqual(get_machine_by_id(m_id)["state"], "under_maintenance")

        # Availability check reports conflict
        avail, msg, conflicts = check_machine_availability(
            machine_id=m_id,
            start_time="2026-10-05T10:00:00+02:00",
            end_time="2026-10-05T12:00:00+02:00",
        )
        self.assertFalse(avail)
        self.assertTrue(any(c["type"] in ("maintenance_job_in_progress", "machine_under_maintenance") for c in conflicts))

        # Complete job -> restores machine to available
        change_maintenance_job_status(job["id"], "completed", actor=self.operator_user)
        self.assertEqual(get_machine_by_id(m_id)["state"], "available")

        delete_maintenance_job(job["id"], actor=self.admin_user)
        delete_machine(m_id, actor=self.admin_user)


if __name__ == "__main__":
    unittest.main()
