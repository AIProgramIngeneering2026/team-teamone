from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from campusspot.dispatcher import PayloadMismatch
from campusspot.job import InvalidStateTransition
from campusspot.storage import (
    IdempotencyRecord,
    SqliteBookingService,
    SqliteJobRepository,
    apply_migrations_to_path,
    compute_payload_hash,
    get_applied_migrations,
)
from campusspot.tool_contract import ToolParams, ToolResult


TZ_YEKAT = timezone(timedelta(hours=5))


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 26, hour, minute, tzinfo=TZ_YEKAT)


def make_fields(
    *,
    job_id: str = "job-1",
    user_id: str = "user-1",
    role: str = "student",
    space_id: str = "CW-10",
    space_category: str = "coworking",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "user_id": user_id,
        "role": role,
        "space_id": space_id,
        "space_category": space_category,
        "start_time": start_time or dt(14, 0),
        "end_time": end_time or dt(16, 0),
    }


class SqliteStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "campusspot.db"
        apply_migrations_to_path(self.db_path)
        self.clock = FixedClock(dt(12, 0))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migrations_apply_to_empty_db_and_are_idempotent(self) -> None:
        fresh_db = Path(self._tmp.name) / "empty.db"

        first_run = apply_migrations_to_path(fresh_db)
        self.assertEqual(first_run, ["001_initial_schema.sql", "002_seed_spaces.sql"])

        # Создаём запись и убеждаемся, что повторный прогон миграций её сохраняет
        service = SqliteBookingService(fresh_db, self.clock)
        created = service.create(make_fields(job_id="job-mig-1"))
        self.assertEqual(created.status, "CONFIRMED")

        second_run = apply_migrations_to_path(fresh_db)
        self.assertEqual(second_run, [])

        conn = sqlite3.connect(str(fresh_db))
        try:
            self.assertEqual(
                get_applied_migrations(conn),
                ["001_initial_schema.sql", "002_seed_spaces.sql"],
            )
        finally:
            conn.close()

        reloaded = SqliteJobRepository(fresh_db).get("job-mig-1")
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "CONFIRMED")
        self.assertGreaterEqual(len(SqliteJobRepository(fresh_db).list_spaces()), 1)

    def test_confirmed_job_and_history_survive_restart(self) -> None:
        service = SqliteBookingService(self.db_path, self.clock)
        created = service.create(make_fields(job_id="job-restart-1"))
        self.assertEqual(created.status, "CONFIRMED")
        self.assertEqual(
            [check.tool_name for check in created.history],
            ["access_rights", "limit_check", "conflict_check"],
        )

        # Имитируем перезапуск сервиса: открываем абсолютно новый экземпляр репозитория
        restarted_repo = SqliteJobRepository(self.db_path)
        reloaded = restarted_repo.get("job-restart-1")
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "CONFIRMED")
        self.assertEqual(reloaded.params.space_id, "CW-10")
        self.assertEqual(reloaded.params.start_time, dt(14, 0))
        self.assertEqual(reloaded.params.end_time, dt(16, 0))
        self.assertEqual(len(reloaded.history), 3)
        self.assertTrue(all(check.passed for check in reloaded.history))

        upcoming = restarted_repo.list_upcoming_by_user("user-1", dt(12, 0))
        self.assertEqual([job.job_id for job in upcoming], ["job-restart-1"])

    def test_cancelled_job_failure_details_survive_restart(self) -> None:
        service = SqliteBookingService(self.db_path, self.clock)
        # Студент пытается забронировать лабораторию -> ACCESS_DENIED -> CANCELLED
        rejected = service.create(
            make_fields(
                job_id="job-denied",
                role="student",
                space_id="LAB-01",
                space_category="lab",
            )
        )
        self.assertEqual(rejected.status, "CANCELLED")

        reloaded = SqliteJobRepository(self.db_path).get("job-denied")
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "CANCELLED")
        self.assertEqual(len(reloaded.history), 1)
        self.assertEqual(reloaded.history[0].error_code, "ACCESS_DENIED")
        self.assertEqual(reloaded.history[0].details["space_category"], "lab")

    def test_status_update_and_history_write_rollback_together_on_error(self) -> None:
        repo = SqliteJobRepository(self.db_path)
        params = ToolParams(
            job_id="job-atomic-1",
            user_id="user-1",
            role="student",
            space_id="CW-10",
            space_category="coworking",
            start_time=dt(14, 0),
            end_time=dt(16, 0),
            extra={"operation": "create"},
        )
        job = repo.get_or_create(params)
        self.assertEqual(job.status, "REQUESTED")

        # Имитируем сбой при записи истории ПОСЛЕ успешного UPDATE jobs в той же транзакции
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_history_insert
                BEFORE INSERT ON job_check_history
                BEGIN
                    SELECT RAISE(ABORT, 'simulated history write failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        valid_checks = (
            ToolResult(True, "access_rights"),
            ToolResult(True, "limit_check"),
            ToolResult(True, "conflict_check"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            job.transition("CONFIRMED", checks=valid_checks)

        # Проверяем, что откат вернул и статус в БД, и историю в исходное состояние
        self.assertEqual(job.status, "REQUESTED")
        reloaded = SqliteJobRepository(self.db_path).get("job-atomic-1")
        assert reloaded is not None
        self.assertEqual(reloaded.status, "REQUESTED")
        self.assertEqual(reloaded.history, ())

    def test_concurrent_overlapping_requests_confirm_only_one_booking(self) -> None:
        barrier = threading.Barrier(2)

        def run_booking(job_id: str, user_id: str, start: datetime, end: datetime):
            service = SqliteBookingService(self.db_path, self.clock)
            barrier.wait(timeout=5)
            return service.create(
                make_fields(
                    job_id=job_id,
                    user_id=user_id,
                    space_id="CW-10",
                    start_time=start,
                    end_time=end,
                )
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(run_booking, "job-race-1", "user-1", dt(14, 0), dt(16, 0))
            f2 = pool.submit(run_booking, "job-race-2", "user-2", dt(15, 0), dt(17, 0))
            first = f1.result(timeout=10)
            second = f2.result(timeout=10)

        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, ["CANCELLED", "CONFIRMED"])

        cancelled = first if first.status == "CANCELLED" else second
        self.assertEqual(cancelled.history[-1].error_code, "SPACE_UNAVAILABLE")

        # Убеждаемся, что после повторного чтения из БД подтверждена ровно одна бронь
        repo = SqliteJobRepository(self.db_path)
        db_statuses = sorted(
            [
                repo.get("job-race-1").status,  # type: ignore[union-attr]
                repo.get("job-race-2").status,  # type: ignore[union-attr]
            ]
        )
        self.assertEqual(db_statuses, ["CANCELLED", "CONFIRMED"])

    def test_idempotency_keys_persist_across_restart_and_reject_payload_mismatch(self) -> None:
        payload_1 = {
            "space_id": "CW-10",
            "start_time": dt(14, 0),
            "end_time": dt(16, 0),
        }
        # Тот же момент времени в UTC должен давать идентичный отпечаток (ADR-0002)
        payload_1_utc = {
            "space_id": "CW-10",
            "start_time": dt(14, 0).astimezone(timezone.utc),
            "end_time": dt(16, 0).astimezone(timezone.utc),
        }
        payload_other = {
            "space_id": "CW-11",
            "start_time": dt(14, 0),
            "end_time": dt(16, 0),
        }

        hash_1 = compute_payload_hash("create_job", payload_1)
        hash_1_utc = compute_payload_hash("create_job", payload_1_utc)
        hash_other = compute_payload_hash("create_job", payload_other)
        self.assertEqual(hash_1, hash_1_utc)
        self.assertNotEqual(hash_1, hash_other)

        repo = SqliteJobRepository(self.db_path)
        saved = repo.save_idempotency_record(
            IdempotencyRecord(
                user_id="user-1",
                idempotency_key="req-001",
                operation="create_job",
                payload_hash=hash_1,
                job_id="job-105",
                response_status=201,
                response_body={"job_id": "job-105", "status": "CONFIRMED"},
            )
        )
        self.assertIsNotNone(saved.created_at)

        # Имитируем перезапуск: новый экземпляр SqliteJobRepository читает ключ из БД
        restarted = SqliteJobRepository(self.db_path)
        replay = restarted.check_idempotency(
            "user-1",
            "req-001",
            operation="create_job",
            payload_hash=hash_1_utc,
        )
        self.assertIsNotNone(replay)
        assert replay is not None
        self.assertEqual(replay.response_status, 201)
        self.assertEqual(replay.job_id, "job-105")
        self.assertEqual(replay.response_body["status"], "CONFIRMED")

        # Тот же ключ с другим телом -> PayloadMismatch (IDEMPOTENCY_KEY_PAYLOAD_MISMATCH)
        with self.assertRaises(PayloadMismatch) as caught:
            restarted.check_idempotency(
                "user-1",
                "req-001",
                operation="create_job",
                payload_hash=hash_other,
            )
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH")

        # Тот же ключ у другого пользователя независим
        self.assertIsNone(
            restarted.check_idempotency(
                "user-2",
                "req-001",
                operation="create_job",
                payload_hash=hash_other,
            )
        )

    def test_check_in_persists_and_blocks_subsequent_cancel(self) -> None:
        service = SqliteBookingService(self.db_path, self.clock)
        job = service.create(make_fields(job_id="job-checkin-1"))
        self.assertEqual(job.status, "CONFIRMED")

        job.check_in(dt(14, 5), user_id="user-1")
        reloaded = SqliteJobRepository(self.db_path).get("job-checkin-1")
        assert reloaded is not None
        self.assertEqual(reloaded.status, "USED")

        with self.assertRaises(InvalidStateTransition):
            reloaded.cancel(user_id="user-1", role="student")

        # Статус в БД после отклонённого перехода остался USED
        after_reject = SqliteJobRepository(self.db_path).get("job-checkin-1")
        assert after_reject is not None
        self.assertEqual(after_reject.status, "USED")
