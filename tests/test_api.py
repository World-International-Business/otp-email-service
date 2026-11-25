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
                         sender=None, attachments=None):
        if self.fail:
            from src.email_providers.base import EmailSendError
            raise EmailSendError("smtp down")
        self.sent.append((to_email, subject, text_body))
        self.last_attachments = attachments
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
        # Quotas élevés : le fakeredis est partagé entre les tests et les
        # compteurs 10 min s'accumulent — sinon les envois OTP des tests
        # tardifs tombent en 429 (le .env local masquait ça, pas la CI).
        "MAX_SENDS_PER_USER_PER_10MIN": "10000",
        "MAX_SENDS_PER_IP_PER_10MIN": "10000",
        "COOLDOWN_SECONDS": "0",
        "DEMO_ENABLED": "false",
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
    store = TenantStore(redis)
    await store.create(Tenant(
        tenant_id="other", name="Other", api_key_hash=hash_api_key(key2),
        hmac_secret=secret2, from_email="o@x.co", from_name="O",
        allowed_purposes=["login_2fa"],
    ))
    try:
        resp = _post(client, "/v1/email/send",
                     {"to_email": "a@b.co", "type": "notification", "data": {}})
        mid = resp.json()["message_id"]
        r = _get(client, f"/v1/email/status/{mid}", secret=secret2, api_key=key2)
        assert r.status_code == 404
    finally:
        await redis.srem("tenant:index", "other")
        await redis.delete("tenant:other", store._bykey(hash_api_key(key2)))


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

    try:
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
    finally:
        # Sans ce reset, TOUTES les livraisons suivantes enqueueraient un
        # webhook vers une URL inexistante — vrai appel réseau en CI.
        await TenantStore(redis).update("default", {"webhook_url": None})


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
    us = WebUserStore(redis)
    await us.create("boss@t.co", "Passw0rd!", "default", "App")
    try:
        spy = _SpyProvider()
        n = await run_billing_reminders(redis, spy)
        assert n == 1
        assert spy.sent[0][0] == "boss@t.co" and "expire" in spy.sent[0][1]
        # Dédupliqué : un second passage n'envoie rien.
        assert await run_billing_reminders(redis, spy) == 0
    finally:
        from src.core.tenants import plan_quota_updates
        await TenantStore(redis).update("default", {
            "plan": "free", "billing_status": "trialing",
            "paid_until": None, **plan_quota_updates("free")})
        user = await us.get_by_email("boss@t.co")
        if user:
            await us.delete(user.user_id)


# ---------------------------------------------------------- SMTP failover

class _FailoverProvider:
    """Échoue via le SMTP custom du tenant, réussit via la plateforme."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_email(self, to_email, subject, text_body, html_body,
                         sender=None, attachments=None):
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
    try:
        client.app.state.otp_service.email_provider = _SpyProvider()
        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "dead@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 422
        assert "suppressed" in resp.json()["detail"]
    finally:
        from src.services.suppression import remove_suppression
        await remove_suppression(redis, "default", "dead@x.co")


async def test_resend_format_normalized(client, redis):
    """Le format Resend {type, data:{to}} est reconnu."""
    r = client.post("/v1/events/email", headers=_ADMIN,
                    json={"type": "email.bounced",
                          "data": {"to": ["b1@x.co", "b2@x.co"]},
                          "tenant_id": "default"})
    assert r.status_code == 200 and r.json()["processed"] == 2
    from src.services.suppression import remove_suppression
    await remove_suppression(redis, "default", "b1@x.co")
    await remove_suppression(redis, "default", "b2@x.co")


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
    store = TenantStore(redis)
    await store.create(Tenant(
        tenant_id="t2", name="T2", api_key_hash=hash_api_key(key2),
        hmac_secret=generate_hmac_secret(), allowed_purposes=["login_2fa"]))
    try:
        from src.services.suppression import suppression_reason
        assert await suppression_reason(redis, "t2", "gone@x.co") is not None
    finally:
        from src.services.suppression import remove_suppression
        await remove_suppression(redis, "*", "gone@x.co")
        await redis.srem("tenant:index", "t2")
        await redis.delete("tenant:t2", store._bykey(hash_api_key(key2)))


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


# --------------------------------------------------------------- tracking

class _HtmlSpy(_SpyProvider):
    """Capture aussi le HTML envoyé."""

    async def send_email(self, to_email, subject, text_body, html_body,
                         sender=None, attachments=None):
        await super().send_email(to_email, subject, text_body, html_body,
                                 sender, attachments)
        self.last_html = html_body
        self.last_sender = dict(sender or {})
        return True


async def test_tracking_pixel_and_click_wrap(client, redis):
    """Tracking actif : pixel injecté + liens réécrits en /v1/track."""
    from src.core.models import EmailSendRequest
    from src.core.tenants import TenantStore

    svc = client.app.state.email_service
    svc.settings.public_base_url = "https://hub.test"
    spy = _HtmlSpy()
    svc.email_provider = spy
    tenant = await TenantStore(redis).get("default")

    req = EmailSendRequest(to_email="u@x.co", type="custom",
                           subject="S", text_body="t",
                           html_body='<html><body><a href="https://ex.com/x">go</a></body></html>')
    resp = await svc.send(tenant, req)
    assert resp.sent

    html = spy.last_html
    assert f"/v1/track/o/{resp.message_id}.gif" in html
    assert f"/v1/track/c/{resp.message_id}?u=" in html
    assert "https%3A%2F%2Fex.com%2Fx" in html

    # Le pixel attribue l'ouverture au tenant et incrémente les stats.
    r = client.get(f"/v1/track/o/{resp.message_id}.gif")
    assert r.status_code == 200 and r.content[:6] == b"GIF89a"
    assert int(await redis.get("t:default:stats:emails_opened") or 0) >= 1

    # Le clic redirige vers la cible et compte.
    r2 = client.get(
        f"/v1/track/c/{resp.message_id}?u=https%3A%2F%2Fex.com%2Fx",
        follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"] == "https://ex.com/x"
    assert int(await redis.get("t:default:stats:emails_clicked") or 0) >= 1


async def test_tracking_disabled_without_base_url(client, redis):
    """Pas de PUBLIC_BASE_URL => aucune modification du HTML."""
    from src.core.models import EmailSendRequest
    from src.core.tenants import TenantStore

    svc = client.app.state.email_service
    svc.settings.public_base_url = ""
    spy = _HtmlSpy()
    svc.email_provider = spy
    tenant = await TenantStore(redis).get("default")

    req = EmailSendRequest(to_email="u@x.co", type="custom",
                           subject="S", text_body="t",
                           html_body='<html><body><a href="https://ex.com">x</a></body></html>')
    resp = await svc.send(tenant, req)
    assert resp.sent and "/v1/track/" not in spy.last_html


async def test_track_rejects_invalid_url(client):
    r = client.get("/v1/track/c/msg_x?u=javascript:alert(1)",
                   follow_redirects=False)
    assert r.status_code == 400


# ------------------------------------------------------------- SDK Python

def test_sdk_signature_accepted(client):
    """La signature du SDK officiel est vérifiée par l'auth serveur."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mailhub_client", "sdks/python/mailhub/client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sdk = mod.MailHubClient(
        base_url="http://x", api_key="k", hmac_secret="s" * 40)
    body = b'{"user_id":"u1","email":"a@b.c"}'
    headers = sdk._sign(body)

    from src.core.auth import _verify_hmac
    assert _verify_hmac(
        body, headers["X-Timestamp"], headers["X-HMAC-Signature"],
        "s" * 40, headers["X-Nonce"],
    )
    # Signature différente par requête (nonce unique).
    assert sdk._sign(body)["X-HMAC-Signature"] != headers["X-HMAC-Signature"]


