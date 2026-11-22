"""Проверка времени, длительности и числа активных броней."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .tool_contract import ToolParams, ToolResult


_MAX_DURATION_MINUTES = 240
_MAX_ACTIVE_BOOKINGS = 2


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedClock:
    """Часы с фиксированным временем для запуска ядра в памяти."""

    instant: datetime

    def now(self) -> datetime:
        return self.instant


class ActiveBookings(Protocol):
    def count_active_by_user(self, user_id: str, *, exclude_job_id: str) -> int: ...


class LimitCheckTool:
    name = "limit_check"

    def __init__(self, clock: Clock, bookings: ActiveBookings) -> None:
        self._clock = clock
        self._bookings = bookings

    def execute(self, params: ToolParams) -> ToolResult:
        start = params.start_time.astimezone(timezone.utc)
        end = params.end_time.astimezone(timezone.utc)
        now = self._clock.now().astimezone(timezone.utc)
        extending = params.extra.get("operation") == "extend"
        original_start = params.extra.get("original_start_time") if extending else start

        if (
            not now < start < end
            or not isinstance(original_start, datetime)
            or original_start.utcoffset() is None
        ):
            return ToolResult(
                passed=False,
                tool_name=self.name,
                error_code="INVALID_NEW_TIME",
                message="Временной интервал недопустим для этой операции",
                details={"field": "start_time"},
            )

        duration = end - original_start.astimezone(timezone.utc)
        if duration > timedelta(minutes=_MAX_DURATION_MINUTES) or (
            not extending
            and self._bookings.count_active_by_user(
                params.user_id, exclude_job_id=params.job_id
            ) >= _MAX_ACTIVE_BOOKINGS
        ):
            return ToolResult(
                passed=False,
                tool_name=self.name,
                error_code="BOOKING_LIMIT_EXCEEDED",
                message="Превышен лимит длительности или числа активных броней",
                details={
                    "max_duration_minutes": _MAX_DURATION_MINUTES,
                    "max_active_bookings": _MAX_ACTIVE_BOOKINGS,
                },
            )

        return ToolResult(passed=True, tool_name=self.name)
