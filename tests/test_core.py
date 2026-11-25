"""Tests unitaires — crypto, OTP store (fakeredis), templates, config."""

import smtplib
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from src.core.otp import OTPStore, verify_otp
from src.core.security import (
    compare_codes,
    generate_request_id,
    generate_secure_code,
    hash_code,
    is_in_rollout,
)
from src.core.tenants import Tenant
from src.services.email_templates import generic_html, otp_html


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="t_test",
        name="Test",
        api_key_hash="x" * 64,
        hmac_secret="secret",
        brand_name="TestBrand",
        allowed_purposes=["login_2fa"],
        otp_ttl_seconds=300,
        max_verify_attempts=3,
    )


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


# ------------------------------------------------------------------ crypto

def test_generate_code_format():
    code = generate_secure_code(6)
    assert code.isdigit() and len(code) == 6


def test_codes_are_random():
    assert generate_secure_code() != generate_secure_code()


def test_request_id_prefix():
    assert generate_request_id().startswith("req_")


def test_hash_compare():
    h = hash_code("123456")
    assert compare_codes("123456", h)
    assert not compare_codes("654321", h)


def test_rollout_bounds():
    assert is_in_rollout("u1", 100)
    assert not is_in_rollout("u1", 0)
    # déterministe
    assert is_in_rollout("u2", 50) == is_in_rollout("u2", 50)


# ------------------------------------------------------------------ OTP store

async def test_otp_lifecycle(redis, tenant):
    store = OTPStore(redis, tenant)
    code, request_id, _ = await store.create("user-uuid")

    ok, remaining = await verify_otp(redis, tenant, request_id, code)
    assert ok and remaining == 0

    # usage unique
    ok, _ = await verify_otp(redis, tenant, request_id, code)
    assert not ok


async def test_otp_wrong_code_attempts(redis, tenant):
    store = OTPStore(redis, tenant)
    code, request_id, _ = await store.create("user-uuid")

    wrong = "000000" if code != "000000" else "111111"
    ok, remaining = await verify_otp(redis, tenant, request_id, wrong)
    assert not ok and remaining == 2

    ok, remaining = await verify_otp(redis, tenant, request_id, code)
    assert ok


async def test_otp_max_attempts(redis, tenant):
    store = OTPStore(redis, tenant)
    _, request_id, _ = await store.create("user-uuid")
    wrong = "999999"

    for _ in range(tenant.max_verify_attempts):
        await verify_otp(redis, tenant, request_id, wrong)

    ok, remaining = await verify_otp(redis, tenant, request_id, wrong)
    assert not ok and remaining == 0


# ------------------------------------------------------------------ templates

def test_otp_html_contains_code_and_brand():
    html = otp_html("123456", "MonApp", "fr")
    assert "123456" in html and "MonApp" in html


def test_otp_html_escapes_brand():
    html = otp_html("123456", "<script>", "fr")
    assert "<script>" not in html


def test_generic_html_en():
    html = generic_html("Welcome", "Hello there", "Brand")
    assert "Welcome" in html and "Brand" in html


# ------------------------------------------------- responsive & branding

def test_email_responsive_structure():
    """Layout fluid-first : tient sur mobile SANS media query, media query en bonus."""
    html = otp_html("123456", "MonApp", "fr")
    assert 'width="100%"' in html
    assert "max-width:520px" in html
    assert "@media only screen and (max-width:560px)" in html
    # règles responsives + classes présentes sur les éléments
    for cls in ("mh-wrap", "mh-px", "mh-digit", "mh-gap"):
        assert f".{cls}" in html and f'class="{cls}' in html
    # les chiffres tiennent dans un écran 320px même sans media query :
    # 6×38px + 5×6px = 258px ≤ 264px de contenu dispo
    assert "width:38px" in html and 'class="mh-gap" style="width:6px' in html
    # preheader invisible même sur clients basiques
    assert "mso-hide:all" in html


