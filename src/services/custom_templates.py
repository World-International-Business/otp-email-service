"""Templates email personnalisés par tenant (welcome/notification/transaction).

Le tenant peut remplacer le sujet, le corps texte et (optionnellement) le
fragment HTML des emails prédéfinis. Les variables s'écrivent {name},
{brand}, {title}… — substitution simple sur {ident}, un identifiant
inconnu reste tel quel (pas de KeyError sur une faute de frappe).
"""

import json
import re
from typing import Any

import redis.asyncio as redis

TEMPLATE_KINDS = ("welcome", "notification", "transaction")

# Variables disponibles par type — affichées dans l'éditeur et utilisées
# pour l'aperçu avec des données factices.
TEMPLATE_VARS: dict[str, tuple[str, ...]] = {
    "welcome": ("name", "brand"),
    "notification": ("title", "message", "brand"),
    "transaction": ("amount", "currency", "status", "brand"),
}

SAMPLE_VARS: dict[str, dict[str, str]] = {
    "welcome": {"name": "Jean Dupont"},
    "notification": {
        "title": "Paiement reçu",
        "message": "Votre paiement de 25 000 XOF a bien été confirmé.",
    },
    "transaction": {
        "amount": "25 000", "currency": "XOF", "status": "confirmée",
    },
}

_VAR_RE = re.compile(r"\{(\w{1,40})\}")
_MAX_LEN = 20_000


def _key(tenant_id: str, kind: str) -> str:
    return f"t:{tenant_id}:tmpl:{kind}"


def render_vars(template: str, variables: dict[str, Any]) -> str:
    """Substitue {var} par sa valeur ; {inconnu} est conservé tel quel."""
    return _VAR_RE.sub(
        lambda m: str(variables.get(m.group(1), m.group(0))), template)


async def get_template(
    r: redis.Redis, tenant_id: str, kind: str
) -> dict[str, str] | None:
    if kind not in TEMPLATE_KINDS:
        return None
    raw = await r.get(_key(tenant_id, kind))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        k: str(data.get(k) or "")
        for k in ("subject", "text", "html")
    }


async def set_template(
    r: redis.Redis, tenant_id: str, kind: str,
    subject: str, text: str, html: str = "",
) -> None:
    if kind not in TEMPLATE_KINDS:
        raise ValueError(f"unknown template kind: {kind}")
    data = {
        "subject": subject[:500],
        "text": text[:_MAX_LEN],
        "html": html[:_MAX_LEN],
    }
    if not any(data.values()):
        await r.delete(_key(tenant_id, kind))
    else:
        await r.set(_key(tenant_id, kind), json.dumps(data))


async def delete_template(r: redis.Redis, tenant_id: str, kind: str) -> None:
    await r.delete(_key(tenant_id, kind))


async def list_templates(
    r: redis.Redis, tenant_id: str
) -> dict[str, dict[str, str] | None]:
    """État des 3 types éditables : override ou None (défaut)."""
    return {
        kind: await get_template(r, tenant_id, kind)
        for kind in TEMPLATE_KINDS
    }
