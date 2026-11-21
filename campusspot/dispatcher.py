"""Синхронное выполнение обязательных проверок новой брони (UC-02)."""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from threading import Lock
from typing import Protocol

from .tool_contract import Tool, ToolParams, ToolResult


class BookingJob(Protocol):
    job_id: str
    status: str

    def transition(self, status: str, *, checks: tuple[ToolResult, ...]) -> None:
        """Job проверяет переход и сохраняет его вместе с историей проверок."""
        ...


class ValidationError(ValueError):
    code = "VALIDATION_ERROR"

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"Некорректное поле: {field}")


def creation_params(fields: Mapping[str, object]) -> ToolParams:
    """Подготовить параметры до создания Job. Время уже разобрано вызывающим слоем."""
    def required_text(key: str) -> str:
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(key)
        return value

    def required_time(key: str) -> datetime:
        value = fields.get(key)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValidationError(key)
        return value

    start = required_time("start_time")
    end = required_time("end_time")
    if end <= start:
        raise ValidationError("end_time")
    return ToolParams(
        job_id=required_text("job_id"), user_id=required_text("user_id"),
        role=required_text("role"), space_id=required_text("space_id"),
        space_category=required_text("space_category"), start_time=start,
        end_time=end, extra={"operation": "create"},
    )


class DispatchInProgress(RuntimeError):
    """Проверки этой брони уже выполняются в процессе."""


# Разные экземпляры Dispatcher в процессе могут получить одну и ту же бронь.
_active: set[str] = set()
_guard = Lock()


class Dispatcher:
    def __init__(self, resolve: Callable[[str], Tool], checks: Iterable[str]):
        self._resolve = resolve
        self._checks = tuple(checks)
        if not self._checks:
            raise ValueError("Список обязательных проверок пуст")

    def create(
        self, fields: Mapping[str, object], new_job: Callable[[ToolParams], BookingJob]
    ) -> BookingJob:
        params = creation_params(fields)
        # Неверная конфигурация Registry не должна оставить новую бронь в REQUESTED.
        tools = [self._resolve(name) for name in self._checks]
        job = new_job(params)
        self._run(job, params, tools)
        return job

    def _run(self, job: BookingJob, params: ToolParams, tools: list[Tool]) -> None:
        if params.job_id != job.job_id:
            raise ValidationError("job_id")
        with _guard:
            if job.job_id in _active:
                raise DispatchInProgress(job.job_id)
            if job.status != "REQUESTED":
                return
            _active.add(job.job_id)
        try:
            history = []
            for tool in tools:
                try:
                    result = tool.execute(params)
                except Exception as exc:
                    failure = ToolResult(
                        passed=False, tool_name=tool.name,
                        error_code=getattr(exc, "code", None) or type(exc).__name__,
                        message=str(exc),
                    )
                    job.transition("CANCELLED", checks=(*history, failure))
                    raise
                history.append(result)
                if not result.passed:
                    job.transition("CANCELLED", checks=tuple(history))
                    return
            job.transition("CONFIRMED", checks=tuple(history))
        finally:
            with _guard:
                _active.remove(job.job_id)