def test_otp_html_respects_branding():
    """Couleur, logo absolu et footer personnalisé du tenant dans le mail OTP."""
    html = otp_html(
        "123456", "MaMarque", "fr",
        magic_url="https://app.example.com/verify",
        color="#e11d48",
        logo_url="https://cdn.example.com/logo.png",
        footer="© 2026 MaMarque — support@mamarque.io",
    )
    # couleur : header, chiffres, bouton
    assert html.count("#e11d48") >= 3
    # logo exact, pas transformé
    assert 'src="https://cdn.example.com/logo.png"' in html
    # footer personnalisé remplace le texte par défaut
    assert "© 2026 MaMarque — support@mamarque.io" in html
    assert "merci de ne pas répondre" not in html


def test_otp_html_default_branding():
    """Sans personnalisation : couleur fallback + footer par défaut."""
    html = otp_html("123456", "MonApp", "fr", color="pas-une-couleur")
    assert "#5060a8" in html
    assert "merci de ne pas répondre" in html


def test_generic_html_respects_branding():
    html = generic_html(
        "Bienvenue", "<p>Salut</p>", "MaBoîte",
        color="#059669", logo_url="https://x.io/l.png", footer="Custom footer",
    )
    assert "#059669" in html
    assert 'src="https://x.io/l.png"' in html
    assert "Custom footer" in html
    # titre injecté + responsive
    assert 'class="mh-h1"' in html
    assert "@media only screen" in html


def test_resolve_logo_url_absolute():
    from src.services.email_templates import resolve_logo_url
    assert resolve_logo_url("/brand/t1/logo", "https://api.mh.io/") == "https://api.mh.io/brand/t1/logo"
    assert resolve_logo_url("https://cdn.x.io/l.png", "https://api.mh.io") == "https://cdn.x.io/l.png"
    # URL relative sans base -> None : pas d'icône cassée dans Gmail
    assert resolve_logo_url("/brand/t1/logo", "") is None
    assert resolve_logo_url(None, "https://api.mh.io") is None


# ------------------------------------------------- SMTP : pas de doublons

class _FakeSMTP:
    """Faux serveur SMTP — quit() tombe en panne comme Gmail/ proxies le font."""

    sent = 0
    refused: dict = {}

    def __init__(self, *a, **k):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return False

    def login(self, user, password):
        pass

    last_msg = ""

    def sendmail(self, from_email, to, msg):
        type(self).sent += 1
        type(self).last_msg = msg
        return dict(type(self).refused)

    def quit(self):
        raise smtplib.SMTPServerDisconnected("connexion fermée par le serveur")

    def close(self):
        pass


def _smtp_settings():
    return SimpleNamespace(
        from_email="noreply@mh.io", from_name="MailHub",
        smtp_host="smtp.x.io", smtp_port=587,
        smtp_user="u", smtp_password="p", smtp_use_ssl=False,
        list_unsubscribe_url="https://mh.io/unsub",
    )


async def test_smtp_quit_failure_counts_as_success(monkeypatch):
    """Un QUIT qui échoue APRES sendmail ne doit pas déclencher de retry
    (sinon le destinataire reçoit des doublons)."""
    from src.email_providers.smtp import SMTPProvider

    _FakeSMTP.sent = 0
    _FakeSMTP.refused = {}
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    ok = await SMTPProvider(_smtp_settings()).send_email(
        "a@b.c", "s", "texte", "<p>html</p>"
    )
    assert ok is True
    assert _FakeSMTP.sent == 1  # un seul envoi réel


async def test_smtp_refused_recipient_raises(monkeypatch):
    """RCPT refusé (sendmail retourne un dict) = vraie erreur -> retry légitime."""
    from src.email_providers.base import EmailSendError
    from src.email_providers.smtp import SMTPProvider

    _FakeSMTP.sent = 0
    _FakeSMTP.refused = {"a@b.c": (550, b"user unknown")}
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with pytest.raises(EmailSendError):
        await SMTPProvider(_smtp_settings()).send_email(
            "a@b.c", "s", "texte", "<p>html</p>"
        )


async def test_smtp_message_has_antispam_headers(monkeypatch):
    """Date + Message-ID (domaine expéditeur) + List-Unsubscribe + sujet encodé."""
    from src.email_providers.smtp import SMTPProvider

    _FakeSMTP.sent = 0
    _FakeSMTP.refused = {}
    _FakeSMTP.last_msg = ""
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    await SMTPProvider(_smtp_settings()).send_email(
        "a@b.c", "Vérification ✓", "texte", "<p>html</p>"
    )
    raw = _FakeSMTP.last_msg
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    assert "\nDate: " in raw or raw.startswith("Date:")
    assert "Message-ID: <" in raw and "@mh.io>" in raw
    assert "List-Unsubscribe: <https://mh.io/unsub>" in raw
    assert "Reply-To: noreply@mh.io" in raw
    assert "utf-8?" in raw  # sujet accentué encodé, pas cassé