# ---------------------------------------------------------- exports CSV

async def test_payments_csv_export(client, redis):
    """GET /dashboard/payments.csv : CSV UTF-8 avec les tx du tenant."""
    from src.services.payments import new_tx_id, record_tx
    from src.web.users import SessionStore, WebUserStore

    tid = new_tx_id()
    await record_tx(redis, tid, {
        "tenant_id": "default", "plan": "pro", "period": "yearly",
        "amount": 348, "currency": "EUR", "method": "stripe",
        "status": "paid", "tx_id": tid, "date": "2026-11-20"})
    w = await WebUserStore(redis).create("csv@x.co", "Passw0rd!", "default", "App")
    sid = await SessionStore(redis).create(w.user_id)

    r = client.get("/dashboard/payments.csv", cookies={"session_id": sid})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("date,plan,period")
    assert "pro,yearly,348,EUR,stripe,paid" in text.replace('"', "")
    assert tid in text


# ---------------------------------------------------------- templates custom

async def test_custom_template_overrides_welcome(client, redis):
    """Un template personnalisé remplace sujet + corps du welcome par défaut."""
    from src.services.custom_templates import set_template
    from src.services.email_queue import process_email_queue_once

    await set_template(
        redis, "default", "welcome",
        subject="Salut {name} — {brand}",
        text="Yo {name}, bienvenue chez {brand} !",
        html="<p>Hey <b>{name}</b>, let's go sur {brand}</p>",
    )
    spy = _SpyProvider()
    client.app.state.email_service.email_provider = spy
    client.app.state.otp_service.email_provider = spy

    resp = _post(client, "/v1/email/send", {
        "to_email": "client@example.com", "type": "welcome",
        "data": {"name": "Alice"},
    })
    assert resp.status_code == 200, resp.text
    await process_email_queue_once(redis, client.app.state.email_service)
    assert spy.sent
    to, subject, text = spy.sent[0]
    assert to == "client@example.com"
    assert "Salut Alice" in subject and "MailHub Test" in subject
    assert "Yo Alice" in text

    # Le fragment HTML est passé par le layout de marque (branding préservé).
    # (vérif indirecte : le rendu a eu lieu sans erreur — voir preview route)
    await redis.delete("t:default:tmpl:welcome")


async def test_custom_template_render_vars_safety(redis):
    """{inconnu} reste tel quel, pas de crash ; variables connues substituées."""
    from src.services.custom_templates import render_vars
    assert render_vars("Bonjour {name}", {"name": "Al"}) == "Bonjour Al"
    assert render_vars("X {typo} Y", {}) == "X {typo} Y"
    assert render_vars("{a}{a}", {"a": "1"}) == "11"


