"""Сборка сценария создания брони с зависимостями в памяти."""

from collections.abc import Mapping
from threading import Lock
from typing import cast

from .access_rights import AccessRightsTool
from .conflict_check import BookingSlot, ConflictCheckTool, ScheduleSource
from .dispatcher import Dispatcher
from .job import Job
from .limit_check import Clock, LimitCheckTool
from .registry import Registry
from .tool_contract import ToolParams


BOOKING_CHECKS = ("access_rights", "limit_check", "conflict_check")
_ACTIVE_STATUSES = frozenset({"REQUESTED", "CONFIRMED"})


class InMemoryJobRepository:
    def __init__(self, *, required_checks: tuple[str, ...] = BOOKING_CHECKS) -> None:
        self._required_checks = required_checks
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def get_or_create(self, params: ToolParams) -> Job:
        with self._lock:
            job = self._jobs.get(params.job_id)
            if job is None:
                job = Job(params, required_checks=self._required_checks)
                self._jobs[params.job_id] = job
            return job

    def count_active_by_user(self, user_id: str, *, exclude_job_id: str) -> int:
        with self._lock:
            return sum(
                job.job_id != exclude_job_id
                and job.params.user_id == user_id
                and job.status in _ACTIVE_STATUSES
                for job in self._jobs.values()
            )

    def get_bookings_for_space(self, space_id: str) -> list[BookingSlot]:
        with self._lock:
            return [
                BookingSlot(
                    job_id=job.job_id,
                    space_id=job.params.space_id,
                    start_time=job.params.start_time,
                    end_time=job.params.end_time,
                    status=job.status,
                )
                for job in self._jobs.values()
                if job.params.space_id == space_id
            ]


class BookingService:
    def __init__(self, clock: Clock, *, schedule: ScheduleSource | None = None) -> None:
        self._jobs = InMemoryJobRepository()
        registry = Registry({
            "access_rights": AccessRightsTool(),
            "limit_check": LimitCheckTool(clock, self._jobs),
            "conflict_check": ConflictCheckTool(
                booking_source=self._jobs, schedule_source=schedule
            ),
        })
        self._dispatcher = Dispatcher(registry.resolve, BOOKING_CHECKS)

    def create(self, fields: Mapping[str, object]) -> Job:
        return cast(Job, self._dispatcher.create(fields, self._jobs.get_or_create))
