"""Recompile Tailwind et ré-embarque le CSS dans src/web/tailwind_css.py.

Usage : npm i && python scripts/embed_css.py
Le CSS compilé est embarqué dans le code Python — aucun fichier statique
ni étape de build n'est requis au déploiement.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "assets" / "input.css"
OUTPUT = ROOT / "assets" / "app.css"
TARGET = ROOT / "src" / "web" / "tailwind_css.py"

HEADER = """# CSS Tailwind compilé — ne pas éditer à la main.
# Régénérer : python scripts/embed_css.py
TAILWIND_CSS = r\"\"\"{css}\"\"\"
"""


def main() -> None:
    subprocess.run(
        ["npx", "tailwindcss", "-i", str(INPUT), "-o", str(OUTPUT), "--minify"],
        cwd=ROOT, check=True, shell=sys.platform == "win32",
    )
    css = OUTPUT.read_text(encoding="utf-8").strip()
    if '"""' in css or css.endswith("\\"):
        raise SystemExit("CSS incompatible avec un triple-quoted literal")
    TARGET.write_text(HEADER.format(css=css), encoding="utf-8")
    print(f"{TARGET.relative_to(ROOT)} régénéré ({len(css)} octets)")


if __name__ == "__main__":
    main()
