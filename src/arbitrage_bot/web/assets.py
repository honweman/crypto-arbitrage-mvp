from hashlib import sha256
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _read_web_asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _asset_version(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()[:12]


APP_JS = _read_web_asset(STATIC_DIR / "app.js")
I18N_JS = _read_web_asset(STATIC_DIR / "i18n.js")
THEME_JS = _read_web_asset(STATIC_DIR / "theme.js")
STYLES_CSS = _read_web_asset(STATIC_DIR / "styles.css")

APP_JS_VERSION = _asset_version(APP_JS)
I18N_JS_VERSION = _asset_version(I18N_JS)
THEME_JS_VERSION = _asset_version(THEME_JS)
STYLES_CSS_VERSION = _asset_version(STYLES_CSS)

HTML = (
    _read_web_asset(TEMPLATE_DIR / "index.html")
    .replace("{{APP_JS_VERSION}}", APP_JS_VERSION)
    .replace("{{I18N_JS_VERSION}}", I18N_JS_VERSION)
    .replace("{{THEME_JS_VERSION}}", THEME_JS_VERSION)
    .replace("{{STYLES_CSS_VERSION}}", STYLES_CSS_VERSION)
)
