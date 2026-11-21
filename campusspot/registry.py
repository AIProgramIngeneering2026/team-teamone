"""Реестр проверок, зарегистрированных при сборке приложения."""

from collections.abc import Mapping

from .tool_contract import Tool


class UnknownOperation(LookupError):
    code = "UNKNOWN_OPERATION"

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Неизвестная операция: {name}")


class Registry:
    """Хранит инструменты проверки брони и выдаёт их по имени.

    Args:
        tools: Таблица имён и готовых экземпляров Tool, например
            {"conflict_check": conflict_tool}.
    """

    def __init__(self, tools: Mapping[str, Tool]):
        """Скопировать таблицу tools, сохранив ссылки на переданные экземпляры Tool."""
        self._tools = dict(tools)

    def resolve(self, name: str) -> Tool:
        """Найти зарегистрированный инструмент.

        Args:
            name: Ключ из таблицы, переданной при создании Registry.

        Returns:
            Экземпляр Tool, зарегистрированный под этим именем.

        Raises:
            UnknownOperation: Имя отсутствует в реестре (код UNKNOWN_OPERATION).
        """
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownOperation(name) from None
