"""Tests d'intégration API — auth HMAC, queue d'envoi, idempotence,
statuts, webhooks sortants et rappels de facturation.

L'app tourne sur fakeredis + provider console (env de test) — aucun service
externe requis. EMAIL_QUEUE_POLL_SECONDS=3600 neutralise le worker de fond :
les tests pilotent process_*_once() manuellement (déterministe).
"""

import datetime as _dt
import hashlib
import hmac
import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

API_KEY = "k" * 64
HMAC_SECRET = "s" * 32


def _sign(body: bytes, secret: str = HMAC_SECRET, api_key: str = API_KEY) -> dict:
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    sig = hmac.new(
        secret.encode(), body + ts.encode() + b"|" + nonce.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-API-Key": api_key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-HMAC-Signature": sig,
    }


def _post(client, path: str, payload: dict, extra_headers: dict | None = None):
    body = json.dumps(payload).encode()
    headers = {**_sign(body), **(extra_headers or {})}
    return client.post(path, content=body,
                       headers={"Content-Type": "application/json", **headers})


def _get(client, path: str, secret: str = HMAC_SECRET, api_key: str = API_KEY):
    return client.get(path, headers=_sign(b"", secret, api_key))


class _SpyProvider:
    """Provider capturant — enregistre les emails au lieu de les envoyer."""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple] = []
        self.fail = fail

    async def send_email(self, to_email, subject, text_body, html_body,
                         sender=None):
        if self.fail:
            from src.email_providers.base import EmailSendError
            raise EmailSendError("smtp down")
        self.sent.append((to_email, subject, text_body))
        return True


@pytest.fixture
def client(monkeypatch):
    for k, v in {
        "API_KEY": API_KEY,
        "HMAC_SECRET": HMAC_SECRET,
        "FROM_EMAIL": "noreply@test.dev",
        "FROM_NAME": "MailHub Test",
        "EMAIL_PROVIDER": "console",
        "USE_FAKEREDIS": "true",
        "DEBUG": "true",
        "ADMIN_API_KEY": "a" * 40,
        "EMAIL_QUEUE_POLL_SECONDS": "3600",
        "BILLING_CHECK_INTERVAL_SECONDS": "3600",
    }.items():
        monkeypatch.setenv(k, v)
    from src.config import get_settings
    get_settings.cache_clear()
    import src.main as m
    with TestClient(m.app) as c:
        yield c


@pytest.fixture
def redis(client):
    return client.app.state.redis


async def test_health(client):
    assert client.get("/health").status_code == 200


async def test_auth_rejected_without_headers(client):
    assert client.post("/v1/email/send", json={}).status_code == 401
    assert client.post("/v1/otp/send", json={}).status_code == 401


