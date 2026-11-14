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
```

---

## 3. Распределение ответственности: кто проверяет параметры?

В соответствии с принципом разделения ответственности (лекция 103) проверки строго распределены между слоями и не дублируют друг друга:

| Уровень / Компонент | Что именно проверяет (зона ответственности) | Что скрывает от соседей | Как сообщает об ошибке |
| :--- | :--- | :--- | :--- |
| **1. Входной HTTP-слой** | Наличие обязательных полей в JSON (`user_id`, `role`, `space_id`, `start_time`, `end_time`), типы данных, формат ISO-8601 для дат, условие `start_time < end_time`, наличие заголовка `Idempotency-Key`. | Протокол обращения клиента (HTTP-заголовки, сериализацию JSON, query-параметры). | Отклоняет запрос до создания `Job` с HTTP `400 Bad Request` (`VALIDATION_ERROR` и указанием конкретного поля). |
| **2. Доменный объект `Job`** | Допустимость перехода между состояниями (`REQUESTED`, `CONFIRMED`, `USED`, `CANCELLED`, `EXPIRED`) и правило: завершённую бронь (`USED`, `CANCELLED`, `EXPIRED`) нельзя запустить, продлить или отменить повторно. | Внутренние инварианты жизненного цикла заявки. | Блокирует переход и выбрасывает доменное исключение `InvalidStateTransition` (HTTP `409 Conflict`). |
| **3. `Registry` / `Dispatcher`** | Существование запрошенного инструмента (`Tool`) в реестре и порядок вызова зарегистрированных проверок. | Способ хранения и выбора конкретных реализаций `Tool`. | Выбрасывает ошибку `UnknownOperation` (HTTP `400 Bad Request`, код `UNKNOWN_OPERATION`). |
| **4. Конкретный `Tool`** | Предметные бизнес-правила конкретной проверки: соответствие роли и категории пространства, лимиты длительности и числа активных броней, пересечения по времени. | Алгоритм конкретной проверки и её частные пороговые значения. | Возвращает `ToolResult(passed=False, error_code=..., message=..., details=...)`. |
| **5. Сетевой `Adapter`** | Доступность внешнего API расписания, укладывание в `timeout`, наличие обязательных полей во внешнем JSON-ответе. | URL поставщика, формат чужого JSON, HTTP-коды поставщика и механику `retry`. | Переводит сбои поставщика во внутренние исключения интеграции: `ToolTimeout`, `ToolProtocolError`, `ToolUnavailable` (HTTP `503 Service Unavailable`). |

---

## 4. Модель ошибок и отказов Tool

Все нештатные ситуации при вызове `Tool.execute(params)` делятся на две категории:

### 4.1. Предметный отказ (Бизнес-правило не пройдено)
Инструмент отработал штатно и выяснил, что бронь нарушает правила кампуса. Исключение не выбрасывается — возвращается `ToolResult(passed=False)`:

| Код в `ToolResult.error_code` | Какой Tool возвращает | Когда возникает | HTTP-код на выходе API |
| :--- | :--- | :--- | :--- |
| `ACCESS_DENIED` | `AccessRightsTool` | Роль пользователя не даёт права бронировать выбранную категорию помещения (например, `student` запрашивает `lab` или `conference_hall`). | `403 Forbidden` |
| `BOOKING_LIMIT_EXCEEDED` | `LimitCheckTool` | Длительность слота > 240 минут (4 ч) либо у пользователя уже есть 2 активных брони (`REQUESTED` / `CONFIRMED`). | `422 Unprocessable Entity` |
| `INVALID_NEW_TIME` | `LimitCheckTool` | При переносе (`UC-08`) или продлении (`UC-09`) новое время начала в прошлом по часам сервера. | `422 Unprocessable Entity` |
| `SPACE_UNAVAILABLE` | `ConflictCheckTool` | На выбранный интервал в помещении идёт учебная пара по расписанию или есть чужая бронь в статусе `CONFIRMED` / `USED`. | `409 Conflict` |
| `SPACE_UNAVAILABLE_DUE_TO_MAINTENANCE` | `ConflictCheckTool` | Помещение закрыто администратором на ремонт (`UC-11`) в запрашиваемый интервал. | `409 Conflict` |

### 4.2. Технические ошибки интеграции и диспетчеризации (Исключения)
Возникают, когда инструмент не может вычислить результат из-за сбоя внешней зависимости или неверной конфигурации:
* `UnknownOperation` — имя операции/инструмента не найдено в `Registry` (HTTP `400 Bad Request`).
* `ToolTimeout` — внешний сервис учебного расписания не ответил в рамках заданного таймаута после исчерпания попыток `retry` в адаптере (HTTP `503 Service Unavailable`, код `EXTERNAL_SCHEDULE_TIMEOUT`).
* `ToolProtocolError` — внешний сервис расписания вернул невалидный JSON или ответ без ожидаемых полей (HTTP `502 Bad Gateway` / `503 Service Unavailable`, код `EXTERNAL_SCHEDULE_BAD_RESPONSE`).
* `ToolUnavailable` — внешний сервис вернул ошибку `5xx` или соединение отклонено (HTTP `503 Service Unavailable`).

Во всех случаях технических ошибок интеграции заявка `Job` не может быть переведена в `CONFIRMED` (переводится из `REQUESTED` в `CANCELLED` с сохранением технической причины в истории проверок).

---

## 5. Описание конкретных Tool проекта через единый контракт

### 5.1. `ConflictCheckTool` — проверка конфликтов расписания, ремонтов и броней
* **Ключ регистрации в `Registry`:** `"conflict_check"`
* **Зависимости (передаются при сборке):** контракт чтения занятости из БД (`JobRepository` / `SpaceRepository`) и `ScheduleAdapter` (HTTP-адаптер внешнего сервиса учебного расписания вуза).
* **Что получает на вход (`ToolParams`):** `job_id`, `space_id`, `space_category`, `start_time`, `end_time` (при переносе `UC-08` `job_id` используется для исключения собственной брони из проверки пересечений).
* **Что делает внутри (скрытый алгоритм):**
  1. Проверяет через хранилище, нет ли на `space_id` активного закрытия на ремонт (`maintenance`) на интервал `[start_time, end_time)`.
  2. Проверяет через `JobRepository`, нет ли других заявок в статусе `CONFIRMED` или `USED` (с учётом фактического времени досрочного освобождения `actual_end_time` из `UC-10`) на пересекающийся интервал.
  3. Если `space_category == "classroom"`, вызывает `ScheduleAdapter.has_classes(space_id, start_time, end_time)` для проверки отсутствия учебных пар.
* **Что возвращает (`ToolResult`):**
  * При отсутствии пересечений: `ToolResult(passed=True, tool_name="conflict_check")`.
  * При закрытии на ремонт: `ToolResult(passed=False, tool_name="conflict_check", error_code="SPACE_UNAVAILABLE_DUE_TO_MAINTENANCE", message="Помещение закрыто на техническое обслуживание", details={"space_id": params.space_id})`.
  * При пересечении с парой или чужой бронью: `ToolResult(passed=False, tool_name="conflict_check", error_code="SPACE_UNAVAILABLE", message="Пространство занято в выбранный интервал времени", details={"space_id": params.space_id, "reason": "schedule_or_booking_overlap"})`.
* **Ошибки интеграции:** пробрасывает `ToolTimeout`, `ToolProtocolError` или `ToolUnavailable` от `ScheduleAdapter`, если внешний календарь расписания недоступен или вернул мусорный ответ.

---

### 5.2. `LimitCheckTool` — проверка временных и количественных лимитов
* **Ключ регистрации в `Registry`:** `"limit_check"`
* **Зависимости (передаются при сборке):** `JobRepository` (для подсчёта активных броней пользователя) и источник серверного времени `Clock`.
* **Что получает на вход (`ToolParams`):** `user_id`, `start_time`, `end_time`, `extra` (при продлении `UC-09` содержит исходное время начала `original_start_time`).
* **Что делает внутри (скрытый алгоритм):**
  1. Проверяет, что запрашиваемый интервал не начинается в прошлом относительно текущего времени сервера (`start_time > clock.now()`).
  2. Вычисляет длительность бронирования `(end_time - start_time)` (или от `original_start_time` при продлении) и проверяет, что она не превышает **240 минут (4 часа)**.
  3. Запрашивает через `JobRepository.count_active_by_user(user_id)` число текущих активных броней (`REQUESTED` и `CONFIRMED`) и проверяет, что их **меньше 2**.
* **Что возвращает (`ToolResult`):**
  * Если все лимиты соблюдены: `ToolResult(passed=True, tool_name="limit_check")`.
  * Если время в прошлом: `ToolResult(passed=False, tool_name="limit_check", error_code="INVALID_NEW_TIME", message="Интервал бронирования должен быть в будущем", details={"field": "start_time"})`.
  * Если превышена длительность или число активных броней: `ToolResult(passed=False, tool_name="limit_check", error_code="BOOKING_LIMIT_EXCEEDED", message="Превышен лимит длительности (макс. 240 мин) или числа активных броней (макс. 2)", details={"max_duration_minutes": 240, "max_active_bookings": 2})`.

---

### 5.3. `AccessRightsTool` — проверка прав роли на категорию помещения
* **Ключ регистрации в `Registry`:** `"access_rights"`
* **Зависимости:** локальная таблица правил доступа (без сетевых вызовов).
* **Что получает на вход (`ToolParams`):** `role`, `space_category`.
* **Что делает внутри (скрытый алгоритм):**
  Сверяет роль с разрешёнными категориями пространств:
  * `student` -> `coworking`, `project_zone`;
  * `group_lead`, `teacher` -> `coworking`, `project_zone`, `classroom`;
  * `special_staff`, `admin` -> все категории, включая `lab` и `conference_hall`.
* **Что возвращает (`ToolResult`):**
  * При наличии прав: `ToolResult(passed=True, tool_name="access_rights")`.
  * При отсутствии прав: `ToolResult(passed=False, tool_name="access_rights", error_code="ACCESS_DENIED", message="Недостаточно прав для бронирования выбранной категории помещения", details={"role": params.role, "space_category": params.space_category})`.
