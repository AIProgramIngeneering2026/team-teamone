"""Правила доступа к категориям помещений CampusSpot."""

from .tool_contract import ToolParams, ToolResult


_STUDENT_SPACES = frozenset({"coworking", "project_zone"})
_CLASSROOM_SPACES = _STUDENT_SPACES | {"classroom"}
_ALL_SPACES = _CLASSROOM_SPACES | {"lab", "conference_hall"}
_ALLOWED_SPACES = {
    "student": _STUDENT_SPACES,
    "group_lead": _CLASSROOM_SPACES,
    "teacher": _CLASSROOM_SPACES,
    "special_staff": _ALL_SPACES,
    "admin": _ALL_SPACES,
}


class AccessRightsTool:
    name = "access_rights"

    def execute(self, params: ToolParams) -> ToolResult:
        if params.space_category in _ALLOWED_SPACES.get(params.role, ()):
            return ToolResult(passed=True, tool_name=self.name)

        return ToolResult(
            passed=False,
            tool_name=self.name,
            error_code="ACCESS_DENIED",
            message="Роль пользователя не позволяет бронировать эту категорию",
            details={"role": params.role, "space_category": params.space_category},
        )
