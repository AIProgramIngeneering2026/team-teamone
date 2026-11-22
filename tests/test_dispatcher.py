import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Barrier, Event, Lock
from unittest.mock import Mock

from campusspot.dispatcher import (
    Dispatcher, DispatchInProgress, PayloadMismatch, SlotConflict, ValidationError, creation_params,
)
from campusspot.registry import Registry, UnknownOperation
from campusspot.tool_contract import ToolResult


class Job:
    def __init__(self, params):
        self.params = params
        self.job_id = params.job_id
        self.status = "REQUESTED"
        self.history = ()

    def transition(self, status, *, checks):
        assert self.status == "REQUESTED"
        self.status = status
        self.history = checks


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.fields = dict(job_id="job-1", user_id="u", role="student",
                           space_id="s", space_category="coworking",
                           start_time=start, end_time=start + timedelta(hours=1))
        self.calls = []
        self.tools = {}
        for name in ("rights", "conflicts", "limits"):
            tool = Mock()
            tool.name = name
            tool.execute.side_effect = self.success(name)
            self.tools[name] = tool
        self.registry = Registry(self.tools)
        self.dispatcher = Dispatcher(self.registry.resolve, self.tools)
        self.job = Job(creation_params(self.fields))
        self.new_job = Mock(return_value=self.job)

    def success(self, name):
        def execute(params):
            self.assertEqual(params.extra["operation"], "create")
            self.assertEqual(params.job_id, self.job.job_id)
            self.calls.append(name)
            return ToolResult(True, name)
        return execute

    def dispatch(self):
        return self.dispatcher.create(self.fields, self.new_job)

    def test_configured_order_history_and_replay(self):
        self.assertIs(self.dispatch(), self.job)
        self.dispatch()
        self.assertEqual(self.job.status, "CONFIRMED")
        self.assertEqual(self.calls, ["rights", "conflicts", "limits"])
        self.assertEqual([check.tool_name for check in self.job.history], self.calls)

    def test_refusal_cancels_with_original_details_and_stops(self):
        refusal = ToolResult(False, "conflicts", "SPACE_UNAVAILABLE", "Занято",
                             {"conflict_type": "booking"})
        self.tools["conflicts"].execute.side_effect = [refusal]
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history, (ToolResult(True, "rights"), refusal))
        self.tools["limits"].execute.assert_not_called()

    def test_exception_cancels_and_preserves_cause(self):
        class Timeout(Exception):
            code = "EXTERNAL_SCHEDULE_TIMEOUT"

        error = Timeout("Расписание недоступно")
        self.tools["conflicts"].execute.side_effect = error
        with self.assertRaises(Timeout) as caught:
            self.dispatch()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history[-1],
                         ToolResult(False, "conflicts", error.code, str(error)))
        self.tools["limits"].execute.assert_not_called()

    def test_unknown_operation_leaves_requested_job_ready_to_retry(self):
        self.dispatcher = Dispatcher(self.registry.resolve, ("rights", "missing"))
        with self.assertRaises(UnknownOperation) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job.status, "REQUESTED")
        self.assertEqual(self.job.history, ())
        self.dispatcher = Dispatcher(self.registry.resolve, self.tools)
        self.assertEqual(self.dispatch().status, "CONFIRMED")

    def test_invalid_input_reports_field_before_creating_job(self):
        for field in self.fields:
            with self.subTest(missing=field):
                data = self.fields.copy()
                del data[field]
                with self.assertRaises(ValidationError) as caught:
                    self.dispatcher.create(data, self.new_job)
                self.assertEqual((caught.exception.code, caught.exception.field),
                                 ("VALIDATION_ERROR", field))
        for field, value in (("role", 42), ("role", "unknown"), ("space_id", ""),
                             ("job_id", "   "), ("space_id", "\t"), ("user_id", " "),
                             ("status", "CONFIRMED"), ("spaceId", "s"),
                             ("space_category", "unknown"),
                             ("start_time", "tomorrow"), ("end_time", self.fields["start_time"])):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError) as caught:
                    self.dispatcher.create(self.fields | {field: value}, self.new_job)
                self.assertEqual(caught.exception.field, field)
        self.new_job.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_identifier_length_matches_openapi(self):
        for field in ("job_id", "user_id", "space_id"):
            with self.subTest(field=field):
                value = "я" * 128
                params = creation_params(self.fields | {field: value})
                self.assertEqual(getattr(params, field), value)
                with self.assertRaises(ValidationError) as caught:
                    self.dispatcher.create(self.fields | {field: value + "я"}, self.new_job)
                self.assertEqual(caught.exception.field, field)
        self.new_job.assert_not_called()

    def test_interval_uses_instants_and_requires_offset(self):
        start = self.fields["start_time"]
        same_instant = start.astimezone(timezone(timedelta(hours=5)))
        for end in (same_instant, start.replace(tzinfo=None)):
            with self.assertRaises(ValidationError) as caught:
                creation_params(self.fields | {"end_time": end})
            self.assertEqual(caught.exception.field, "end_time")
        end = (start + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=-5)))
        self.assertEqual(creation_params(self.fields | {"end_time": end}).end_time, end)

    def test_concurrent_dispatchers_run_one_chain(self):
        entered, release = Event(), Event()

        def wait_for_second_call(params):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Второй вызов не завершился")
            return ToolResult(True, "rights")

        self.tools["rights"].execute.side_effect = wait_for_second_call
        other = Dispatcher(self.registry.resolve, self.tools)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.dispatch)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(DispatchInProgress):
                    other.create(self.fields, self.new_job)
            finally:
                release.set()
            self.assertIs(first.result(timeout=5), self.job)
        self.tools["rights"].execute.assert_called_once()
        self.new_job.assert_called_once()
        self.assertEqual(self.job.status, "CONFIRMED")

    def test_empty_checks(self):
        with self.assertRaises(ValueError):
            Dispatcher(self.registry.resolve, [])

    def test_wrong_job_id(self):
        self.new_job.return_value = Job(creation_params(self.fields | {"job_id": "wrong"}))
        with self.assertRaises(ValidationError) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.field, "job_id")
        self.assertEqual(self.calls, [])

    def test_plain_enum_status_is_rejected(self):
        class Status(Enum):
            REQUESTED = "REQUESTED"
        self.job.status = Status.REQUESTED
        with self.assertRaisesRegex(TypeError, "BookingJob.status"):
            self.dispatch()
        self.assertEqual(self.calls, [])

    def test_changed_payload_rejected_before_checks(self):
        for status in ("REQUESTED", "CONFIRMED"):
            with self.subTest(status=status):
                self.job.status = status
                with self.assertRaises(PayloadMismatch):
                    self.dispatcher.create(self.fields | {"space_id": "other"}, self.new_job)
                self.assertEqual(self.job.status, status)
                self.assertEqual(self.job.history, ())
        self.assertEqual(self.calls, [])

    def test_completed_replay_skips_registry(self):
        for status in ("CONFIRMED", "CANCELLED", "USED", "EXPIRED"):
            with self.subTest(status=status):
                self.job.status = status
                resolve = Mock(side_effect=AssertionError("unexpected lookup"))
                dispatcher = Dispatcher(resolve, ["missing"])
                self.assertIs(dispatcher.create(self.fields, self.new_job), self.job)
                resolve.assert_not_called()

    def test_tool_exception_uses_registry_name(self):
        error = RuntimeError("offline")
        tool = Mock(spec=["execute"])
        tool.execute.side_effect = error
        dispatcher = Dispatcher(Registry({"rights": tool}).resolve, ["rights"])
        with self.assertRaises(RuntimeError) as caught:
            dispatcher.create(self.fields, self.new_job)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.job.history[-1].tool_name, "rights")

    def test_manual_cancellation_while_tool_is_running(self):
        entered, release = Event(), Event()

        def blocking_check(params):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test timed out")
            return ToolResult(True, "rights")

        self.tools["rights"].execute.side_effect = blocking_check
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.dispatch)
            try:
                self.assertTrue(entered.wait(5))
                self.job.transition("CANCELLED", checks=())
            finally:
                release.set()
            self.assertIs(running.result(timeout=5), self.job)
        self.assertEqual(self.job.status, "CANCELLED")
        self.assertEqual(self.job.history, ())
        self.tools["conflicts"].execute.assert_not_called()
        self.tools["limits"].execute.assert_not_called()

    def test_cancellation_between_status_read_and_transition(self):
        class InvalidStateTransition(ValueError):
            code = "INVALID_STATE_TRANSITION"

        def cancel_before_commit(status, *, checks):
            self.job.status = "CANCELLED"
            raise InvalidStateTransition()

        self.job.transition = cancel_before_commit
        self.assertIs(self.dispatch(), self.job)
        self.assertEqual(self.job.history, ())
        self.assertEqual(self.job.status, "CANCELLED")

    def test_overlapping_jobs_only_one_reserves_slot(self):
        both_checked = Barrier(2)
        write_lock = Lock()
        reservations = []
        refusal = ToolResult(False, "conflict_check", "SPACE_UNAVAILABLE",
                             details={"conflict_type": "booking"})

        class StoredJob(Job):
            def transition(self, status, *, checks):
                with write_lock:
                    if status == "CONFIRMED":
                        p = self.params
                        if any(r.space_id == p.space_id and r.start_time < p.end_time
                               and p.start_time < r.end_time for r in reservations):
                            raise SlotConflict(refusal)
                        reservations.append(p)
                    super().transition(status, checks=checks)

        def check(params):
            both_checked.wait(timeout=5)
            return ToolResult(True, "conflict_check")

        tool = Mock()
        tool.execute.side_effect = check
        dispatcher = Dispatcher(Registry({"conflict_check": tool}).resolve, ["conflict_check"])
        second = self.fields | {"job_id": "job-2",
                                "start_time": self.fields["start_time"] + timedelta(minutes=30)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatcher.create, fields, StoredJob)
                       for fields in (self.fields, second)]
            jobs = [future.result(timeout=5) for future in futures]
        self.assertCountEqual([job.status for job in jobs], ["CONFIRMED", "CANCELLED"])
        winner = next(job for job in jobs if job.status == "CONFIRMED")
        loser = next(job for job in jobs if job.status == "CANCELLED")
        self.assertEqual(reservations, [winner.params])
        self.assertEqual(loser.history, (ToolResult(True, "conflict_check"), refusal))
