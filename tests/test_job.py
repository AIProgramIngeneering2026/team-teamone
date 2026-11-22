from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from campusspot.job import AccessDenied, CheckInWindowClosed, InvalidStateTransition, Job
from campusspot.tool_contract import ToolParams, ToolResult


START = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
PARAMS = ToolParams(
    job_id="job-1",
    user_id="owner",
    role="student",
    space_id="cw-1",
    space_category="coworking",
    start_time=START,
    end_time=START + timedelta(hours=1),
)
REQUIRED_CHECKS = ("access_rights", "limit_check", "conflict_check")
PASSED = tuple(ToolResult(True, name) for name in REQUIRED_CHECKS)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.job = Job(PARAMS, required_checks=REQUIRED_CHECKS)

    def test_creation_and_successful_check_in(self):
        self.assertEqual(self.job.job_id, PARAMS.job_id)
        self.assertEqual(self.job.status, "REQUESTED")
        self.job.transition("CONFIRMED", checks=PASSED)
        self.job.check_in(START, user_id=PARAMS.user_id)
        self.assertEqual(self.job.status, "USED")
        self.assertEqual(self.job.history, PASSED)

    def test_requested_can_only_be_confirmed_or_cancelled(self):
        for status in ("REQUESTED", "USED", "EXPIRED", "UNKNOWN"):
            with self.subTest(status=status):
                with self.assertRaises(InvalidStateTransition) as error:
                    self.job.transition(status)
                self.assertEqual(error.exception.code, "INVALID_STATE_TRANSITION")
                self.assertEqual(self.job.status, "REQUESTED")
                self.assertEqual(self.job.history, ())

    def test_confirmation_requires_all_successful_checks_in_order(self):
        failed = (*PASSED[:-1], ToolResult(False, REQUIRED_CHECKS[-1]))
        for checks in ((), PASSED[:1], tuple(reversed(PASSED)), failed):
            with self.subTest(checks=checks):
                with self.assertRaises(InvalidStateTransition):
                    self.job.transition("CONFIRMED", checks=checks)
                self.assertEqual(self.job.status, "REQUESTED")
                self.assertEqual(self.job.history, ())

    def test_late_success_does_not_reopen_user_cancelled_job(self):
        self.job.cancel(user_id=PARAMS.user_id, role="student")
        with self.assertRaises(InvalidStateTransition):
            self.job.transition("CONFIRMED", checks=PASSED)
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history, ())

    def test_failed_checks_stay_in_history_after_late_success_is_rejected(self):
        details = {"space_id": PARAMS.space_id}
        failure = ToolResult(
            False, "conflict_check", error_code="SPACE_UNAVAILABLE", details=details
        )
        self.job.transition("CANCELLED", checks=(*PASSED[:-1], failure))
        details["space_id"] = "other"
        with self.assertRaises(InvalidStateTransition):
            self.job.transition("CONFIRMED", checks=PASSED)
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history[-1].error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(self.job.history[-1].details, {"space_id": PARAMS.space_id})

    def test_owner_or_admin_can_cancel_and_repeat_preserves_history(self):
        self.job.transition("CONFIRMED", checks=PASSED)
        with self.assertRaises(AccessDenied):
            self.job.cancel(user_id="other", role="student")
        self.assertEqual(self.job.status, "CONFIRMED")

        self.job.cancel(user_id="other", role="admin")
        self.job.cancel(user_id=PARAMS.user_id, role="student")
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history, PASSED)

    def test_check_in_requires_owner_and_open_window(self):
        self.job.transition("CONFIRMED", checks=PASSED)
        with self.assertRaises(AccessDenied):
            self.job.check_in(START, user_id="other")
        for now in (START - timedelta(seconds=1), START + timedelta(minutes=15)):
            with self.subTest(now=now):
                with self.assertRaises(CheckInWindowClosed):
                    self.job.check_in(now, user_id=PARAMS.user_id)
        self.assertEqual(self.job.status, "CONFIRMED")

    def test_window_ends_at_end_time_for_short_booking(self):
        params = replace(PARAMS, end_time=START + timedelta(minutes=5))
        job = Job(params, required_checks=REQUIRED_CHECKS)
        job.transition("CONFIRMED", checks=PASSED)
        with self.assertRaises(CheckInWindowClosed):
            job.check_in(params.end_time, user_id=params.user_id)
        job.transition("EXPIRED", now=params.end_time)
        self.assertEqual(job.status, "EXPIRED")

    def test_expiry_before_deadline_and_cancelling_terminal_jobs_are_rejected(self):
        self.job.transition("CONFIRMED", checks=PASSED)
        with self.assertRaises(InvalidStateTransition):
            self.job.transition("EXPIRED", now=START + timedelta(minutes=14))
        self.job.transition("EXPIRED", now=START + timedelta(minutes=15))

        used = Job(PARAMS, required_checks=REQUIRED_CHECKS)
        used.transition("CONFIRMED", checks=PASSED)
        used.check_in(START, user_id=PARAMS.user_id)
        used.check_in(START, user_id=PARAMS.user_id)
        for job in (self.job, used):
            with self.subTest(status=job.status):
                old_status = job.status
                with self.assertRaises(InvalidStateTransition):
                    job.cancel(user_id=PARAMS.user_id, role="student")
                self.assertEqual(job.status, old_status)
                self.assertEqual(job.history, PASSED)
