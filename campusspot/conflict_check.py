from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

# Если общий модуль контракта Tool (#36 / #40) уже влит в main — импортируем из него,
# иначе используем локальное определение из docs/tool-contract.md.
try:
    from .tool_contract import Tool, ToolParams, ToolResult  # type: ignore[import-not-found]
except ImportError:

    @dataclass(frozen=True)
    class ToolParams:
        """Входные параметры для выполнения проверки заявки на бронь."""
        job_id: str
        user_id: str
        role: str
        space_id: str
        space_category: str
        start_time: datetime
        end_time: datetime
        extra: Mapping[str, Any] = field(default_factory=dict)

    @dataclass(frozen=True)
    class ToolResult:
        """Единый формат результата работы любого Tool."""
        passed: bool
        tool_name: str
        error_code: Optional[str] = None
        message: Optional[str] = None
        details: Mapping[str, Any] = field(default_factory=dict)

    class Tool(Protocol):
        """Единый контракт инструмента проверки."""
        name: str

        def execute(self, params: ToolParams) -> ToolResult:
            ...


ACTIVE_BOOKING_STATUSES: frozenset[str] = frozenset({"CONFIRMED", "USED"})


def intervals_overlap(
    start: datetime,
    end: datetime,
    busy_start: datetime,
    busy_end: datetime,
) -> bool:
    """
    Проверяет пересечение двух полуоткрытых интервалов [start, end) и [busy_start, busy_end).
    Условие из задачи #9: start < busy_end and busy_start < end.
    Соседние интервалы (например, 10:00-11:00 и 11:00-12:00) не пересекаются.
    """
    return start < busy_end and busy_start < end


@dataclass(frozen=True)
class ScheduleSlot:
    """Запись об учебном занятии в расписании аудитории."""
    space_id: str
    start_time: datetime
    end_time: datetime
    title: str = "Учебное занятие"


@dataclass(frozen=True)
class BookingSlot:
    """Представление брони для проверки пересечений интервалов."""
    job_id: str
    space_id: str
    start_time: datetime
    end_time: datetime
    status: str
    actual_end_time: Optional[datetime] = None

    @property
    def effective_end_time(self) -> datetime:
        """Учитывает фактическое время освобождения (actual_end_time) для UC-10."""
        if self.actual_end_time is not None and self.actual_end_time < self.end_time:
            return self.actual_end_time
        return self.end_time


class ScheduleSource(Protocol):
    """Интерфейс источника данных учебного расписания (без привязки к HTTP/БД)."""

    def get_slots_for_space(self, space_id: str) -> Sequence[ScheduleSlot]:
        """Возвращает учебные занятия для указанного помещения."""
        ...


class BookingSource(Protocol):
    """Интерфейс источника данных о существующих бронях (без привязки к СУБД)."""

    def get_bookings_for_space(self, space_id: str) -> Sequence[BookingSlot]:
        """Возвращает список броней для указанного помещения."""
        ...


class InMemoryScheduleSource:
    """Реализация источника учебного расписания в памяти для тестов доменного ядра."""

    def __init__(self, slots: Optional[Iterable[ScheduleSlot]] = None) -> None:
        self._slots: list[ScheduleSlot] = list(slots) if slots is not None else []

    def add_slot(self, slot: ScheduleSlot) -> None:
        self._slots.append(slot)

    def get_slots_for_space(self, space_id: str) -> Sequence[ScheduleSlot]:
        return [slot for slot in self._slots if slot.space_id == space_id]


class InMemoryBookingSource:
    """Реализация хранилища броней в памяти для проверки конфликтов."""

    def __init__(self, bookings: Optional[Iterable[BookingSlot]] = None) -> None:
        self._bookings: list[BookingSlot] = list(bookings) if bookings is not None else []

    def add_booking(self, booking: BookingSlot) -> None:
        self._bookings.append(booking)

    def get_bookings_for_space(self, space_id: str) -> Sequence[BookingSlot]:
        return [booking for booking in self._bookings if booking.space_id == space_id]


class ConflictCheckTool:
    """
    Инструмент проверки пересечений запрашиваемого интервала с учебными занятиями
    и другими активными бронями (CONFIRMED / USED) по сценарию UC-05 (#9).
    Регистрируется в Registry под именем 'conflict_check'.
    """

    name: str = "conflict_check"

    def __init__(
        self,
        booking_source: Optional[BookingSource] = None,
        schedule_source: Optional[ScheduleSource] = None,
    ) -> None:
        self._bookings: BookingSource = booking_source or InMemoryBookingSource()
        self._schedule: ScheduleSource = schedule_source or InMemoryScheduleSource()

    def execute(self, params: ToolParams) -> ToolResult:
        """
        Проверяет отсутствие конфликтов на полуоткрытом интервале [params.start_time, params.end_time).
        Возвращает ToolResult(passed=False, error_code="SPACE_UNAVAILABLE") при пересечении.
        """
        # 1. Проверка пересечений с учебными занятиями — только для категории classroom (docs/tool-contract.md, п. 5.1)
        if params.space_category == "classroom":
            for lesson in self._schedule.get_slots_for_space(params.space_id):
                if intervals_overlap(
                    params.start_time,
                    params.end_time,
                    lesson.start_time,
                    lesson.end_time,
                ):
                    return ToolResult(
                        passed=False,
                        tool_name=self.name,
                        error_code="SPACE_UNAVAILABLE",
                        message="Пространство занято учебным занятием в выбранный интервал времени.",
                        details={
                            "space_id": params.space_id,
                            "conflict_type": "schedule",
                            "busy_start": lesson.start_time.isoformat(),
                            "busy_end": lesson.end_time.isoformat(),
                            "title": lesson.title,
                        },
                    )

        # 2. Проверка пересечений с другими подтверждёнными или используемыми бронями (CONFIRMED / USED)
        for booking in self._bookings.get_bookings_for_space(params.space_id):
            # Исключаем саму проверяемую заявку (для создания, переноса #19 и продления #20)
            if booking.job_id == params.job_id:
                continue

            # Учитываем только активные брони, занимающие помещение (CONFIRMED и USED)
            if booking.status not in ACTIVE_BOOKING_STATUSES:
                continue

            if intervals_overlap(
                params.start_time,
                params.end_time,
                booking.start_time,
                booking.effective_end_time,
            ):
                return ToolResult(
                    passed=False,
                    tool_name=self.name,
                    error_code="SPACE_UNAVAILABLE",
                    message="Пространство уже забронировано на пересекающийся интервал времени.",
                    details={
                        "space_id": params.space_id,
                        "conflict_type": "booking",
                        "conflicting_job_id": booking.job_id,
                        "conflicting_status": booking.status,
                        "busy_start": booking.start_time.isoformat(),
                        "busy_end": booking.effective_end_time.isoformat(),
                    },
                )

        return ToolResult(passed=True, tool_name=self.name)
