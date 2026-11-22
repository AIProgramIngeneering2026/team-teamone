
from datetime import datetime, timedelta, timezone
import pytest

from campusspot.job import Job, InvalidStateTransition
from campusspot.dispatcher import (
    Dispatcher,
    DispatchInProgress,
    ValidationError,
    creation_params,
)
from campusspot.tool_contract import ToolParams, ToolResult

START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
CHECKS = ("access_rights", "limit_check", "conflict_check")


class FakeTool:
    def __init__(self, name: str, passed: bool = True, exception: Exception | None = None):
        self.name = name
        self.passed = passed
        self.exception = exception
        self.call_count = 0

    def execute(self, params: ToolParams) -> ToolResult:
        self.call_count += 1
        if self.exception:
            raise self.exception
        return ToolResult(passed=self.passed, tool_name=self.name)


def valid_fields(**overrides) -> dict:
    data = {
        "job_id": "job-38-test",
        "user_id": "student-1",
        "role": "student",
        "space_id": "cw-1",
        "space_category": "coworking",
        "start_time": START,
        "end_time": END,
    }
    data.update(overrides)
    return data


# 1. Обычный путь: REQUESTED -> проверки -> CONFIRMED -> USED
def test_happy_path_full_scenario():
    tools = {name: FakeTool(name, passed=True) for name in CHECKS}
    dispatcher = Dispatcher(resolve=lambda name: tools[name], checks=CHECKS)

    storage = {}
    def get_or_create(params: ToolParams) -> Job:
        job = Job(params, required_checks=CHECKS)
        storage[params.job_id] = job
        return job

    fields = valid_fields()
    job = dispatcher.create(fields, get_or_create)

    assert job.status == "CONFIRMED"
    for tool in tools.values():
        assert tool.call_count == 1

    check_in_time = START + timedelta(minutes=5)
    job.check_in(check_in_time, user_id=fields["user_id"])
    assert job.status == "USED"


# 2. Злодейство 31: Двойная работа (конкурентный запуск)
def test_evil_31_concurrent_dispatch_blocked():
    class BlockingTool:
        def __init__(self):
            self.name = "access_rights"

        def execute(self, params: ToolParams) -> ToolResult:
            dispatcher.create(valid_fields(job_id=params.job_id), lambda p: job)
            return ToolResult(True, self.name)

    blocking_tool = BlockingTool()
    dispatcher = Dispatcher(resolve=lambda name: blocking_tool, checks=("access_rights",))

    params = creation_params(valid_fields())
    job = Job(params, required_checks=("access_rights",))

    with pytest.raises(DispatchInProgress):
        dispatcher.create(valid_fields(job_id=params.job_id), lambda p: job)


# 3. Злодейство 33: Отменённое стало готовым
def test_evil_33_cancelled_cannot_become_confirmed():
    tool = FakeTool("access_rights", passed=True)
    dispatcher = Dispatcher(resolve=lambda name: tool, checks=("access_rights",))

    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=("access_rights",))

    job.cancel(user_id=fields["user_id"], role="student")
    assert job.status == "CANCELLED"

    dispatcher.create(fields, lambda p: job)
    assert job.status == "CANCELLED"


# 4. Злодейство 34: Отказ или исключение Tool бракует бронь
def test_evil_34_tool_failure_marks_cancelled():
    failing_tool = FakeTool("access_rights", passed=False)
    dispatcher = Dispatcher(resolve=lambda name: failing_tool, checks=("access_rights",))

    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=("access_rights",))

    dispatcher.create(fields, lambda p: job)
    assert job.status == "CANCELLED"


def test_evil_34_tool_exception_marks_cancelled():
    crashing_tool = FakeTool("conflict_check", exception=RuntimeError("Сетевой сбой"))
    dispatcher = Dispatcher(resolve=lambda name: crashing_tool, checks=("conflict_check",))

    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=("conflict_check",))

    with pytest.raises(RuntimeError):
        dispatcher.create(fields, lambda p: job)

    assert job.status == "CANCELLED"


# 5. Злодейство 35: Успех без работы
def test_evil_35_cannot_bypass_mandatory_checks():
    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=CHECKS)

    with pytest.raises(InvalidStateTransition):
        job.check_in(START + timedelta(minutes=5), user_id=fields["user_id"])

    with pytest.raises(InvalidStateTransition):
        job.transition("CONFIRMED", checks=())

    assert job.status == "REQUESTED"


# 6. Злодейство 36: Повтор успешного
def test_evil_36_confirmed_job_does_not_rerun_tools():
    tool = FakeTool("access_rights", passed=True)
    dispatcher = Dispatcher(resolve=lambda name: tool, checks=("access_rights",))

    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=("access_rights",))

    dispatcher.create(fields, lambda p: job)
    assert job.status == "CONFIRMED"
    assert tool.call_count == 1

    dispatcher.create(fields, lambda p: job)
    assert job.status == "CONFIRMED"
    assert tool.call_count == 1


# 7. Валидация входных данных (VALIDATION_ERROR)
def test_validation_error_missing_and_invalid_fields():
    fields_no_user = valid_fields()
    del fields_no_user["user_id"]
    with pytest.raises(ValidationError) as exc1:
        creation_params(fields_no_user)
    assert exc1.value.code == "VALIDATION_ERROR"
    assert exc1.value.field == "user_id"

    fields_bad_time = valid_fields(start_time=END, end_time=START)
    with pytest.raises(ValidationError) as exc2:
        creation_params(fields_bad_time)
    assert exc2.value.code == "VALIDATION_ERROR"
    assert exc2.value.field == "end_time"


def test_structurally_invalid_input_does_not_run_checks():
    tool = FakeTool("access_rights", passed=True)
    dispatcher = Dispatcher(resolve=lambda name: tool, checks=("access_rights",))

    bad_fields = valid_fields(space_category="unknown_category")
    with pytest.raises(ValidationError):
        dispatcher.create(bad_fields, lambda p: None)

    assert tool.call_count == 0


# 8. Неизвестная операция (UNKNOWN_OPERATION)
def test_unknown_operation_in_registry():
    class CustomUnknownOperation(Exception):
        code = "UNKNOWN_OPERATION"

    dispatcher = Dispatcher(resolve=lambda n: (_ for _ in ()).throw(CustomUnknownOperation()), checks=("unknown_tool",))
    fields = valid_fields()
    params = creation_params(fields)
    job = Job(params, required_checks=("unknown_tool",))

    with pytest.raises(Exception) as exc:
        dispatcher.create(fields, lambda p: job)
    assert getattr(exc.value, "code", None) == "UNKNOWN_OPERATION"
