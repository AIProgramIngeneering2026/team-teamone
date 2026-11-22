import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from campusspot.dispatcher import Dispatcher
from campusspot.job import AccessDenied, CheckInWindowClosed, InvalidStateTransition, Job
from campusspot.registry import Registry
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

    def test_params_keep_an_immutable_snapshot_of_extra(self):
        source = {
            "operation": "extend",
            "original_start_time": START,
            "context": {"spaces": [{"id": PARAMS.space_id}]},
        }
        job = Job(replace(PARAMS, extra=source), required_checks=REQUIRED_CHECKS)

        source["operation"] = "create"
        source["context"]["spaces"][0]["id"] = "other"
        source["context"]["spaces"].append({"id": "third"})

        self.assertEqual(job.params.extra["operation"], "extend")
        self.assertEqual(job.params.extra["original_start_time"], START)
        spaces = job.params.extra["context"]["spaces"]
        self.assertEqual(spaces, ({"id": PARAMS.space_id},))
        with self.assertRaises(TypeError):
            job.params.extra["operation"] = "reschedule"
        with self.assertRaises(TypeError):
            spaces[0]["id"] = "changed through Job"

    def test_tool_cannot_change_saved_params_and_break_replay(self):
        def attempt_to_mutate(params):
            try:
                params.extra["source"] = "tool"
            except TypeError:
                pass
            return ToolResult(True, "limit_check")

        checks = ("limit_check",)
        tool = Mock()
        tool.execute.side_effect = attempt_to_mutate
        dispatcher = Dispatcher(Registry({"limit_check": tool}).resolve, checks)
        jobs = {}

        def get_or_create(params):
            if params.job_id not in jobs:
                jobs[params.job_id] = Job(params, required_checks=checks)
            return jobs[params.job_id]

        fields = {
            "job_id": PARAMS.job_id,
            "user_id": PARAMS.user_id,
            "role": PARAMS.role,
            "space_id": PARAMS.space_id,
            "space_category": PARAMS.space_category,
            "start_time": PARAMS.start_time,
            "end_time": PARAMS.end_time,
        }
        job = dispatcher.create(fields, get_or_create)

        self.assertEqual(job.status, "CONFIRMED")
        self.assertIs(dispatcher.create(fields, get_or_create), job)
        self.assertEqual(job.params.extra, {"operation": "create"})
        tool.execute.assert_called_once()

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

    def test_history_keeps_an_immutable_snapshot_of_nested_details(self):
        details = {
            "conflict": {"space_id": PARAMS.space_id, "related_job_ids": ["job-2"]},
        }
        failure = ToolResult(False, "conflict_check", "SPACE_UNAVAILABLE", details=details)
        self.job.transition("CANCELLED", checks=(failure,))

        details["conflict"]["space_id"] = "other"
        details["conflict"]["related_job_ids"].append("job-3")

        saved = self.job.history[-1].details["conflict"]
        self.assertEqual(saved["space_id"], PARAMS.space_id)
        self.assertEqual(saved["related_job_ids"], ("job-2",))
        with self.assertRaises(TypeError):
            saved["space_id"] = "changed through history"

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
