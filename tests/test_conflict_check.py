import unittest
from datetime import datetime, timedelta, timezone

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


class FailingScheduleSource:
    """Тестовая заглушка недоступного источника расписания."""

    def __init__(self) -> None:
        self.called = False

    def get_slots_for_space(self, space_id: str):
        self.called = True
        raise RuntimeError("Schedule service unavailable")


class ConflictCheckToolTests(unittest.TestCase):
    def test_free_slot_passes(self) -> None:
        """Свободный слот без занятий и чужих броней успешно проходит проверку."""
        tool = ConflictCheckTool(
            booking_source=InMemoryBookingSource(),
            schedule_source=InMemoryScheduleSource(),
        )
        result = tool.execute(make_params(dt(10, 30), dt(12, 0)))

        self.assertTrue(result.passed)
        self.assertEqual(result.tool_name, "conflict_check")
        self.assertIsNone(result.error_code)

    def test_conflict_with_scheduled_lesson_1030_1200_vs_1000_1130(self) -> None:
        """Запрос на 10:30-12:00 при занятии 10:00-11:30 в classroom даёт SPACE_UNAVAILABLE."""
        schedule = InMemoryScheduleSource(
            [ScheduleSlot(space_id="ROOM-204", start_time=dt(10, 0), end_time=dt(11, 30))]
        )
        tool = ConflictCheckTool(schedule_source=schedule)

        result = tool.execute(
            make_params(dt(10, 30), dt(12, 0), space_id="ROOM-204", space_category="classroom")
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.tool_name, "conflict_check")
        self.assertEqual(result.error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(result.details["space_id"], "ROOM-204")
        self.assertEqual(result.details["conflict_type"], "schedule")

    def test_coworking_skips_unavailable_schedule_and_checks_bookings(self) -> None:
        """
        Для coworking источник учебного расписания не вызывается даже при его недоступности,
        а конфликты с другими бронями по-прежнему проверяются.
        """
        schedule = FailingScheduleSource()
        bookings = InMemoryBookingSource(
            [
                BookingSlot(
                    job_id="job-cw-1",
                    space_id="CW-10",
                    start_time=dt(10, 0),
                    end_time=dt(11, 30),
                    status="CONFIRMED",
                )
            ]
        )
        tool = ConflictCheckTool(booking_source=bookings, schedule_source=schedule)

        # Свободный слот в коворкинге проходит без обращения к расписанию
        free_result = tool.execute(
            make_params(dt(12, 0), dt(13, 0), space_id="CW-10", space_category="coworking")
        )
        self.assertTrue(free_result.passed)
        self.assertFalse(schedule.called)

        # Конфликтный слот в коворкинге отклоняется по броням без обращения к расписанию
        busy_result = tool.execute(
            make_params(dt(10, 30), dt(12, 0), space_id="CW-10", space_category="coworking")
        )
        self.assertFalse(busy_result.passed)
        self.assertEqual(busy_result.error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(busy_result.details["conflict_type"], "booking")
        self.assertFalse(schedule.called)

    def test_conflict_with_confirmed_or_used_booking(self) -> None:
        """Запрос на 10:30-12:00 при брони CONFIRMED/USED на 10:00-11:30 даёт SPACE_UNAVAILABLE."""
        for active_status in ("CONFIRMED", "USED"):
            with self.subTest(status=active_status):
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

                self.assertFalse(result.passed)
                self.assertEqual(result.error_code, "SPACE_UNAVAILABLE")
                self.assertEqual(result.details["space_id"], "ROOM-204")
                self.assertEqual(result.details["conflict_type"], "booking")
                self.assertEqual(result.details["conflicting_job_id"], "job-existing")
                self.assertEqual(result.details["conflicting_status"], active_status)

    def test_early_release_actual_end_time_frees_remaining_interval(self) -> None:
        """
        Бронь USED на 10:00-14:00 с досрочным освобождением в 11:00
        пропускает запрос 11:00-12:00 и отклоняет 10:30-11:30.
        """
        bookings = InMemoryBookingSource(
            [
                BookingSlot(
                    job_id="job-used-early",
                    space_id="CW-10",
                    start_time=dt(10, 0),
                    end_time=dt(14, 0),
                    status="USED",
                    actual_end_time=dt(11, 0),
                )
            ]
        )
        tool = ConflictCheckTool(booking_source=bookings)

        # Запрос после фактического освобождения (11:00-12:00) проходит
        free_res = tool.execute(
            make_params(dt(11, 0), dt(12, 0), space_id="CW-10", space_category="coworking")
        )
        self.assertTrue(free_res.passed)

        # Запрос, пересекающий фактический интервал 10:00-11:00 (10:30-11:30), отклоняется
        conflict_res = tool.execute(
            make_params(dt(10, 30), dt(11, 30), space_id="CW-10", space_category="coworking")
        )
        self.assertFalse(conflict_res.passed)
        self.assertEqual(conflict_res.error_code, "SPACE_UNAVAILABLE")
        self.assertEqual(conflict_res.details["busy_end"], dt(11, 0).isoformat())

    def test_adjacent_intervals_pass(self) -> None:
        """Соседние интервалы 10:00-11:00 и 11:00-12:00 (и 09:00-10:00) проходят проверку."""
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

        self.assertTrue(
            tool.execute(make_params(dt(11, 0), dt(12, 0), space_id="CW-10", space_category="coworking")).passed
        )
        self.assertTrue(
            tool.execute(make_params(dt(9, 0), dt(10, 0), space_id="CW-10", space_category="coworking")).passed
        )

    def test_exact_match_and_nested_intervals_fail(self) -> None:
        """Полное совпадение, вложенный и охватывающий интервалы дают конфликт."""
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

        cases = [
            ("exact_match", dt(10, 0), dt(11, 30)),
            ("nested", dt(10, 15), dt(11, 0)),
            ("enclosing", dt(9, 30), dt(12, 0)),
        ]
        for label, req_start, req_end in cases:
            with self.subTest(case=label):
                result = tool.execute(
                    make_params(req_start, req_end, space_id="CW-10", space_category="coworking")
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.error_code, "SPACE_UNAVAILABLE")

    def test_busy_other_space_does_not_affect_result(self) -> None:
        """Занятость другого помещения не влияет на результат."""
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
        self.assertTrue(result.passed)

    def test_non_confirmed_bookings_do_not_cause_conflict(self) -> None:
        """Брони в статусах REQUESTED, CANCELLED и EXPIRED не блокируют слот."""
        for inactive_status in ("REQUESTED", "CANCELLED", "EXPIRED"):
            with self.subTest(status=inactive_status):
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

                result = tool.execute(
                    make_params(dt(10, 0), dt(12, 0), space_id="CW-10", space_category="coworking")
                )
                self.assertTrue(result.passed)

    def test_same_job_id_is_excluded_for_reschedule(self) -> None:
        """Собственная бронь с тем же job_id исключается из проверки пересечений."""
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

        result = tool.execute(
            make_params(
                dt(14, 30),
                dt(15, 30),
                space_id="CW-10",
                job_id="job-105",
                space_category="coworking",
            )
        )
        self.assertTrue(result.passed)

    def test_intervals_overlap_formula(self) -> None:
        """Проверка базовой функции пересечения полуоткрытых интервалов."""
        self.assertTrue(intervals_overlap(dt(10, 30), dt(12, 0), dt(10, 0), dt(11, 30)))
        self.assertFalse(intervals_overlap(dt(10, 0), dt(11, 0), dt(11, 0), dt(12, 0)))
