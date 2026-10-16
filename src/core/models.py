"""Schémas Pydantic pour les requêtes et réponses."""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


class Language(str, Enum):
    fr = "fr"
    en = "en"


class Channel(str, Enum):
    email = "email"
    sms_fallback = "sms_fallback"


class NotificationChannel(str, Enum):
    email = "email"
    sms = "sms"    # réservé — non encore implémenté
    push = "push"  # réservé — non encore implémenté


class MessageStatus(str, Enum):
    """Cycle de vie d'une notification, façon provider."""

    queued = "queued"
    processing = "processing"
    sent = "sent"
    delivered = "delivered"  # réservé : confirmé via callback provider
    bounced = "bounced"      # réservé : confirmé via callback provider
    failed = "failed"


class OTPSendRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID utilisateur")
    email: EmailStr = Field(..., description="Adresse email destinataire")
    country_code: str = Field(
        ...,
        min_length=2,
        max_length=3,
        description="Code pays ISO (ex: FR, US, CI)",
    )
    purpose: str = Field(
        ...,
        description="Usage de l'OTP (ex: login_2fa, order_confirmation, password_reset)",
    )
    language: Language = Field(default=Language.en, description="Langue de l'email")


class OTPSendResponse(BaseModel):
    channel: Channel
    request_id: str = Field(..., description="Identifiant unique de la tentative")
    expires_in: int = Field(..., description="Durée de validité en secondes")
    message_id: str | None = Field(
        default=None,
        description="Identifiant du message en file d'attente (suivi via /v1/notifications/{id})",
    )
    debug_code: str | None = Field(
        default=None,
        description="Code OTP en clair — DEBUG uniquement, jamais en production",
        exclude=True,
    )


class OTPVerifyRequest(BaseModel):
    request_id: str = Field(..., description="request_id retourné par /send")
    code: str = Field(..., pattern=r"^\d{6}$", description="Code OTP 6 chiffres")


class OTPVerifyResponse(BaseModel):
    verified: bool
    attempts_remaining: int = Field(
        default=0,
        ge=0,
        description="Nombre de tentatives restantes (0 si succès ou bloqué)",
    )


class HealthResponse(BaseModel):
    status: str
    redis: str
    version: str


class EmailType(str, Enum):
    welcome = "welcome"
    notification = "notification"
    transaction = "transaction"
    custom = "custom"


class EmailSendRequest(BaseModel):
    to_email: EmailStr = Field(..., description="Adresse email destinataire")
    type: EmailType = Field(..., description="Type d'email à envoyer")
    language: Language = Field(default=Language.en, description="Langue de l'email")
    data: dict = Field(
        default_factory=dict,
        description="Variables du template (ex: {name: 'John', amount: '100 EUR'})",
    )
    subject: str | None = Field(
        default=None,
        description="Sujet libre — obligatoire si type=custom",
    )
    text_body: str | None = Field(
        default=None,
        description="Corps texte libre — obligatoire si type=custom",
    )
    html_body: str | None = Field(
        default=None,
        description="Corps HTML libre — optionnel si type=custom",
    )


class EmailSendResponse(BaseModel):
    sent: bool
    message_id: str = Field(..., description="Identifiant de l'envoi")
    to_email: str = Field(..., description="Destinataire (masqué dans les logs)")
    status: MessageStatus = Field(
        default=MessageStatus.queued,
        description="Statut du message dans le cycle de vie (queued au retour)",
    )


# ------------------------------------------------------------------
# Notifications — API agnostique avec file d'attente (style Provider)
# ------------------------------------------------------------------


