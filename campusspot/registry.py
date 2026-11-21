"""Реестр проверок, зарегистрированных при сборке приложения."""

from collections.abc import Mapping

from .tool_contract import Tool


class UnknownOperation(LookupError):
    code = "UNKNOWN_OPERATION"

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Неизвестная операция: {name}")


class Registry:
    def __init__(self, tools: Mapping[str, Tool]):
        self._tools = dict(tools)

    def resolve(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownOperation(name) from None
