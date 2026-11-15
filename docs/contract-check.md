| **Карта** | **Ответ контракта** |
| --- | --- |
| К1 Двойной клик | После успеха повтор с тем же `Idempotency-Key` и параметрами → исходный `201` с сохранённым телом — [openapi.yaml, POST /jobs][k1] |
| К2 Ответ потерялся | Повтор с прежним ключом и параметрами → сохранённый ответ. После временного `503` проверка запускается заново — [openapi.yaml, IdempotencyKey][k2] |
| К3 Неизвестная операция | Имя Tool отсутствует в Registry → `400 UNKNOWN_OPERATION` — [tool-contract.md, Registry][k3] |
| К4 Кривые параметры | `400 VALIDATION_ERROR`, неверное поле указано в `errors[].field` — [openapi.yaml, BadRequest][k4] |
| К5 Воскрешение | Выход из конечного состояния → `409 INVALID_STATE_TRANSITION`. Для новой брони создаётся отдельная Job — [tool-contract.md, Job][k5] |
| К6 Отмена | Владелец или администратор отменяет `REQUESTED` или `CONFIRMED` → `200`, статус `CANCELLED` — [openapi.yaml, POST /jobs/{id}/cancel][k6] |
| К7 Огромный вход | JSON-тело больше 16 КиБ → `413 PAYLOAD_TOO_LARGE` до разбора JSON и вызова Dispatcher — [openapi.yaml, лимит тела][k7] |
| К8 Чужой или несуществующий id | Чужая и отсутствующая бронь → одинаковый `404 JOB_NOT_FOUND`. Администратор вправе отменить чужую бронь — [openapi.yaml, JobNotFound][k8] |
| К9 Перезапуск сервера | БД сохраняет зафиксированный результат или откатывает транзакцию. Повтор безопасен с прежним ключом — [ADR-0002, восстановление после сбоя][k9] |
| К10 Внешний сервис | Календарь недоступен после повторов или ответ некорректен → `503`, создаваемая бронь переходит в `CANCELLED` — [openapi.yaml, ScheduleUnavailable][k10] |

[k1]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L124-L126
[k2]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L409-L426
[k3]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/tool-contract.md#3-распределение-ответственности-кто-проверяет-параметры
[k4]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L560-L580
[k5]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/tool-contract.md#3-распределение-ответственности-кто-проверяет-параметры
[k6]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L189-L207
[k7]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L22-L24
[k8]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L595-L600
[k9]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e25918184dc6cb7f91c8720011496192c56507ea/docs/adr/0002-idempotency-protection.md#решение
[k10]: https://github.com/AIProgramIngeneering2026/team-teamone/blob/e2ef4da988c4e215e988d4d54d23cf445d1daf7f/docs/api/openapi.yaml#L717-L735

## Что предложил ИИ

**Промпт**

> Изучи предоставленный контракт и колоду с возможными уязвимостями. Найди ещё три ситуации, для которых документация допускает разные ответы. Для каждой найденной уязвимости укажи конкретное исходное состояние, действие, приводящее к узвимости и краткое описание самой уязвимости.

| Карта и ход атакующего | решение команды |
| --- | --- |
| **К1 Двойной клик — ремонт.** Администратор дважды нажал «Закрыть на ремонт». Вернёт ли второй `POST /spaces/{id}/maintenance` прежний `maintenance_id` или создаст второе закрытие? | **Приняли с уточнением.** `Idempotency-Key` обязателен. Повтор с тем же ключом, помещением и параметрами → исходный `201`, тот же `maintenance_id` и исходные списки затронутых броней. [OpenAPI, регистрация ремонта](https://github.com/AIProgramIngeneering2026/team-teamone/blob/2d715263e4a626a67766b9534051323ce7769eba/docs/api/openapi.yaml#L364-L374) · [ADR-0002](https://github.com/AIProgramIngeneering2026/team-teamone/blob/e25918184dc6cb7f91c8720011496192c56507ea/docs/adr/0002-idempotency-protection.md#решение) |
| **К4 Кривые параметры — поиск.** В `GET /spaces` два значения `end_time`: 11:00 и 12:00. Какой интервал проверит сервис? | **Приняли.** Повтор `start_time` или `end_time` → `400 VALIDATION_ERROR`. В `errors[]` указаны `in: query` и имя повторённого параметра в `field`. [OpenAPI, поиск](https://github.com/AIProgramIngeneering2026/team-teamone/blob/2d715263e4a626a67766b9534051323ce7769eba/docs/api/openapi.yaml#L35-L39) |
| **К9 Перезапуск сервера — неявка.** Бронь с 14:00 до 16:00. Сервис был выключен с 14:05 до 14:30, обычные 15 минут на явку прошли. Увидит ли первый после запуска поиск помещение среди свободных? | **Приняли, но значительно переработали.** Сбой пересёкся с окном явки. После восстановления в 14:30 первый поиск исключает помещение: бронь остаётся `CONFIRMED`, владелец может подтвердить явку до 14:35. [OpenAPI, окно явки](https://github.com/AIProgramIngeneering2026/team-teamone/blob/cd95b298e3af08d1980f460543482fcdcce30d6d/docs/api/openapi.yaml#L190-L197) · [ADR-0001](https://github.com/AIProgramIngeneering2026/team-teamone/blob/e25918184dc6cb7f91c8720011496192c56507ea/docs/adr/0001-sync-job-execution.md#решение) |