class NotificationRequest(BaseModel):
    """Requête de notification agnostique — le provider ne connaît pas le métier.

    Le client fournit soit un `template_id` (template stocké via /v1/templates),
    soit un contenu inline (`subject` + `text`/`html`). Les variables `{{var}}`
    sont rendues avec `data`.
    """

    channel: NotificationChannel = Field(
        default=NotificationChannel.email, description="Canal de diffusion"
    )
    to: EmailStr = Field(..., description="Destinataire")
    template_id: str | None = Field(
        default=None,
        description="ID d'un template tenant (via /v1/templates)",
    )
    subject: str | None = Field(
        default=None, description="Sujet inline — requis sans template_id"
    )
    text: str | None = Field(
        default=None, description="Corps texte inline — requis sans template_id"
    )
    html: str | None = Field(
        default=None, description="Corps HTML inline (optionnel)"
    )
    data: dict = Field(
        default_factory=dict,
        description="Variables de rendu pour les placeholders {{var}}",
    )
    language: Language = Field(default=Language.en, description="Variante de langue")
    metadata: dict = Field(
        default_factory=dict,
        description="Contexte client libre (purpose, order_id…) — renvoyé via webhooks",
    )

    @model_validator(mode="after")
    def _content_or_template(self):
        if not self.template_id and not (self.subject and self.text):
            raise ValueError(
                "subject and text are required when template_id is not provided"
            )
        return self


class NotificationAcceptedResponse(BaseModel):
    """Réponse 202 : la notification est acceptée et mise en file d'attente."""

    message_id: str = Field(..., description="Identifiant du message (suivi)")
    status: MessageStatus = Field(default=MessageStatus.queued)
    channel: NotificationChannel
    to: str


class MessageStatusEvent(BaseModel):
    status: MessageStatus
    at: str = Field(..., description="Horodatage ISO-8601 UTC")
    detail: str | None = None


class MessageStatusResponse(BaseModel):
    """État courant d'un message, consultable par le client."""

    message_id: str
    channel: NotificationChannel
    to: str
    subject: str | None
    status: MessageStatus
    attempts: int
    metadata: dict
    error: str | None
    history: list[MessageStatusEvent]
    created_at: str
    updated_at: str


# ------------------------------------------------------------------
# Templates — stockés par tenant, rendus avec {{var}}
# ------------------------------------------------------------------


class TemplateVariant(BaseModel):
    """Contenu d'un template pour une langue donnée."""

    subject: str = Field(..., description="Sujet — supporte {{var}}")
    text: str | None = Field(default=None, description="Corps texte — supporte {{var}}")
    html: str | None = Field(default=None, description="Corps HTML — supporte {{var}}")


class TemplateUpsertRequest(BaseModel):
    variants: dict[str, TemplateVariant] = Field(
        ...,
        description="Variantes par langue : {'default': {...}, 'fr': {...}, 'en': {...}}",
    )
    description: str | None = None


class TemplateResponse(BaseModel):
    template_id: str
    variants: dict[str, TemplateVariant]
    description: str | None
    created_at: str
    updated_at: str


# ------------------------------------------------------------------
# Admin — gestion des tenants
# ------------------------------------------------------------------


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(..., description="Nom d'affichage du tenant")
    brand_name: str = Field(default="SendBID")
    from_email: EmailStr | None = None
    from_name: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    risky_countries: list[str] = Field(default_factory=list)
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    allowed_purposes: list[str] = Field(
        default_factory=list,
        description="Whitelist OTP — vide = agnostique (tous purposes acceptés)",
    )
    webhook_url: str | None = Field(
        default=None,
        description="URL du client recevant les événements de statut (POST signé HMAC)",
    )
    max_sends_per_user_per_10min: int = 3
    max_sends_per_ip_per_10min: int = 3
    cooldown_seconds: int = 60
    max_verify_attempts: int = 5
    otp_ttl_seconds: int = 300


class TenantCreateResponse(BaseModel):
    tenant_id: str
    api_key: str = Field(..., description="À communiquer au client UNE SEULE FOIS")
    hmac_secret: str = Field(..., description="À communiquer au client UNE SEULE FOIS")


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    brand_name: str | None = None
    from_email: EmailStr | None = None
    from_name: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    risky_countries: list[str] | None = None
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    allowed_purposes: list[str] | None = None
    webhook_url: str | None = None
    max_sends_per_user_per_10min: int | None = None
    max_sends_per_ip_per_10min: int | None = None
    cooldown_seconds: int | None = None
    max_verify_attempts: int | None = None
    otp_ttl_seconds: int | None = None
    is_active: bool | None = None


class TenantInfoResponse(BaseModel):
    tenant_id: str
    name: str
    brand_name: str
    from_email: str | None
    rollout_percentage: int
    allowed_purposes: list[str]
    webhook_url: str | None
    is_active: bool
