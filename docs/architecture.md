# Архитектура MVP

## Границы решения

Сервис состоит из одного API-приложения и Celery worker, использующих общую доменную и
прикладную логику. PostgreSQL — источник истины для денег, задач и callback. Redis — транспорт
очереди и хранилище rate limit; потеря Redis не должна повреждать баланс или создавать
повторное списание.

Для надёжной передачи между PostgreSQL и Redis используется transactional outbox. Событие
создаётся в одной транзакции с generation и списанием. Celery Beat повторно публикует
необработанные события; возможная повторная доставка безопасна благодаря условным переходам
статуса worker.

Зависимости направлены внутрь:

```text
API / Celery tasks -> application use cases -> domain
          |                    ^
          +-> infrastructure -+
              (PostgreSQL, Redis, HTTPX/Fal.ai)
```

Практическое ограничение Clean Architecture: интерфейсы вводятся только на важных внешних
границах (репозитории и провайдер генерации), а не для каждого класса.

## Модели данных

### users

- `id: uuid`, первичный ключ;
- `api_key_hash: str`, уникальный HMAC/хеш ключа; открытый ключ хранится только у клиента;
- `balance: bigint`, целое число условных токенов, `CHECK balance >= 0`;
- `created_at`, `updated_at`.

### payment_events

- `id: uuid`;
- `external_id: str`, уникальный идентификатор платёжного события;
- `user_id`, `amount: bigint`, `created_at`;
- уникальность `external_id` обеспечивает идемпотентность на уровне БД.

### generations

- `id: uuid`, `user_id`;
- `kind`: `text_to_image | image_to_image | text_to_video | image_to_video`;
- `status`: `created | queued | processing | completed | failed`;
- `prompt`, `source_url`, `callback_url`;
- `input_params: jsonb`, `result: jsonb`, `error_code`, `error_message`;
- `cost: bigint` — снимок цены на момент запроса;
- `charged_at`, `refunded_at` — маркеры однократного списания/возврата;
- `provider`, `provider_request_id`, `attempt_count`;
- `processing_token`, `processing_started_at`, `lease_expires_at` для fencing и recovery;
- `created_at`, `updated_at`, `completed_at`.

Создание generation и условное списание (`UPDATE ... WHERE balance >= cost`) выполняются
в одной транзакции. Пара `(user_id, idempotency_key)` уникальна: повторная постановка
использует тот же `generation.id`, а не создаёт новую запись и не списывает баланс повторно.
Возврат выполняется только при атомарном
переходе `refunded_at IS NULL -> now()` вместе с увеличением баланса.

## HTTP endpoints

- `POST /v1/auth` — создать пользователя и один раз вернуть API-ключ;
- `GET /v1/me` — баланс и идентификатор текущего пользователя;
- `POST /v1/payments/webhook` — тестовое пополнение с отдельным секретом;
- `POST /v1/generations` — единый endpoint четырёх режимов, требует `Idempotency-Key`, ответ `202`;
- `GET /v1/generations/{id}` — статус, ошибка и результат владельца;
- `GET /metrics` — Prometheus exposition format;
- `GET /health` — liveness (позже отдельно добавится readiness).

Все пользовательские endpoints, кроме `/auth`, принимают `X-API-Key`. Для платёжного
webhook используется `X-Payment-Secret`.

## Переходы состояний

```text
created --(транзакция подтверждена)--> queued --(worker начал)--> processing
                                             ^                    |       |
                                             | temporary error    |       +--> completed
                                             +--------------------+       |
                                                                          +--> failed
                                                                               |
                                                                               +--> refund once
```

Разрешены только `created -> queued -> processing -> completed|failed` и
`processing -> queued` для retry. Терминальные состояния неизменяемы. Переходы делаются
условным `UPDATE ... WHERE status IN (...)`, поэтому два worker не завершают задачу дважды.
Каждый worker получает fencing token. Celery Beat возвращает истёкший lease в `queued` и
повторно открывает generation-outbox; запись старого worker с прежним token отклоняется.
Слишком долго ожидающая `queued` задача также повторно публикуется: это восстанавливает
работу после потери Redis, а возможный дубль безопасен благодаря условному захвату.

## Callback outbox

При терминальном переходе generation в той же транзакции создаётся `callback_outbox`.
Dispatcher резервирует событие и ставит короткую Celery-задачу. Перед HTTP-вызовом задача
атомарно получает lease и увеличивает persisted `attempt_count`. После ошибки сохраняется
`next_attempt_at = now + 15 seconds`; после пятой ошибки событие остаётся в БД для аудита.
`X-Webhook-Delivery` равен стабильному ID outbox-записи.

## Провайдеры и failover

`GenerationProvider` принимает нормализованный запрос и возвращает нормализованный
результат. `FalGenerationProvider` сопоставляет четыре режима четырём model endpoint и
использует Queue API через HTTPX. После исчерпания retry worker переключается на
настраиваемый резервный адаптер только пока основной провайдер не выдал внешний request ID.
После получения ID переключение запрещено, чтобы не создать вторую платную генерацию.

По официальной документации Fal.ai долгие задачи следует отправлять через Queue API и
получать status/result или webhook. В MVP worker отправляет запрос, сохраняет внешний
`request_id`, затем опрашивает очередь; это сохраняет единый внутренний lifecycle и
делает retry идемпотентным. Используются модели `fal-ai/wan-25-preview/{text-to-image,
image-to-image,text-to-video,image-to-video}`.

## План на 24 часа

| Время | Результат |
|---:|---|
| 1.5 ч | Каркас, конфигурация, Compose, health-check |
| 3 ч | Async SQLAlchemy, Alembic, user и безопасный API-ключ |
| 2 ч | Баланс, идемпотентный payment webhook, конкурентные тесты |
| 2.5 ч | Generation, валидация четырёх режимов, create/status API |
| 3 ч | Celery lifecycle и fake provider |
| 3 ч | HTTPX Fal Queue adapter и схемы четырёх моделей |
| 2 ч | Retry, однократный refund и безопасный failover |
| 1.5 ч | Callback: ровно 5 попыток через 15 секунд |
| 1 ч | Redis rate limit 10/min + block 60s |
| 1 ч | Метрики и JSON/rotating logs |
| 2 ч | API/integration tests и гонки денег |
| 1.5 ч | README, Compose smoke-test, финальная чистка |

## Приоритеты

Обязательно реализовать: четыре режима, auth, деньги и идемпотентность, lifecycle,
асинхронный worker, fake для тестов, retry/refund, rate limit, callback policy, базовые
метрики/логи, Compose, миграции и ключевые API-тесты.

Фактически упрощено: резерв реализован как fake; используется polling вместо входящего Fal
webhook; хранятся ссылки вместо собственного object storage; нет tracing и нагрузочных
тестов. Callback-outbox и worker lease реализованы в PostgreSQL.
