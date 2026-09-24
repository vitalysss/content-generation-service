FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml ./
RUN mkdir -p src/app && touch src/app/__init__.py
RUN pip install --no-cache-dir .
COPY src ./src

COPY alembic.ini ./
COPY migrations ./migrations

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/logs && chown -R app:app /app/logs
USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
