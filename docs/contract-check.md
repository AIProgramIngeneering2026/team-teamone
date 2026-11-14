| **Карта** | **Ответ контракта** |
| --- | --- |
| К1 Двойной клик | Повтор с тем же `Idempotency-Key` после успеха → `200` с прежним `job_id` — [openapi.yaml, POST /jobs][k1] |
| К2 Ответ потерялся | Повторить запрос с прежним ключом → та же бронь или сохранённый отказ — [openapi.yaml, IdempotencyKey][k2] |
| К3 Неизвестная операция | Имя Tool отсутствует в Registry → `400 UNKNOWN_OPERATION` — [tool-contract.md, Registry][k3] |
| К4 Кривые параметры | `400 VALIDATION_ERROR`, неверное поле указано в `errors[].field` — [openapi.yaml, BadRequest][k4] |
| К5 Воскрешение | Выход из конечного состояния → `409 INVALID_STATE_TRANSITION`. Для новой брони создаётся отдельная Job — [tool-contract.md, Job][k5] |
| К6 Отмена | Владелец или администратор отменяет `REQUESTED` или `CONFIRMED` → `200`, статус `CANCELLED` — [openapi.yaml, POST /jobs/{id}/cancel][k6] |
| К7 Огромный вход | Идентификатор длиннее 128 символов → `400 VALIDATION_ERROR`. Общий лимит тела запроса не задан — [openapi.yaml, Identifier][k7] |
| К8 Чужой или несуществующий id | В обоих случаях → `404 JOB_NOT_FOUND` с одинаковым сообщением — [openapi.yaml, JobNotFound][k8] |
| К9 Перезапуск сервера | БД сохраняет зафиксированный результат или откатывает транзакцию. Повтор безопасен с прежним ключом — [ADR-0001, последствия][k9] |
| К10 Внешний сервис | Календарь недоступен после повторов или ответ некорректен → `503`, создаваемая бронь переходит в `CANCELLED` — [openapi.yaml, ScheduleUnavailable][k10] |

[k1]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L107-L140
[k2]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L386-L401
[k3]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/c5deb7bfa992be888a3d5d831f15e8f5b8176320/docs/tool-contract.md#3-распределение-ответственности-кто-проверяет-параметры
[k4]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L540-L559
[k5]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/c5deb7bfa992be888a3d5d831f15e8f5b8176320/docs/tool-contract.md#3-распределение-ответственности-кто-проверяет-параметры
[k6]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L171-L197
[k7]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L409-L413
[k8]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L566-L571
[k9]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/c5deb7bfa992be888a3d5d831f15e8f5b8176320/docs/adr/0001-sync-job-execution.md#цена-решения-последствия
[k10]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/d2f9b3ca16496344516b38d0365f325f01c8af2b/docs/api/openapi.yaml#L656-L670

## Что предложил ИИ

**Промпт**

> Изучи предоставленный контракт и колоду с возможными уязвимостями. Найди ещё три ситуации, для которых документация допускает разные ответы. Для каждой найденной уязвимости укажи конкретное исходное состояние, действие, приводящее к узвимости и краткое описание самой уязвимости.

| Карта и ход атакующего | Предложение для контракта |
| --- | --- |
| **К1 Двойной клик — ремонт.** Администратор дважды нажал «Закрыть на ремонт». Вернёт ли второй `POST /spaces/{id}/maintenance` прежний `maintenance_id` или создаст второе закрытие? | Добавить `Idempotency-Key` для регистрации ремонта. Первый запрос → `201`, повтор с тем же ключом и телом → `200` с прежним `maintenance_id`. |
| **К4 Кривые параметры — поиск.** В `GET /spaces` два значения `end_time`: 11:00 и 12:00. Какой интервал проверит сервис? | Отклонять повторяющийся параметр времени: `400 VALIDATION_ERROR`, в `errors[]` указать `in: query` и `field: end_time`. |
| **К9 Перезапуск сервера — неявка.** Бронь с 14:00 до 16:00. Сервис был выключен с 14:05 до 14:30, окно явки истекло. Увидит ли первый после запуска поиск свободное помещение или старую бронь `CONFIRMED`? | До первого поиска после запуска перевести просроченные `CONFIRMED` в `EXPIRED`. Тогда пропущенная проверка явки не удержит слот до 16:00.|
