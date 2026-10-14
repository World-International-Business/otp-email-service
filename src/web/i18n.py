"""Multilangue de l'interface web (FR, EN, DE, ES, AR).

Principe : les pages sont écrites en français dans le code. Juste avant
l'envoi, `I18nRoute` traduit le HTML (textes, placeholders, titres) à
partir du dictionnaire `translations.py`, selon le cookie `lang`.
L'arabe passe la page en mode droite-à-gauche (dir="rtl").
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Request
from fastapi.routing import APIRoute

from src.web.translations import PATTERNS, RAW, TRANSLATIONS

DEFAULT_LANG = "fr"
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# code -> (nom natif, direction)
LANGUAGES: dict[str, tuple[str, str]] = {
    "fr": ("Français", "ltr"),
    "en": ("English", "ltr"),
    "de": ("Deutsch", "ltr"),
    "es": ("Español", "ltr"),
    "ar": ("العربية", "rtl"),
}
_IDX = {"en": 0, "de": 1, "es": 2, "ar": 3}


def get_lang(request: Request) -> str:
    code = request.cookies.get(LANG_COOKIE, DEFAULT_LANG)
    return code if code in LANGUAGES else DEFAULT_LANG


def safe_next(url: str | None) -> str:
    """N'accepte que les chemins internes (évite les redirections ouvertes)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url or "\n" in url or "\r" in url:
        return "/"
    return url


# ---------------------------------------------------------------------
# Traduction du HTML
# ---------------------------------------------------------------------
# Une seule passe : on saute les blocs <script>/<style>, on traduit les
# attributs (placeholder, aria-label, title, alt) et les nœuds de texte.
_TOKEN = re.compile(
    r'(?P<skip><script\b.*?</script>|<style\b.*?</style>)'
    r'|\b(?P<attr>placeholder|aria-label|title|alt)="(?P<val>[^"]*)"'
    r'|(?<=>)(?P<text>[^<]+)(?=<)',
    re.S | re.I,
)


def _lookup(key: str, lang: str) -> str | None:
    entry = TRANSLATIONS.get(key)
    if entry:
        return entry[_IDX[lang]]
    for rx, templates in PATTERNS:
        m = rx.match(key)
        if m:
            return templates[_IDX[lang]].format(*m.groups())
    return None


