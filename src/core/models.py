"""Schémas Pydantic pour les requêtes et réponses."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class Language(str, Enum):
    fr = "fr"
    en = "en"


class Channel(str, Enum):
    email = "email"
    sms_fallback = "sms_fallback"


class OTPSendRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID utilisateur")
    email: EmailStr = Field(..., description="Adresse email destinataire")
    country_code: str = Field(
        ...,
        min_length=2,
        max_length=2,
        pattern=r"^[A-Za-z]{2}$",
        description="Code pays ISO 3166-1 alpha-2 (ex: FR, US, CI)",
    )
    purpose: str = Field(
        ...,
        description="Usage de l'OTP (ex: login_2fa, order_confirmation, password_reset)",
    )
    language: Language = Field(default=Language.en, description="Langue de l'email")
    magic_link: bool = Field(
        default=False,
        description="Si true, l'email contient aussi un bouton 'vérifier en un clic'",
    )


class OTPSendResponse(BaseModel):
    channel: Channel
    request_id: str = Field(..., description="Identifiant unique de la tentative")
    expires_in: int = Field(..., description="Durée de validité en secondes")
    debug_code: str | None = Field(
        default=None,
        description="Code OTP en clair — DEBUG uniquement, jamais en production",
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


MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_TOTAL_BYTES = 8 * 1024 * 1024  # décodé — +33% en base64 transport


class Attachment(BaseModel):
    """Pièce jointe — contenu encodé en base64 côté JSON."""

    filename: str = Field(..., min_length=1, max_length=200)
    content_base64: str = Field(..., description="Contenu du fichier encodé base64")
    content_type: str = Field(default="application/octet-stream", max_length=100)


def _validate_attachments(atts: list[Attachment]) -> list[Attachment]:
    """Vérifie le base64 et le poids total (422 sinon)."""
    import base64
    import binascii

    total = 0
    for att in atts:
        try:
            total += len(base64.b64decode(att.content_base64, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"attachments: contenu base64 invalide pour {att.filename!r}"
            ) from exc
    if total > MAX_ATTACHMENT_TOTAL_BYTES:
        raise ValueError(
            f"attachments: poids total {total} octets > "
            f"{MAX_ATTACHMENT_TOTAL_BYTES} (8 Mo décodés)"
        )
    return atts


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
    attachments: list[Attachment] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS,
        description="Pièces jointes (base64) — 10 max, 8 Mo décodés au total",
    )
    send_at: datetime | None = Field(
        default=None,
        description="Envoi programmé (ISO 8601) — max 30 jours, "
                    "nécessite EMAIL_QUEUE_ENABLED. Passé = envoi immédiat.",
    )

    @field_validator("attachments")
    @classmethod
    def _check_attachments(cls, v: list[Attachment]) -> list[Attachment]:
        return _validate_attachments(v)


class EmailSendResponse(BaseModel):
    sent: bool = Field(..., description="True = accepté par le service (voir status)")
    message_id: str = Field(..., description="Identifiant de l'envoi")
    to_email: str = Field(..., description="Destinataire (masqué dans les logs)")
    status: str = Field(
        default="sent",
        description="queued (file+retry) | sent — consulter GET /v1/emails/status/{message_id}",
    )
    scheduled_for: str | None = Field(
        default=None,
        description="Horodatage ISO si l'envoi est programmé (send_at)",
    )


class EmailStatusResponse(BaseModel):
    message_id: str
    status: str = Field(
        ..., description="queued | retry | sent | dead | failed"
    )
    to: str | None = None
    type: str | None = None
    attempts: int = 0
    error: str | None = None
    updated: int = 0
    scheduled_for: str | None = None


MAX_BATCH_RECIPIENTS = 200


class EmailBatchRequest(BaseModel):
    """Même email à N destinataires — un seul appel au lieu de N requêtes.

    Chaque destinataire reçoit son propre message_id, son quota destinataire
    et sa suppression vérifiés indépendamment."""

    to: list[EmailStr] = Field(
        ..., min_length=1, max_length=MAX_BATCH_RECIPIENTS,
        description=f"Destinataires ({MAX_BATCH_RECIPIENTS} max par appel)",
    )
    type: EmailType = Field(..., description="Type d'email à envoyer")
    language: Language = Field(default=Language.en, description="Langue de l'email")
    data: dict = Field(
        default_factory=dict,
        description="Variables du template — identiques pour tous les destinataires",
    )
    subject: str | None = Field(default=None, description="Obligatoire si type=custom")
    text_body: str | None = Field(default=None, description="Obligatoire si type=custom")
    html_body: str | None = Field(default=None, description="Optionnel si type=custom")
    attachments: list[Attachment] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS,
    )
    send_at: datetime | None = Field(
        default=None,
        description="Envoi programmé — nécessite EMAIL_QUEUE_ENABLED",
    )

    @field_validator("attachments")
    @classmethod
    def _check_attachments(cls, v: list[Attachment]) -> list[Attachment]:
        return _validate_attachments(v)


class EmailBatchResult(BaseModel):
    to_email: str
    message_id: str | None = None
    status: str = Field(..., description="queued | rejected")
    error: str | None = None


class EmailBatchResponse(BaseModel):
    sent: int = Field(..., description="Nombre d'emails acceptés (queued)")
    failed: int = Field(..., description="Nombre de destinataires rejetés (422/429)")
    results: list[EmailBatchResult]


# ------------------------------------------------------------------
# Admin — gestion des tenants
# ------------------------------------------------------------------


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(..., description="Nom d'affichage du tenant")
    brand_name: str = Field(default="SendBID")
    from_email: EmailStr | None = None
    from_name: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_ssl: bool | None = None
    risky_countries: list[str] = Field(default_factory=list)
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    allowed_purposes: list[str] = Field(default_factory=lambda: ["login_2fa"])
    plan: str = "free"
    brand_color: str = "#5060a8"
    email_logo_url: str | None = None
    email_footer: str | None = None
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
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_ssl: bool | None = None
    risky_countries: list[str] | None = None
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    allowed_purposes: list[str] | None = None
    plan: str | None = None
    brand_color: str | None = None
    email_logo_url: str | None = None
    email_footer: str | None = None
    max_sends_per_user_per_10min: int | None = None
    max_sends_per_ip_per_10min: int | None = None
    cooldown_seconds: int | None = None
    max_verify_attempts: int | None = None
    otp_ttl_seconds: int | None = None
    is_active: bool | None = None
    unlimited: bool | None = None
    plan_requested: str | None = None
    billing_status: str | None = None
    paid_until: str | None = None
    payment_provider: str | None = None
    payment_ref: str | None = None


class TenantInfoResponse(BaseModel):
    tenant_id: str
    name: str
    brand_name: str
    from_email: str | None
    rollout_percentage: int
    allowed_purposes: list[str]
    plan: str = "free"
    brand_color: str = "#5060a8"
    email_logo_url: str | None = None
    email_footer: str | None = None
    is_active: bool
