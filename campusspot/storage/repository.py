"""Постоянное хранилище SQLite для Job, истории проверок и ключей идемпотентности."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Protocol, cast

from ..access_rights import AccessRightsTool
from ..booking import BOOKING_CHECKS
from ..conflict_check import BookingSlot, ConflictCheckTool, ScheduleSlot, ScheduleSource
from ..dispatcher import Dispatcher, PayloadMismatch, SlotConflict
from ..job import Job
from ..limit_check import Clock, LimitCheckTool
from ..registry import Registry
from ..tool_contract import ToolParams, ToolResult


_UTC_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utc_key(value: datetime) -> str:
    """Форматирует datetime в UTC-строку с фиксированной шириной для точного сравнения в SQL."""
    if value.utcoffset() is None:
        raise ValueError("Ожидался datetime с часовым поясом")
    return value.astimezone(timezone.utc).strftime(_UTC_FMT)


def _now_utc_key() -> str:
    return datetime.now(timezone.utc).strftime(_UTC_FMT)


def _to_jsonable(value: Any) -> Any:
    """Рекурсивно преобразует замороженные метаданные Job и datetime в JSON-совместимые типы."""
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_to_jsonable(item) for item in value)
    if isinstance(value, datetime):
        if value.utcoffset() is not None:
            return _utc_key(value)
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def compute_payload_hash(
    operation: str,
    payload: Mapping[str, Any],
    *,
    target_resource_id: str = "",
) -> str:
    """Вычисляет детерминированный SHA-256 отпечаток запроса с нормализацией времени в UTC."""
    canonical_obj = {
        "operation": operation,
        "target_resource_id": target_resource_id,
        "payload": _to_jsonable(payload),
    }
    encoded = json.dumps(
        canonical_obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SpaceRecord:
    """Запись каталога помещений кампуса."""

    space_id: str
    category: str
    name: str


@dataclass(frozen=True)
class IdempotencyRecord:
    """Сохранённый в БД снимок результата идемпотентной операции (ADR-0002)."""

    user_id: str
    idempotency_key: str
    operation: str
    payload_hash: str
    response_status: int
    response_body: dict[str, Any]
    target_resource_id: str = ""
    job_id: str | None = None
    created_at: str | None = None


class JobRepository(Protocol):
    """Интерфейс хранилища заявок, независимый от конкретной СУБД."""

    def get(self, job_id: str) -> Job | None: ...

    def get_or_create(self, params: ToolParams) -> Job: ...

    def count_active_by_user(self, user_id: str, *, exclude_job_id: str) -> int: ...

    def get_bookings_for_space(self, space_id: str) -> list[BookingSlot]: ...


class _UtcConflictCheckTool(ConflictCheckTool):
    def execute(self, params: ToolParams) -> ToolResult:
        return super().execute(
            replace(
                params,
                start_time=params.start_time.astimezone(timezone.utc),
                end_time=params.end_time.astimezone(timezone.utc),
            )
        )


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


class _SqliteJob(Job):
    """Обёртка над доменным Job, выполняющая переходы атомарно в SQLite."""

    def __init__(
        self,
        params: ToolParams,
        *,
        required_checks: tuple[str, ...],
        repo: SqliteJobRepository,
        status: str = "REQUESTED",
        history: tuple[ToolResult, ...] = (),
    ) -> None:
        super().__init__(params, required_checks=required_checks)
        self._repo = repo
        self._status = status
        self._history = history

    def transition(
        self,
        status: str,
        *,
        checks: tuple[ToolResult, ...] = (),
        now: datetime | None = None,
    ) -> None:
        self._repo._transition_job(self, status, checks=checks, now=now)


class SqliteJobRepository:
    """Хранилище Job, истории проверок, помещений и ключей идемпотентности в SQLite.

    Схема БД должна быть предварительно создана через apply_migrations / apply_migrations_to_path.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        required_checks: tuple[str, ...] = BOOKING_CHECKS,
    ) -> None:
        self._db_path = Path(db_path)
        self._required_checks = tuple(required_checks)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # --- Работа с каталогом помещений ---

    def get_space(self, space_id: str) -> SpaceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT space_id, category, name FROM spaces WHERE space_id = ?",
                (space_id,),
            ).fetchone()
            if row is None:
                return None
            return SpaceRecord(
                space_id=row["space_id"],
                category=row["category"],
                name=row["name"],
            )

    def list_spaces(self, *, category: str | None = None) -> list[SpaceRecord]:
        with self._connect() as conn:
            if category is None:
                rows = conn.execute(
                    "SELECT space_id, category, name FROM spaces ORDER BY space_id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT space_id, category, name FROM spaces
                    WHERE category = ?
                    ORDER BY space_id ASC
                    """,
                    (category,),
                ).fetchall()
            return [
                SpaceRecord(
                    space_id=row["space_id"],
                    category=row["category"],
                    name=row["name"],
                )
                for row in rows
            ]

    # --- Работа с Job и историей проверок ---

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            return self._load_job(conn, job_id)

    def get_or_create(self, params: ToolParams) -> Job:
        now_key = _now_utc_key()
        extra_json = json.dumps(_to_jsonable(params.extra), ensure_ascii=False, sort_keys=True)
        checks_json = json.dumps(list(self._required_checks), ensure_ascii=False)

        with self._write_transaction() as conn:
            existing = self._load_job(conn, params.job_id)
            if existing is not None:
                return existing

            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, role, space_id, space_category,
                    start_time_iso, end_time_iso, start_time_utc, end_time_utc,
                    actual_end_time_iso, actual_end_time_utc, status,
                    extra_json, required_checks_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'REQUESTED', ?, ?, ?, ?)
                """,
                (
                    params.job_id,
                    params.user_id,
                    params.role,
                    params.space_id,
                    params.space_category,
                    params.start_time.isoformat(),
                    params.end_time.isoformat(),
                    _utc_key(params.start_time),
                    _utc_key(params.end_time),
                    extra_json,
                    checks_json,
                    now_key,
                    now_key,
                ),
            )
            return _SqliteJob(
                params,
                required_checks=self._required_checks,
                repo=self,
                status="REQUESTED",
                history=(),
            )

    def list_upcoming_by_user(self, user_id: str, now: datetime) -> list[Job]:
        """Возвращает предстоящие подтверждённые брони пользователя по возрастанию start_time (UC-07)."""
        now_key = _utc_key(now)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE user_id = ? AND status = 'CONFIRMED' AND start_time_utc > ?
                ORDER BY start_time_utc ASC
                """,
                (user_id, now_key),
            ).fetchall()
            jobs: list[Job] = []
            for row in rows:
                loaded = self._load_job(conn, row["job_id"])
                if loaded is not None:
                    jobs.append(loaded)
            return jobs

    def count_active_by_user(self, user_id: str, *, exclude_job_id: str | None = None) -> int:
        with self._connect() as conn:
            if exclude_job_id is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE user_id = ? AND status IN ('REQUESTED', 'CONFIRMED')
                    """,
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE user_id = ? AND job_id != ? AND status IN ('REQUESTED', 'CONFIRMED')
                    """,
                    (user_id, exclude_job_id),
                ).fetchone()
            return int(row[0]) if row is not None else 0

    def get_bookings_for_space(self, space_id: str) -> list[BookingSlot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id, space_id, start_time_iso, end_time_iso, actual_end_time_iso, status
                FROM jobs
                WHERE space_id = ?
                """,
                (space_id,),
            ).fetchall()
            return [
                BookingSlot(
                    job_id=row["job_id"],
                    space_id=row["space_id"],
                    start_time=datetime.fromisoformat(row["start_time_iso"]).astimezone(timezone.utc),
                    end_time=datetime.fromisoformat(row["end_time_iso"]).astimezone(timezone.utc),
                    status=row["status"],
                    actual_end_time=(
                        datetime.fromisoformat(row["actual_end_time_iso"]).astimezone(timezone.utc)
                        if row["actual_end_time_iso"] is not None
                        else None
                    ),
                )
                for row in rows
            ]

    def _transition_job(
        self,
        job: _SqliteJob,
        status: str,
        *,
        checks: tuple[ToolResult, ...] = (),
        now: datetime | None = None,
    ) -> None:
        """Выполняет валидацию перехода, проверку пересечений (при CONFIRMED),
        обновление статуса Job и запись истории проверок в одной транзакции SQLite.
        """
        with self._write_transaction() as conn:
            db_job = self._load_job(conn, job.job_id)
            if db_job is None:
                raise LookupError(f"Заявка не найдена в БД: {job.job_id}")

            # Синхронизируем локальный экземпляр с актуальным состоянием в БД до валидации
            job._status = db_job._status
            job._history = db_job._history

            # Проверяем все правила переходов строго через доменный класс Job (без дублирования логики)
            probe = _SqliteJob(
                db_job.params,
                required_checks=db_job._required_checks,
                repo=self,
                status=db_job._status,
                history=db_job._history,
            )
            Job.transition(probe, status, checks=checks, now=now)

            # При подтверждении брони атомарно проверяем пересечения внутри BEGIN IMMEDIATE
            if status == "CONFIRMED":
                conflict_row = conn.execute(
                    """
                    SELECT job_id, status, start_time_iso, end_time_iso, actual_end_time_iso
                    FROM jobs
                    WHERE space_id = ?
                      AND job_id != ?
                      AND status IN ('CONFIRMED', 'USED')
                      AND start_time_utc < ?
                      AND (
                          CASE
                              WHEN actual_end_time_utc IS NOT NULL AND actual_end_time_utc < end_time_utc
                              THEN actual_end_time_utc
                              ELSE end_time_utc
                          END
                      ) > ?
                    LIMIT 1
                    """,
                    (
                        db_job.params.space_id,
                        db_job.job_id,
                        _utc_key(db_job.params.end_time),
                        _utc_key(db_job.params.start_time),
                    ),
                ).fetchone()
                if conflict_row is not None:
                    busy_end_iso = (
                        conflict_row["actual_end_time_iso"]
                        if conflict_row["actual_end_time_iso"] is not None
                        and datetime.fromisoformat(conflict_row["actual_end_time_iso"])
                        < datetime.fromisoformat(conflict_row["end_time_iso"])
                        else conflict_row["end_time_iso"]
                    )
                    raise SlotConflict(
                        ToolResult(
                            passed=False,
                            tool_name="conflict_check",
                            error_code="SPACE_UNAVAILABLE",
                            message="Пространство уже забронировано на пересекающийся интервал времени.",
                            details={
                                "space_id": db_job.params.space_id,
                                "conflict_type": "booking",
                                "conflicting_job_id": conflict_row["job_id"],
                                "conflicting_status": conflict_row["status"],
                                "busy_start": conflict_row["start_time_iso"],
                                "busy_end": busy_end_iso,
                            },
                        )
                    )

            # В одной транзакции обновляем статус Job и дописываем новые записи истории проверок
            now_key = _now_utc_key()
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, now_key, job.job_id),
            )
            base_seq = len(db_job._history)
            for offset, check in enumerate(checks):
                conn.execute(
                    """
                    INSERT INTO job_check_history (
                        job_id, seq, passed, tool_name, error_code, message, details_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.job_id,
                        base_seq + offset,
                        1 if check.passed else 0,
                        check.tool_name,
                        check.error_code,
                        check.message,
                        json.dumps(_to_jsonable(check.details), ensure_ascii=False, sort_keys=True),
                        now_key,
                    ),
                )

        # Обновляем состояние в памяти только после успешного COMMIT в БД
        job._status = probe._status
        job._history = probe._history

    def _load_job(self, conn: sqlite3.Connection, job_id: str) -> _SqliteJob | None:
        row = conn.execute(
            """
            SELECT job_id, user_id, role, space_id, space_category,
                   start_time_iso, end_time_iso, status, extra_json, required_checks_json
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        history_rows = conn.execute(
            """
            SELECT passed, tool_name, error_code, message, details_json
            FROM job_check_history
            WHERE job_id = ?
            ORDER BY seq ASC
            """,
            (job_id,),
        ).fetchall()

        params = ToolParams(
            job_id=row["job_id"],
            user_id=row["user_id"],
            role=row["role"],
            space_id=row["space_id"],
            space_category=row["space_category"],
            start_time=datetime.fromisoformat(row["start_time_iso"]),
            end_time=datetime.fromisoformat(row["end_time_iso"]),
            extra=json.loads(row["extra_json"]),
        )
        required_checks = tuple(json.loads(row["required_checks_json"]))
        history = tuple(
            ToolResult(
                passed=bool(item["passed"]),
                tool_name=item["tool_name"],
                error_code=item["error_code"],
                message=item["message"],
                details=json.loads(item["details_json"]),
            )
            for item in history_rows
        )
        return _SqliteJob(
            params,
            required_checks=required_checks,
            repo=self,
            status=row["status"],
            history=history,
        )

    # --- Работа с ключами идемпотентности в БД (ADR-0002) ---

    def get_idempotency_record(
        self,
        user_id: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        with self._connect() as conn:
            return self._load_idempotency_record(conn, user_id, idempotency_key)

    def check_idempotency(
        self,
        user_id: str,
        idempotency_key: str,
        *,
        operation: str,
        payload_hash: str,
        target_resource_id: str = "",
    ) -> IdempotencyRecord | None:
        """Проверяет ключ идемпотентности в БД.

        - Если ключа нет -> возвращает None.
        - Если ключ есть и (operation, target_resource_id, payload_hash) совпадают -> возвращает сохранённый снимок.
        - Если ключ использован с другим отпечатком/операцией/ресурсом -> выбрасывает PayloadMismatch (409).
        """
        existing = self.get_idempotency_record(user_id, idempotency_key)
        if existing is None:
            return None
        if (
            existing.operation != operation
            or existing.target_resource_id != target_resource_id
            or existing.payload_hash != payload_hash
        ):
            raise PayloadMismatch(idempotency_key)
        return existing

    def save_idempotency_record(self, record: IdempotencyRecord) -> IdempotencyRecord:
        """Атомарно сохраняет снимок ответа по ключу (user_id, idempotency_key) в БД."""
        created_at = record.created_at or _now_utc_key()
        body_json = json.dumps(
            _to_jsonable(record.response_body),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._write_transaction() as conn:
            existing = self._load_idempotency_record(conn, record.user_id, record.idempotency_key)
            if existing is not None:
                if (
                    existing.operation != record.operation
                    or existing.target_resource_id != record.target_resource_id
                    or existing.payload_hash != record.payload_hash
                ):
                    raise PayloadMismatch(record.idempotency_key)
                return existing

            conn.execute(
                """
                INSERT INTO idempotency_keys (
                    user_id, idempotency_key, operation, target_resource_id,
                    payload_hash, job_id, response_status, response_body_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.user_id,
                    record.idempotency_key,
                    record.operation,
                    record.target_resource_id,
                    record.payload_hash,
                    record.job_id,
                    record.response_status,
                    body_json,
                    created_at,
                ),
            )
            return replace(record, created_at=created_at)

    def _load_idempotency_record(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        row = conn.execute(
            """
            SELECT user_id, idempotency_key, operation, target_resource_id,
                   payload_hash, job_id, response_status, response_body_json, created_at
            FROM idempotency_keys
            WHERE user_id = ? AND idempotency_key = ?
            """,
            (user_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            user_id=row["user_id"],
            idempotency_key=row["idempotency_key"],
            operation=row["operation"],
            target_resource_id=row["target_resource_id"],
            payload_hash=row["payload_hash"],
            job_id=row["job_id"],
            response_status=int(row["response_status"]),
            response_body=json.loads(row["response_body_json"]),
            created_at=row["created_at"],
        )


class SqliteBookingService:
    """Сборка сценария создания брони (UC-02) поверх SqliteJobRepository."""

    def __init__(
        self,
        db_path: str | Path,
        clock: Clock,
        *,
        schedule: ScheduleSource | None = None,
    ) -> None:
        self._jobs = SqliteJobRepository(db_path)
        registry = Registry(
            {
                "access_rights": AccessRightsTool(),
                "limit_check": LimitCheckTool(clock, self._jobs),
                "conflict_check": _UtcConflictCheckTool(
                    booking_source=self._jobs,
                    schedule_source=_UtcScheduleSource(schedule) if schedule is not None else None,
                ),
            }
        )
        self._dispatcher = Dispatcher(registry.resolve, BOOKING_CHECKS)

    @property
    def repository(self) -> SqliteJobRepository:
        return self._jobs

    def create(self, fields: Mapping[str, object]) -> Job:
        return cast(Job, self._dispatcher.create(fields, self._jobs.get_or_create))
