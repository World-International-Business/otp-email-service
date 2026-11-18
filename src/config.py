"""Configuration centralisée via pydantic-settings."""

from functools import lru_cache

from pydantic import EmailStr, Field, field_validator
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
    admin_api_key: str = Field(..., min_length=32)
    admin_web_password: str | None = None  # mot de passe alternatif pour /admin/login

    redis_url: str = "redis://localhost:6379/0"
    public_base_url: str = ""  # ex: https://mail.aiforge.world — pour les magic links
    use_fakeredis: bool = False

    # Console de démo /demo — endpoints non authentifiés, local uniquement
    demo_enabled: bool = False

    # Inscription web publique
    web_signup_enabled: bool = True
    signup_invite_code: str | None = None  # si défini, requis à l'inscription
    web_login_max_attempts: int = 5        # par IP / 10 min

    email_provider: str = "gmail"
    from_email: EmailStr
    from_name: str = "SendBID"

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    # None = auto : SSL implicite si port 465, STARTTLS sinon
    smtp_use_ssl: bool | None = None

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

    @field_validator("hmac_secret", mode="before")
    @classmethod
    def _coerce_hmac_secret(cls, value: str | bytes) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        return value

    @field_validator("smtp_use_ssl", mode="before")
    @classmethod
    def _coerce_smtp_use_ssl(cls, value: object) -> object:
        # Chaîne vide (SMTP_USE_SSL= dans un .env) = auto (None)
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "debug", "use_fakeredis", "demo_enabled", "web_signup_enabled",
        mode="before",
    )
    @classmethod
    def _empty_str_bool(cls, value: object, info) -> object:
        # DEBUG= ou DEMO_ENABLED= vide dans un .env → valeur par défaut,
        # sinon pydantic lève une ValidationError au démarrage.
        if isinstance(value, str) and not value.strip():
            field = cls.model_fields[info.field_name]
            return field.default
        return value

@lru_cache
def get_settings() -> Settings:
    return Settings()