def _translate_text(raw: str, lang: str) -> str:
    key = " ".join(raw.split())
    if not key:
        return raw
    tr = _lookup(key, lang)
    if tr is None:
        if "\n" not in raw:
            return raw
        # bloc multi-lignes (ex. <pre>) : on traduit les lignes connues (commentaires)
        out = []
        for line in raw.split("\n"):
            s = line.strip()
            t = _lookup(s, lang) if s else None
            if t is not None:
                line = line[: len(line) - len(line.lstrip())] + t + line[len(line.rstrip()):]
            out.append(line)
        return "\n".join(out)
    lead = raw[: len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    return lead + tr + trail


def _translate_token(m: re.Match, lang: str) -> str:
    if m.group("skip") is not None:
        return m.group(0)
    if m.group("attr") is not None:
        tr = _lookup("@" + m.group("val").strip(), lang)
        return f'{m.group("attr")}="{tr}"' if tr is not None else m.group(0)
    return _translate_text(m.group("text"), lang)


# ---------------------------------------------------------------------
# Sélecteur de langue
# ---------------------------------------------------------------------
_GLOBE = (
    '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/></svg>'
)
_CHEVRON = (
    '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">'
    '<path stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/></svg>'
)
_CHECK = (
    '<svg class="w-4 h-4 text-indigo-600" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">'
    '<path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
)

# variante -> (classes du bouton, classes + style du menu)
_VARIANTS = {
    "light": (
        "text-slate-600 hover:text-slate-900 hover:bg-slate-100",
        "top-full mt-2", "inset-inline-end:0",
    ),
    "header": (
        "text-slate-200 hover:text-white hover:bg-white/10",
        "top-full mt-2", "inset-inline-end:0",
    ),
    "sidebar": (
        "text-slate-400 hover:text-white hover:bg-white/5",
        "bottom-full mb-2", "inset-inline-start:0",
    ),
}


def lang_switcher(lang: str, next_url: str, variant: str) -> str:
    btn_cls, pos_cls, pos_style = _VARIANTS[variant]
    nxt = quote(next_url, safe="/?=&%")
    items = []
    for code, (name, _dir) in LANGUAGES.items():
        current = code == lang
        items.append(
            f'<a href="/lang/{code}?next={nxt}" hreflang="{code}" lang="{code}" '
            f'class="flex items-center justify-between gap-6 px-4 py-2 text-sm hover:bg-slate-50 '
            f'{"font-bold text-slate-900" if current else "text-slate-600"}">'
            f'<span>{name}</span>{_CHECK if current else ""}</a>'
        )
    return (
        '<div class="relative">'
        f'<button type="button" aria-haspopup="true" aria-label="Language" '
        f'onclick="this.nextElementSibling.classList.toggle(\'hidden\')" '
        f'class="flex items-center gap-1.5 px-2.5 py-2 rounded-lg text-sm font-semibold transition {btn_cls}">'
        f'{_GLOBE}<span>{lang.upper()}</span>{_CHEVRON}</button>'
        f'<div class="lang-menu hidden absolute z-50 min-w-[10.5rem] rounded-xl bg-white border border-slate-200 '
        f'shadow-xl py-1 {pos_cls}" style="{pos_style}">{"".join(items)}</div>'
        '</div>'
    )


_I18N_CSS = """<style id="i18n-css">
[dir=rtl] body{font-family:'Noto Sans Arabic','Inter',system-ui,sans-serif}
[dir=rtl] *{letter-spacing:0!important}
[dir=rtl] .text-left{text-align:right}
[dir=rtl] .ml-auto{margin-left:0;margin-right:auto}
[dir=rtl] .ml-2{margin-left:0;margin-right:.5rem}
[dir=rtl] pre,[dir=rtl] .font-mono{direction:ltr;text-align:left}
[dir=rtl] code{direction:ltr;unicode-bidi:isolate}
[dir=rtl] input[type=email]{direction:ltr}
@media (max-width:767px){[dir=rtl] #sidebar.-translate-x-full{--tw-translate-x:100%}}
</style>"""
_AR_FONT = (
    '<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Arabic:wght@400;500;600;700;800&display=swap" '
    'rel="stylesheet">'
)
_CLOSE_MENU_JS = (
    "<script>document.addEventListener('click',function(e){"
    "document.querySelectorAll('.lang-menu').forEach(function(m){"
    "if(!m.parentElement.contains(e.target))m.classList.add('hidden');});});</script>"
)


def translate_html(html: str, lang: str, next_url: str = "/") -> str:
    """Traduit une page HTML complète et y insère le sélecteur de langue."""
    if lang not in LANGUAGES:
        lang = DEFAULT_LANG
    direction = LANGUAGES[lang][1]

    if lang != DEFAULT_LANG:
        html = _TOKEN.sub(lambda m: _translate_token(m, lang), html)
        for src, tr in RAW.items():
            html = html.replace(src, tr[_IDX[lang]])

    html = re.sub(r'<html lang="[^"]*"', f'<html lang="{lang}" dir="{direction}"', html, count=1)
    html = html.replace("__LANG_SWITCH_LIGHT__", lang_switcher(lang, next_url, "light"))
    html = html.replace("__LANG_SWITCH_HEADER__", lang_switcher(lang, next_url, "header"))
    html = html.replace("__LANG_SWITCH_SIDEBAR__", lang_switcher(lang, next_url, "sidebar"))

    head_extra = _I18N_CSS + (_AR_FONT if lang == "ar" else "")
    html = html.replace("</head>", head_extra + "</head>", 1)
    html = html.replace("</body>", _CLOSE_MENU_JS + "</body>", 1)
    return html


# ---------------------------------------------------------------------
# Route FastAPI : traduit automatiquement toutes les réponses HTML
# ---------------------------------------------------------------------
class I18nRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            response = await original(request)
            ctype = response.headers.get("content-type", "")
            if ctype.startswith("text/html") and hasattr(response, "body"):
                nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                html = response.body.decode("utf-8")
                response.body = translate_html(html, get_lang(request), safe_next(nxt)).encode("utf-8")
                response.headers["content-length"] = str(len(response.body))
                response.headers.append("Vary", "Cookie")
            return response

        return handler