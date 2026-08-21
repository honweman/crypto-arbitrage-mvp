from pathlib import Path


WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _read_web_asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


HTML = _read_web_asset(TEMPLATE_DIR / "index.html")
APP_JS = _read_web_asset(STATIC_DIR / "app.js")
STYLES_CSS = _read_web_asset(STATIC_DIR / "styles.css")
