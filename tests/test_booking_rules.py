import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from threading import Barrier
from unittest.mock import patch

from campusspot.access_rights import AccessRightsTool
from campusspot.booking import BookingService, InMemoryJobRepository
from campusspot.conflict_check import InMemoryScheduleSource, ScheduleSlot
from campusspot.dispatcher import Dispatcher, ValidationError
from campusspot.limit_check import FixedClock, LimitCheckTool
from campusspot.registry import Registry
from campusspot.tool_contract import ToolParams, ToolResult


class RepeatedHour(tzinfo):
    """Два смещения для повторяющегося часа без системной базы часовых поясов."""

    def utcoffset(self, value):
        return timedelta(hours=1 if value is not None and value.fold else 2)


FOLD_NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
FOLD_START = datetime(2026, 10, 25, 2, 10, tzinfo=RepeatedHour())
FOLD_END = FOLD_START.replace(minute=40)


NOW = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)
START = NOW + timedelta(hours=1)
PARAMS = ToolParams(
    job_id="candidate",
    user_id="student-1",
    role="student",
    space_id="cw-1",
    space_category="coworking",
    start_time=START,
    end_time=START + timedelta(hours=1),
)


def fields(params: ToolParams) -> dict[str, object]:
    return {
        "job_id": params.job_id,
        "user_id": params.user_id,
        "role": params.role,
        "space_id": params.space_id,
        "space_category": params.space_category,
        "start_time": params.start_time,
        "end_time": params.end_time,
    }


class BookingRuleTests(unittest.TestCase):
    def setUp(self):
        self.jobs = InMemoryJobRepository(required_checks=("limit_check",))
        self.tool = LimitCheckTool(FixedClock(NOW), self.jobs)

    def test_duration_boundary(self):
        for minutes, expected in ((240, None), (241, "BOOKING_LIMIT_EXCEEDED")):
            with self.subTest(minutes=minutes):
                params = replace(PARAMS, end_time=START + timedelta(minutes=minutes))
                result = self.tool.execute(params)
                self.assertEqual(result.error_code, expected)
                self.assertEqual(result.passed, expected is None)

    def test_current_job_is_excluded_and_pending_and_confirmed_jobs_count(self):
        self.jobs.get_or_create(PARAMS)
        other = self.jobs.get_or_create(replace(PARAMS, job_id="other"))
        other.transition("CONFIRMED", checks=(ToolResult(True, "limit_check"),))
        cancelled = self.jobs.get_or_create(replace(PARAMS, job_id="cancelled"))
        cancelled.transition("CANCELLED")
        self.jobs.get_or_create(replace(PARAMS, job_id="different-user", user_id="student-2"))
        self.assertTrue(self.tool.execute(PARAMS).passed)

        self.jobs.get_or_create(replace(PARAMS, job_id="pending"))
        result = self.tool.execute(PARAMS)
        self.assertEqual(result.error_code, "BOOKING_LIMIT_EXCEEDED")
        self.assertEqual(result.details["max_active_bookings"], 2)

    def test_start_must_be_strictly_in_the_future(self):
        for start in (NOW, NOW - timedelta(seconds=1)):
            with self.subTest(start=start):
                result = self.tool.execute(replace(PARAMS, start_time=start))
                self.assertFalse(result.passed)
                self.assertEqual(result.error_code, "INVALID_NEW_TIME")
                self.assertEqual(result.details["field"], "start_time")

    def test_extension_uses_original_start_and_ignores_active_count(self):
        self.jobs.get_or_create(replace(PARAMS, job_id="other-1"))
        self.jobs.get_or_create(replace(PARAMS, job_id="other-2"))
        original = NOW - timedelta(hours=2)
        params = replace(
            PARAMS,
            end_time=original + timedelta(hours=4),
            extra={"operation": "extend", "original_start_time": original},
        )
        self.assertTrue(self.tool.execute(params).passed)
        result = self.tool.execute(replace(params, end_time=params.end_time + timedelta(seconds=1)))
        self.assertEqual(result.error_code, "BOOKING_LIMIT_EXCEEDED")

    def test_access_matrix(self):
        tool = AccessRightsTool()
        cases = (
            ("student", "coworking", True),
            ("student", "project_zone", True),
            ("student", "classroom", False),
            ("student", "lab", False),
            ("student", "conference_hall", False),
            ("group_lead", "classroom", True),
            ("teacher", "classroom", True),
            ("teacher", "conference_hall", False),
            ("special_staff", "lab", True),
            ("admin", "conference_hall", True),
        )
        for role, space_category, passed in cases:
            with self.subTest(role=role, space_category=space_category):
                result = tool.execute(replace(PARAMS, role=role, space_category=space_category))
                self.assertEqual(result.passed, passed)
                if not passed:
                    self.assertEqual(result.error_code, "ACCESS_DENIED")
                    self.assertEqual(result.details, {"role": role, "space_category": space_category})


class BookingServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = BookingService(FixedClock(NOW))

    def test_confirmed_job_is_reused_and_releases_space_if_cancelled(self):
        job = self.service.create(fields(PARAMS))
        self.assertEqual(job.status, "CONFIRMED")
        self.assertEqual(
            tuple(result.tool_name for result in job.history),
            ("access_rights", "limit_check", "conflict_check"),
        )
        self.assertIs(self.service.create(fields(PARAMS)), job)

        next_params = replace(PARAMS, job_id="conflicting")
        conflict = self.service.create(fields(next_params))
        self.assertEqual(conflict.status, "CANCELLED")
        self.assertEqual(conflict.history[-1].error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(conflict.history[-1].details["conflicting_job_id"], job.job_id)

        job.cancel(user_id=PARAMS.user_id, role=PARAMS.role)
        new_job = self.service.create(fields(replace(PARAMS, job_id="after-cancellation")))
        self.assertEqual(new_job.status, "CONFIRMED")

    def test_concurrent_requests_cannot_confirm_the_same_space(self):
        both_read = Barrier(2)
        get_bookings = InMemoryJobRepository.get_bookings_for_space

        def read_then_wait(repository, space_id):
            bookings = get_bookings(repository, space_id)
            both_read.wait(timeout=5)
            return bookings

        second = replace(PARAMS, job_id="other-job", user_id="other-user")
        with patch.object(InMemoryJobRepository, "get_bookings_for_space", read_then_wait):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self.service.create, fields(params))
                    for params in (PARAMS, second)
                ]
                jobs = [future.result(timeout=10) for future in futures]

        self.assertCountEqual([job.status for job in jobs], ["CONFIRMED", "CANCELLED"])
        confirmed = next(job for job in jobs if job.status == "CONFIRMED")
        cancelled = next(job for job in jobs if job.status == "CANCELLED")
        self.assertEqual(cancelled.history[-2], ToolResult(True, "conflict_check"))
        self.assertEqual(cancelled.history[-1].error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(cancelled.history[-1].details["conflicting_job_id"], confirmed.job_id)

    def test_repeated_local_time_is_compared_by_actual_booking_instants(self):
        service = BookingService(FixedClock(FOLD_NOW))
        first = replace(PARAMS, start_time=FOLD_START, end_time=FOLD_END)
        second = replace(
            first,
            job_id="after-clock-change",
            start_time=FOLD_START.replace(fold=1),
            end_time=FOLD_END.replace(fold=1),
        )

        confirmed = service.create(fields(first))
        later = service.create(fields(second))

        self.assertEqual(confirmed.status, "CONFIRMED")
        self.assertEqual(later.status, "CONFIRMED")
        self.assertEqual(later.params.start_time.fold, 1)
        self.assertIs(later.params.start_time.tzinfo, FOLD_START.tzinfo)

    def test_repeated_local_time_is_compared_by_actual_schedule_instants(self):
        schedule = InMemoryScheduleSource([
            ScheduleSlot(PARAMS.space_id, FOLD_START, FOLD_END)
        ])
        params = replace(
            PARAMS,
            role="teacher",
            space_category="classroom",
            start_time=FOLD_START.replace(fold=1),
            end_time=FOLD_END.replace(fold=1),
        )

        result = BookingService(FixedClock(FOLD_NOW), schedule=schedule).create(fields(params))

        self.assertEqual(result.status, "CONFIRMED")

    def test_room_schedule_conflict_retains_details(self):
        params = replace(PARAMS, role="teacher", space_category="classroom")
        schedule = InMemoryScheduleSource([ScheduleSlot(
            space_id=params.space_id,
            start_time=START,
            end_time=START + timedelta(hours=1),
        )])
        job = BookingService(FixedClock(NOW), schedule=schedule).create(fields(params))
        self.assertEqual(job.status, "CANCELLED")
        self.assertEqual(job.history[-1].error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(job.history[-1].details["conflict_type"], "schedule")

    def test_access_denial_stops_before_other_tools(self):
        job = self.service.create(fields(replace(PARAMS, space_category="lab")))
        self.assertEqual(job.status, "CANCELLED")
        self.assertEqual(tuple(result.error_code for result in job.history), ("ACCESS_DENIED",))

    def test_bad_input_identifies_field(self):
        fields_with_bad_end = fields(replace(PARAMS, end_time=PARAMS.start_time))
        with self.assertRaises(ValidationError) as error:
            self.service.create(fields_with_bad_end)
        self.assertEqual(error.exception.code, "VALIDATION_ERROR")
        self.assertEqual(error.exception.field, "end_time")

    def test_dispatcher_persists_exception_as_cancelled_job(self):
        class FailingTool:
            name = "failing"

            def execute(self, params: ToolParams) -> ToolResult:
                raise TimeoutError("Расписание недоступно")

        registry = Registry({"failing": FailingTool()})
        jobs = InMemoryJobRepository(required_checks=("failing",))
        dispatcher = Dispatcher(registry.resolve, ("failing",))
        with self.assertRaises(TimeoutError):
            dispatcher.create(fields(PARAMS), jobs.get_or_create)
        job = jobs.get_or_create(PARAMS)
        self.assertEqual(job.status, "CANCELLED")
        self.assertEqual(job.history[-1].error_code, "TimeoutError")
