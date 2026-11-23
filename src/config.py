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
    admin_api_key: str | None = Field(default=None, min_length=32)
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

    email_provider: str = "smtp"  # smtp | brevo | resend | console (« gmail » = alias legacy)
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

    # Paiements — remplir quand les comptes marchands seront créés
    stripe_secret_key: str | None = None      # sk_live_… / sk_test_…
    stripe_webhook_secret: str | None = None  # whsec_…
    stripe_price_pro: str | None = None       # price_… du plan Pro
    stripe_price_enterprise: str | None = None  # price_… du plan Enterprise
    paypal_client_id: str | None = None
    paypal_client_secret: str | None = None
    paypal_webhook_id: str | None = None
    paypal_env: str = "live"  # live | sandbox
    # Mobile money — Orange Money (Cameroon etc.)
    orange_money_merchant_key: str | None = None
    orange_money_merchant_secret: str | None = None
    # Mobile money — MTN MoMo (Collection API)
    mtn_momo_api_user: str | None = None
    mtn_momo_api_key: str | None = None
    mtn_momo_subscription_key: str | None = None
    # Agrégateur mobile money — CinetPay (Orange Money, MTN MoMo, Wave, Moov…)
    cinetpay_api_key: str | None = None
    cinetpay_site_id: str | None = None

    # Tarifs affichés/facturés en ligne (0 ou absent = bouton masqué,
    # le plan reste activable manuellement par l'admin comme avant)
    plan_price_pro: float = 0.0           # carte/PayPal, devise billing_currency
    plan_price_enterprise: float = 0.0
    billing_currency: str = "EUR"
    plan_price_pro_mobile: int = 0        # agrégateur mobile, devise billing_currency_mobile
    plan_price_enterprise_mobile: int = 0
    billing_currency_mobile: str = "XOF"
    # Tarifs annuels (période 365 j) — 0 ou absent = option masquée
    plan_price_pro_yearly: float = 0.0
    plan_price_enterprise_yearly: float = 0.0
    plan_price_pro_mobile_yearly: int = 0
    plan_price_enterprise_mobile_yearly: int = 0

    # Coordonnées de réception affichées pour les paiements manuels
    billing_bank_details: str | None = None   # RIB/IBAN — virement bancaire
    billing_momo_number: str | None = None    # numéro — transfert mobile money direct
    billing_momo_name: str | None = None      # titulaire du compte mobile money
    billing_manual_note: str | None = None    # instructions libres (Western Union, cash…)

    # Résiliation automatique des abonnements expirés (secondes entre chaque balayage)
    billing_check_interval_seconds: int = 3600

    # File d'envoi d'emails (livraison asynchrone + retries) et webhooks sortants
    email_queue_enabled: bool = True      # False = envoi synchrone comme avant
    email_queue_poll_seconds: float = 1.0  # intervalle de poll du worker
    email_max_attempts: int = 4            # 1 essai + 3 retries (60s, 5min, 15min) puis dead

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
