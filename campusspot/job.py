"""Жизненный цикл брони и история проверок CampusSpot."""

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import MappingProxyType

from .tool_contract import ToolParams, ToolResult


_ALLOWED_TRANSITIONS = {
    "REQUESTED": frozenset({"CONFIRMED", "CANCELLED"}),
    "CONFIRMED": frozenset({"USED", "CANCELLED", "EXPIRED"}),
    "USED": frozenset(),
    "CANCELLED": frozenset(),
    "EXPIRED": frozenset(),
}
_CHECK_IN_WINDOW = timedelta(minutes=15)


class InvalidStateTransition(ValueError):
    code = "INVALID_STATE_TRANSITION"


class CheckInWindowClosed(ValueError):
    code = "CHECK_IN_WINDOW_CLOSED"


class AccessDenied(PermissionError):
    code = "ACCESS_DENIED"


class Job:
    def __init__(self, params: ToolParams, *, required_checks: Iterable[str]) -> None:
        self._params = params
        self._required_checks = tuple(required_checks)
        if not self._required_checks:
            raise ValueError("Для подтверждения брони нужны обязательные проверки")

        self._status = "REQUESTED"
        self._history: tuple[ToolResult, ...] = ()
        self._lock = RLock()

    @property
    def params(self) -> ToolParams:
        return self._params

    @property
    def job_id(self) -> str:
        return self._params.job_id

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def history(self) -> tuple[ToolResult, ...]:
        with self._lock:
            return self._history

    def transition(
        self,
        status: str,
        *,
        checks: tuple[ToolResult, ...] = (),
        now: datetime | None = None,
    ) -> None:
        """Проверить переход и сохранить статус вместе с результатами Tool."""
        with self._lock:
            if status not in _ALLOWED_TRANSITIONS[self._status]:
                raise InvalidStateTransition(
                    f"Переход {self._status} → {status} запрещён"
                )

            if status == "CONFIRMED":
                names = tuple(result.tool_name for result in checks)
                if names != self._required_checks or not all(
                    result.passed for result in checks
                ):
                    raise InvalidStateTransition("Обязательные проверки не пройдены")

            if status in {"USED", "EXPIRED"}:
                self._validate_window(status, now)

            self._history += tuple(
                replace(result, details=MappingProxyType(dict(result.details)))
                for result in checks
            )
            self._status = status

    def _validate_window(self, status: str, now: datetime | None) -> None:
        if now is None or now.utcoffset() is None:
            raise ValueError("Передайте текущее время с часовым поясом")

        start = self._params.start_time.astimezone(timezone.utc)
        end = self._params.end_time.astimezone(timezone.utc)
        deadline = min(start + _CHECK_IN_WINDOW, end)
        instant = now.astimezone(timezone.utc)
        if status == "USED" and not start <= instant < deadline:
            raise CheckInWindowClosed("Текущее время вне окна явки")
        if status == "EXPIRED" and instant < deadline:
            raise InvalidStateTransition("Окно явки ещё открыто")

    def check_in(self, now: datetime, *, user_id: str) -> None:
        with self._lock:
            if user_id != self._params.user_id:
                raise AccessDenied("Подтвердить явку может только владелец брони")
            if self._status != "USED":
                self.transition("USED", now=now)

    def cancel(self, *, user_id: str, role: str) -> None:
        with self._lock:
            if user_id != self._params.user_id and role != "admin":
                raise AccessDenied("Отменить бронь может владелец или администратор")
            if self._status != "CANCELLED":
                self.transition("CANCELLED")
