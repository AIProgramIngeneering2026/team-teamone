"""Синхронное выполнение обязательных проверок новой брони (UC-02)."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol

from .tool_contract import Tool, ToolParams, ToolResult


class BookingJob(Protocol):
    """Часть интерфейса Job, необходимая диспетчеру для создания брони."""

    @property
    def job_id(self) -> str: ...

    @property
    def params(self) -> ToolParams: ...

    @property
    def status(self) -> str:
        """Строковый статус модели Job (включая перечисления на базе str)."""
        ...

    def transition(self, status: str, *, checks: tuple[ToolResult, ...]) -> None:
        """Сохранить допустимый переход и историю атомарно.

        При CONFIRMED хранилище проверяет пересечение интервалов в той же
        транзакции, что и резервирование. Занятый слот вызывает SlotConflict
        без изменения брони. Внешняя отмена даёт INVALID_STATE_TRANSITION.
        """
        ...


class ValidationError(ValueError):
    code = "VALIDATION_ERROR"

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"Некорректное поле: {field}")


class PayloadMismatch(ValueError):
    code = "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"


class SlotConflict(Exception):
    """Хранилище обнаружило пересечение при записи CONFIRMED."""

    def __init__(self, result: ToolResult):
        super().__init__(result.message)
        self.result = result


def creation_params(fields: Mapping[str, object]) -> ToolParams:
    """Подготовить параметры до создания Job. Время уже разобрано вызывающим слоем."""
    def required_text(key: str) -> str:
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
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

    allowed = {"job_id", "user_id", "role", "space_id", "space_category", "start_time", "end_time"}
    for key in fields:
        if key not in allowed:
            raise ValidationError(key)

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
    """Выполняет обязательные проверки при создании брони.

    Args:
        resolve: Функция поиска Tool по имени. Передайте метод registry.resolve.
        checks: Имена зарегистрированных проверок в порядке запуска,
            например ("access_rights", "conflict_check").
    """

    def __init__(self, resolve: Callable[[str], Tool], checks: Iterable[str]):
        """Сохранить функцию поиска resolve и порядок вызовов из checks.

        Raises:
            ValueError: В checks не передано ни одной проверки.
        """
        self._resolve = resolve
        self._checks = tuple(checks)
        if not self._checks:
            raise ValueError("Список обязательных проверок пуст")

    def create(
        self, fields: Mapping[str, object], get_or_create_job: Callable[[ToolParams], BookingJob]
    ) -> BookingJob:
        """Выполнить проверки создания брони.

        Args:
            fields: Словарь с job_id, user_id, role, space_id, space_category,
                start_time и end_time. Обе даты — datetime с часовым поясом.
            get_or_create_job: Функция, принимающая ToolParams. По params.job_id
                возвращает сохранённую бронь либо создаёт и сохраняет новую
                в состоянии REQUESTED с тем же идентификатором.

        Returns:
            Job после проверок. При повторе возвращается существующая бронь
            в её текущем состоянии.

        Raises:
            ValidationError: Ошибка в полях брони или несовпадение job_id.
            PayloadMismatch: Сохранённая бронь создана с другими параметрами.
            DispatchInProgress: В процессе уже выполняются проверки этой брони.
            UnknownOperation: Registry не нашёл одну из обязательных проверок.

        Если Tool выбросил исключение, после отмены брони оно передаётся вызывающему коду.
        """
        params = creation_params(fields)
        with _guard:
            if params.job_id in _active:
                raise DispatchInProgress(params.job_id)
            _active.add(params.job_id)
        try:
            job = get_or_create_job(params)
            if job.job_id != params.job_id:
                raise ValidationError("job_id")
            if _utc_params(job.params) != _utc_params(params):
                raise PayloadMismatch(params.job_id)
            status = job.status
            if not isinstance(status, str):
                raise TypeError("BookingJob.status должен быть строкой")
            if status == "REQUESTED":
                tools = [(name, self._resolve(name)) for name in self._checks]
                self._run(job, job.params, tools)
            return job
        finally:
            with _guard:
                _active.remove(params.job_id)

    def _run(self, job: BookingJob, params: ToolParams, tools: list[tuple[str, Tool]]) -> None:
        """Применить проверки к брони и передать историю в job.transition.

        Args:
            job: Бронь в состоянии REQUESTED.
            params: Проверенные параметры этой брони для вызовов Tool.execute.
            tools: Экземпляры Tool в порядке выполнения. Первый отказ или сбой
                останавливает дальнейшие вызовы.
        """
        history = []
        for name, tool in tools:
            if job.status == "CANCELLED":
                return
            try:
                result = tool.execute(params)
            except Exception as exc:
                # Job получает историю до сбоя включительно, а вызывающий код —
                # исходное исключение, чтобы различать, например, таймаут и отказ.
                failure = ToolResult(
                    passed=False, tool_name=name,
                    error_code=getattr(exc, "code", None) or type(exc).__name__,
                    message=str(exc),
                )
                if _transition(job, "CANCELLED", (*history, failure)):
                    raise
                return
            history.append(result)
            if not result.passed:
                _transition(job, "CANCELLED", tuple(history))
                return
        try:
            _transition(job, "CONFIRMED", tuple(history))
        except SlotConflict as conflict:
            _transition(job, "CANCELLED", (*history, conflict.result))


def _utc_params(params: ToolParams) -> ToolParams:
    """Сравнивать моменты времени с учётом смещения, включая повторный час осенью."""
    return replace(params, start_time=params.start_time.astimezone(timezone.utc),
                   end_time=params.end_time.astimezone(timezone.utc))


def _transition(job: BookingJob, status: str, checks: tuple[ToolResult, ...]) -> bool:
    """Вернуть False, если ручная отмена опередила результат проверки."""
    if job.status == "CANCELLED":
        return False
    try:
        job.transition(status, checks=checks)
    except Exception as exc:
        if getattr(exc, "code", None) == "INVALID_STATE_TRANSITION" and job.status == "CANCELLED":
            return False
        raise
    return True
