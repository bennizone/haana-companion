"""
Auth-Backend für HAANA Admin-Interface
Zwei Modi:
  - HA-Ingress:   SUPERVISOR_TOKEN Env-Var vorhanden → X-Ingress-Path Header reicht
  - Standalone:   Token aus /data/config/config.json (admin_token), Cookie oder Bearer
"""

import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ── Konfiguration ─────────────────────────────────────────────────────────────

SUPERVISOR_TOKEN: str | None = os.environ.get("SUPERVISOR_TOKEN")
IS_INGRESS_MODE: bool = bool(SUPERVISOR_TOKEN)

CONF_FILE = Path(os.environ.get("HAANA_CONF_FILE", "/data/config/config.json"))

# Cookie-Name
COOKIE_NAME = "haana_session"


# ── Token-Verwaltung (Standalone-Modus) ───────────────────────────────────────

def get_admin_token() -> str:
    """
    Liest den Admin-Token aus config.json.
    Falls kein Token vorhanden → generiert einen neuen und speichert ihn.
    Gibt den Token zurück.
    """
    cfg = _load_raw_config()
    token = cfg.get("admin_token", "")
    if not token:
        token = secrets.token_urlsafe(32)
        cfg["admin_token"] = token
        _save_raw_config(cfg)
        logger.info(f"HAANA Admin Token: {token}")
    return token


def _load_raw_config() -> dict:
    """Lädt config.json ohne Migration-Logik."""
    if CONF_FILE.exists():
        try:
            return json.loads(CONF_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_raw_config(cfg: dict) -> None:
    """Speichert config.json (Minimal-Schreiber, um Circular-Import zu vermeiden)."""
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Auth-Check ────────────────────────────────────────────────────────────────

def is_authenticated(request: Request) -> bool:
    """
    Gibt True zurück wenn der Request authentifiziert ist.
    Im Ingress-Modus: X-Ingress-Path oder X-Supervisor-Token Header vorhanden.
    Im Standalone-Modus: Cookie haana_session oder Authorization: Bearer <token>.
    """
    if IS_INGRESS_MODE:
        # HA Ingress leitet nur authentifizierte User weiter.
        # X-Ingress-Path ist gesetzt wenn der Request über Ingress kommt.
        supervisor_token_header = request.headers.get("X-Supervisor-Token")
        if supervisor_token_header:
            # Wenn der Header vorhanden ist, muss er mit dem SUPERVISOR_TOKEN übereinstimmen
            if SUPERVISOR_TOKEN and secrets.compare_digest(supervisor_token_header, SUPERVISOR_TOKEN):
                return True
            # Header vorhanden aber ungültig → verweigern
            return False
        if request.headers.get("X-Ingress-Path"):
            # X-Ingress-Path allein reicht (HA-Proxy setzt ihn, kein normaler Browser kann das im Add-on-Kontext)
            return True
        # Wenn kein Ingress-Header: Standalone-Prüfung als Fallback
        # (z.B. direkter Zugriff während Entwicklung)
        return _check_standalone_token(request)
    else:
        return _check_standalone_token(request)


def _check_standalone_token(request: Request) -> bool:
    """Prüft Cookie oder Authorization-Header gegen den gespeicherten Admin-Token."""
    expected = get_admin_token()

    # 1. Cookie prüfen
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    if cookie_val and secrets.compare_digest(cookie_val, expected):
        return True

    # 2. Authorization: Bearer <token> Header prüfen
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer "):]
        if bearer and secrets.compare_digest(bearer, expected):
            return True

    return False


# ── FastAPI Dependency ────────────────────────────────────────────────────────

async def require_auth(request: Request):
    """
    FastAPI-Dependency: Wirft 401 wenn nicht authentifiziert.
    Wird nicht direkt verwendet (Middleware macht den Check),
    aber als Dependency für einzelne Endpoints verfügbar.
    """
    if not is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
        )


# ── Startup-Log ───────────────────────────────────────────────────────────────

def log_startup_info() -> None:
    """Gibt beim Start Infos zum Auth-Modus aus."""
    if IS_INGRESS_MODE:
        logger.info("[Auth] Modus: HA-Ingress (SUPERVISOR_TOKEN vorhanden)")
    else:
        token = get_admin_token()
        logger.info("[Auth] Modus: Standalone")
        # Token in stdout ausgeben (einmalig lesbar im Add-on-Log)
        print(f"HAANA Admin Token: {token}", flush=True)