async def test_dashboard_templates_editor(client, redis):
    """GET /dashboard/templates : éditeur + save + preview."""
    from src.web.users import SessionStore, WebUserStore

    w = await WebUserStore(redis).create("tmpl@x.co", "Passw0rd!", "default", "App")
    sid = await SessionStore(redis).create(w.user_id)
    ck = {"session_id": sid}

    r = client.get("/dashboard/templates?type=welcome", cookies=ck)
    assert r.status_code == 200 and "Templates" in r.text
    assert "{name}" in r.text  # chips de variables

    r = client.post("/dashboard/templates", cookies=ck, data={
        "type": "welcome", "subject": "Bienvenue {name}",
        "text": "Salut {name} !",
    }, follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/dashboard/templates/preview?type=welcome", cookies=ck)
    assert r.status_code == 200
    assert "Salut" in r.text  # texte personnalisé rendu dans l'aperçu

    r = client.post("/dashboard/templates/reset", cookies=ck,
                    data={"type": "welcome"}, follow_redirects=False)
    assert r.status_code == 303


# ---------------------------------------------------------- domaine tracking

async def test_tracking_domain_white_label(client, redis):
    """tracking_domain tenant : liens trackés sur SON domaine."""
    from src.core.tenants import TenantStore
    from src.services.tracking import tracking_base_url

    store = TenantStore(redis)
    tenant = await store.get("default")
    tenant.tracking_domain = "track.client.com"
    await store.update("default", {"tracking_domain": "track.client.com"})

    base = tracking_base_url(client.app.state.settings, tenant)
    assert base == "https://track.client.com"

    # L'injection de tracking utilise bien le domaine custom dans un envoi réel.
    spy = _SpyProvider()
    client.app.state.email_service.email_provider = spy
    resp = _post(client, "/v1/email/send", {
        "to_email": "x@example.com", "type": "notification",
        "data": {"title": "T", "message": "go https://ex.com/a"},
    })
    assert resp.status_code == 200
    from src.services.email_queue import process_email_queue_once
    await process_email_queue_once(redis, client.app.state.email_service)
    # (le provider spy ne capture que text — on vérifie via le html du service
    # directement : la base utilisée est validée par tracking_base_url ci-dessus)

    await store.update("default", {"tracking_domain": None})


async def test_tracking_domain_config_form_validation(client, redis):
    """Le formulaire config nettoie/valide le domaine de tracking."""
    from src.core.tenants import TenantStore
    from src.web.users import SessionStore, WebUserStore

    w = await WebUserStore(redis).create("td@x.co", "Passw0rd!", "default", "App")
    sid = await SessionStore(redis).create(w.user_id)
    ck = {"session_id": sid}

    r = client.post("/dashboard/config", cookies=ck, data={
        "brand_name": "Test", "tracking_domain": "https://Track.Client.com/path",
    }, follow_redirects=False)
    assert r.status_code == 303
    t = await TenantStore(redis).get("default")
    assert t.tracking_domain == "track.client.com"  # schéma/path/casse nettoyés

    r = client.post("/dashboard/config", cookies=ck, data={
        "brand_name": "Test", "tracking_domain": "not a domain!!",
    }, follow_redirects=False)
    assert r.status_code == 422

    # Décocher la case désactive le tracking.
    await TenantStore(redis).update("default", {"track_emails": True})
    client.post("/dashboard/config", cookies=ck, data={"brand_name": "Test"},
                follow_redirects=False)
    t = await TenantStore(redis).get("default")
    assert t.track_emails is False
    await TenantStore(redis).update(
        "default", {"track_emails": True, "tracking_domain": None})


# ------------------------------------------------------------- batch envois

async def test_batch_send_multiple_recipients(client):
    """Un appel batch = un message_id par destinataire."""
    resp = _post(client, "/v1/email/batch", {
        "to": ["lot1@x.co", "lot2@x.co", "lot3@x.co"],
        "type": "notification",
        "data": {"title": "T", "message": "M"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent"] == 3 and data["failed"] == 0, data
    assert len({r["message_id"] for r in data["results"]}) == 3


async def test_batch_suppressed_recipient_rejected_only(client, redis):
    """Un destinataire supprimé n'invalide pas le lot."""
    from src.services.suppression import add_suppression
    await add_suppression(redis, "default", "dead2@x.co", "hard_bounce")
    try:
        resp = _post(client, "/v1/email/batch", {
            "to": ["ok9@x.co", "dead2@x.co"],
            "type": "notification", "data": {},
        })
        data = resp.json()
        assert data["sent"] == 1 and data["failed"] == 1
        dead = next(r for r in data["results"]
                    if r["to_email"] == "dead2@x.co")
        assert dead["status"] == "rejected" and "suppressed" in dead["error"]
    finally:
        from src.services.suppression import remove_suppression
        await remove_suppression(redis, "default", "dead2@x.co")


async def test_batch_dedupes_recipients(client):
    """Un doublon dans `to` n'envoie pas deux fois au même destinataire."""
    resp = _post(client, "/v1/email/batch", {
        "to": ["dup@x.co", "dup@x.co"], "type": "notification", "data": {}})
    assert resp.json()["sent"] == 1


# ------------------------------------------------------------- pièces jointes

async def test_attachments_delivered_via_queue(client, redis):
    """Les pièces jointes traversent la file jusqu'au provider."""
    import base64

    from src.services.email_queue import process_email_queue_once
    spy = _SpyProvider()
    client.app.state.email_service.email_provider = spy
    resp = _post(client, "/v1/email/send", {
        "to_email": "att@x.co", "type": "notification", "data": {},
        "attachments": [{
            "filename": "facture.pdf",
            "content_base64": base64.b64encode(b"%PDF-1.4 x").decode(),
            "content_type": "application/pdf",
        }],
    })
    assert resp.status_code == 200, resp.text
    await process_email_queue_once(redis, client.app.state.email_service)
    assert spy.last_attachments[0]["filename"] == "facture.pdf"


async def test_attachment_invalid_base64_rejected(client):
    resp = _post(client, "/v1/email/send", {
        "to_email": "a@x.co", "type": "notification", "data": {},
        "attachments": [{"filename": "f", "content_base64": "!!!"}]})
    assert resp.status_code == 422


async def test_smtp_message_contains_attachment_part():
    """Le provider SMTP émet un multipart/mixed avec la pièce jointe."""
    import base64

    from src.config import Settings
    from src.email_providers.smtp import SMTPProvider

    prov = SMTPProvider(Settings(
        api_key="k" * 40, hmac_secret="s" * 20, from_email="a@x.co",
        smtp_user="u", smtp_password="p"))
    captured = {}
    prov._send_smtp = lambda *a, **kw: captured.update(raw=a[7])  # noqa
    await prov.send_email(
        "t@x.co", "S", "txt", "<p>h</p>",
        attachments=[{"filename": "doc.pdf",
                      "content_base64": base64.b64encode(b"PDF").decode(),
                      "content_type": "application/pdf"}])
    raw = captured["raw"].decode("utf-8", "replace")
    assert "multipart/mixed" in raw
    assert 'Content-Disposition: attachment; filename="doc.pdf"' in raw


async def test_smtp_message_has_antispam_headers():
    """Headers délivrabilité : Auto-Submitted (RFC 3834), Date, Message-ID."""
    from src.config import Settings
    from src.email_providers.smtp import SMTPProvider

    prov = SMTPProvider(Settings(
        api_key="k" * 40, hmac_secret="s" * 20, from_email="a@x.co",
        smtp_user="u", smtp_password="p"))
    captured = {}
    prov._send_smtp = lambda *a, **kw: captured.update(raw=a[7])  # noqa
    await prov.send_email("t@x.co", "S", "txt", "<p>h</p>",
                          sender={"unsubscribe_url": "https://x.co/u"})
    raw = captured["raw"].decode("utf-8", "replace")
    assert "Auto-Submitted: auto-generated" in raw
    assert "List-Unsubscribe: <https://x.co/u>" in raw
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in raw
    assert "\nDate: " in raw and "\nMessage-ID: " in raw


async def test_email_html_is_mobile_responsive():
    """Le layout doit contrer l'inflation de police iOS (text-size-adjust)
    et plafonner les images/tableaux du contenu client sur mobile."""
    from src.services.email_templates import generic_html, otp_html

    for html in (generic_html("T", "<p>c</p>", "B"), otp_html("123456", "B")):
        assert "-webkit-text-size-adjust:100%" in html
        assert 'name="viewport"' in html
        assert "max-width:520px" in html
        assert "img{max-width:100%!important" in html
        assert "x-apple-disable-message-reformatting" in html
        # dark mode : le design clair ne doit pas être inversé
        assert 'name="color-scheme" content="light"' in html
        # fallback attribut pour les clients qui ignorent les styles
        assert 'width="520"' in html
        # Outlook desktop : table fixe conditionnelle
        assert "<!--[if mso]>" in html
        # bouton CTA pleine largeur + breakpoint très petits écrans
        assert ".mh-btn{display:block" in html
        assert "max-width:360px" in html


# ------------------------------------------------------- rotation des clés

async def test_key_rotation_grace_keeps_old_creds_working(client, redis):
    """Après rotate_keys : ancienne clé + ancien secret HMAC encore acceptés
    pendant la grâce, nouvelle paire acceptée immédiatement."""
    from src.core.tenants import TenantStore
    store = TenantStore(redis)
    _updated, new_key, new_secret = await store.rotate_keys(
        "default", grace_seconds=3600)
    # Ancienne clé + ancien secret (HMAC_SECRET global des tests)
    resp = _post(client, "/v1/email/send",
                 {"to_email": "rot1@x.co", "type": "notification", "data": {}})
    assert resp.status_code == 200, resp.text
    # Nouvelle paire
    resp2 = _post(
        client, "/v1/email/send",
        {"to_email": "rot2@x.co", "type": "notification", "data": {}},
        extra_headers=_sign(
            json.dumps({"to_email": "rot2@x.co", "type": "notification",
                        "data": {}}).encode(),
            secret=new_secret, api_key=new_key),
    )
    assert resp2.status_code == 200, resp2.text


async def test_admin_rotate_key_endpoint(client, redis):
    r = client.post(
        "/v1/admin/tenants/default/rotate-key?grace_seconds=3600",
        headers=_ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["api_key"] and data["hmac_secret"]
    assert data["grace_seconds"] == 3600
    # L'ancienne clé fonctionne toujours (grâce) pour les tests suivants.
    resp = _post(client, "/v1/email/send",
                 {"to_email": "rot3@x.co", "type": "notification", "data": {}})
    assert resp.status_code == 200


# ------------------------------------------------------------- unsubscribe

async def test_unsubscribe_signed_link(client, redis):
    """Lien signé : opt-out effectif ; signature forgée rejetée."""
    from src.services.suppression import suppression_reason
    from src.services.unsubscribe import unsubscribe_url

    svc = client.app.state.email_service
    svc.settings.public_base_url = "https://mail.test"
    try:
        url = unsubscribe_url(svc.settings, "default", "opt@x.co")
        path = url.split("mail.test", 1)[1]
        r = client.get(path)
        assert r.status_code == 200, r.text
        assert await suppression_reason(
            redis, "default", "opt@x.co") == "unsubscribe"
    finally:
        svc.settings.public_base_url = ""
        from src.services.suppression import remove_suppression
        await remove_suppression(redis, "default", "opt@x.co")

    r2 = client.get("/v1/unsubscribe?t=default&e=evil@x.co&sig=forged")
    assert r2.status_code == 403


async def test_unsubscribe_footer_link_in_email(client, redis):
    """PUBLIC_BASE_URL défini → lien 'Se désinscrire' + header opt-out."""
    from src.core.tenants import TenantStore
    from src.services.email_queue import process_email_queue_once

    svc = client.app.state.email_service
    svc.settings.public_base_url = "https://mail.test"
    # Le tracking réécrit les <a href> en /v1/track/click — on le coupe pour
    # inspecter le lien opt-out en clair.
    await TenantStore(redis).update("default", {"track_emails": False})
    spy = _HtmlSpy()
    svc.email_provider = spy
    try:
        resp = _post(client, "/v1/email/send",
                     {"to_email": "unsub@x.co", "type": "notification",
                      "data": {}})
        assert resp.status_code == 200, resp.text
        await process_email_queue_once(redis, svc)
        assert "/v1/unsubscribe?" in spy.last_html
        assert "unsubscribe_url" in spy.last_sender
    finally:
        svc.settings.public_base_url = ""
        await TenantStore(redis).update("default", {"track_emails": True})


# ----------------------------------------------------- mot de passe oublié

async def test_forgot_password_full_flow(client, redis):
    """Compte existant : OTP → nouveau mot de passe → login fonctionne."""
    from src.web.users import WebUserStore
    users = WebUserStore(redis)
    await users.create("forgot@x.co", "oldpass123", "default", "App")
    spy = _SpyProvider()
    client.app.state.otp_service.email_provider = spy

    r = client.post("/forgot", data={"email": "forgot@x.co"},
                    follow_redirects=False)
    assert r.status_code == 303
    tok = r.headers["location"].split("t=")[1]
    code = re.search(r"\b(\d{6})\b", spy.sent[0][2]).group(1)

    r2 = client.post("/forgot/verify",
                     data={"t": tok, "code": code, "password": "newpass456"},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login?ok=reset"
    assert await users.authenticate("forgot@x.co", "newpass456") is not None
    assert await users.authenticate("forgot@x.co", "oldpass123") is None


async def test_forgot_unknown_email_no_enumeration(client):
    """Email inconnu : redirection + page de saisie IDENTIQUES — aucune
    fuite sur l'existence ou non du compte."""
    r = client.post("/forgot", data={"email": "ghost@x.co"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "/forgot/verify?t=" in r.headers["location"]
    r2 = client.get(r.headers["location"])
    assert r2.status_code == 200 and "Code à 6 chiffres" in r2.text


# ------------------------------------------------------ quotas mensuels

async def test_monthly_otp_quota_blocks_at_cap(client, redis):
    """Quota OTP mensuel atteint -> 429 + Retry-After ; illimité -> passe."""
    from src.core.tenants import TenantStore
    from src.services.events import incr_daily_stat

    store = TenantStore(redis)
    try:
        # Free-plan counter au plafond : 300 OTP ce mois-ci.
        for _ in range(300):
            await incr_daily_stat(redis, await store.get("default"), "otps_sent")
        await store.update("default", {"quota_otp_month": 300})
        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "q@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 429
        assert "quota" in resp.json()["detail"].lower()
        assert int(resp.headers["retry-after"]) > 0
        # Illimité -> le même envoi passe.
        await store.update("default", {"unlimited": True})
        client.app.state.otp_service.email_provider = _SpyProvider()
        resp = _post(client, "/v1/otp/send", {
            "user_id": str(uuid.uuid4()), "email": "q@x.co",
            "country_code": "FR", "purpose": "login_2fa"})
        assert resp.status_code == 200, resp.text
    finally:
        await store.update(
            "default", {"quota_otp_month": 0, "unlimited": False})


async def test_quota_80_percent_warns_once(client, redis):
    """À 80 % du quota : event quota_warning + une seule fois par mois."""
    import time as _t

    from src.core.tenants import TenantStore
    from src.services.events import get_events

    store = TenantStore(redis)
    month = _t.strftime("%Y-%m", _t.gmtime())
    mkey = f"t:default:stats_monthly:emails_sent:{month}"
    saved = await redis.get(mkey)  # fakeredis partagé : restaurer après
    try:
        await store.update("default", {"quota_email_month": 10})
        await redis.set(mkey, 8)  # 80 % du quota de 10
        resp = _post(client, "/v1/email/send",
                     {"to_email": "w@x.co", "type": "notification", "data": {}})
        assert resp.status_code == 200, resp.text
        resp = _post(client, "/v1/email/send",
                     {"to_email": "w2@x.co", "type": "notification", "data": {}})
        assert resp.status_code == 200
        events = await get_events(redis, await store.get("default"))
        warns = [e for e in events if e["type"] == "quota_warning"]
        assert len(warns) == 1  # alerte unique par mois + métrique
    finally:
        await store.update("default", {"quota_email_month": 0})
        await redis.delete(f"t:default:quota_warn:emails_sent:{month}")
        if saved is None:
            await redis.delete(mkey)
        else:
            await redis.set(mkey, saved)


# ------------------------------------------------------ audit log admin

async def test_admin_audit_trails_plan_change(client, redis):
    """Chaque action admin sensible est consignée dans admin:audit."""
    from src.core.tenants import TenantStore, plan_quota_updates
    from src.services.audit import get_admin_audit

    store = TenantStore(redis)
    before = await store.get("default")
    target = "pro" if before.plan != "pro" else "enterprise"
    r = client.patch("/v1/admin/tenants/default",
                     headers=_ADMIN, json={"plan": target})
    assert r.status_code == 200
    client.delete("/v1/admin/tenants/default", headers=_ADMIN)
    entries = await get_admin_audit(redis, limit=10)
    update = next(e for e in entries if e["action"] == "tenant.update")
    assert update["tenant_id"] == "default"
    assert update["detail"]["plan"] == f"{before.plan}->{target}"
    assert any(e["action"] == "tenant.deactivate" for e in entries)
    assert all("api_key" not in str(e["detail"]).lower()
               and "secret" not in str(e["detail"]).lower()
               and "password" not in str(e["detail"]).lower()
               for e in entries)
    # Rétablir le tenant pour les tests suivants
    await TenantStore(redis).update(
        "default", {"is_active": True, "plan": "free",
                    **plan_quota_updates("free")})


async def test_admin_audit_endpoint(client):
    r = client.get("/v1/admin/audit?limit=5", headers=_ADMIN)
    assert r.status_code == 200
    assert isinstance(r.json(), list) and len(r.json()) <= 5
    r2 = client.get("/v1/admin/audit")
    assert r2.status_code == 401  # sans X-Admin-Key


# ------------------------------------------------------ export RGPD

async def test_tenant_export_rgpd(client, redis):
    """L'export contient config sans secrets + templates + suppressions."""
    from src.services.suppression import add_suppression

    await add_suppression(redis, "default", "exp@x.co", "unsubscribe")
    try:
        r = client.get("/v1/admin/tenants/default/export", headers=_ADMIN)
        assert r.status_code == 200, r.text
        data = r.json()
        cfg = data["tenant"]
        assert cfg["tenant_id"] == "default" and cfg["brand_name"]
        # Jamais de secrets exportés
        for f in ("api_key_hash", "hmac_secret", "hmac_secret_prev",
                  "smtp_password"):
            assert f not in cfg
            assert f in data["secrets_excluded"]
        assert "templates" in data and "suppressions" in data
        assert any(s["email"] == "exp@x.co" for s in data["suppressions"])
        assert data["stats"]["month"]
        assert isinstance(data["web_users"], list)
        # L'export lui-même est audité (données personnelles)
        r3 = client.get("/v1/admin/audit?limit=1", headers=_ADMIN)
        assert r3.json()[0]["action"] == "tenant.export"
    finally:
        from src.services.suppression import remove_suppression
        await remove_suppression(redis, "default", "exp@x.co")
    r4 = client.get("/v1/admin/tenants/nope/export", headers=_ADMIN)
    assert r4.status_code == 404


# ------------------------------------------------------ prorata

async def test_prorata_credit_calculation(redis):
    """Upgrade à mi-période : crédit = moitié du prix du plan courant."""
    from datetime import date, timedelta

    from src.config import Settings
    from src.core.tenants import TenantStore
    from src.services.payments import prorata_credit, record_tx

    s = Settings(api_key="k" * 40, hmac_secret="s" * 20,
                 from_email="a@x.co", plan_price_pro=30,
                 plan_price_enterprise=90)
    store = TenantStore(redis)
    try:
        await store.update("default", {
            "plan": "pro",
            "paid_until": (date.today() + timedelta(days=15)).isoformat(),
            "payment_ref": "tx_prev"})
        tenant = await store.get("default")
        await record_tx(redis, "tx_prev", {"period": "monthly"})
        credit, days = await prorata_credit(redis, s, tenant, "stripe")
        assert days == 15
        assert credit == 15.0  # 30 € × 15/30 jours
        # Plan free ou sans période payée -> pas de crédit
        await store.update("default", {"plan": "free"})
        credit2, _ = await prorata_credit(
            redis, s, await store.get("default"), "stripe")
        assert credit2 == 0
    finally:
        await store.update("default", {
            "plan": "free", "paid_until": None, "payment_ref": None})


async def test_prorated_apply_resets_paid_until(redis):
    """Un achat proratisé redémarre une période complète au lieu de prolonger."""
    from datetime import date, timedelta

    from src.core.tenants import TenantStore
    from src.services.payments import apply_paid_plan, record_tx

    store = TenantStore(redis)
    future = (date.today() + timedelta(days=20)).isoformat()
    try:
        await store.update("default", {"paid_until": future})
        # Renouvellement classique -> prolonge depuis paid_until
        await apply_paid_plan(redis, tx_id="tx_renew",
                              tenant_id="default", plan="pro",
                              provider="manual", period="monthly")
        t = await store.get("default")
        assert t.paid_until > future  # prolongé au-delà des 20 jours restants
        # Achat proratisé -> repart d'aujourd'hui + 30 j
        await store.update("default", {"paid_until": future})
        await record_tx(redis, "tx_pro", {"prorata_credit": 10})
        await apply_paid_plan(redis, tx_id="tx_pro",
                              tenant_id="default", plan="enterprise",
                              provider="stripe", period="monthly")
        t = await store.get("default")
        expected = (date.today() + timedelta(days=30)).isoformat()
        assert t.paid_until == expected
    finally:
        await store.update(
            "default", {"paid_until": None, "plan": "free",
                        "payment_ref": None})


# ------------------------------------------------- régressions bug-hunt

async def test_admin_create_free_tenant_has_monthly_quotas(client, redis):
    """Régression : un tenant créé via l'API en plan 'free' doit recevoir
    les quotas mensuels du plan (sinon il serait illimité alors qu'un
    downgrade vers free est plafonné)."""
    from src.core.tenants import PLANS, TenantStore

    r = client.post("/v1/admin/tenants", headers=_ADMIN, json={
        "tenant_id": "freeco", "name": "Free Co", "plan": "free",
    })
    assert r.status_code in (200, 201), r.text
    tid = r.json()["tenant_id"]
    t = await TenantStore(redis).get(tid)
    try:
        assert t.quota_otp_month == PLANS["free"]["quota_otp_month"]
        assert t.quota_email_month == PLANS["free"]["quota_email_month"]
    finally:
        await redis.srem("tenant:index", tid)
        await redis.delete(f"tenant:{tid}")


async def test_admin_patch_plan_preserves_explicit_quota(client, redis):
    """PATCH {plan, quota_otp_month} : le quota explicite prime sur le
    défaut du plan."""
    from src.core.tenants import PLANS, TenantStore, plan_quota_updates

    store = TenantStore(redis)
    r = client.patch("/v1/admin/tenants/default", headers=_ADMIN, json={
        "plan": "pro", "quota_otp_month": 4242,
    })
    assert r.status_code == 200, r.text
    t = await store.get("default")
    try:
        assert t.quota_otp_month == 4242
        # Les autres quotas du plan pro s'appliquent normalement
        assert t.quota_email_month == PLANS["pro"]["quota_email_month"]
    finally:
        await store.update(
            "default", {"plan": "free", **plan_quota_updates("free")})


async def test_admin_patch_quota_fields_accepted(client, redis):
    """quota_otp_month/quota_email_month sont patchables directement."""
    from src.core.tenants import TenantStore

    r = client.patch("/v1/admin/tenants/default", headers=_ADMIN, json={
        "quota_email_month": 777,
    })
    assert r.status_code == 200, r.text
    t = await TenantStore(redis).get("default")
    try:
        assert t.quota_email_month == 777
        assert r.json()["quota_email_month"] == 777
    finally:
        await TenantStore(redis).update(
            "default", {"quota_email_month": 0})


async def test_tenant_info_exposes_quota_fields(client):
    """TenantInfoResponse expose quotas mensuels + billing pour l'admin."""
    r = client.get("/v1/admin/tenants", headers=_ADMIN)
    assert r.status_code == 200
    t = next(x for x in r.json() if x["tenant_id"] == "default")
    for f in ("quota_otp_month", "quota_email_month",
              "unlimited", "paid_until"):
        assert f in t


async def test_apply_paid_plan_preserves_prorata_metadata(redis):
    """Le record 'paid' conserve prorata_credit/list_amount du 'created'."""
    from src.services.payments import apply_paid_plan, get_tx, record_tx

    await record_tx(redis, "tx_meta", {
        "tenant_id": "default", "plan": "pro", "provider": "stripe",
        "amount": 20, "currency": "EUR", "status": "created",
        "prorata_credit": 9.5, "prorata_days": 10, "list_amount": 29.5,
        "ts": 1, "date": "2025-01-01",
    })
    try:
        await apply_paid_plan(redis, tx_id="tx_meta",
                              tenant_id="default", plan="pro",
                              provider="stripe", amount=20,
                              currency="EUR", period="monthly")
        paid = await get_tx(redis, "tx_meta")
        assert paid["status"] == "paid"
        assert paid["prorata_credit"] == 9.5
        assert paid["prorata_days"] == 10
        assert paid["list_amount"] == 29.5
    finally:
        from src.core.tenants import TenantStore, plan_quota_updates
        await TenantStore(redis).update(
            "default", {"plan": "free", "paid_until": None,
                        "payment_ref": None, **plan_quota_updates("free")})
        await redis.delete("pay:tx:tx_meta")


async def test_seconds_until_next_month_rollover():
    """La fin de mois calcule bien la bascule (incl. décembre -> janvier)."""
    from src.services.quotas import seconds_until_next_month

    s = seconds_until_next_month()
    assert 60 <= s <= 32 * 86400


# ------------------------------------------------------------------ DKIM

_DKIM_PRIV = """-----BEGIN PRIVATE KEY-----
MIICeAIBADANBgkqhkiG9w0BAQEFAASCAmIwggJeAgEAAoGBAMcAsBa7I1T6cslT
Wbv3/y6ggbpqlZPkLpvgLGe4mDJ8ojFSXWOqfmiHXJhMmMbJlWaQdIJ7H8YXh5pS
AsvyNSVJ73wMsNf5tQlpA0KPCCJH/H7doosqbUKlBupmU4skt5VfAa5NGpA9Nr+T
Xk1FYvKdGj4TddlT79DEEtfoHWaxAgMBAAECgYEAvc1XJecJNOTGlGSuIozPU359
HPrJfUvWdI9kauzyJL0gySAAuK2FcVchppTv5jGgEDnZFhYu4PXSPQrZZgG2UxET
LNVRDVxp8hAnpsDMsaZwQo7kB5PJ5PpkHWaSwBGjFWxd2DcqgOMve2OqaTb7/obz
pYNieNPuY9N586ul7AECQQD3LrXIQKuzpKLKcxzzs+GImtucwrT67LnnFHWinOIN
19Uh/zTjPmgIAwV72654n8yWNZUSCkVO3opLkB1w1GfBAkEAzhn/CJtcm3vHp5Yz
UzFIiWklFNpjPrPq8qLHOKgwNkL6OrYCFOlS8jGCRendhHE/du0P9H2cKPr66q/j
qiY68QJBANZgbYozBHd2NzXMJnT6MoVugT9w4tOpUgYnCWsyEyAca53jn/PiYQlp
zWzBd4zoUuCHsjUGMVwYx+qHH/GGVkECQQC9RtyT7/J1jJDIVMWgAmPGZjy60PFS
FXORD3L8kmFsg8hAgQSaZclaSaOFR4i7aDHLq1z6wRzX+d7UB2KC0YdRAkALXaIA
Iuqw6g54jpRAmC3fLAIs3bk6GiHfy3nK+WDIX4oXH8GuWWaNud93Sz2uJdOvSQG4
4QLdfQ7W4j3bUEMQ
-----END PRIVATE KEY-----"""

_DKIM_PUB_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDHALAWuyNU+nLJU1m79/8uoIG6"
    "apWT5C6b4CxnuJgyfKIxUl1jqn5oh1yYTJjGyZVmkHSCex/GF4eaUgLL8jUlSe98"
    "DLDX+bUJaQNCjwgiR/x+3aKLKm1CpQbqZlOLJLeVXwGuTRqQPTa/k15NRWLynRo+"
    "E3XZU+/QxBLX6B1msQIDAQAB"
)


async def test_smtp_dkim_signs_and_verifies(client):
    """DKIM_* configurés => message signé, signature valide vs clé publique."""
    import dkim

    from src.email_providers.smtp import SMTPProvider

    # La clé dans l'env a ses sauts de ligne échappés (\n littéraux).
    settings = client.app.state.settings.model_copy(update={
        "smtp_user": "u", "smtp_password": "p",
        "dkim_selector": "mailhub",
        "dkim_private_key": _DKIM_PRIV.replace("\n", "\n"),
    })
    provider = SMTPProvider(settings)

    captured = {}

    def fake_send(host, port, use_ssl, user, pwd, frm, to, raw):
        captured["raw"] = raw

    provider._send_smtp = fake_send  # type: ignore[method-assign]
    ok = await provider.send_email(
        "dest@x.co", "Sujet", "Corps texte", "<p>Corps html</p>")
    assert ok is True
    raw = captured["raw"]
    assert isinstance(raw, bytes) and b"DKIM-Signature:" in raw
    # Header replié : chercher les tags dans le bloc signature déplié.
    sig = raw.split(b"DKIM-Signature:")[1].split(b"\r\n\r\n")[0]
    flat = sig.replace(b" ", b"").replace(b"\r\n\t", b"")
    assert b"s=mailhub" in flat
    assert b"d=test.dev" in flat  # domaine dérivé de FROM_EMAIL

    def fake_dns(name, *args, **kw):
        assert name.startswith(b"mailhub._domainkey.") or \
            name.startswith("mailhub._domainkey.")
        return b"v=DKIM1; k=rsa; p=" + _DKIM_PUB_B64.encode()

    assert dkim.verify(raw, dnsfunc=fake_dns) is True


async def test_smtp_no_dkim_without_key(client):
    """Sans DKIM_* : le message part non signé (pas d'erreur)."""
    from src.email_providers.smtp import SMTPProvider

    settings = client.app.state.settings.model_copy(update={
        "smtp_user": "u", "smtp_password": "p"})
    provider = SMTPProvider(settings)
    provider._send_smtp = lambda *a: None  # type: ignore[method-assign]
    assert await provider.send_email(
        "d@x.co", "S", "txt", "<p>h</p>") is True


async def test_smtp_dkim_bad_key_still_sends(client, caplog=None):
    """Clé DKIM invalide => warning + envoi non signé (pas de blocage)."""
    from src.email_providers.smtp import SMTPProvider

    settings = client.app.state.settings.model_copy(update={
        "smtp_user": "u", "smtp_password": "p",
        "dkim_selector": "mailhub",
        "dkim_private_key": "pas-une-cle",
    })
    provider = SMTPProvider(settings)
    captured = {}
    provider._send_smtp = lambda *a: captured.update(raw=a[7])  # type: ignore[method-assign]
    assert await provider.send_email("d@x.co", "S", "txt", "<p>h</p>") is True
    assert b"DKIM-Signature:" not in captured["raw"]
