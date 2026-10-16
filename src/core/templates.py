"""Templates de notification dynamiques — stockés en Redis, par tenant.

Un tenant peut gérer ses propres templates via /v1/templates au lieu de
dépendre des templates en dur du code. Syntaxe de rendu : {{variable}}.

Clés Redis :
  {tenant_prefix}tpl:{template_id}   -> JSON du template
  {tenant_prefix}tpl:index           -> set des template_id
"""

import json
import re
from typing import Any

from src.core.models import TemplateVariant
from src.core.security import now_utc
from src.core.tenants import Tenant

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_TAG_RE = re.compile(r"<[^>]+>")


def render_string(template: str | None, data: dict[str, Any]) -> str | None:
    """Remplace les {{var}} par les valeurs de `data`. Inconnu -> chaîne vide."""
    if template is None:
        return None
    return _VAR_RE.sub(lambda m: str(_lookup(data, m.group(1)) or ""), template)


def _lookup(data: dict[str, Any], dotted_key: str) -> Any:
    """Supporte {{name}} et les accès imbriqués {{user.name}}."""
    current: Any = data
    for part in dotted_key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def html_to_text(html: str) -> str:
    """Fallback texte brut quand un template ne fournit que du HTML."""
    text = _TAG_RE.sub("", html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class EmailTemplate:
    """Template résolu et prêt à rendre."""

    def __init__(self, variant: TemplateVariant):
        self.variant = variant

    def render(
        self, data: dict[str, Any]
    ) -> tuple[str, str | None, str | None]:
        """Retourne (subject, text, html) rendus."""
        subject = render_string(self.variant.subject, data) or ""
        text = render_string(self.variant.text, data)
        html = render_string(self.variant.html, data)
        if text is None and html is not None:
            text = html_to_text(html)
        return subject, text, html


class TemplateStore:
    """Registre Redis des templates d'un tenant."""

    def __init__(self, redis, tenant: Tenant):
        self.redis = redis
        self.tenant = tenant

    def _key(self, template_id: str) -> str:
        return f"{self.tenant.key_prefix()}tpl:{template_id}"

    def _index_key(self) -> str:
        return f"{self.tenant.key_prefix()}tpl:index"

    async def upsert(
        self,
        template_id: str,
        variants: dict[str, TemplateVariant],
        description: str | None = None,
    ) -> dict:
        existing = await self.get(template_id)
        now = now_utc().isoformat()
        record = {
            "template_id": template_id,
            "variants": {k: v.model_dump() for k, v in variants.items()},
            "description": description,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        pipe = self.redis.pipeline()
        pipe.set(self._key(template_id), json.dumps(record))
        pipe.sadd(self._index_key(), template_id)
        await pipe.execute()
        return record

    async def get(self, template_id: str) -> dict | None:
        raw = await self.redis.get(self._key(template_id))
        return json.loads(raw) if raw else None

    async def list(self) -> list[dict]:
        ids = await self.redis.smembers(self._index_key())
        records = []
        for tid in sorted(ids):
            rec = await self.get(tid)
            if rec:
                records.append(rec)
        return records

    async def delete(self, template_id: str) -> bool:
        pipe = self.redis.pipeline()
        pipe.delete(self._key(template_id))
        pipe.srem(self._index_key(), template_id)
        deleted, _ = await pipe.execute()
        return bool(deleted)

    async def render(
        self,
        template_id: str,
        language: str,
        data: dict[str, Any],
    ) -> tuple[str, str | None, str | None] | None:
        """Résout un template tenant.

        Sélection de la variante : `language` -> `default` -> première variante.
        Retourne None si le template n'existe pas.
        """
        record = await self.get(template_id)
        if record is None:
            return None

        variants = record["variants"]
        variant_data = (
            variants.get(language)
            or variants.get("default")
            or next(iter(variants.values()), None)
        )
        if variant_data is None:
            return None

        merged = {"brand_name": self.tenant.brand_name, **data}
        return EmailTemplate(TemplateVariant.model_validate(variant_data)).render(merged)
