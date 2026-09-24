import json
import logging
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from prometheus_client import Counter, Histogram

from app.settings import Settings

HTTP_REQUESTS_TOTAL = Counter(
    "content_service_http_requests_total",
    "Total number of HTTP requests",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "content_service_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_request_logging(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("app.http")
    if getattr(logger, "_content_service_configured", False):
        return logger

    log_directory = Path(settings.log_directory)
    log_directory.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_directory / "requests.log",
        when="midnight",
        backupCount=settings.log_retention_days,
        encoding="utf-8",
        delay=True,
        utc=True,
    )
    file_handler.setFormatter(formatter)

    logger.setLevel(settings.app_log_level.upper())
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    logger._content_service_configured = True  # type: ignore[attr-defined]
    return logger
