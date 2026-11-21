"""Синхронное выполнение обязательных проверок новой брони (UC-02)."""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol

from .tool_contract import Tool, ToolParams, ToolResult


class BookingJob(Protocol):
    """Часть интерфейса Job, необходимая диспетчеру для создания брони."""

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
        if not isinstance(value, str) or not value:
            raise ValidationError(key)
        return value

    def identifier(key: str) -> str:
        value = required_text(key)
        if len(value) > 128:
            raise ValidationError(key)
        return value

    def required_time(key: str) -> datetime:
        value = fields.get(key)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValidationError(key)
        return value

    start = required_time("start_time")
    end = required_time("end_time")
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise ValidationError("end_time")
    role = required_text("role")
    if role not in {"student", "group_lead", "teacher", "special_staff", "admin"}:
        raise ValidationError("role")
    category = required_text("space_category")
    if category not in {"coworking", "project_zone", "classroom", "lab", "conference_hall"}:
        raise ValidationError("space_category")
    return ToolParams(
        job_id=identifier("job_id"), user_id=identifier("user_id"),
        role=role, space_id=identifier("space_id"),
        space_category=category, start_time=start,
        end_time=end, extra={"operation": "create"},
    )


class DispatchInProgress(RuntimeError):
    """Проверки этой брони уже выполняются в процессе."""


# Все диспетчеры процесса разделяют множество выполняемых job_id. Блокировка
# защищает изменения множества, поэтому проверки разных броней могут идти параллельно.
_active: set[str] = set()
_guard = Lock()


class Dispatcher:
    def __init__(self, resolve: Callable[[str], Tool], checks: Iterable[str]):
        self._resolve = resolve
        self._checks = tuple(checks)
        if not self._checks:
            raise ValueError("Список обязательных проверок пуст")

    def create(
        self, fields: Mapping[str, object], get_or_create_job: Callable[[ToolParams], BookingJob]
    ) -> BookingJob:
        """Хранилище возвращает тот же Job при повторе job_id."""
        params = creation_params(fields)
        with _guard:
            if params.job_id in _active:
                raise DispatchInProgress(params.job_id)
            _active.add(params.job_id)
        try:
            # resolve — переданный при сборке метод Registry. Ищем сразу все Tool:
            # опечатка даже в последнем имени должна выявиться до создания Job.
            tools = [self._resolve(name) for name in self._checks]
            job = get_or_create_job(params)
            if job.job_id != params.job_id:
                raise ValidationError("job_id")
            # При повторе хранилище возвращает прежний Job. Его итоговый статус
            # означает, что цепочка уже завершилась и её результат можно вернуть.
            if job.status == "REQUESTED":
                self._run(job, params, tools)
            return job
        finally:
            with _guard:
                _active.remove(params.job_id)

    def _run(self, job: BookingJob, params: ToolParams, tools: list[Tool]) -> None:
        history = []
        for tool in tools:
            try:
                result = tool.execute(params)
            except Exception as exc:
                # Job получает историю до сбоя включительно, а вызывающий код —
                # исходное исключение, чтобы различать, например, таймаут и отказ.
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
