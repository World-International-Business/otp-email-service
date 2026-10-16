"""Configuration centralisée via pydantic-settings."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "otp-email-service"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    api_key: str = Field(..., min_length=32)
    hmac_secret: bytes = Field(..., min_length=16)
    admin_api_key: str = Field(
        default="admin_key_change_me_in_production_64chars_minimum_required_xx",
        min_length=32,
    )
    # Clé de chiffrement des secrets tenant au repos (Fernet).
    # Vide = dérivation depuis hmac_secret (OK en dev, déconseillé en prod).
    encryption_key: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    use_fakeredis: bool = False

    email_provider: str = "gmail"
    from_email: str
    from_name: str = "SendBID"

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None

    brevo_api_key: str | None = None
    resend_api_key: str | None = None

    risky_countries: str = "CN,RU,BY,IR,KP"
    rollout_percentage: int = Field(default=100, ge=0, le=100)

    @property
    def risky_countries_set(self) -> set[str]:
        return {c.strip().upper() for c in self.risky_countries.split(",") if c.strip()}

    max_sends_per_user_per_10min: int = 3
    max_sends_per_ip_per_10min: int = 3
    cooldown_seconds: int = 60
    max_verify_attempts: int = 5
    otp_ttl_seconds: int = 300

    # File d'attente de notifications + worker asynchrone
    notification_queue: str = "queue:notifications"
    message_ttl_seconds: int = 604800  # 7 jours de rétention des statuts
    worker_max_attempts: int = 3
    worker_poll_timeout_seconds: int = 5
    worker_idle_sleep_seconds: float = 1.0

    # Webhooks sortants (événements de statut vers les clients)
    webhook_timeout_seconds: float = 5.0

    @field_validator("hmac_secret", mode="before")
    @classmethod
    def _coerce_hmac_secret(cls, value: str | bytes) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        return value

@lru_cache
def get_settings() -> Settings:
    return Settings()
