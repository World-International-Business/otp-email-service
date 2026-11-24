"""Endpoint /metrics — exposition Prometheus (texte, version 0.0.4).

Protégé par X-Admin-Key comme les routes /v1/admin — les compteurs par
tenant ne sont pas une donnée publique. Extrait scrape_configs :

    scrape_configs:
      - job_name: mailhub
        metrics_path: /metrics
        static_configs: [{targets: ["mailhub:8001"]}]
        http_headers:
          X-Admin-Key: {credentials: "<ADMIN_API_KEY>"}
"""

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from src.core.auth import require_admin
from src.core.tenants import TenantStore

metrics_router = APIRouter(dependencies=[require_admin])

# Métriques lues dans t:{id}:stats:{name} (compteurs cumulés)
_COUNTERS = {
    "otps_sent": "mailhub_otps_sent_total",
    "otps_verified": "mailhub_otps_verified_total",
    "emails_sent": "mailhub_emails_sent_total",
    "emails_bounced": "mailhub_emails_bounced_total",
    "emails_softbounced": "mailhub_emails_softbounced_total",
    "emails_complained": "mailhub_emails_complained_total",
}


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@metrics_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> PlainTextResponse:
    redis = request.app.state.redis
    tenants = await TenantStore(redis).list()

    lines: list[str] = [
        "# HELP mailhub_tenants_total Tenants enregistrés",
        "# TYPE mailhub_tenants_total gauge",
        f"mailhub_tenants_total {len(tenants)}",
        "# HELP mailhub_tenants_active Tenants actifs",
        "# TYPE mailhub_tenants_active gauge",
        f"mailhub_tenants_active {sum(1 for t in tenants if t.is_active)}",
    ]

    # Profondeurs de files (globales)
    email_q = int(await redis.zcard("mail:queue") or 0)
    wh_q = int(await redis.zcard("wh:queue") or 0)
    support = int(await redis.llen("support:open") or 0)
    lines += [
        "# HELP mailhub_email_queue_depth Jobs email en file/retry",
        "# TYPE mailhub_email_queue_depth gauge",
        f"mailhub_email_queue_depth {email_q}",
        "# HELP mailhub_webhook_queue_depth Livraisons webhook en file/retry",
        "# TYPE mailhub_webhook_queue_depth gauge",
        f"mailhub_webhook_queue_depth {wh_q}",
        "# HELP mailhub_support_open Tickets support ouverts",
        "# TYPE mailhub_support_open gauge",
        f"mailhub_support_open {support}",
    ]

    today = date.today().isoformat()
    pipe = redis.pipeline()
    for t in tenants:
        prefix = t.key_prefix()
        for name in _COUNTERS:
            pipe.get(f"{prefix}stats:{name}")
        pipe.get(f"{prefix}stats_daily:emails_sent:{today}")
        pipe.get(f"{prefix}stats_daily:otps_sent:{today}")
    flat = await pipe.execute()

    declared: set[str] = set()
    idx = 0
    for t in tenants:
        label = f'tenant="{_esc(t.tenant_id)}"'
        for name in _COUNTERS:
            mname = _COUNTERS[name]
            if mname not in declared:
                lines += [
                    f"# TYPE {mname} counter",
                ]
                declared.add(mname)
            lines.append(f"{mname}{{{label}}} {int(flat[idx] or 0)}")
            idx += 1
        daily = int(flat[idx] or 0) + int(flat[idx + 1] or 0)
        idx += 2
        if "mailhub_sends_today" not in declared:
            lines.append("# TYPE mailhub_sends_today gauge")
            declared.add("mailhub_sends_today")
        lines.append(f"mailhub_sends_today{{{label}}} {daily}")
        if "mailhub_tenant_smtp_ok" not in declared:
            lines.append("# TYPE mailhub_tenant_smtp_ok gauge")
            declared.add("mailhub_tenant_smtp_ok")
        lines.append(
            f"mailhub_tenant_smtp_ok{{{label}}} "
            f"{1 if t.smtp_status == 'ok' else 0}"
        )

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )
