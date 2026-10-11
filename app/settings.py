import os
import tomllib
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


AMD_DIR = Path(os.environ.get("AMD_DIR", "/amd"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://192.168.0.237:4533").rstrip("/")
NAVIDROME_PUBLIC_URL = os.environ.get("NAVIDROME_PUBLIC_URL", NAVIDROME_URL).rstrip("/")
ALLOW_NON_ADMIN = _bool("ALLOW_NON_ADMIN", False)
APPLE_STOREFRONT = os.environ.get("APPLE_STOREFRONT", "us").lower()
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
COOKIE_SECURE = _bool("COOKIE_SECURE", False)
SCAN_TIMEOUT = int(os.environ.get("SCAN_TIMEOUT", "900"))
WM_DATA_DIR = Path(os.environ.get("WM_DATA_DIR", "/wm"))


def amd_config() -> dict:
    with open(AMD_DIR / "config.toml", "rb") as f:
        return tomllib.load(f)


def apple_language() -> str:
    return os.environ.get("APPLE_LANGUAGE") or amd_config().get("region", {}).get("language", "en-US")
