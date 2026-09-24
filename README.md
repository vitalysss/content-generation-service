# Content Generation Service

MVP асинхронного сервиса генерации изображений и видео через Fal.ai. Поддерживаются
`text_to_image`, `image_to_image`, `text_to_video` и `image_to_video`, баланс условных
токенов, фоновые задачи, retry, refund, клиентские webhooks, rate limit, метрики и логи.

Подробные архитектурные решения и диаграмма состояний находятся в
[`docs/architecture.md`](docs/architecture.md).
Пошаговый запуск на чистом компьютере и сквозная проверка описаны в
[`docs/deployment.md`](docs/deployment.md).

## Стек и устройство

- Python 3.12, FastAPI, Pydantic v2;
- SQLAlchemy 2 async, Alembic, PostgreSQL 15;
- Celery и Redis;
- HTTPX и Fal.ai Queue API;
- Prometheus client, Pytest, Ruff;
- Docker Compose.

PostgreSQL является источником истины для баланса, состояния генераций и доставки
клиентских webhook. API создаёт задачу и возвращает `202`, не ожидая Fal.ai.
Transactional outbox передаёт задачу в Celery, worker выполняет генерацию, а Celery Beat
регулярно отправляет необработанные события и восстанавливает просроченные worker lease.

## Быстрый запуск

Требования для чистого компьютера: Git и Docker Desktop (или Docker Engine с Compose v2).
Python на хосте для запуска через Docker не требуется.

1. Создайте локальный файл конфигурации:

   ```bash
   cp .env.example .env
   ```

2. Для локального fake-провайдера оставьте:

   ```dotenv
   GENERATION_PROVIDER=fake
   FAL_KEY=
   ```

3. Запустите сервис:

   ```bash
   docker compose up --build -d
   ```

На Windows при ошибке BuildKit из-за кириллицы в пути можно использовать PowerShell:

```powershell
$env:DOCKER_BUILDKIT = "0"
$env:COMPOSE_DOCKER_CLI_BUILD = "0"
docker compose up --build -d
```

После запуска доступны:

- API: `http://localhost:8000`;
- Swagger UI: `http://localhost:8000/docs`;
- health check: `GET http://localhost:8000/health`;
- Prometheus metrics: `GET http://localhost:8000/metrics`.

Сервис `migrate` автоматически применяет Alembic-миграции до запуска API и worker.

Каждый push и pull request также проверяется в GitHub Actions на чистом Linux-runner:
запускаются Ruff и Pytest, затем Compose-стек собирается с нуля и проверяется через
`GET /health`.

## Конфигурация

Основные параметры задаются в `.env`:

| Переменная | Назначение |
|---|---|
| `GENERATION_PROVIDER` | Основной провайдер: `fake` или `fal` |
| `FALLBACK_GENERATION_PROVIDER` | Резервный провайдер; в MVP — `fake` |
| `FAL_KEY` | Секретный ключ Fal.ai, нужен только для `fal` |
| `PAYMENT_WEBHOOK_SECRET` | Секрет тестового платёжного webhook |
| `API_KEY_PEPPER` | Pepper для HMAC пользовательских API-ключей |
| `COST_*` | Стоимость каждого из четырёх режимов |
| `RATE_LIMIT_*` | Лимит, окно и длительность блокировки |
| `CALLBACK_TIMEOUT_SECONDS` | HTTP timeout клиентского webhook |
| `PROCESSING_LEASE_SECONDS` | Lease worker; должен быть больше Fal timeout |
| `QUEUED_RECOVERY_SECONDS` | Когда повторно публиковать слишком долго ожидающую задачу |
| `LOG_RETENTION_DAYS` | Количество ежедневных архивов логов |
| `VIDEO_RESOLUTION_*_PERCENT` | Коэффициенты цены видео по разрешению |

Перед внешним развёртыванием обязательно замените значения `change-me`. `.env` исключён
из Git. Открытые API-ключи пользователей и ключ Fal.ai не записываются в код или БД.

Для настоящего Fal.ai:

