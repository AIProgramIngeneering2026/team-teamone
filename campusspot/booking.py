"""Сборка сценария создания брони с зависимостями в памяти."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from threading import Lock
from typing import cast

from .access_rights import AccessRightsTool
from .conflict_check import (
    BookingSlot,
    ConflictCheckTool,
    InMemoryBookingSource,
    ScheduleSlot,
    ScheduleSource,
)
from .dispatcher import Dispatcher, SlotConflict
from .job import Job
from .limit_check import Clock, LimitCheckTool
from .registry import Registry
from .tool_contract import ToolParams, ToolResult


BOOKING_CHECKS = ("access_rights", "limit_check", "conflict_check")
_ACTIVE_STATUSES = frozenset({"REQUESTED", "CONFIRMED"})


class _UtcConflictCheckTool(ConflictCheckTool):
    def execute(self, params: ToolParams) -> ToolResult:
        return super().execute(replace(
            params,
            start_time=params.start_time.astimezone(timezone.utc),
            end_time=params.end_time.astimezone(timezone.utc),
        ))


class _UtcScheduleSource:
    def __init__(self, source: ScheduleSource) -> None:
        self._source = source

    def get_slots_for_space(self, space_id: str) -> list[ScheduleSlot]:
        return [
            replace(
                slot,
                start_time=slot.start_time.astimezone(timezone.utc),
                end_time=slot.end_time.astimezone(timezone.utc),
            )
            for slot in self._source.get_slots_for_space(space_id)
        ]


class _InMemoryJob(Job):
    def __init__(
        self,
        params: ToolParams,
        *,
        required_checks: tuple[str, ...],
        confirm: Callable[[Job, tuple[ToolResult, ...]], None],
    ) -> None:
        super().__init__(params, required_checks=required_checks)
        self._confirm = confirm

    def transition(
        self,
        status: str,
        *,
        checks: tuple[ToolResult, ...] = (),
        now: datetime | None = None,
    ) -> None:
        if status == "CONFIRMED":
            self._confirm(self, checks)
        else:
            super().transition(status, checks=checks, now=now)


class InMemoryJobRepository:
    def __init__(self, *, required_checks: tuple[str, ...] = BOOKING_CHECKS) -> None:
        self._required_checks = required_checks
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def get_or_create(self, params: ToolParams) -> Job:
        with self._lock:
            job = self._jobs.get(params.job_id)
            if job is None:
                job = _InMemoryJob(
                    params, required_checks=self._required_checks, confirm=self._confirm
                )
                self._jobs[params.job_id] = job
            return job

    def _confirm(self, job: Job, checks: tuple[ToolResult, ...]) -> None:
        with self._lock:
            if job.status == "REQUESTED":
                # Проверяем только брони. Учебное расписание уже проверено
                # инструментом диспетчера до захвата блокировки.
                bookings = InMemoryBookingSource(
                    self._booking_slots_for_space(job.params.space_id)
                )
                result = _UtcConflictCheckTool(booking_source=bookings).execute(job.params)
                if not result.passed:
                    raise SlotConflict(result)

            # Вызываем доменный переход, минуя повторный вход в хранилище.
            Job.transition(job, "CONFIRMED", checks=checks)

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
            return self._booking_slots_for_space(space_id)

    def _booking_slots_for_space(self, space_id: str) -> list[BookingSlot]:
        return [
            BookingSlot(
                job_id=job.job_id,
                space_id=job.params.space_id,
                start_time=job.params.start_time.astimezone(timezone.utc),
                end_time=job.params.end_time.astimezone(timezone.utc),
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
            "conflict_check": _UtcConflictCheckTool(
                booking_source=self._jobs,
                schedule_source=_UtcScheduleSource(schedule) if schedule is not None else None,
            ),
        })
        self._dispatcher = Dispatcher(registry.resolve, BOOKING_CHECKS)

    def create(self, fields: Mapping[str, object]) -> Job:
        return cast(Job, self._dispatcher.create(fields, self._jobs.get_or_create))
