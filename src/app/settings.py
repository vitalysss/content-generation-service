from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Content Generation Service"
    app_env: str = "local"
    app_log_level: str = "INFO"
    log_directory: str = "logs"
    log_retention_days: int = 7
    database_url: str = "postgresql+asyncpg://app:app@postgres:5432/content_service"
    redis_url: str = "redis://redis:6379/0"
    fal_key: SecretStr = SecretStr("")
    generation_provider: str = "fake"
    fallback_generation_provider: str = "fake"
    callback_timeout_seconds: float = 10.0
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60
    rate_limit_block_seconds: int = 60
    fal_queue_base_url: str = "https://queue.fal.run"
    fal_poll_interval_seconds: float = 2.0
    fal_request_timeout_seconds: float = 300.0
    processing_lease_seconds: int = 600
    queued_recovery_seconds: int = 120
    callback_delivery_lease_seconds: int = 60
    payment_webhook_secret: SecretStr = SecretStr("change-me")
    api_key_pepper: SecretStr = SecretStr("change-me")
    cost_text_to_image: int = 10
    cost_image_to_image: int = 15
    cost_text_to_video: int = 50
    cost_image_to_video: int = 60
    video_resolution_480p_percent: int = 50
    video_resolution_720p_percent: int = 75
    video_resolution_1080p_percent: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