async def test_otp_send_and_verify_flow(client):
    spy = _SpyProvider()
    client.app.state.otp_service.email_provider = spy
    resp = _post(client, "/v1/otp/send", {
        "user_id": str(uuid.uuid4()),
        "email": "user@example.com",
        "country_code": "FR",
        "purpose": "login_2fa",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["channel"] == "email" and data["request_id"].startswith("req_")
    # Le code n'est jamais dans la réponse API : on le lit dans le corps
    # de l'email capturé par le provider espion.
    code = re.search(r"\b(\d{6})\b", spy.sent[0][2]).group(1)
    resp2 = _post(client, "/v1/otp/verify",
                  {"request_id": data["request_id"], "code": code})
    assert resp2.status_code == 200 and resp2.json()["verified"] is True


async def test_otp_idempotency_key_replays_same_response(client):
    client.app.state.otp_service.email_provider = _SpyProvider()
    payload = {"user_id": str(uuid.uuid4()), "email": "u2@example.com",
               "country_code": "FR", "purpose": "login_2fa"}
    h = {"Idempotency-Key": "test-key-1"}
    r1 = _post(client, "/v1/otp/send", payload, h)
    r2 = _post(client, "/v1/otp/send", payload, h)
    assert r1.status_code == r2.status_code == 200
    # Même request_id => même code, aucun email supplémentaire.
    assert r1.json()["request_id"] == r2.json()["request_id"]
    assert len(client.app.state.otp_service.email_provider.sent) == 1


async def test_email_send_queued_then_delivered(client, redis):
    resp = _post(client, "/v1/email/send", {
        "to_email": "client@example.com", "type": "notification",
        "data": {"name": "Alice"},
    })
    assert resp.status_code == 200, resp.text
    mid = resp.json()["message_id"]
    assert resp.json()["status"] == "queued"

    r = _get(client, f"/v1/email/status/{mid}")
    assert r.status_code == 200 and r.json()["status"] == "queued"

    from src.services.email_queue import process_email_queue_once
    n = await process_email_queue_once(redis, client.app.state.email_service)
    assert n == 1
    r = _get(client, f"/v1/email/status/{mid}")
    assert r.json()["status"] == "sent"


async def test_email_idempotency_key(client):
    payload = {"to_email": "x@example.com", "type": "notification", "data": {}}
    h = {"Idempotency-Key": "mail-key-1"}
    r1 = _post(client, "/v1/email/send", payload, h)
    r2 = _post(client, "/v1/email/send", payload, h)
    assert r1.json()["message_id"] == r2.json()["message_id"]


async def test_status_is_tenant_scoped(client, redis):
    """Le message_id d'un tenant n'est pas lisible par un autre tenant."""
    from src.core.tenants import (
        Tenant,
        TenantStore,
        generate_api_key,
        generate_hmac_secret,
        hash_api_key,
    )

    key2 = generate_api_key()
    secret2 = generate_hmac_secret()
    await TenantStore(redis).create(Tenant(
        tenant_id="other", name="Other", api_key_hash=hash_api_key(key2),
        hmac_secret=secret2, from_email="o@x.co", from_name="O",
        allowed_purposes=["login_2fa"],
    ))
    resp = _post(client, "/v1/email/send",
                 {"to_email": "a@b.co", "type": "notification", "data": {}})
    mid = resp.json()["message_id"]
    r = _get(client, f"/v1/email/status/{mid}", secret=secret2, api_key=key2)
    assert r.status_code == 404


async def test_queue_retry_then_dead_letter(client, redis):
    """Provider en panne => retry puis dead + quota destinataire rendu."""
    from src.services.email_queue import get_status, process_email_queue_once

    client.app.state.email_service.email_provider = _SpyProvider(fail=True)
    resp = _post(client, "/v1/email/send",
                 {"to_email": "f@x.co", "type": "notification", "data": {}})
    mid = resp.json()["message_id"]

    await process_email_queue_once(redis, client.app.state.email_service,
                                   max_attempts=2)
    st = await get_status(redis, mid)
    assert st["status"] == "retry" and st["attempts"] == 1

    # Forcer l'échéance du retry
    job_ids = await redis.zrange("mail:queue", 0, -1)
    assert job_ids
    for jid in job_ids:
        await redis.zadd("mail:queue", {jid: 0})
    await process_email_queue_once(redis, client.app.state.email_service,
                                   max_attempts=2)
    st = await get_status(redis, mid)
    assert st["status"] == "dead" and "smtp down" in st["error"]


async def test_outbound_webhook_signed_and_delivered(client, redis, respx_mock):
    """Le tenant reçoit un POST signé HMAC quand l'email part."""
    from src.core.tenants import TenantStore
    from src.services.email_queue import process_email_queue_once
    from src.services.webhooks import process_webhook_queue_once

    await TenantStore(redis).update(
        "default", {"webhook_url": "https://client.example.com/hook"})
    route = respx_mock.post("https://client.example.com/hook").respond(200)

    resp = _post(client, "/v1/email/send",
                 {"to_email": "w@x.co", "type": "notification", "data": {}})
    assert resp.status_code == 200
    await process_email_queue_once(redis, client.app.state.email_service)
    await process_webhook_queue_once(redis)

    assert route.called
    req = route.calls[0].request
    assert req.headers["X-MailHub-Event"] == "email.sent"
    expected = hmac.new(HMAC_SECRET.encode(), req.content,
                        hashlib.sha256).hexdigest()
    assert req.headers["X-MailHub-Signature"] == f"sha256={expected}"
    body = json.loads(req.content)
    assert body["event"] == "email.sent" and body["data"]["to"] == "w@x.co"


async def test_stripe_webhook_requires_secret(client):
    """Sans STRIPE_WEBHOOK_SECRET configuré : 404 ; mauvaise signature : 400."""
    r = client.post("/v1/billing/webhooks/stripe",
                    content=b"{}", headers={"stripe-signature": "x"})
    assert r.status_code in (400, 404)


async def test_billing_reminder_sends_email(client, redis):
    """J-7 avant paid_until => email de rappel au compte dashboard."""
    from datetime import date, timedelta

    from src.core.tenants import TenantStore
    from src.services.billing_reminders import run_billing_reminders
    from src.web.users import WebUserStore

    until = (date.today() + timedelta(days=7)).isoformat()
    await TenantStore(redis).update("default", {
        "plan": "pro", "billing_status": "active", "paid_until": until})
    await WebUserStore(redis).create("boss@t.co", "Passw0rd!", "default", "App")

    spy = _SpyProvider()
    n = await run_billing_reminders(redis, spy)
    assert n == 1
    assert spy.sent[0][0] == "boss@t.co" and "expire" in spy.sent[0][1]
    # Dédupliqué : un second passage n'envoie rien.
    assert await run_billing_reminders(redis, spy) == 0


# ---------------------------------------------------------- SMTP failover

class _FailoverProvider:
    """Échoue via le SMTP custom du tenant, réussit via la plateforme."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_email(self, to_email, subject, text_body, html_body,
                         sender=None):
        self.calls.append(dict(sender or {}))
        if (sender or {}).get("smtp_host") == "smtp.tenant-down.tld":
            from src.email_providers.base import EmailSendError
            raise EmailSendError("tenant smtp down")
        return True


async def test_otp_failover_to_platform_sender(client, redis):
    """SMTP du tenant en panne => l'OTP part via l'expéditeur plateforme."""
    from src.core.tenants import TenantStore
    from src.services.events import get_events

    store = TenantStore(redis)
    try:
        await store.update(
            "default", {"smtp_host": "smtp.tenant-down.tld",
                        "from_name": "Acme"})
        prov = _FailoverProvider()
        client.app.state.otp_service.email_provider = prov

        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "u@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 200, resp.text

        # 2 tentatives : SMTP tenant (échoué) puis plateforme (succès)
        assert len(prov.calls) == 2
        assert prov.calls[0].get("smtp_host") == "smtp.tenant-down.tld"
        assert "smtp_host" not in prov.calls[1]
        assert prov.calls[1]["from_email"] == "noreply@test.dev"
        assert prov.calls[1]["from_name"] == "Acme via MailHub Test"

        tenant = await store.get("default")
        events = await get_events(redis, tenant)
        assert any(e["type"] == "smtp_failover" for e in events)
        sent = next(e for e in events if e["type"] == "otp_sent")
        assert sent["via"] == "failover"
    finally:
        await store.update("default", {"smtp_host": None, "from_name": None})


async def test_email_failover_to_platform_sender(client, redis):
    """Même bascule sur le chemin transactionnel (deliver)."""
    from src.core.models import EmailSendRequest
    from src.core.tenants import TenantStore

    store = TenantStore(redis)
    try:
        await store.update(
            "default", {"smtp_host": "smtp.tenant-down.tld"})
        tenant = await store.get("default")
        svc = client.app.state.email_service
        prov = _FailoverProvider()
        svc.email_provider = prov

        req = EmailSendRequest(to_email="u@x.co", type="notification",
                               data={"title": "Hi", "message": "m"})
        resp = await svc.send(tenant, req)
        assert resp.sent is True and len(prov.calls) == 2
        assert "smtp_host" not in prov.calls[1]
    finally:
        await store.update("default", {"smtp_host": None})


async def test_failover_disabled_returns_502(client, redis):
    """EMAIL_FAILOVER_ENABLED=false => l'échec remonte sans bascule."""
    from src.core.tenants import TenantStore

    store = TenantStore(redis)
    try:
        await store.update(
            "default", {"smtp_host": "smtp.tenant-down.tld"})
        client.app.state.otp_service.settings.email_failover_enabled = False
        prov = _FailoverProvider()
        client.app.state.otp_service.email_provider = prov

        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "u@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 502
        assert len(prov.calls) == 1
    finally:
        await store.update("default", {"smtp_host": None})


async def test_no_failover_without_custom_smtp(client, redis):
    """Tenant sur le transport plateforme => pas de double tentative."""
    prov = _SpyProvider(fail=True)
    client.app.state.otp_service.email_provider = prov
    resp = _post(client, "/v1/otp/send", {
        "user_id": str(uuid.uuid4()), "email": "u@x.co",
        "country_code": "FR", "purpose": "login_2fa"})
    assert resp.status_code == 502


# --------------------------------------------------- suppression / bounces

_ADMIN = {"X-Admin-Key": "a" * 40}


async def test_bounce_event_suppresses_recipient(client, redis):
    """Webhook bounce => adresse supprimée => envois suivants refusés."""
    r = client.post("/v1/events/email", headers=_ADMIN,
                    json={"event": "hard_bounce", "email": "dead@x.co",
                          "tenant_id": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["suppressed"] is True

    client.app.state.otp_service.email_provider = _SpyProvider()
    resp = _post(client, "/v1/otp/send", {
        "user_id": str(uuid.uuid4()), "email": "dead@x.co",
        "country_code": "FR", "purpose": "login_2fa"})
    assert resp.status_code == 422
    assert "suppressed" in resp.json()["detail"]


async def test_resend_format_normalized(client, redis):
    """Le format Resend {type, data:{to}} est reconnu."""
    r = client.post("/v1/events/email", headers=_ADMIN,
                    json={"type": "email.bounced",
                          "data": {"to": ["b1@x.co", "b2@x.co"]},
                          "tenant_id": "default"})
    assert r.status_code == 200 and r.json()["processed"] == 2


async def test_global_suppression_blocks_all_tenants(client, redis):
    """Sans tenant_id, un hard bounce supprime au niveau plateforme."""
    from src.core.tenants import (
        Tenant,
        TenantStore,
        generate_api_key,
        generate_hmac_secret,
        hash_api_key,
    )
    client.post("/v1/events/email", headers=_ADMIN,
                json={"event": "hard_bounce", "email": "gone@x.co"})
    key2 = generate_api_key()
    await TenantStore(redis).create(Tenant(
        tenant_id="t2", name="T2", api_key_hash=hash_api_key(key2),
        hmac_secret=generate_hmac_secret(), allowed_purposes=["login_2fa"]))
    from src.services.suppression import suppression_reason
    assert await suppression_reason(redis, "t2", "gone@x.co") is not None


async def test_soft_bounce_not_suppressed(client, redis):
    """Soft bounce : compté dans les stats mais adresse toujours joignable."""
    r = client.post("/v1/events/email", headers=_ADMIN,
                    json={"event": "soft_bounce", "email": "full@x.co",
                          "tenant_id": "default"})
    assert r.json()["suppressed"] is False
    from src.services.suppression import suppression_reason
    assert await suppression_reason(redis, "default", "full@x.co") is None


async def test_events_requires_admin_key(client):
    assert client.post("/v1/events/email",
                       json={"event": "hard_bounce", "email": "a@b.c"}
                       ).status_code in (401, 403)


# ------------------------------------------------------------ warm-up SMTP

async def test_warmup_caps_new_smtp_sender(client, redis):
    """SMTP custom neuf : au-delà du cap journalier warm-up -> 429."""
    import time as _t
    from datetime import date

    from src.core.tenants import TenantStore

    store = TenantStore(redis)
    try:
        await store.update("default", {
            "smtp_host": "smtp.new.tld", "smtp_configured_at": _t.time()})
        today = date.today().isoformat()
        # Déjà au cap du jour 0 (50) : 40 emails + 10 OTP
        await redis.set(f"t:default:stats_daily:emails_sent:{today}", 40)
        await redis.set(f"t:default:stats_daily:otps_sent:{today}", 10)

        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "u@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 429
        assert "warm-up" in resp.json()["detail"].lower()
        assert int(resp.headers["retry-after"]) > 0
    finally:
        await store.update("default", {
            "smtp_host": None, "smtp_configured_at": None})
        await redis.delete(
            f"t:default:stats_daily:emails_sent:{today}",
            f"t:default:stats_daily:otps_sent:{today}")


async def test_warmup_allows_under_cap(client, redis):
    """Sous le cap, l'envoi passe normalement."""
    import time as _t
    from datetime import date

    from src.core.tenants import TenantStore

    store = TenantStore(redis)
    try:
        await store.update("default", {
            "smtp_host": "smtp.new.tld", "smtp_configured_at": _t.time()})
        # fakeredis est partagé entre tests : on fixe le compteur du jour.
        today = date.today().isoformat()
        await redis.set(f"t:default:stats_daily:emails_sent:{today}", 5)
        await redis.set(f"t:default:stats_daily:otps_sent:{today}", 5)
        client.app.state.otp_service.email_provider = _SpyProvider()
        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "u@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 200
    finally:
        await store.update("default", {
            "smtp_host": None, "smtp_configured_at": None})
        await redis.delete(
            f"t:default:stats_daily:emails_sent:{today}",
            f"t:default:stats_daily:otps_sent:{today}")


async def test_warmup_cap_growth_and_expiry(client, redis):
    """Cap : jour0=50, jour1=75 ; fini après WARMUP_DAYS ; inactif sans SMTP."""
    import time as _t

    from src.core.tenants import TenantStore
    from src.services.warmup import warmup_cap

    settings = client.app.state.settings
    store = TenantStore(redis)
    base = {"smtp_host": "smtp.new.tld"}

    try:
        await store.update(
            "default", {**base, "smtp_configured_at": _t.time()})
        t = await store.get("default")
        assert warmup_cap(settings, t) == settings.warmup_start_volume

        await store.update("default", {
            "smtp_configured_at": _t.time() - 86400})
        t = await store.get("default")
        assert warmup_cap(settings, t) == int(
            settings.warmup_start_volume * settings.warmup_growth)

        await store.update("default", {
            "smtp_configured_at": _t.time()
            - 86400 * (settings.warmup_days + 1)})
        t = await store.get("default")
        assert warmup_cap(settings, t) is None

        await store.update("default", {"smtp_host": None})
        t = await store.get("default")
        assert warmup_cap(settings, t) is None

        settings.warmup_enabled = False
        await store.update("default", {
            "smtp_host": "smtp.new.tld",
            "smtp_configured_at": _t.time()})
        t = await store.get("default")
        assert warmup_cap(settings, t) is None
    finally:
        await store.update("default", {
            "smtp_host": None, "smtp_configured_at": None})


# --------------------------------------------------------------- /metrics

async def test_metrics_prometheus_format(client):
    """GET /metrics (X-Admin-Key) : texte Prometheus avec gauges + compteurs."""
    r = client.get("/metrics", headers=_ADMIN)
    assert r.status_code == 200
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    assert "mailhub_tenants_total" in body
    assert "mailhub_email_queue_depth" in body
    assert 'mailhub_otps_sent_total{tenant="default"}' in body
    assert 'mailhub_tenant_smtp_ok{tenant="default"}' in body


async def test_metrics_requires_admin_key(client):
    assert client.get("/metrics").status_code in (401, 403)


# ------------------------------------------------------------- scheduling

async def test_send_at_schedules_delivery(client, redis):
    """send_at futur : job différé, status=queued + scheduled_for, 0 livraison."""
    from src.services.email_queue import process_email_queue_once

    future = (_dt.datetime.now(_dt.timezone.utc)
              + _dt.timedelta(hours=2)).isoformat()
    spy = _SpyProvider()
    client.app.state.email_service.email_provider = spy
    resp = _post(client, "/v1/email/send", {
        "to_email": "later@x.co", "type": "notification",
        "send_at": future,
        "data": {"title": "T", "message": "m"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    assert resp.json()["scheduled_for"]

    mid = resp.json()["message_id"]
    st = _get(client, f"/v1/email/status/{mid}").json()
    assert st["status"] == "queued" and st["scheduled_for"]

    # Le worker ne le prend pas avant l'échéance.
    processed = await process_email_queue_once(
        redis, client.app.state.email_service)
    assert processed == 0 and len(spy.sent) == 0


async def test_send_at_rejects_over_30_days(client):
    future = (_dt.datetime.now(_dt.timezone.utc)
              + _dt.timedelta(days=40)).isoformat()
    resp = _post(client, "/v1/email/send", {
        "to_email": "x@x.co", "type": "notification",
        "send_at": future, "data": {"title": "T", "message": "m"}})
    assert resp.status_code == 422


async def test_send_at_past_sends_immediately(client):
    """send_at dans le passé = envoi normal immédiat."""
    past = (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(hours=1)).isoformat()
    resp = _post(client, "/v1/email/send", {
        "to_email": "x@x.co", "type": "notification",
        "send_at": past, "data": {"title": "T", "message": "m"}})
    assert resp.status_code == 200
    assert resp.json()["scheduled_for"] is None


# ---------------------------------------------------------------- sandbox

async def test_sandbox_captures_without_smtp(client, redis):
    """tenant.sandbox => aucun appel provider, envois capturés en outbox."""
    from src.core.tenants import TenantStore
    from src.services.events import get_events
    from src.services.sandbox import list_outbox

    store = TenantStore(redis)
    try:
        await store.update("default", {"sandbox": True})
        prov = _SpyProvider()
        client.app.state.otp_service.email_provider = prov
        client.app.state.email_service.email_provider = prov

        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "u@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 200, resp.text
        assert len(prov.sent) == 0  # aucun mail réel

        box = await list_outbox(redis, "default")
        assert len(box) == 1
        assert box[0]["type"] == "otp" and box[0]["to"] == "u@x.co"
        assert re.search(r"\b\d{6}\b", box[0]["text_body"])

        tenant = await store.get("default")
        events = await get_events(redis, tenant)
        sent = next(e for e in events if e["type"] == "otp_sent")
        assert sent["via"] == "sandbox"
    finally:
        await store.update("default", {"sandbox": False})
        await redis.delete("t:default:sandbox_outbox")


async def test_sandbox_transactional_captured(client, redis):
    """Le chemin transactionnel capture aussi (deliver)."""
    from src.core.models import EmailSendRequest
    from src.core.tenants import TenantStore
    from src.services.sandbox import list_outbox

    store = TenantStore(redis)
    try:
        await store.update("default", {"sandbox": True})
        prov = _SpyProvider()
        client.app.state.email_service.email_provider = prov
        tenant = await store.get("default")
        req = EmailSendRequest(to_email="u@x.co", type="notification",
                               data={"title": "Hi", "message": "m"})
        resp = await client.app.state.email_service.send(tenant, req)
        assert resp.sent is True and len(prov.sent) == 0
        box = await list_outbox(redis, "default")
        assert any(m["type"] == "notification" for m in box)
    finally:
        await store.update("default", {"sandbox": False})
        await redis.delete("t:default:sandbox_outbox")
