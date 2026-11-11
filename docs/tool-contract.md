# Контракт исполнителей правил (Tool Contract) — CampusSpot

Документ фиксирует единый контракт взаимодействия между `Dispatcher` и инструментами проверки правил бронирования (`Tools`) в соответствии с паттернами **Strategy**, **Registry** и **Adapter** (лекция 103).

---

## 1. Назначение и место в архитектуре

В сервисе **CampusSpot** каждое правило проверки заявки на бронь (`Job`) оформлено как независимая реализация общего контракта `Tool`:
* **`Dispatcher`** организует выполнение сценария: он не знает внутренних алгоритмов конкретных проверок, SQL-запросов или адресов внешних сервисов и обращается к инструментам только через контракт `Tool.execute(params)`.
* **`Registry` (Реестр)** хранит таблицу соответствий `имя_инструмента -> экземпляр Tool`, которая формируется в точке сборки приложения при старте. При запросе незарегистрированного имени `Registry` возвращает единую ошибку `UnknownOperation`.
* **Локальность изменений (Open/Closed Principle):** добавление нового правила проверки требует только создания нового класса, реализующего `Tool`, и его регистрации в `Registry` при сборке приложения без изменения кода `Dispatcher`.

---

## 2. Интерфейс контракта в коде (Python Protocol)

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol


@dataclass(frozen=True)
class ToolParams:
    """Входные параметры для выполнения проверки заявки на бронь."""
    job_id: str
    user_id: str
    role: str                    # student | group_lead | teacher | special_staff | admin
    space_id: str
    space_category: str          # coworking | project_zone | classroom | lab | conference_hall
    start_time: datetime
    end_time: datetime
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Единый формат результата работы любого Tool."""
    passed: bool                         # True — проверка пройдена; False — отказ по бизнес-правилу
    tool_name: str                       # Имя сработавшего инструмента в Registry
    error_code: Optional[str] = None     # Машиночитаемый код отказа (если passed == False)
    message: Optional[str] = None        # Понятное описание причины отказа
    details: Mapping[str, Any] = field(default_factory=dict)  # Поля и метаданные для диагностики


class Tool(Protocol):
    """Единый контракт инструмента проверки (Strategy)."""
    name: str

    def execute(self, params: ToolParams) -> ToolResult:
        """Выполняет проверку параметров бронирования и возвращает ToolResult."""
        ...
