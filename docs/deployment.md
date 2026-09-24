# Развёртывание и проверка

Ниже описан запуск сервиса на чистом компьютере с fake-провайдером. Такой режим не
обращается к Fal.ai и не расходует деньги.

## Требования

- Git;
- Docker Desktop для Windows/macOS или Docker Engine с Compose v2 для Linux;
- свободные порты `8000`, `5432` и `6379`.

Python на компьютере не требуется: API, worker, миграции, PostgreSQL и Redis запускаются
в контейнерах.

Проверьте установку:

```text
git --version
docker --version
docker compose version
```

## 1. Скачать и настроить

```text
git clone https://github.com/vitalysss/content-generation-service.git
cd content-generation-service
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Linux/macOS:

```bash
cp .env.example .env
```

Для локальной проверки оставьте в `.env`:

```dotenv
APP_ENV=local
GENERATION_PROVIDER=fake
FALLBACK_GENERATION_PROVIDER=fake
FAL_KEY=
```

Значения `PAYMENT_WEBHOOK_SECRET` и `API_KEY_PEPPER` с `change-me` допустимы только для
локального теста. Перед публикацией сервиса замените их длинными случайными строками.

## 2. Собрать и запустить

```text
docker compose up --build -d
```

Первый запуск обычно занимает несколько минут. Compose самостоятельно:

1. запускает PostgreSQL и Redis;
2. применяет все Alembic-миграции;
3. запускает API, Celery worker и Celery Beat.

Проверьте контейнеры:

```text
docker compose ps -a
```

`api`, `worker`, `beat`, `postgres` и `redis` должны быть запущены. Контейнер `migrate`
должен завершиться с кодом `0`.

## 3. Базовая проверка

Откройте:

- <http://localhost:8000/health> — ожидается `{"status":"ok"}`;
- <http://localhost:8000/docs> — Swagger UI;
- <http://localhost:8000/metrics> — метрики Prometheus.

В PowerShell health check можно выполнить так:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

В Linux/macOS:

```bash
curl --fail http://localhost:8000/health
```

## 4. Сквозная проверка в PowerShell

Запускайте из каталога проекта после старта Compose:

```powershell
$base = "http://localhost:8000/v1"

# Регистрация: API-ключ показывается только один раз.
$auth = Invoke-RestMethod -Method Post -Uri "$base/auth"
$auth

# Тестовое пополнение баланса.
$payment = @{
    external_user_id = $auth.user_id
    amount = 100
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "$base/payments/webhook" `
    -Headers @{
        "X-Payment-Secret" = "change-me"
        "X-Payment-Event-ID" = "manual-payment-001"
    } `
    -ContentType "application/json" `
    -Body $payment

# Постановка генерации через fake-провайдер.
$request = @{
    kind = "text_to_image"
    prompt = "A lighthouse in a storm"
    parameters = @{ seed = 42 }
} | ConvertTo-Json -Depth 4

$generation = Invoke-RestMethod -Method Post `
    -Uri "$base/generations" `
    -Headers @{
        "X-API-Key" = $auth.api_key
        "Idempotency-Key" = "manual-generation-001"
    } `
    -ContentType "application/json" `
    -Body $request

do {
    Start-Sleep -Seconds 1
    $result = Invoke-RestMethod `
        -Uri "$base/generations/$($generation.id)" `
        -Headers @{ "X-API-Key" = $auth.api_key }
    $result.status
} until ($result.status -in @("completed", "failed"))

$result | ConvertTo-Json -Depth 5
```

Успешный результат:

- пополнение возвращает `status: applied` и баланс `100`;
- generation проходит состояния очереди и заканчивается как `completed`;
- в `result.url` находится тестовый URL fake-провайдера;
- стоимость `text_to_image` по умолчанию равна `10` токенам.

Для повторного запуска скрипта измените значения `manual-payment-001` и
`manual-generation-001`: это ключи идемпотентности.

## 5. Диагностика

Общий статус и последние логи:

```text
docker compose ps -a
docker compose logs --tail=200
```

Логи отдельного сервиса:

```text
docker compose logs api
docker compose logs worker
docker compose logs migrate
```

Типовые причины проблем:

- порт занят — освободите `8000`, `5432` или `6379` либо измените публикацию портов в
  `compose.yaml`;
- `migrate` завершился с ошибкой — сначала изучите `docker compose logs migrate`;
- generation остаётся в очереди — проверьте `worker`, `beat` и Redis;
- на Windows проект находится в пути с кириллицей и старый Docker BuildKit падает —
  перенесите репозиторий в короткий латинский путь, например `C:\Projects\content-generation-service`;
- после изменения `.env` пересоздайте контейнеры командой `docker compose up -d --force-recreate`.

## 6. Остановка и обновление

Остановить сервис, сохранив данные:

```text
docker compose down
```

Обновить код и пересобрать:

```text
git pull
docker compose up --build -d
```

Полностью удалить локальные данные PostgreSQL и Redis:

```text
docker compose down -v
```

Последняя команда необратимо удаляет Docker volumes и предназначена только для полного
сброса тестового окружения.

## 7. Автоматическая проверка

Каждый push и pull request запускает GitHub Actions. Workflow на чистом Ubuntu-runner:

1. устанавливает Python 3.12 и зависимости;
2. запускает Ruff и все Pytest-тесты;
3. проверяет Compose-конфигурацию;
4. собирает и запускает весь стек;
5. ожидает успешный ответ `/health`;
6. останавливает контейнеры и удаляет тестовые volumes.

Статус проверок: <https://github.com/vitalysss/content-generation-service/actions>.
