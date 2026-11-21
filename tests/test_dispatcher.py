import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

from campusspot.dispatcher import Dispatcher, DispatchInProgress, ValidationError
from campusspot.tool_contract import ToolResult


class UnknownOperation(LookupError):
    code = "UNKNOWN_OPERATION"


class Job:
    def __init__(self, job_id):
        self.job_id = job_id
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
        self.dispatcher = Dispatcher(self.resolve, self.tools)
        self.job = Job("job-1")
        self.new_job = Mock(return_value=self.job)

    def resolve(self, name):
        if name not in self.tools:
            raise UnknownOperation(name)
        return self.tools[name]

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

    def test_unknown_operation_precedes_job_creation_and_execution(self):
        del self.tools["limits"]
        with self.assertRaises(UnknownOperation) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")
        self.assertEqual(self.calls, [])
        self.new_job.assert_not_called()

    def test_invalid_input_reports_field_before_creating_job(self):
        for field in self.fields:
            with self.subTest(missing=field):
                data = self.fields.copy()
                del data[field]
                with self.assertRaises(ValidationError) as caught:
                    self.dispatcher.create(data, self.new_job)
                self.assertEqual((caught.exception.code, caught.exception.field),
                                 ("VALIDATION_ERROR", field))
        for field, value in (("role", 42), ("space_id", " "),
                             ("start_time", "tomorrow"), ("end_time", self.fields["start_time"])):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError) as caught:
                    self.dispatcher.create(self.fields | {field: value}, self.new_job)
                self.assertEqual(caught.exception.field, field)
        self.new_job.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_concurrent_dispatchers_run_one_chain(self):
        entered, release = Event(), Event()

        def wait_for_second_call(params):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Второй вызов не завершился")
            return ToolResult(True, "rights")

        self.tools["rights"].execute.side_effect = wait_for_second_call
        other = Dispatcher(self.resolve, self.tools)
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
        self.assertEqual(self.job.status, "CONFIRMED")