```dotenv
GENERATION_PROVIDER=fal
FALLBACK_GENERATION_PROVIDER=fake
FAL_KEY=your-real-secret
```

Тесты никогда не вызывают настоящий Fal.ai и не расходуют токены.

Используемые официальные Fal.ai endpoints:

- [text-to-image](https://fal.ai/models/fal-ai/wan-25-preview/text-to-image/api);
- [image-to-image](https://fal.ai/models/fal-ai/wan-25-preview/image-to-image/api);
- [text-to-video](https://fal.ai/models/fal-ai/wan-25-preview/text-to-video/api);
- [image-to-video](https://fal.ai/models/fal-ai/wan-25-preview/image-to-video/api).

## API

### 1. Регистрация

```bash
curl -X POST http://localhost:8000/v1/auth
```

Ответ содержит `user_id`, начальный баланс и API-ключ вида `cgs_...`. Открытый ключ
возвращается только один раз. В PostgreSQL хранится HMAC-SHA256 с секретным pepper.

Авторизованные запросы используют заголовок:

```text
X-API-Key: cgs_...
```

### 2. Тестовое пополнение

```bash
curl -X POST http://localhost:8000/v1/payments/webhook \
  -H "Content-Type: application/json" \
  -H "X-Payment-Secret: change-me" \
  -H "X-Payment-Event-ID: payment-001" \
  -d '{"external_user_id":"USER_UUID","amount":100}'
```

`X-Payment-Event-ID` уникален. Идентичный повтор возвращает `duplicate` и не пополняет
баланс снова. Повтор того же ID с другими данными возвращает `409`. В local/test режиме
заголовок можно не передавать: для точного формата из задания сервис построит стабильный
ID из `external_user_id` и `amount`. В production заголовок обязателен, иначе два реальных
платежа одинаковой суммы невозможно отличить от повторной доставки.

### 3. Создание генерации

```bash
curl -X POST http://localhost:8000/v1/generations \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cgs_..." \
  -H "Idempotency-Key: generation-001" \
  -d '{
    "kind":"text_to_image",
    "prompt":"A lighthouse in a storm",
    "callback_url":"https://client.example/webhooks/generations",
    "parameters":{"seed":42}
  }'
```

Для `image_to_image` и `image_to_video` обязателен `source_url`. Одинаковый запрос с тем
же `Idempotency-Key` возвращает прежнюю задачу без повторного списания. Тот же ключ с
другим телом возвращает `409`.

`parameters` валидируется отдельной схемой для каждого режима. Неизвестные поля и значения
вне диапазона отклоняются. Цена изображений умножается на `num_images`; цена видео зависит
от `duration` и настраиваемого коэффициента `resolution`. В generation сохраняется снимок
рассчитанной цены, поэтому последующее изменение тарифов не меняет уже созданную задачу.

### 4. Получение результата

```bash
curl -H "X-API-Key: cgs_..." \
  http://localhost:8000/v1/generations/GENERATION_UUID
```

Возможные статусы: `created`, `queued`, `processing`, `completed`, `failed`.

## Деньги и идемпотентность

- Generation и outbox-запись создаются в одной транзакции.
- Баланс списывается условным `UPDATE ... WHERE balance >= cost`.
- Пара `(user_id, idempotency_key)` уникальна в PostgreSQL.
- Повторное получение одного платёжного `external_id` не начисляет деньги снова.
- Refund выполняется условно при `refunded_at IS NULL`, поэтому возможен только один раз.
- Статусы изменяются условными `UPDATE`, и два worker не могут одновременно завершить
  одну generation.
- Каждый переход в `processing` получает уникальный fencing token и lease. Старый worker
  не может записать результат после того, как lease перехвачен другим worker.
- Celery Beat возвращает просроченные `processing` в очередь через transactional outbox.

## Retry и failover

Fal-адаптер разделяет отправку и получение результата. Сразу после успешной отправки
`provider_request_id` сохраняется в PostgreSQL. Retry продолжает опрос прежнего request ID,
а не создаёт новую платную генерацию.

Временные ошибки (`timeout`, транспортная ошибка, `408`, `409`, `425`, `429`, `5xx`)
повторяются с задержками 5, 10 и 20 секунд. Постоянные ошибки завершают задачу и возвращают
баланс. После исчерпания retry резервный fake-провайдер используется только если основной
ещё не вернул request ID. После получения ID переключение запрещено, чтобы не запустить
две платные генерации.

## Клиентский webhook

Если указан `callback_url`, запись callback-outbox создаётся в одной транзакции с
`completed` или `failed`. Отдельный dispatcher выполняет максимум пять доставок с
интервалом не менее 15 секунд. Состояние попыток, следующая дата и успешная доставка
хранятся в PostgreSQL, поэтому перезапуск Redis или worker не теряет событие. Payload:

```json
{
  "event": "generation.completed",
  "generation_id": "UUID",
  "status": "completed",
  "result": {"url": "..."},
  "error": null
}
```

`X-Webhook-Delivery` равен ID outbox-события и одинаков для всех попыток. Получатель должен
хранить этот ID и не выполнять бизнес-операцию повторно.

## Rate limit

Авторизованные endpoints допускают 10 запросов пользователя за 60 секунд. Одиннадцатый
запрос включает блокировку на 60 секунд и возвращает `429` с `Retry-After`. Проверка,
увеличение счётчика и блокировка выполняются атомарным Lua-скриптом в Redis.

`/auth`, `/health`, `/metrics` и платёжный webhook не входят в пользовательский лимит.

## Метрики и логи

`GET /metrics` публикует:

- `content_service_http_requests_total`;
- `content_service_http_request_duration_seconds`;
- `content_service_generations` и `content_service_generations_created_total`;
- `content_service_generation_cost_tokens_total` для списаний и возвратов;
- `content_service_callback_outbox` и `content_service_callback_attempts_total`.

Метрики генераций и callback рассчитываются из PostgreSQL, поэтому показывают единое
состояние API и отдельных Celery worker, а не локальные счётчики одного процесса.

В labels используется шаблон маршрута, например `/v1/generations/{generation_id}`, а не
UUID. Каждый ответ содержит `X-Request-ID`. JSON-логи запросов пишутся в
`logs/requests.log`, ротируются ежедневно и хранят семь архивов.

## Проверки

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
pytest --cov=app --cov-report=term-missing
ruff check src tests
```

Проверяются API, деньги, idempotency, динамическая тарификация, outbox, worker lease и
fencing token, четыре Fal-режима, классификация ошибок, retry/refund/failover, устойчивый
callback-outbox, rate limit, метрики и ротация логов. Актуальное количество тестов и
покрытие выводятся командами выше.

## Осознанные ограничения MVP

- Резервный провайдер — детерминированный fake, а не второй коммерческий API.
- `callback_url` не защищён от SSRF. В production нужны запрет private/loopback адресов,
  повторная DNS-проверка и allowlist.
- Callback не имеет пользовательской HMAC-подписи; получатель использует delivery ID лишь
  для дедупликации.
- Fixed-window rate limit допускает всплеск на границе двух окон.
- Ссылки на файлы хранятся как URL; собственного object storage нет.
- Нет Kubernetes, readiness probe, tracing и полноценного distributed circuit breaker.
- Fal Queue API имеет неопределённое окно: провайдер мог принять POST, но соединение могло
  оборваться до получения request ID. Для устранения нужен поддерживаемый Fal idempotency
  key или reconciliation.

## Остановка

```bash
docker compose down
```

Чтобы также удалить локальные данные PostgreSQL и Redis, отдельно используйте
`docker compose down -v`. Эта команда необратимо удаляет Docker volumes.

## Статус

- [x] Каркас и Docker Compose
- [x] PostgreSQL, миграции, пользователи и API-ключи
- [x] Баланс и платёжный webhook
- [x] Generation API и четыре режима
- [x] Celery lifecycle и transactional outbox
- [x] FalProvider
- [x] Retry, refund и failover
- [x] Клиентские webhooks
- [x] Redis rate limit
- [x] Prometheus-метрики и rotating JSON logs
- [x] Тесты
- [x] Документация
