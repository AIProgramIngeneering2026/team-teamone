"""Общий контракт проверок из docs/tool-contract.md."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ToolParams:
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
    passed: bool
    tool_name: str
    error_code: str | None = None
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str

    def execute(self, params: ToolParams) -> ToolResult: ...