async def test_queue_lock_prevents_double_processing(tenant):
    """Un job verrouillé par un autre worker n'est pas traité une 2e fois."""
    from unittest.mock import AsyncMock

    from src.core.tenants import TenantStore
    from src.services.email_queue import QUEUE_KEY, enqueue, process_email_queue_once

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await TenantStore(redis).create(tenant)
    await enqueue(redis, tenant.tenant_id, "a@b.c", "notification",
                  "s", "texte", "<p>h</p>")
    job_id = (await redis.zrange(QUEUE_KEY, 0, -1))[0]

    # Un autre worker a déjà pris le job -> rien n'est livré.
    await redis.set(f"mail:lock:{job_id}", "1", nx=True, ex=120)
    service = SimpleNamespace(deliver=AsyncMock())
    assert await process_email_queue_once(redis, service) == 0
    service.deliver.assert_not_called()

    # Lock libéré -> le job est livré une seule fois.
    await redis.delete(f"mail:lock:{job_id}")
    assert await process_email_queue_once(redis, service) == 1
    service.deliver.assert_awaited_once()
    assert await process_email_queue_once(redis, service) == 0


# ------------------------------------------------- santé SMTP + DNS

async def test_smtp_health_ok(monkeypatch):
    """Connexion + login réussis -> ok."""
    from src.services.smtp_health import check_smtp_health

    _FakeSMTP.sent = 0
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    res = await check_smtp_health("smtp.x.io", 587, "u", "p", False)
    assert res["ok"] and res["stage"] == "ok" and res["latency_ms"] >= 0


async def test_smtp_health_bad_credentials(monkeypatch):
    """Login refusé -> stage 'login', ok=False (tenant alerté)."""
    from src.services.smtp_health import check_smtp_health

    class _BadSMTP(_FakeSMTP):
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"credentials denied")

    monkeypatch.setattr(smtplib, "SMTP", _BadSMTP)
    res = await check_smtp_health("smtp.x.io", 587, "u", "p", False)
    assert not res["ok"] and res["stage"] == "login"
    assert "535" in res["error"]


async def test_smtp_health_unreachable(monkeypatch):
    """Hôte injoignable -> stage 'connect'."""
    from src.services.smtp_health import check_smtp_health

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _boom)
    res = await check_smtp_health("down.x.io", 587, "u", "p", False)
    assert not res["ok"] and res["stage"] == "connect"


def test_domain_verdict_all_ok():
    from src.services.dns_check import domain_verdict
    ok = {"mx": True, "spf_ok": True, "dkim_ok": True, "dmarc_ok": True}
    assert domain_verdict(ok) == "ok"
    missing = {"mx": True, "spf_ok": False, "dkim_ok": False, "dmarc_ok": True}
    assert "SPF" in domain_verdict(missing) and "DKIM" in domain_verdict(missing)


async def test_dns_check_parses_records(monkeypatch):
    """MX + SPF + DMARC + sélecteur DKIM détectés correctement."""
    import src.services.dns_check as dc

    class _R:
        def __init__(self, s): self.strings = [s.encode()]

    def fake_resolve(domain, rtype, lifetime=5):
        if rtype == "MX" and domain == "x.io":
            return ["mx"]
        if rtype == "TXT":
            if domain == "x.io":
                return [_R("v=spf1 include:_spf.x.io ~all")]
            if domain == "_dmarc.x.io":
                return [_R("v=DMARC1; p=quarantine")]
            if domain == "mail._domainkey.x.io":
                return [_R("v=DKIM1; k=rsa; p=abc")]
        raise dns.resolver.NXDOMAIN

    import dns.resolver
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolve)
    res = await dc.check_email_domain("x.io")
    assert res["mx"] and res["spf_ok"] and res["dkim_ok"] and res["dmarc_ok"]
    assert res["dkim_selector"] == "mail"
    assert dc.domain_verdict(res) == "ok"
