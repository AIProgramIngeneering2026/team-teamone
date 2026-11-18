from datetime import datetime, timezone, timedelta

import pytest

from campusspot.conflict_check import (
    BookingSlot,
    ConflictCheckTool,
    InMemoryBookingSource,
    InMemoryScheduleSource,
    ScheduleSlot,
    ToolParams,
    intervals_overlap,
)

TZ_YEKAT = timezone(timedelta(hours=5))


def dt(hour: int, minute: int = 0) -> datetime:
    """Вспомогательная функция создания времени 2026-11-20 ЧЧ:ММ+05:00."""
    return datetime(2026, 11, 20, hour, minute, tzinfo=TZ_YEKAT)


def make_params(
    start_time: datetime,
    end_time: datetime,
    space_id: str = "ROOM-204",
    job_id: str = "job-new",
    space_category: str = "classroom",
) -> ToolParams:
    return ToolParams(
        job_id=job_id,
        user_id="user-1",
        role="student",
        space_id=space_id,
        space_category=space_category,
        start_time=start_time,
        end_time=end_time,
    )


def test_free_slot_passes() -> None:
    """Свободный слот без занятий и чужих броней успешно проходит проверку."""
    tool = ConflictCheckTool(
        booking_source=InMemoryBookingSource(),
        schedule_source=InMemoryScheduleSource(),
    )
    result = tool.execute(make_params(dt(10, 30), dt(12, 0)))

    assert result.passed is True
    assert result.tool_name == "conflict_check"
    assert result.error_code is None


def test_conflict_with_scheduled_lesson_1030_1200_vs_1000_1130() -> None:
    """
    Критерий 1 из #9: Запрос на 10:30–12:00 при занятии с 10:00 до 11:30 в том же помещении
    возвращает ToolResult(passed=False, error_code='SPACE_UNAVAILABLE') с данными о конфликте.
    """
    schedule = InMemoryScheduleSource(
        [ScheduleSlot(space_id="ROOM-204", start_time=dt(10, 0), end_time=dt(11, 30))]
    )
    tool = ConflictCheckTool(schedule_source=schedule)

    result = tool.execute(make_params(dt(10, 30), dt(12, 0), space_id="ROOM-204"))

    assert result.passed is False
    assert result.tool_name == "conflict_check"
    assert result.error_code == "SPACE_UNAVAILABLE"
    assert result.details["space_id"] == "ROOM-204"
    assert result.details["conflict_type"] == "schedule"


@pytest.mark.parametrize("active_status", ["CONFIRMED", "USED"])
def test_conflict_with_confirmed_or_used_booking_1030_1200_vs_1000_1130(active_status: str) -> None:
    """
    Критерий 1 из #9: Запрос на 10:30–12:00 при другой брони в CONFIRMED или USED
    с 10:00 до 11:30 в том же помещении возвращает SPACE_UNAVAILABLE с данными о конфликте.
    """
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-existing",
                space_id="ROOM-204",
                start_time=dt(10, 0),
                end_time=dt(11, 30),
                status=active_status,
            )
        ]
    )
    tool = ConflictCheckTool(booking_source=bookings)

    result = tool.execute(make_params(dt(10, 30), dt(12, 0), space_id="ROOM-204"))

    assert result.passed is False
    assert result.error_code == "SPACE_UNAVAILABLE"
    assert result.details["space_id"] == "ROOM-204"
    assert result.details["conflict_type"] == "booking"
    assert result.details["conflicting_job_id"] == "job-existing"
    assert result.details["conflicting_status"] == active_status


def test_adjacent_intervals_1000_1100_and_1100_1200_pass() -> None:
    """
    Критерий 2 из #9: Соседние интервалы 10:00–11:00 и 11:00–12:00 проходят проверку.
    """
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-1",
                space_id="CW-10",
                start_time=dt(10, 0),
                end_time=dt(11, 0),
                status="CONFIRMED",
            )
        ]
    )
    tool = ConflictCheckTool(booking_source=bookings)

    # Слот сразу после (11:00–12:00)
    res_after = tool.execute(make_params(dt(11, 0), dt(12, 0), space_id="CW-10"))
    assert res_after.passed is True

    # Слот сразу перед (09:00–10:00)
    res_before = tool.execute(make_params(dt(9, 0), dt(10, 0), space_id="CW-10"))
    assert res_before.passed is True


@pytest.mark.parametrize(
    "req_start,req_end",
    [
        (dt(10, 0), dt(11, 30)),   # Полное совпадение
        (dt(10, 15), dt(11, 0)),   # Вложенный интервал внутри занятого
        (dt(9, 30), dt(12, 0)),    # Охватывающий интервал вокруг занятого
    ],
)
def test_exact_match_and_nested_intervals_fail(req_start: datetime, req_end: datetime) -> None:
    """
    Критерий 2 из #9: Полное совпадение и вложенный интервал дают конфликт.
    """
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-1",
                space_id="CW-10",
                start_time=dt(10, 0),
                end_time=dt(11, 30),
                status="CONFIRMED",
            )
        ]
    )
    tool = ConflictCheckTool(booking_source=bookings)

    result = tool.execute(make_params(req_start, req_end, space_id="CW-10"))
    assert result.passed is False
    assert result.error_code == "SPACE_UNAVAILABLE"


def test_busy_other_space_does_not_affect_result() -> None:
    """
    Критерий 2 из #9: Занятость другого помещения не влияет на результат.
    """
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-other",
                space_id="ROOM-999",
                start_time=dt(10, 0),
                end_time=dt(12, 0),
                status="CONFIRMED",
            )
        ]
    )
    schedule = InMemoryScheduleSource(
        [ScheduleSlot(space_id="ROOM-999", start_time=dt(10, 0), end_time=dt(12, 0))]
    )
    tool = ConflictCheckTool(booking_source=bookings, schedule_source=schedule)

    result = tool.execute(make_params(dt(10, 0), dt(12, 0), space_id="ROOM-204"))
    assert result.passed is True


@pytest.mark.parametrize("inactive_status", ["REQUESTED", "CANCELLED", "EXPIRED"])
def test_non_confirmed_bookings_do_not_cause_conflict(inactive_status: str) -> None:
    """Брони в статусах REQUESTED, CANCELLED и EXPIRED не блокируют слот."""
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-inactive",
                space_id="CW-10",
                start_time=dt(10, 0),
                end_time=dt(12, 0),
                status=inactive_status,
            )
        ]
    )
    tool = ConflictCheckTool(booking_source=bookings)

    result = tool.execute(make_params(dt(10, 0), dt(12, 0), space_id="CW-10"))
    assert result.passed is True


def test_same_job_id_is_excluded_for_reschedule() -> None:
    """Собственная бронь с тем же job_id исключается из проверки пересечений (задел под #19)."""
    bookings = InMemoryBookingSource(
        [
            BookingSlot(
                job_id="job-105",
                space_id="CW-10",
                start_time=dt(14, 0),
                end_time=dt(15, 0),
                status="CONFIRMED",
            )
        ]
    )
    tool = ConflictCheckTool(booking_source=bookings)

    result = tool.execute(make_params(dt(14, 30), dt(15, 30), space_id="CW-10", job_id="job-105"))
    assert result.passed is True


def test_intervals_overlap_formula() -> None:
    """Проверка базовой функции пересечения полуоткрытых интервалов."""
    assert intervals_overlap(dt(10, 30), dt(12, 0), dt(10, 0), dt(11, 30)) is True
    assert intervals_overlap(dt(10, 0), dt(11, 0), dt(11, 0), dt(12, 0)) is False
