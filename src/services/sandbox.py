"""Mode sandbox — capture des envois sans SMTP réel.

Un tenant en ``sandbox=True`` passe TOUT le pipeline (rendu, rate limit,
suppression, warm-up, events, webhooks, statuts) mais le message final
n'est pas envoyé : il est stocké dans ``t:{id}:sandbox_outbox`` (50 max)
pour inspection depuis le dashboard. Idéal pour tester une intégration
sans risquer un vrai envoi.
"""

import json
import time

OUTBOX_MAX = 50
_HTML_KEEP = 4000  # tronqué pour ne pas gonfler Redis


def _key(tenant_id: str) -> str:
    return f"t:{tenant_id}:sandbox_outbox"


async def capture_send(
    redis,
    tenant_id: str,
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str,
    kind: str,
    ref: str = "",
) -> None:
    entry = {
        "to": to,
        "subject": subject,
        "text_body": text_body[:_HTML_KEEP],
        "html_body": html_body[:_HTML_KEEP],
        "type": kind,
        "ref": ref,
        "ts": int(time.time()),
    }
    pipe = redis.pipeline()
    pipe.lpush(_key(tenant_id), json.dumps(entry))
    pipe.ltrim(_key(tenant_id), 0, OUTBOX_MAX - 1)
    await pipe.execute()


async def list_outbox(redis, tenant_id: str, limit: int = OUTBOX_MAX) -> list[dict]:
    raw = await redis.lrange(_key(tenant_id), 0, limit - 1)
    out = []
    for r in raw:
        try:
            out.append(json.loads(r))
        except (ValueError, TypeError):
            continue
    return out
