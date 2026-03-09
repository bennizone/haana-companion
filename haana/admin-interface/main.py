"""
HAANA Admin-Interface – FastAPI Backend

Routen:
  GET  /                              → index.html (SPA)
  GET  /api/conversations/{instance} → letzte Konversationen
  GET  /api/logs/{category}          → letzte Log-Einträge (memory-ops, tool-calls)
  GET  /api/instances                → verfügbare Instanzen
  GET  /api/config                   → aktuelle Konfiguration
  POST /api/config                   → Konfiguration speichern
  GET  /api/claude-md/{instance}     → CLAUDE.md einer Instanz lesen
  POST /api/claude-md/{instance}     → CLAUDE.md speichern
  GET  /api/status                   → Systemstatus (Qdrant, Ollama, Log-Stats)
  GET  /api/events/{instance}        → SSE-Stream für neue Konversationen
  GET  /api/users                    → User-Liste
  POST /api/users                    → User anlegen (inkl. Container-Start)
  PATCH /api/users/{user_id}         → User aktualisieren (Container-Restart)
  DELETE /api/users/{user_id}        → User löschen (Container entfernen)
  POST /api/users/{user_id}/restart  → Container neu starten
  GET  /api/logs-download             → Logs als ZIP herunterladen (scope=all|system|conversations)
  DELETE /api/logs-delete             → Logs löschen (scope=all|system|conversations|conversations:inst)
  GET  /api/logs/export/{instance}   → User-Logs als ZIP exportieren
  DELETE /api/logs/user/{instance}   → Alle User-Daten löschen (?confirm=true)
  DELETE /api/logs/day/{inst}/{date} → Tages-Log löschen
  POST /api/logs/rebuild/{inst}/{date} → Memories aus Tages-Log re-extrahieren
  POST /api/logs/check-rebuild/{inst}  → Geänderte Logs finden (?auto_rebuild=true)
  GET  /api/whatsapp-status          → Bridge-Verbindungsstatus (Proxy)
  GET  /api/whatsapp-qr              → QR-Code als Base64 Data-URL (Proxy)
  POST /api/whatsapp-logout          → WhatsApp-Session trennen (Proxy)
"""

import asyncio
import http.client
import json
import logging
import os
import pty
import re
import secrets
import select
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs
import glob as _glob

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth as _auth

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(__file__))
import git_integration as _git
from terminal import (
    ws_terminal as _ws_terminal,
    set_provider as _term_set_provider,
    get_status as _term_status,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import docker as _docker
    _docker_client = _docker.from_env()
except Exception:
    _docker_client = None

from core.process_manager import (
    detect_mode, create_agent_manager, AgentManager,
)
from core.ollama_compat import create_ollama_router

logger = logging.getLogger(__name__)

app = FastAPI(title="HAANA Admin", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Auth-Middleware ───────────────────────────────────────────────────────────

# Pfade die OHNE Authentifizierung zugänglich sind
_AUTH_EXEMPT_PREFIXES = ("/static/", "/ws/")  # WICHTIG: WS-Endpoints müssen intern selbst auth prüfen!
_AUTH_EXEMPT_EXACT = {"/", "/api/auth/login", "/api/auth/logout", "/api/auth/status", "/api/health", "/api/setup-status"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Exempt: exakte Matches
        if path in _AUTH_EXEMPT_EXACT:
            return await call_next(request)

        # Exempt: Präfix-Matches (static, ws)
        if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)

        # Auth prüfen
        if not _auth.is_authenticated(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated", "mode": "ingress" if _auth.IS_INGRESS_MODE else "standalone"},
            )

        return await call_next(request)


app.add_middleware(AuthMiddleware)

# ── Rebuild-Zustand (pro Instanz) ─────────────────────────────────────────────
# status: "idle" | "running" | "done" | "error" | "cancelled"
_rebuild: dict[str, dict] = {
    inst: {"status": "idle", "done": 0, "total": 0, "started": 0.0, "error": ""}
    for inst in ["benni", "domi", "ha-assist", "ha-advanced"]
}


def _sync_rebuild_state():
    """Rebuild-State mit dynamischen Usern aus config.json synchronisieren."""
    cfg = load_config()
    for u in cfg.get("users", []):
        uid = u.get("id", "")
        if uid and uid not in _rebuild:
            _rebuild[uid] = {"status": "idle", "done": 0, "total": 0, "started": 0.0, "error": ""}


# ── Dream-Zustand (pro Instanz) ──────────────────────────────────────────────
# status: "idle" | "running" | "done" | "error"
_dream_state: dict[str, dict] = {}


# ── Log-Retention Cleanup ─────────────────────────────────────────────────────

def _cleanup_logs_once():
    """Löscht Log-Dateien die älter als konfigurierte Retention sind."""
    cfg = load_config()
    retention: dict = cfg.get("log_retention", {})

    now = time.time()
    deleted = 0
    for category, days in retention.items():
        if days is None:
            continue  # niemals löschen
        cutoff = now - int(days) * 86400
        pattern = str(LOG_ROOT / category / "**" / "*.jsonl")
        for fpath in _glob.glob(pattern, recursive=True):
            try:
                if Path(fpath).stat().st_mtime < cutoff:
                    Path(fpath).unlink()
                    deleted += 1
            except Exception:
                pass
    if deleted:
        import logging as _log
        _log.getLogger(__name__).info(f"[Cleanup] {deleted} Log-Datei(en) gelöscht")


async def _cleanup_loop():
    """Läuft beim Start und dann täglich."""
    _cleanup_logs_once()
    while True:
        await asyncio.sleep(86400)
        _cleanup_logs_once()


# ── Betriebsmodus ────────────────────────────────────────────────────────────
HAANA_MODE = detect_mode()
_agent_manager: Optional[AgentManager] = None


@app.on_event("startup")
async def startup_event():
    global _agent_manager
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_dream_scheduler())
    _sync_rebuild_state()
    _auth.log_startup_info()
    # Skills-Verzeichnis in /data/ sicherstellen (update-resistent)
    _data_skills = Path("/data/skills")
    if not _data_skills.exists():
        _data_skills.mkdir(parents=True, exist_ok=True)
        logger.info(
            "[Startup] /data/skills/ erstellt. "
            "Eigene Skills hier ablegen – sie überschreiben /app/skills/ beim nächsten Agent-Start."
        )
    else:
        logger.debug("[Startup] /data/skills/ vorhanden (update-resistent)")
    # AgentManager initialisieren (nach Config-Laden, damit _resolve_llm verfügbar ist)
    _agent_manager = create_agent_manager(
        HAANA_MODE,
        main_app=app,
        docker_client=_docker_client,
        resolve_llm_fn=_resolve_llm,
        find_ollama_url_fn=_find_ollama_url,
    )
    # Ollama-kompatibler Router (Fake-Ollama für HA Voice Integration)
    # Kein Agent-Stack — direkter LLM-Call mit Memory-Enrichment (via Qdrant+Ollama, kein mem0)
    ollama_router = create_ollama_router(
        get_config=load_config,
        resolve_llm=_resolve_llm,
        find_ollama_url=_find_ollama_url,
        get_agent_url=lambda inst: _agent_manager.agent_url(inst),
    )
    app.include_router(ollama_router)
    # Add-on Modus: Agents beim Start automatisch starten (kein Docker-SDK)
    if HAANA_MODE == "addon":
        asyncio.create_task(_autostart_agents())

async def _autostart_agents():
    """Startet alle konfigurierten User-Agents im Add-on-Modus."""
    import logging as _log
    _logger = _log.getLogger(__name__)
    cfg = load_config()
    for user in cfg.get("users", []):
        uid = user.get("id", "")
        if not uid:
            continue
        try:
            result = await _agent_manager.start_agent(user, cfg)
            if result.get("ok"):
                _logger.info(f"[Autostart] Agent '{uid}' gestartet")
            else:
                _logger.warning(f"[Autostart] Agent '{uid}': {result.get('error', 'unbekannt')}")
        except Exception as e:
            _logger.error(f"[Autostart] Agent '{uid}' fehlgeschlagen: {e}")

# ── Pfade ────────────────────────────────────────────────────────────────────

DATA_ROOT  = Path(os.environ.get("HAANA_DATA_DIR",  "/data"))
CONF_FILE  = Path(os.environ.get("HAANA_CONF_FILE", "/data/config/config.json"))
INST_DIR   = Path(os.environ.get("HAANA_INST_DIR",  "/app/instanzen"))

# Media-Verzeichnis: HAANA_MEDIA_DIR > /media/haana (falls existent) > /data
def _get_media_dir() -> Path:
    env = os.environ.get("HAANA_MEDIA_DIR", "").strip()
    if env:
        return Path(env)
    default = Path("/media/haana")
    if default.exists():
        return default
    return Path("/data")

MEDIA_ROOT = _get_media_dir()

# Log-Verzeichnis: HAANA_LOG_DIR > {MEDIA_DIR}/logs
def _get_log_root() -> Path:
    env = os.environ.get("HAANA_LOG_DIR", "").strip()
    if env:
        return Path(env)
    return MEDIA_ROOT / "logs"

LOG_ROOT = _get_log_root()

INSTANCES = ["benni", "domi", "ha-assist", "ha-advanced"]  # statische Basis-Instanzen

def get_all_instances() -> list[str]:
    """Alle Instanzen: statische + dynamische User aus config.json."""
    cfg = load_config()
    user_ids = [u["id"] for u in cfg.get("users", []) if u.get("id")]
    # Combine: statische zuerst, dann weitere dynamische (de-dup)
    result = list(INSTANCES)
    for uid in user_ids:
        if uid not in result:
            result.append(uid)
    return result

# Agent-API URLs (aus Env, Fallback für lokale Entwicklung)
AGENT_URLS: dict[str, str] = {
    "benni":       os.environ.get("AGENT_URL_BENNI",       "http://localhost:8001"),
    "domi":        os.environ.get("AGENT_URL_DOMI",        "http://localhost:8002"),
    "ha-assist":   os.environ.get("AGENT_URL_HA_ASSIST",   "http://localhost:8003"),
    "ha-advanced": os.environ.get("AGENT_URL_HA_ADVANCED", "http://localhost:8004"),
}

# Docker-Management Konstanten
HOST_BASE       = os.environ.get("HAANA_HOST_BASE",        "/opt/haana")
DATA_VOLUME     = os.environ.get("HAANA_DATA_VOLUME",       "haana_haana-data")
COMPOSE_NETWORK = os.environ.get("HAANA_COMPOSE_NETWORK",  "haana_default")
AGENT_IMAGE     = os.environ.get("HAANA_AGENT_IMAGE",       "haana-instanz-benni")
TEMPLATES_DIR   = INST_DIR / "templates"

# ── Default-Konfiguration ────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "providers": [],
    "llms": [],
    "memory": {
        "extraction_llm":          "",
        "extraction_llm_fallback": "",
        "context_enrichment":      False,
        "context_before":          3,
        "context_after":           2,
        "window_size":    int(os.environ.get("HAANA_WINDOW_SIZE",    "20")),
        "window_minutes": int(os.environ.get("HAANA_WINDOW_MINUTES", "60")),
        "min_messages":   5,
    },
    "embedding": {
        "provider_id":          "__local__",
        "model":                os.environ.get("HAANA_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        "dims":                 int(os.environ.get("HAANA_EMBEDDING_DIMS", "384")),
        "fallback_provider_id": "",
    },
    "log_retention": {
        "conversations": None,   # niemals löschen
        "llm-calls":     30,
        "tool-calls":    30,
        "memory-ops":    30,
    },
    "services": {
        "ha_url":        os.environ.get("HA_URL", ""),
        "ha_token":      "",
        "ha_mcp_enabled": False,
        "ha_mcp_type":   "extended",  # "builtin" = HA built-in (SSE), "extended" = ha-mcp add-on (HTTP)
        "ha_mcp_url":    "",   # leer = auto-detect je nach Typ
        "ha_mcp_token":  "",   # leer = ha_token verwenden
        "ha_auto_backup": False,  # HA-Backup vor Agent-Änderungen
        "qdrant_url":    os.environ.get("QDRANT_URL", "http://qdrant:6333"),
    },
    "users": [
        {
            "id": "ha-assist", "display_name": "HAANA Voice", "role": "voice",
            "system": True,
            "language": "de",
            "primary_llm": "", "fallback_llm": "",

            "ha_user": "", "whatsapp_phone": "",
            "api_port": 8003, "container_name": "haana-instanz-ha-assist-1",
            "claude_md_template": "ha-assist",
            "caldav_url": "", "caldav_user": "", "caldav_pass": "",
            "imap_host": "", "imap_port": 993, "imap_user": "", "imap_pass": "",
            "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "",
        },
        {
            "id": "ha-advanced", "display_name": "HAANA Advanced", "role": "voice-advanced",
            "system": True,
            "language": "de",
            "primary_llm": "", "fallback_llm": "",

            "ha_user": "", "whatsapp_phone": "",
            "api_port": 8004, "container_name": "haana-instanz-ha-advanced-1",
            "claude_md_template": "ha-advanced",
            "caldav_url": "", "caldav_user": "", "caldav_pass": "",
            "imap_host": "", "imap_port": 993, "imap_user": "", "imap_pass": "",
            "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "",
        },
    ],
    "ollama_compat": {
        "enabled": False,
        "exposed_models": ["ha-assist", "ha-advanced"],
    },
    "dream": {
        "enabled": False,
        "schedule": "02:00",
        "llm": "",
        "scopes": [],
    },
    "whatsapp": {
        "mode": "separate",
        "self_prefix": "!h ",
    },
}


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

_SYSTEM_USERS = {
    "ha-assist":   DEFAULT_CONFIG["users"][0],  # HAANA Voice
    "ha-advanced": DEFAULT_CONFIG["users"][1],  # HAANA Advanced
}
_SYSTEM_USER_IDS = set(_SYSTEM_USERS.keys())


def _ensure_system_users(cfg: dict) -> None:
    """Stellt sicher, dass die System-Instanzen immer in users vorhanden sind (max. 1×).

    Vorhandene Einträge werden mit Defaults gemergt (User-Änderungen bleiben erhalten).
    Fehlende Einträge werden aus DEFAULT_CONFIG eingefügt.
    """
    users = cfg.setdefault("users", [])
    existing = {u["id"]: u for u in users if u.get("id") in _SYSTEM_USER_IDS}

    # Vorhandene System-User aus der Liste entfernen (werden unten wieder eingefügt)
    cfg["users"] = [u for u in users if u.get("id") not in _SYSTEM_USER_IDS]

    # System-User mergen: Default als Basis, vorhandene Werte überschreiben
    for sys_id, default_user in _SYSTEM_USERS.items():
        if sys_id in existing:
            merged = {**default_user, **existing[sys_id]}
            merged["system"] = True  # Schutz: system-Flag immer setzen
            cfg["users"].append(merged)
        else:
            cfg["users"].append(dict(default_user))


def _ensure_user_defaults(cfg: dict) -> None:
    """Stellt sicher, dass alle User neu hinzugefügte Felder mit Defaults haben."""
    for user in cfg.get("users", []):
        user.setdefault("language", "de")
    # Embedding-Defaults für bestehende Configs ohne embedding-Sektion
    if "embedding" not in cfg:
        cfg["embedding"] = dict(DEFAULT_CONFIG["embedding"])
    else:
        for k, v in DEFAULT_CONFIG["embedding"].items():
            cfg["embedding"].setdefault(k, v)


def _slugify(text: str) -> str:
    """Einfacher Slug: Kleinbuchstaben, Umlaute ersetzen, nur [a-z0-9-]."""
    text = text.lower().strip()
    for old, new in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        text = text.replace(old, new)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "item"


def _migrate_config(cfg: dict) -> bool:
    """Migriert alte llm_providers[]-Struktur zu providers[] + llms[].

    Returns True wenn Migration durchgeführt wurde.
    """
    if "providers" in cfg or "llm_providers" not in cfg:
        return False

    old_slots = cfg.pop("llm_providers", [])
    cfg.pop("use_cases", None)

    # Provider deduplizieren nach (type, url, key)
    seen_providers: dict[tuple, str] = {}
    providers: list[dict] = []
    llms: list[dict] = []
    slot_to_llm_id: dict[int, str] = {}

    for slot in old_slots:
        pkey = (slot.get("type", "custom"), slot.get("url", ""), slot.get("key", ""))
        if pkey not in seen_providers:
            ptype = slot.get("type", "custom")
            pid = f"{ptype}-{len(providers) + 1}"
            providers.append({
                "id": pid,
                "name": slot.get("name", pid),
                "type": ptype,
                "url": slot.get("url", ""),
                "key": slot.get("key", ""),
            })
            seen_providers[pkey] = pid

        provider_id = seen_providers[pkey]
        lid = _slugify(slot.get("name", f"llm-{slot.get('slot', len(llms) + 1)}"))
        # Eindeutigkeit sicherstellen
        base_lid = lid
        counter = 2
        existing_ids = {l["id"] for l in llms}
        while lid in existing_ids:
            lid = f"{base_lid}-{counter}"
            counter += 1

        llms.append({
            "id": lid,
            "name": slot.get("name", f"LLM {slot.get('slot', '')}"),
            "provider_id": provider_id,
            "model": slot.get("model", ""),
        })
        slot_to_llm_id[slot.get("slot", len(llms))] = lid

    cfg["providers"] = providers
    cfg["llms"] = llms

    # User-Felder migrieren
    for user in cfg.get("users", []):
        if "primary_llm_slot" in user:
            old_slot = user.pop("primary_llm_slot")
            user["primary_llm"] = slot_to_llm_id.get(old_slot, llms[0]["id"] if llms else "")
        if "extraction_llm_slot" in user:
            user.pop("extraction_llm_slot")
        # extraction_llm ist jetzt nur noch global (memory.extraction_llm)
        user.pop("extraction_llm", None)
        user.setdefault("fallback_llm", "")

    # Embedding migrieren: provider → provider_id
    emb = cfg.get("embedding", {})
    if "provider" in emb and "provider_id" not in emb:
        old_prov_type = emb.pop("provider")
        matching = next((p for p in providers if p["type"] == old_prov_type), None)
        emb["provider_id"] = matching["id"] if matching else ""
        emb.setdefault("fallback_provider_id", "")

    # Memory: extraction_llm global setzen
    mem = cfg.setdefault("memory", {})
    if "extraction_llm" not in mem:
        # Ollama-basiertes LLM als Default-Extraction
        ollama_llm = next(
            (l for l in llms if any(
                p["type"] == "ollama" and p["id"] == l["provider_id"] for p in providers
            )),
            None,
        )
        mem["extraction_llm"] = ollama_llm["id"] if ollama_llm else ""
        mem["extraction_llm_fallback"] = ""

    return True


def _migrate_providers_v2(cfg: dict) -> bool:
    """Migriert Provider v2: auth_method für Anthropic, ollama_url entfernen.

    Returns True wenn Migration durchgeführt wurde.
    """
    changed = False

    # Anthropic-Provider: auth_method hinzufügen
    for p in cfg.get("providers", []):
        if p.get("type") == "anthropic" and "auth_method" not in p:
            p["auth_method"] = "oauth" if not p.get("key") else "api_key"
            changed = True

    # services.ollama_url entfernen, Wert in Ollama-Providern sicherstellen
    services = cfg.get("services", {})
    old_ollama_url = services.pop("ollama_url", None)
    if old_ollama_url is not None:
        changed = True
        # Sicherstellen dass mindestens ein Ollama-Provider die URL hat
        for p in cfg.get("providers", []):
            if p.get("type") == "ollama" and not p.get("url"):
                p["url"] = old_ollama_url

    # OAuth credentials migration: bestehende /claude-auth/.credentials.json
    # in den passenden Provider-Ordner verschieben
    for p in cfg.get("providers", []):
        if p.get("type") == "anthropic" and p.get("auth_method") == "oauth" and "oauth_dir" not in p:
            p["oauth_dir"] = f"/data/claude-auth/{p['id']}"
            changed = True

    return changed


def _resolve_llm(llm_id: str, cfg: dict) -> tuple[dict, dict]:
    """Löst eine LLM-ID zu (llm_dict, provider_dict) auf. Gibt ({}, {}) zurück wenn nicht gefunden."""
    llm = next((l for l in cfg.get("llms", []) if l["id"] == llm_id), {})
    if not llm:
        return {}, {}
    provider = next((p for p in cfg.get("providers", []) if p.get("id") == llm.get("provider_id")), {})
    return llm, provider


def _find_ollama_url(cfg: dict) -> str:
    """Findet die Ollama-URL aus Providern: Embedding → Extraction → erster Ollama."""
    emb = cfg.get("embedding", {})
    emb_prov = next((p for p in cfg.get("providers", []) if p.get("id") == emb.get("provider_id")), {})
    if emb_prov.get("type") == "ollama" and emb_prov.get("url"):
        return emb_prov["url"]

    # Extraction-Provider
    mem = cfg.get("memory", {})
    e_llm_id = mem.get("extraction_llm", "")
    if e_llm_id:
        e_llm, e_prov = _resolve_llm(e_llm_id, cfg)
        if e_prov.get("type") == "ollama" and e_prov.get("url"):
            return e_prov["url"]

    # Erster Ollama-Provider
    for p in cfg.get("providers", []):
        if p.get("type") == "ollama" and p.get("url"):
            return p["url"]

    return ""


def _find_references(entity_type: str, entity_id: str, cfg: dict) -> list[str]:
    """Findet alle Referenzen auf eine Entity (provider oder llm).

    Returns Liste von Strings wie "User benni (Primary)", "LLM claude-primary", etc.
    """
    refs: list[str] = []

    if entity_type == "provider":
        # Welche LLMs referenzieren diesen Provider?
        for llm in cfg.get("llms", []):
            if llm.get("provider_id") == entity_id:
                refs.append(f"LLM: {llm.get('name', llm['id'])}")
        # Embedding
        emb = cfg.get("embedding", {})
        if emb.get("provider_id") == entity_id:
            refs.append("Embedding (Primary)")
        if emb.get("fallback_provider_id") == entity_id:
            refs.append("Embedding (Fallback)")

    elif entity_type == "llm":
        # Welche User referenzieren dieses LLM?
        for user in cfg.get("users", []):
            uid = user.get("id", "?")
            if user.get("primary_llm") == entity_id:
                refs.append(f"User {uid} (Primary)")
            if user.get("fallback_llm") == entity_id:
                refs.append(f"User {uid} (Fallback)")
        # Memory global
        mem = cfg.get("memory", {})
        if mem.get("extraction_llm") == entity_id:
            refs.append("Memory Extraction (Global)")
        if mem.get("extraction_llm_fallback") == entity_id:
            refs.append("Memory Extraction Fallback (Global)")

    return refs


def load_config() -> dict:
    if CONF_FILE.exists():
        try:
            cfg = json.loads(CONF_FILE.read_text(encoding="utf-8"))
            # Embeddings-Use-Case entfernen (wurde in separate Sektion ausgelagert)
            cfg.get("use_cases", {}).pop("embeddings", None)
            # Migration von alter Struktur
            if _migrate_config(cfg):
                save_config(cfg)
            if _migrate_providers_v2(cfg):
                save_config(cfg)
            # Dream-Config mit Defaults auffüllen falls fehlend
            dream_defaults = DEFAULT_CONFIG["dream"]
            if "dream" not in cfg:
                cfg["dream"] = dict(dream_defaults)
            else:
                for k, v in dream_defaults.items():
                    cfg["dream"].setdefault(k, v)
            _ensure_system_users(cfg)
            _ensure_user_defaults(cfg)
            return cfg
        except Exception:
            pass
    cfg = dict(DEFAULT_CONFIG)
    cfg["providers"] = list(DEFAULT_CONFIG["providers"])
    cfg["llms"] = list(DEFAULT_CONFIG["llms"])
    cfg["users"] = list(DEFAULT_CONFIG["users"])
    _ensure_system_users(cfg)
    _ensure_user_defaults(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    to_save = cfg
    CONF_FILE.write_text(json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8")


def read_recent_logs(category: str, sub: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Liest die letzten N Einträge (neueste zuerst) einer Log-Kategorie."""
    import glob
    pattern = str((LOG_ROOT / category / sub / "*.jsonl") if sub else (LOG_ROOT / category / "*.jsonl"))
    files = sorted(glob.glob(pattern), reverse=True)
    records: list[dict] = []
    for filepath in files:
        try:
            lines = Path(filepath).read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
            if len(records) >= limit:
                return records
    return records


# ── Auth-Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Gibt Auth-Status und Modus zurück. Immer erreichbar (kein Auth-Guard)."""
    mode = "ingress" if _auth.IS_INGRESS_MODE else "standalone"
    authenticated = _auth.is_authenticated(request)
    return {"authenticated": authenticated, "mode": mode}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """
    Standalone-Login mit Token.
    Body: {"token": "..."}
    Bei Erfolg: setzt HTTP-Only Cookie haana_session (7 Tage).
    """
    if _auth.IS_INGRESS_MODE:
        # Im Ingress-Modus ist kein Login nötig
        return {"ok": True, "mode": "ingress"}

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiger JSON-Body")

    provided = body.get("token", "")
    expected = _auth.get_admin_token()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(401, "Ungültiger Token")

    response = JSONResponse({"ok": True, "mode": "standalone"})
    response.set_cookie(
        key=_auth.COOKIE_NAME,
        value=expected,
        max_age=7 * 24 * 3600,  # 7 Tage
        httponly=True,
        samesite="lax",
        secure=False,  # kein HTTPS im lokalen Netz erzwingen
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    """Löscht den Session-Cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=_auth.COOKIE_NAME, samesite="lax")
    return response


# ── HTML ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "instances": get_all_instances(),
        "base_path": ingress_path + "/" if ingress_path else "/",
    })


# ── API: Konversationen ───────────────────────────────────────────────────────

@app.get("/api/instances")
async def get_instances():
    result = []
    for inst in get_all_instances():
        inst_dir = LOG_ROOT / "conversations" / inst
        count = sum(1 for _ in inst_dir.glob("*.jsonl")) if inst_dir.exists() else 0
        result.append({"name": inst, "log_days": count})
    return result


@app.get("/api/conversations/{instance}")
async def get_conversations(instance: str, limit: int = 50):
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")
    records = read_recent_logs("conversations", instance, limit)
    return records


# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.get("/api/logs/{category}")
async def get_logs(category: str, limit: int = 100):
    valid = {"memory-ops", "tool-calls", "llm-calls"}
    if category not in valid:
        raise HTTPException(400, f"Kategorie muss eine von {valid} sein")
    return read_recent_logs(category, limit=limit)


_SCOPE_RE = re.compile(r"^(all|system|conversations(:[a-zA-Z0-9_-]+)?)$")


@app.get("/api/logs-download")
async def download_logs(scope: str = "all"):
    """Erstellt ein ZIP mit Logs. scope: all | system | conversations | conversations:{instance}"""
    if not _SCOPE_RE.match(scope):
        raise HTTPException(400, "Ungültiger Scope (erlaubt: all, system, conversations, conversations:<id>)")
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if scope in ("all", "system"):
            for cat in ("memory-ops", "tool-calls", "llm-calls"):
                cat_dir = LOG_ROOT / cat
                if cat_dir.exists():
                    for f in sorted(cat_dir.glob("*.jsonl")):
                        zf.write(f, f"system-logs/{cat}/{f.name}")

        if scope == "all" or scope.startswith("conversations"):
            conv_dir = LOG_ROOT / "conversations"
            if conv_dir.exists():
                # scope=conversations:benni → nur benni
                inst_filter = scope.split(":", 1)[1] if ":" in scope else None
                for inst_dir in sorted(conv_dir.iterdir()):
                    if not inst_dir.is_dir():
                        continue
                    if inst_filter and inst_dir.name != inst_filter:
                        continue
                    for f in sorted(inst_dir.glob("*.jsonl")):
                        zf.write(f, f"conversations/{inst_dir.name}/{f.name}")

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"haana-logs-{scope.replace(':', '-')}-{ts}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.delete("/api/logs-delete")
async def delete_logs(request: Request):
    """Löscht Logs. Body: {"scope": "all"|"system"|"conversations"|"conversations:benni"}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    scope = body.get("scope", "all")
    if not _SCOPE_RE.match(scope):
        raise HTTPException(400, "Ungültiger Scope")
    deleted = 0

    if scope in ("all", "system"):
        for cat in ("memory-ops", "tool-calls", "llm-calls"):
            cat_dir = LOG_ROOT / cat
            if cat_dir.exists():
                for f in cat_dir.glob("*.jsonl"):
                    f.unlink()
                    deleted += 1

    if scope == "all" or scope.startswith("conversations"):
        conv_dir = LOG_ROOT / "conversations"
        if conv_dir.exists():
            inst_filter = scope.split(":", 1)[1] if ":" in scope else None
            for inst_dir in sorted(conv_dir.iterdir()):
                if not inst_dir.is_dir():
                    continue
                if inst_filter and inst_dir.name != inst_filter:
                    continue
                for f in inst_dir.glob("*.jsonl"):
                    f.unlink()
                    deleted += 1
                # Leeres Verzeichnis aufräumen
                if not any(inst_dir.iterdir()):
                    inst_dir.rmdir()

        # Rebuild-Progress auch löschen
        progress_dir = LOG_ROOT / ".rebuild-progress"
        if progress_dir.exists():
            if inst_filter:
                pf = progress_dir / f"{inst_filter}.json"
                if pf.exists():
                    pf.unlink()
            elif scope in ("all", "conversations"):
                for pf in progress_dir.glob("*.json"):
                    pf.unlink()

    import logging as _log
    _log.getLogger(__name__).info(f"[LogDelete] {deleted} Datei(en) gelöscht (scope={scope})")
    return {"ok": True, "deleted": deleted}


# ── API: Konfiguration ────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # Im Addon-Modus: HA URL und Token aus Env-Vars befüllen wenn leer
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    ha_url_env = os.environ.get("HA_URL", "")
    svc = cfg.setdefault("services", {})
    if not svc.get("ha_url") and ha_url_env:
        svc["ha_url"] = ha_url_env
    if not svc.get("ha_token") and supervisor_token:
        svc["ha_token"] = supervisor_token
    return cfg


@app.post("/api/config")
async def post_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")
    save_config(body)
    return {"ok": True}


@app.get("/api/references/{entity_type}/{entity_id}")
async def get_references(entity_type: str, entity_id: str):
    """Gibt alle Referenzen auf eine Entity (provider/llm) zurück."""
    if entity_type not in ("provider", "llm"):
        raise HTTPException(400, "entity_type muss 'provider' oder 'llm' sein")
    cfg = load_config()
    refs = _find_references(entity_type, entity_id, cfg)
    return {"refs": refs, "count": len(refs)}


# ── API: CLAUDE.md ────────────────────────────────────────────────────────────

@app.get("/api/claude-md/{instance}")
async def get_claude_md(instance: str):
    if instance not in get_all_instances():
        raise HTTPException(404, "Instanz nicht gefunden")
    path = INST_DIR / instance / "CLAUDE.md"
    if not path.exists():
        raise HTTPException(404, "CLAUDE.md nicht gefunden")
    return {"content": path.read_text(encoding="utf-8")}


@app.post("/api/claude-md/{instance}")
async def post_claude_md(instance: str, request: Request):
    if instance not in get_all_instances():
        raise HTTPException(404, "Instanz nicht gefunden")
    try:
        body = await request.json()
        content = body.get("content", "")
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")
    path = INST_DIR / instance / "CLAUDE.md"
    if not path.parent.exists():
        raise HTTPException(404, "Instanz-Verzeichnis nicht gefunden")
    path.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.get("/api/claude-md-template/{template_name}")
async def get_claude_md_template(template_name: str):
    """Liefert den Rohinhalt eines CLAUDE.md-Templates (ohne Platzhalter-Ersatz)."""
    safe = re.sub(r"[^a-z0-9\-]", "", template_name.lower())
    tpl_path = TEMPLATES_DIR / f"{safe}.md"
    if not tpl_path.exists():
        tpl_path = TEMPLATES_DIR / "user.md"
    if not tpl_path.exists():
        raise HTTPException(404, "Template nicht gefunden")
    return {"content": tpl_path.read_text(encoding="utf-8"), "template": safe}


# ── API: Setup Wizard ─────────────────────────────────────────────────────────

@app.get("/api/setup-status")
async def setup_status():
    """Erkennt ob die Ersteinrichtung nötig ist (kein Provider / keine User)."""
    cfg = load_config()
    providers = cfg.get("providers", [])
    # Ollama braucht keinen Key – prüfe ob min. 1 Provider mit Key ODER Ollama-URL existiert
    has_provider = any(
        p.get("key") or (p.get("type", "").lower() == "ollama" and p.get("url"))
        for p in providers
    )
    # Explizites Flag: Wizard wurde abgeschlossen
    setup_done = cfg.get("setup_done", False)
    users = [u for u in cfg.get("users", []) if u.get("id") not in _SYSTEM_USER_IDS]

    if setup_done:
        return {"needs_setup": False}
    if not providers or not has_provider:
        return {"needs_setup": True, "step": 1}
    if not users:
        return {"needs_setup": True, "step": 2}
    return {"needs_setup": False}


@app.get("/api/supervisor/addons")
async def supervisor_addons():
    """Listet installierte Add-ons via HA Supervisor API (nur im Add-on-Modus)."""
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        return {"ok": False, "addon_mode": False, "addons": []}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "http://supervisor/addons",
                headers={"Authorization": f"Bearer {supervisor_token}"},
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            addons = data.get("addons", [])
            result = [
                {"slug": a.get("slug", ""), "name": a.get("name", ""), "state": a.get("state", "")}
                for a in addons
            ]
            return {"ok": True, "addon_mode": True, "addons": result}
    except Exception as e:
        return {"ok": False, "addon_mode": True, "addons": [], "error": str(e)[:200]}


@app.get("/api/supervisor/self")
async def supervisor_self():
    """Liefert Infos über das eigene Add-on (Hostname, Ingress-URL etc.)."""
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        return {"ok": False, "error": "Not running as add-on"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {supervisor_token}"},
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            return {
                "ok": True,
                "hostname": data.get("hostname", ""),
                "ingress_url": data.get("ingress_url", ""),
                "port": 8080,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/setup/current-config")
async def setup_current_config():
    """Gibt bestehende Config für Vorausfüllung im Extend-Modus zurück (Keys maskiert)."""
    cfg = load_config()

    def _mask_key(key: str) -> str:
        """Maskiert einen API-Key: erste 4 + '...' + letzte 4 Zeichen."""
        if not key or len(key) < 9:
            return "***"
        return key[:4] + "..." + key[-4:]

    providers_out = []
    for p in cfg.get("providers", []):
        providers_out.append({
            "id":         p.get("id", ""),
            "type":       p.get("type", ""),
            "name":       p.get("name", ""),
            "url":        p.get("url", ""),
            "key_masked": _mask_key(p.get("key", "")),
        })

    llms_out = []
    for llm in cfg.get("llms", []):
        llms_out.append({
            "name":     llm.get("name", ""),
            "type":     llm.get("type", ""),
            "provider": llm.get("provider", ""),
            "model":    llm.get("model", ""),
        })

    users_out = []
    for u in cfg.get("users", []):
        if u.get("id") in _SYSTEM_USER_IDS:
            continue
        users_out.append({
            "id":           u.get("id", ""),
            "display_name": u.get("display_name", ""),
            "primary_llm":  u.get("primary_llm", ""),
            "fallback_llm": u.get("fallback_llm", ""),
            "language":     u.get("language", "de"),
        })

    ha_assist_llm   = ""
    ha_advanced_llm = ""
    for u in cfg.get("users", []):
        if u.get("id") == "ha-assist":
            ha_assist_llm = u.get("primary_llm", "")
        if u.get("id") == "ha-advanced":
            ha_advanced_llm = u.get("primary_llm", "")

    return {
        "providers":       providers_out,
        "llms":            llms_out,
        "users":           users_out,
        "ha_assist_llm":   ha_assist_llm,
        "ha_advanced_llm": ha_advanced_llm,
        "extraction_llm":  cfg.get("memory", {}).get("extraction_llm", ""),
        "dream_enabled":   cfg.get("dream", {}).get("enabled", False),
    }


@app.post("/api/setup/reset")
async def setup_reset(request: Request):
    """Setzt setup_done = false; bei mode='fresh' wird die Config geleert."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    mode = body.get("mode", "extend")
    cfg  = load_config()

    if mode == "fresh":
        cfg["providers"] = []
        cfg["llms"]      = []
        # Non-System-User löschen; System-User bleiben erhalten
        cfg["users"] = [u for u in cfg.get("users", []) if u.get("id") in _SYSTEM_USER_IDS]

    cfg["setup_done"] = False
    save_config(cfg)
    return {"ok": True, "mode": mode}


@app.post("/api/setup/complete")
async def setup_complete(request: Request):
    """Schließt den Setup-Wizard ab: Provider, LLMs, User, System-Agents konfigurieren."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    cfg  = load_config()
    mode = body.get("mode", "fresh")  # "fresh" | "extend"

    # 1. Providers setzen / mergen
    if "providers" in body:
        if mode == "extend":
            # Extend-Modus: bestehende Provider beibehalten, neue hinzufügen / Keys aktualisieren
            def _prov_merge_key(p: dict) -> tuple:
                return (p.get("type", ""), p.get("url", "") or p.get("name", ""))
            existing = cfg.get("providers", [])
            existing_map = {_prov_merge_key(p): p for p in existing}
            for bp in body["providers"]:
                mk = _prov_merge_key(bp)
                if mk in existing_map:
                    ep = existing_map[mk]
                    # Key nur überschreiben wenn nicht leer (leerer Key = "nicht ändern")
                    new_key = bp.get("key", "")
                    if new_key:
                        ep["key"] = new_key
                    if bp.get("url"):
                        ep["url"] = bp["url"]
                    if bp.get("name"):
                        ep["name"] = bp["name"]
                else:
                    existing.append(bp)
                    existing_map[mk] = bp
            cfg["providers"] = list(existing_map.values())
        else:
            cfg["providers"] = body["providers"]

    # 2. LLMs setzen
    if "llms" in body:
        cfg["llms"] = body["llms"]

    # 3. User anlegen (mit Defaults)
    if "users" in body:
        existing_ports = [u.get("api_port", 0) for u in cfg.get("users", [])]
        for wu in body["users"]:
            uid = wu.get("id", "").strip().lower()
            if not uid or uid in _SYSTEM_USER_IDS:
                continue
            # Nur anlegen wenn noch nicht vorhanden (extend: bestehende User bleiben)
            if any(u["id"] == uid for u in cfg.get("users", [])):
                continue
            port = _find_free_port(existing_ports)
            existing_ports.append(port)
            user = {
                "id":                  uid,
                "display_name":        wu.get("display_name", uid.capitalize()),
                "role":                wu.get("role", "admin"),
                "language":            wu.get("language", "de"),
                "primary_llm":         wu.get("primary_llm", ""),
                "fallback_llm":        wu.get("fallback_llm", ""),
                "ha_user":             wu.get("ha_user", uid),
                "whatsapp_phone":      wu.get("whatsapp_phone", ""),
                "api_port":            port,
                "container_name":      f"haana-instanz-{uid}-1",
                "claude_md_template":  wu.get("claude_md_template", "admin" if wu.get("role") == "admin" else "user"),
                "caldav_url": "", "caldav_user": "", "caldav_pass": "",
                "imap_host": "", "imap_port": 993, "imap_user": "", "imap_pass": "",
                "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "",
            }
            cfg.setdefault("users", []).append(user)

    # 4. System-User LLMs zuweisen
    ha_assist_llm = body.get("ha_assist_llm", "")
    ha_advanced_llm = body.get("ha_advanced_llm", "")
    for u in cfg.get("users", []):
        if u["id"] == "ha-assist" and ha_assist_llm:
            u["primary_llm"] = ha_assist_llm
        if u["id"] == "ha-advanced" and ha_advanced_llm:
            u["primary_llm"] = ha_advanced_llm

    # 5. Memory extraction LLM
    extraction_llm = body.get("extraction_llm", "")
    if extraction_llm:
        cfg.setdefault("memory", {})["extraction_llm"] = extraction_llm

    # 6. Dream-Einstellungen
    if "dream_enabled" in body:
        cfg.setdefault("dream", {})["enabled"] = bool(body["dream_enabled"])

    # 7. Services (MCP etc.)
    if "services" in body:
        svc = cfg.setdefault("services", {})
        for k, v in body["services"].items():
            svc[k] = v

    # System-User sicherstellen
    _ensure_system_users(cfg)
    _ensure_user_defaults(cfg)

    # 8. CLAUDE.md für jeden neuen User generieren
    for u in cfg.get("users", []):
        uid = u.get("id", "")
        if not uid:
            continue
        claude_md_dir = INST_DIR / uid
        claude_md_dir.mkdir(parents=True, exist_ok=True)
        claude_md_path = claude_md_dir / "CLAUDE.md"
        if not claude_md_path.exists():
            content = _render_claude_md(
                u.get("claude_md_template", "user"),
                u.get("display_name", uid.capitalize()),
                uid,
                u.get("ha_user", uid),
                u.get("language", "de"),
            )
            claude_md_path.write_text(content, encoding="utf-8")

    # 9. Setup als abgeschlossen markieren + Speichern
    cfg["setup_done"] = True
    save_config(cfg)

    # 10. Agents starten
    started = []
    errors = []
    for u in cfg.get("users", []):
        uid = u.get("id", "")
        if not uid:
            continue
        try:
            result = await _agent_manager.start_agent(u, cfg)
            if result.get("ok"):
                started.append(uid)
            else:
                errors.append({"id": uid, "error": result.get("error", "unbekannt")})
        except Exception as e:
            errors.append({"id": uid, "error": str(e)[:200]})

    # Rebuild-State erweitern
    for u in cfg.get("users", []):
        uid = u.get("id", "")
        if uid and uid not in _rebuild:
            _rebuild[uid] = {"status": "idle", "done": 0, "total": 0, "started": 0.0, "error": ""}

    return {"ok": True, "started": started, "errors": errors}


# ── API: Status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    import httpx
    cfg = load_config()
    qdrant_url = cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333")
    ollama_url = _find_ollama_url(cfg)

    status: dict = {"qdrant": "unknown", "ollama": "unknown", "logs": {}}

    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.get(f"{qdrant_url}/collections")
            colls = r.json().get("result", {}).get("collections", [])
            coll_names = [c["name"] for c in colls]
            # Prüfe ob Collections leer sind (für Rebuild-Empfehlung)
            total_vectors = 0
            configured_dims = cfg.get("embedding", {}).get("dims", 1024)
            dims_mismatch = False
            for cname in coll_names:
                try:
                    cr = await client.get(f"{qdrant_url}/collections/{cname}")
                    res = cr.json().get("result", {})
                    total_vectors += res.get("points_count", 0) or res.get("vectors_count", 0) or 0
                    # Dimensions-Check: Collection-Dimension vs. konfigurierte
                    coll_dim = (res.get("config", {}).get("params", {})
                                .get("vectors", {}).get("size", 0))
                    if coll_dim and coll_dim != configured_dims:
                        dims_mismatch = True
                except Exception:
                    pass
            # Konversations-Logs vorhanden?
            conv_files = _glob.glob(str(LOG_ROOT / "conversations" / "**" / "*.jsonl"), recursive=True)
            has_logs = len(conv_files) > 0
            status["qdrant"] = {
                "ok": True,
                "collections": coll_names,
                "rebuild_suggested": has_logs and total_vectors == 0,
                "dims_mismatch": dims_mismatch,
                "configured_dims": configured_dims,
            }
        except Exception as e:
            status["qdrant"] = {"ok": False, "error": str(e)}

        if ollama_url:
            try:
                r = await client.get(f"{ollama_url}/api/tags")
                models = [m["name"] for m in r.json().get("models", [])]
                status["ollama"] = {"ok": True, "models": models}
            except Exception as e:
                status["ollama"] = {"ok": False, "error": str(e)}

    # Log-Statistiken
    for inst in get_all_instances():
        inst_log = LOG_ROOT / "conversations" / inst
        if inst_log.exists():
            days = sorted(inst_log.glob("*.jsonl"), reverse=True)
            status["logs"][inst] = {
                "days": len(days),
                "latest": days[0].name.replace(".jsonl", "") if days else None,
            }

    # Prescriptive Hints
    hints: list[dict] = []
    providers = cfg.get("providers", [])
    has_provider = any(
        p.get("key") or (p.get("type", "").lower() == "ollama" and p.get("url"))
        for p in providers
    )
    if not providers or not has_provider:
        hints.append({"type": "error", "msg": "no_provider_key", "action": "config_providers"})
    non_system_users = [u for u in cfg.get("users", []) if u.get("id") not in _SYSTEM_USER_IDS]
    if not non_system_users:
        hints.append({"type": "warning", "msg": "no_users", "action": "users"})
    # Agents offline prüfen
    if non_system_users and _agent_manager:
        all_offline = True
        for u in non_system_users:
            s = _agent_manager.agent_status(u["id"])
            if s and s not in ("offline", "stopped", "unknown", "not_found"):
                all_offline = False
                break
        if all_offline:
            hints.append({"type": "info", "msg": "agents_offline", "action": "users"})
    status["hints"] = hints

    return status


# ── Chat-Proxy (Webchat → Agent-API) ─────────────────────────────────────────

@app.post("/api/chat/{instance}")
async def chat_proxy(instance: str, request: Request):
    """
    Sendet eine Nachricht an eine Agent-Instanz und gibt die Antwort zurück.
    Proxy zur Agent-API (core/api.py, läuft im Agent-Container).
    """
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message darf nicht leer sein")

    agent_url = _get_agent_url(instance)
    if not agent_url:
        raise HTTPException(503, f"Keine Agent-URL für '{instance}' konfiguriert")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{agent_url}/chat",
                json={"message": message, "channel": "webchat"},
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"Agent '{instance}' nicht erreichbar (läuft der Container?)")
    except httpx.TimeoutException:
        raise HTTPException(504, "Agent hat nicht rechtzeitig geantwortet")
    except Exception as e:
        raise HTTPException(502, f"Agent-Fehler: {str(e)[:200]}")


@app.get("/api/memory-stats")
async def memory_stats():
    """
    Liefert pro Instanz: Konversations-Logs (Zeilen), Qdrant-Vektoren pro Scope.
    Wird für Rebuild-Checkboxen verwendet.
    """
    import httpx
    cfg = load_config()
    qdrant_url = cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333")

    # Qdrant-Vektoren pro Collection laden
    coll_vectors: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{qdrant_url}/collections")
            colls = r.json().get("result", {}).get("collections", [])
            for c in colls:
                try:
                    cr = await client.get(f"{qdrant_url}/collections/{c['name']}")
                    _cr = cr.json().get("result", {})
                    coll_vectors[c["name"]] = _cr.get("points_count", 0) or _cr.get("vectors_count", 0) or 0
                except Exception:
                    coll_vectors[c["name"]] = 0
    except Exception:
        pass

    # Templates ohne write_scopes brauchen keinen Rebuild
    _READ_ONLY_TEMPLATES = {"ha-assist"}

    result = []
    for inst in get_all_instances():
        # Read-only Instanzen (kein eigener Memory) aus Rebuild-Liste ausschließen
        user = next((u for u in cfg.get("users", []) if u["id"] == inst), None)
        if user and user.get("claude_md_template", "") in _READ_ONLY_TEMPLATES:
            continue

        # Log-Zeilen zählen
        log_entries = 0
        log_days = 0
        inst_log = LOG_ROOT / "conversations" / inst
        if inst_log.exists():
            files = list(inst_log.glob("*.jsonl"))
            log_days = len(files)
            for f in files:
                try:
                    log_entries += sum(1 for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip())
                except Exception:
                    pass

        # Qdrant-Vektoren: primärer Scope ist {uid}_memory
        scopes: dict[str, int] = {}
        if user:
            tpl = user.get("claude_md_template", "")
            if tpl == "ha-advanced":
                # ha-advanced schreibt nur household_memory
                scopes["household_memory"] = coll_vectors.get("household_memory", 0)
            else:
                for scope in (f"{inst}_memory", "household_memory"):
                    scopes[scope] = coll_vectors.get(scope, 0)
        else:
            scopes[f"{inst}_memory"] = coll_vectors.get(f"{inst}_memory", 0)
            scopes["household_memory"] = coll_vectors.get("household_memory", 0)

        total_vectors = sum(scopes.values())
        result.append({
            "instance": inst,
            "log_entries": log_entries,
            "log_days": log_days,
            "scopes": scopes,
            "total_vectors": total_vectors,
            "rebuild_suggested": log_entries > 0 and total_vectors == 0,
        })

    return result


# ── Instanz-Steuerung (Container stop/restart) ────────────────────────────────

@app.post("/api/instances/{instance}/restart")
async def restart_instance(instance: str):
    """Agent-Instanz neu starten (mit aktueller Config)."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    # Container komplett neu erstellen damit Env-Vars aktualisiert werden
    cfg = load_config()
    for user in cfg.get("users", []):
        if user["id"] == instance:
            return await _agent_manager.start_agent(user, cfg)
    # Fallback: einfacher Restart für statische Instanzen
    return await _agent_manager.restart_agent(instance)


@app.post("/api/instances/{instance}/stop")
async def stop_instance(instance: str):
    """Agent-Instanz graceful stoppen."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    return await _agent_manager.stop_agent(instance)


@app.post("/api/instances/{instance}/force-stop")
async def force_stop_instance(instance: str):
    """Agent-Instanz sofort beenden (laufende Memory-Extraktion geht verloren)."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    return await _agent_manager.stop_agent(instance, force=True)


@app.post("/api/instances/restart-all")
async def restart_all_instances():
    """Alle Agent-Instanzen mit aktueller Config neu starten."""
    cfg = load_config()
    results = {}

    # Dynamische User-Agents
    for user in cfg.get("users", []):
        uid = user["id"]
        result = await _agent_manager.start_agent(user, cfg)
        results[uid] = result

    # Statische Instanzen (ohne User-Config)
    for inst in INSTANCES:
        if inst not in results:
            result = await _agent_manager.restart_agent(inst)
            results[inst] = result

    all_ok = all(r.get("ok", False) for r in results.values())
    return {"ok": all_ok, "results": results}


@app.post("/api/qdrant/restart")
async def restart_qdrant():
    """Qdrant-Container neu starten (nur im Standalone-Modus)."""
    if not _docker_client:
        return {"ok": False, "error": "Docker nicht verfügbar"}
    try:
        c = _docker_client.containers.get("haana-qdrant-1")
        c.restart(timeout=10)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.delete("/api/qdrant/collections/{name}")
async def delete_qdrant_collection(name: str):
    """Löscht eine Qdrant-Collection."""
    import httpx
    cfg = load_config()
    qdrant_url = cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{qdrant_url}/collections/{name}")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ── Konversations-Logs direkt lesen/schreiben (Editieren) ─────────────────────

@app.get("/api/conversations/{instance}/files")
async def list_conversation_files(instance: str):
    """Listet alle vorhandenen Datumsdateien für eine Instanz."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    inst_log = LOG_ROOT / "conversations" / instance
    if not inst_log.exists():
        return []
    files = sorted(inst_log.glob("*.jsonl"), reverse=True)
    result = []
    for f in files:
        try:
            lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            result.append({"date": f.stem, "entries": len(lines), "size_kb": round(f.stat().st_size / 1024, 1)})
        except Exception:
            result.append({"date": f.stem, "entries": 0, "size_kb": 0})
    return result


@app.get("/api/conversations/{instance}/raw/{date}")
async def get_conversation_raw(instance: str, date: str):
    """Gibt den rohen JSONL-Inhalt einer Datums-Log-Datei zurück."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "Ungültiges Datumsformat (erwartet YYYY-MM-DD)")
    path = LOG_ROOT / "conversations" / instance / f"{date}.jsonl"
    if not path.exists():
        raise HTTPException(404, "Datei nicht gefunden")
    return {"content": path.read_text(encoding="utf-8"), "entries": sum(1 for ln in path.read_text().splitlines() if ln.strip())}


@app.put("/api/conversations/{instance}/raw/{date}")
async def put_conversation_raw(instance: str, date: str, request: Request):
    """Überschreibt eine Datums-Log-Datei mit neuem Inhalt."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "Ungültiges Datumsformat")
    try:
        body = await request.json()
        content = body.get("content", "")
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")
    path = LOG_ROOT / "conversations" / instance / f"{date}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    entries = sum(1 for ln in content.splitlines() if ln.strip())
    return {"ok": True, "entries": entries}


@app.post("/api/test-connection")
async def test_connection(request: Request):
    """
    Testet eine Verbindung zu einem Dienst.
    Body: {"type": "qdrant"|"ollama"|"http", "url": "..."}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    url   = (body.get("url") or "").strip()
    type_ = (body.get("type") or "http").strip()

    if not url:
        raise HTTPException(400, "url fehlt")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if type_ == "qdrant":
                r = await client.get(f"{url}/collections")
                colls = r.json().get("result", {}).get("collections", [])
                return {"ok": True, "detail": f"{len(colls)} Collection(s)"}
            elif type_ == "ollama":
                r = await client.get(f"{url}/api/tags")
                models = [m["name"] for m in r.json().get("models", [])]
                return {"ok": True, "detail": f"{len(models)} Modell(e)"}
            else:
                r = await client.get(url)
                return {"ok": r.status_code < 400, "detail": f"HTTP {r.status_code}"}
    except httpx.ConnectError as e:
        return {"ok": False, "detail": f"Verbindung abgelehnt: {str(e)[:100]}"}
    except httpx.TimeoutException:
        return {"ok": False, "detail": "Timeout (>5s)"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/api/test-ha")
async def test_ha(request: Request):
    """Testet Home Assistant URL + Long-Lived Token."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    ha_url   = (body.get("ha_url")   or "").rstrip("/")
    ha_token = (body.get("ha_token") or "").strip()
    ha_url   = ha_url   or os.environ.get("HA_URL", "").rstrip("/")
    ha_token = ha_token or os.environ.get("SUPERVISOR_TOKEN", "")
    if not ha_url:
        return {"ok": False, "detail": "ha_url fehlt"}
    if not ha_token:
        return {"ok": False, "detail": "ha_token fehlt"}

    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{ha_url}/api/",
                headers={"Authorization": f"Bearer {ha_token}"},
            )
            if r.status_code == 401:
                return {"ok": False, "detail": "Token ungültig (401 Unauthorized)"}
            if r.status_code == 200:
                msg = r.json().get("message", "API erreichbar")
                return {"ok": True, "detail": msg}
            return {"ok": r.status_code < 400, "detail": f"HTTP {r.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "detail": "Verbindung abgelehnt – URL erreichbar?"}
    except httpx.TimeoutException:
        return {"ok": False, "detail": "Timeout (>8s)"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/api/test-ha-mcp")
async def test_ha_mcp(request: Request):
    """Testet den HA MCP Server SSE-Endpunkt."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    mcp_url = (body.get("mcp_url") or "").strip()
    token   = (body.get("token")   or "").strip()
    token   = token or os.environ.get("SUPERVISOR_TOKEN", "")

    if not mcp_url:
        return {"ok": False, "detail": "MCP URL fehlt"}
    if not token:
        return {"ok": False, "detail": "Token fehlt"}

    mcp_type = (body.get("mcp_type") or "extended").strip()

    import httpx
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=8.0, read=5.0, write=5.0, pool=5.0)
        ) as client:
            if mcp_type == "builtin":
                # Built-in HA MCP: SSE (GET), Bearer auth
                headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
                async with client.stream("GET", mcp_url, headers=headers) as r:
                    ct = r.headers.get("content-type", "")
                    sc = r.status_code
                    if sc == 401:
                        return {"ok": False, "detail": "Token ungültig (401 Unauthorized)"}
                    if sc == 404:
                        return {"ok": False, "detail": "Endpunkt nicht gefunden (404) – MCP Server in HA aktiviert?"}
                    if sc in (200, 206):
                        if "event-stream" in ct:
                            return {"ok": True, "detail": "MCP Server erreichbar ✓ (SSE)"}
                        return {"ok": True, "detail": f"Erreichbar · HTTP {sc} · {ct or 'kein Content-Type'}"}
                    return {"ok": sc < 400, "detail": f"HTTP {sc}"}
            else:
                # Extended ha-mcp: Streamable HTTP (POST), MCP initialize
                headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                init_msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26",
                                       "capabilities": {},
                                       "clientInfo": {"name": "haana-test", "version": "1.0"}}}
                r = await client.post(mcp_url, json=init_msg, headers=headers)
                sc = r.status_code
                if sc == 401:
                    return {"ok": False, "detail": "Token ungültig (401)"}
                if sc == 404:
                    return {"ok": False, "detail": "Endpunkt nicht gefunden (404)"}
                if sc in (200, 202):
                    return {"ok": True, "detail": f"MCP Server erreichbar ✓ (HTTP, Status {sc})"}
                # SSE-formatted response (ha-mcp returns SSE even over POST)
                ct = r.headers.get("content-type", "")
                if "event-stream" in ct:
                    return {"ok": True, "detail": "MCP Server erreichbar ✓ (SSE-over-HTTP)"}
                return {"ok": sc < 400, "detail": f"HTTP {sc}"}
    except httpx.ConnectError:
        return {"ok": False, "detail": "Verbindung abgelehnt – HA erreichbar?"}
    except httpx.ReadTimeout:
        return {"ok": True, "detail": "MCP Server erreichbar ✓ (Timeout nach Connect – normal bei SSE)"}
    except httpx.TimeoutException:
        return {"ok": False, "detail": "Connect-Timeout – HA erreichbar?"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


@app.get("/api/ha-pipelines")
async def ha_pipelines():
    """Listet verfügbare Voice-Pipelines (Sprachassistenten) aus Home Assistant auf."""
    cfg = load_config()
    ha_url   = cfg.get("services", {}).get("ha_url",   "").rstrip("/")
    ha_token = cfg.get("services", {}).get("ha_token", "").strip()
    if not ha_url or not ha_token:
        return {"ok": False, "error": "HA URL oder Token nicht konfiguriert", "pipelines": []}
    import websockets, ssl as _ssl
    try:
        # HA Pipelines sind nur per WebSocket abrufbar
        ws_url = ha_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/websocket"
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        async with websockets.connect(ws_url, ssl=ssl_ctx, open_timeout=8) as ws:
            # Auth-Phase
            msg = json.loads(await ws.recv())  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await ws.recv())  # auth_ok oder auth_invalid
            if msg.get("type") != "auth_ok":
                return {"ok": False, "error": "HA Token ungültig", "pipelines": []}
            # Pipelines abrufen
            await ws.send(json.dumps({"id": 1, "type": "assist_pipeline/pipeline/list"}))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                return {"ok": False, "error": "Pipelines nicht verfügbar", "pipelines": []}
            raw_pipelines = msg.get("result", {}).get("pipelines", [])
            pipelines = []
            for p in raw_pipelines:
                pipelines.append({
                    "id":           p.get("id", ""),
                    "name":         p.get("name", ""),
                    "stt_engine":   p.get("stt_engine", ""),
                    "stt_language":  p.get("stt_language", ""),
                    "tts_engine":   p.get("tts_engine", ""),
                    "tts_language":  p.get("tts_language", ""),
                    "tts_voice":    p.get("tts_voice", ""),
                })
            return {"ok": True, "pipelines": pipelines}
    except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError):
        return {"ok": False, "error": "HA nicht erreichbar", "pipelines": []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "pipelines": []}


@app.get("/api/ha-stt-tts")
async def ha_stt_tts():
    """Listet verfügbare STT- und TTS-Entitäten aus Home Assistant auf."""
    cfg = load_config()
    ha_url   = cfg.get("services", {}).get("ha_url",   "").rstrip("/")
    ha_token = cfg.get("services", {}).get("ha_token", "").strip()
    if not ha_url or not ha_token:
        return {"ok": False, "error": "HA URL oder Token nicht konfiguriert", "stt": [], "tts": []}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{ha_url}/api/states",
                headers={"Authorization": f"Bearer {ha_token}"},
            )
            if r.status_code == 401:
                return {"ok": False, "error": "HA Token ungültig", "stt": [], "tts": []}
            r.raise_for_status()
            stt_entities = []
            tts_entities = []
            for state in r.json():
                eid = state.get("entity_id", "")
                name = state.get("attributes", {}).get("friendly_name", eid)
                if eid.startswith("stt."):
                    stt_entities.append({"id": eid, "name": name})
                elif eid.startswith("tts."):
                    tts_entities.append({"id": eid, "name": name})
            return {"ok": True, "stt": stt_entities, "tts": tts_entities}
    except httpx.ConnectError:
        return {"ok": False, "error": "HA nicht erreichbar", "stt": [], "tts": []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "stt": [], "tts": []}


@app.get("/api/ha-users")
async def ha_users():
    """Listet Home Assistant Person-Entitäten für User-Mapping auf."""
    cfg = load_config()
    ha_url   = (cfg.get("services", {}).get("ha_url", "") or os.environ.get("HA_URL", "")).rstrip("/")
    ha_token = (cfg.get("services", {}).get("ha_token", "") or os.environ.get("SUPERVISOR_TOKEN", "")).strip()
    if not ha_url or not ha_token:
        return {"ok": False, "error": "HA URL oder Token nicht konfiguriert", "users": []}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{ha_url}/api/states",
                headers={"Authorization": f"Bearer {ha_token}"},
            )
            if r.status_code == 401:
                return {"ok": False, "error": "HA Token ungültig", "users": []}
            r.raise_for_status()
            persons = []
            for state in r.json():
                eid = state.get("entity_id", "")
                if eid.startswith("person."):
                    uid  = eid[len("person."):]
                    name = state.get("attributes", {}).get("friendly_name", uid)
                    persons.append({"id": eid, "name": name, "friendly_name": name, "display_name": name})
            return {"ok": True, "users": persons}
    except httpx.ConnectError:
        return {"ok": False, "error": "HA nicht erreichbar", "users": []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "users": []}


@app.get("/api/whatsapp-config")
async def whatsapp_config_endpoint():
    """Liefert WhatsApp-Routing-Konfiguration für die Bridge.
    Pro User wird sowohl die Phone-JID als auch eine optionale LID als Route geliefert,
    da neuere WhatsApp-Versionen LID statt Phone-JID senden."""
    cfg = load_config()
    wa  = cfg.get("whatsapp", {"mode": "separate", "self_prefix": "!h "})
    routes = []
    for user in cfg.get("users", []):
        phone = user.get("whatsapp_phone", "").strip()
        if not phone or user.get("system"):
            continue
        jid = phone if "@" in phone else f"{phone}@s.whatsapp.net"
        uid = user["id"]
        if HAANA_MODE == "addon":
            # Add-on: Agents laufen als Sub-App im selben Prozess
            agent_url = f"http://localhost:8080/agent/{uid}"
        else:
            container = user.get("container_name", f"haana-instanz-{uid}-1")
            port      = user.get("api_port", 8001)
            agent_url = f"http://{container}:{port}"
        target = {"agent_url": agent_url, "user_id": uid}
        routes.append({"jid": jid, **target})
        # Optionale LID als zweite Route registrieren
        lid = user.get("whatsapp_lid", "").strip()
        if lid:
            lid_jid = lid if "@" in lid else f"{lid}@lid"
            routes.append({"jid": lid_jid, **target})
    # STT/TTS-Konfiguration aus services-Sektion für die Bridge bereitstellen
    services = cfg.get("services", {})
    stt = None
    tts = None
    ha_url   = services.get("ha_url", "").strip()
    ha_token = services.get("ha_token", "").strip()
    if ha_url and ha_token:
        stt_entity = services.get("stt_entity", "").strip()
        tts_entity = services.get("tts_entity", "").strip()
        if stt_entity:
            stt = {
                "ha_url":       ha_url,
                "ha_token":     ha_token,
                "stt_entity":   stt_entity,
                "stt_language": services.get("stt_language", "de-DE"),
            }
        if tts_entity:
            tts = {
                "ha_url":       ha_url,
                "ha_token":     ha_token,
                "tts_entity":   tts_entity,
                "tts_language": services.get("stt_language", "de-DE"),
                "tts_voice":    services.get("tts_voice", ""),
                "tts_also_text": services.get("tts_also_text", False),
            }

    return {"mode": wa.get("mode", "separate"), "self_prefix": wa.get("self_prefix", "!h "), "routes": routes, "stt": stt, "tts": tts}


# ── WhatsApp Bridge Proxy (Status / QR / Logout) ──────────────────────────────

_WA_BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge:3001")


@app.get("/api/whatsapp-status")
async def whatsapp_status():
    """Proxy: Bridge-Verbindungsstatus abfragen."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{_WA_BRIDGE_URL}/status")
            return r.json()
    except httpx.ConnectError:
        return {"status": "offline", "error": "Bridge nicht erreichbar"}
    except Exception as e:
        return {"status": "offline", "error": str(e)[:200]}


@app.get("/api/whatsapp-qr")
async def whatsapp_qr():
    """Proxy: aktuellen QR-Code als Base64 Data-URL abrufen."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{_WA_BRIDGE_URL}/qr")
            return r.json()
    except httpx.ConnectError:
        return {"error": "Bridge nicht erreichbar", "status": "offline"}
    except Exception as e:
        return {"error": str(e)[:200], "status": "offline"}


@app.post("/api/whatsapp-logout")
async def whatsapp_logout():
    """Proxy: WhatsApp-Session trennen."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{_WA_BRIDGE_URL}/logout")
            return r.json()
    except httpx.ConnectError:
        return {"ok": False, "error": "Bridge nicht erreichbar"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _is_trivial_entry(rec: dict) -> bool:
    """Prüft ob ein Konversations-Eintrag trivial ist (kein Memory-Wert)."""
    user_msg = (rec.get("user") or "").strip()
    asst_msg = (rec.get("assistant") or "").strip()
    # Leere Nachrichten
    if not user_msg and not asst_msg:
        return True
    # Sehr kurze User-Nachrichten ohne Substanz
    if len(user_msg) < 15 and not asst_msg:
        return True
    # Typische Kommando-Patterns (Licht, Status, Hallo etc.)
    _trivial_patterns = [
        r"^(hallo|hi|hey|moin|guten (morgen|tag|abend)|tschüss|bye|danke|ok|ja|nein|stop|abbrechen)\.?!?$",
        r"^(licht|lampe|rollo|jalousie|heizung|temperatur|status|wetter)\b.{0,30}$",
        r"^(schalte|mach|stell|dreh|öffne|schließe)\b.{0,40}$",
    ]
    lower = user_msg.lower()
    for pat in _trivial_patterns:
        if re.match(pat, lower):
            return True
    return False


def _scan_rebuild_entries(instance: str, skip_trivial: bool = True) -> dict:
    """Scannt Logs und gibt Statistiken + gefilterte Einträge zurück."""
    conv_files = sorted(
        _glob.glob(str(LOG_ROOT / "conversations" / instance / "*.jsonl"))
    )
    total_raw = 0
    total_filtered = 0
    entries = []  # (file_path, line_index, rec)

    for fpath in conv_files:
        if not Path(fpath).exists():
            continue
        lines = Path(fpath).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            total_raw += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if skip_trivial and _is_trivial_entry(rec):
                total_filtered += 1
                continue
            entries.append((fpath, i, rec))

    return {
        "total_raw": total_raw,
        "total_filtered": total_filtered,
        "total_relevant": len(entries),
        "entries": entries,
        "files": conv_files,
    }


@app.post("/api/rebuild-scan/{instance}")
async def rebuild_scan(instance: str, request: Request):
    """Scannt Logs und gibt Statistiken zurück (Pre-Filtering, Cost Estimation)."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    skip_trivial = body.get("skip_trivial", True)

    scan = _scan_rebuild_entries(instance, skip_trivial=skip_trivial)
    # Token-Schätzung: ~150 Token pro Eintrag (User+Assistant avg)
    est_tokens = scan["total_relevant"] * 150
    # Provider-Typ für Kostenhinweis
    cfg = load_config()
    mem_cfg = cfg.get("memory", {})
    extract_llm_id = mem_cfg.get("extraction_llm", "")
    provider_type = "ollama"
    for llm in cfg.get("llms", []):
        if llm.get("id") == extract_llm_id:
            for prov in cfg.get("providers", []):
                if prov.get("id") == llm.get("provider_id"):
                    provider_type = prov.get("type", "ollama")
            break

    return {
        "total_raw": scan["total_raw"],
        "total_filtered": scan["total_filtered"],
        "total_relevant": scan["total_relevant"],
        "est_tokens": est_tokens,
        "provider_type": provider_type,
        "is_api": provider_type not in ("ollama",),
    }


# Persistenter Rebuild-Progress (für Pause/Resume)
_REBUILD_PROGRESS_DIR = LOG_ROOT / ".rebuild-progress"


def _load_rebuild_progress(instance: str) -> dict | None:
    """Lädt gespeicherten Rebuild-Fortschritt."""
    pfile = _REBUILD_PROGRESS_DIR / f"{instance}.json"
    if pfile.exists():
        try:
            return json.loads(pfile.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_rebuild_progress(instance: str, progress: dict):
    """Speichert Rebuild-Fortschritt für Pause/Resume."""
    _REBUILD_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    pfile = _REBUILD_PROGRESS_DIR / f"{instance}.json"
    pfile.write_text(json.dumps(progress), encoding="utf-8")


def _clear_rebuild_progress(instance: str):
    """Löscht gespeicherten Rebuild-Fortschritt."""
    pfile = _REBUILD_PROGRESS_DIR / f"{instance}.json"
    if pfile.exists():
        pfile.unlink()


@app.post("/api/rebuild-memory/{instance}")
async def start_rebuild(instance: str, request: Request):
    """Startet den Memory-Rebuild aus Konversations-Logs für eine Instanz."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    state = _rebuild.get(instance)
    if state and state["status"] == "running":
        return {"ok": False, "error": "Rebuild läuft bereits"}

    try:
        body = await request.json()
    except Exception:
        body = {}
    skip_trivial = body.get("skip_trivial", True)
    try:
        delay_ms = max(0, min(5000, int(body.get("delay_ms", 0))))
    except (ValueError, TypeError):
        delay_ms = 0
    resume = body.get("resume", False)

    # Scan mit Pre-Filtering
    scan = _scan_rebuild_entries(instance, skip_trivial=skip_trivial)
    entries = scan["entries"]

    if not entries:
        return {"ok": False, "error": "Keine relevanten Konversations-Logs gefunden"}

    # Resume: bereits verarbeitete Einträge überspringen
    resume_from = 0
    if resume:
        progress = _load_rebuild_progress(instance)
        if progress:
            resume_from = progress.get("processed", 0)

    if resume_from >= len(entries):
        _clear_rebuild_progress(instance)
        return {"ok": False, "error": "Rebuild bereits abgeschlossen"}

    # Agent-Erreichbarkeit prüfen vor Start
    agent_url = _get_agent_url(instance)
    import httpx as _httpx_pre
    try:
        async with _httpx_pre.AsyncClient(timeout=5.0) as _c:
            _r = await _c.get(f"{agent_url}/health")
            if not _r.is_success:
                return {"ok": False, "error": f"Agent '{instance}' antwortet nicht (Health-Check fehlgeschlagen). Container läuft?"}
    except Exception as _e:
        return {"ok": False, "error": f"Agent '{instance}' nicht erreichbar: {str(_e)[:120]}. Container läuft?"}

    total = len(entries) - resume_from
    _rebuild[instance] = {
        "status": "running", "done": 0, "total": total, "errors": 0,
        "started": time.time(), "error": "",
        "skipped_trivial": scan["total_filtered"],
        "resumed_from": resume_from,
    }

    async def _run():
        state = _rebuild[instance]
        import httpx
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for idx in range(resume_from, len(entries)):
                    if state["status"] == "cancelled":
                        # Fortschritt speichern für Resume
                        _save_rebuild_progress(instance, {
                            "processed": resume_from + state["done"],
                            "total_entries": len(entries),
                            "paused_at": time.time(),
                        })
                        return
                    _fpath, _line_idx, rec = entries[idx]
                    try:
                        r = await client.post(
                            f"{agent_url}/rebuild-entry",
                            json={
                                "user":      rec.get("user", ""),
                                "assistant": rec.get("assistant", ""),
                            },
                        )
                        if not r.is_success:
                            state["errors"] += 1
                    except Exception:
                        state["errors"] += 1
                    state["done"] += 1
                    # Rate-Limiting
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000.0)
            state["status"] = "done"
            _clear_rebuild_progress(instance)
        except Exception as e:
            state["status"] = "error"
            state["error"]  = str(e)[:200]
            # Fortschritt speichern bei Fehler
            _save_rebuild_progress(instance, {
                "processed": resume_from + state["done"],
                "total_entries": len(entries),
                "error": str(e)[:200],
                "paused_at": time.time(),
            })

    asyncio.create_task(_run())
    return {"ok": True, "total": total, "skipped_trivial": scan["total_filtered"], "resumed_from": resume_from}


@app.post("/api/rebuild-cancel/{instance}")
async def cancel_rebuild(instance: str):
    """Pausiert/bricht einen laufenden Rebuild ab. Progress wird gespeichert für Resume."""
    state = _rebuild.get(instance)
    if state and state["status"] == "running":
        state["status"] = "cancelled"
        return {"ok": True}
    return {"ok": False, "error": "Kein laufender Rebuild"}


@app.delete("/api/rebuild-progress/{instance}")
async def discard_rebuild_progress(instance: str):
    """Verwirft gespeicherten Rebuild-Fortschritt (ohne laufenden Rebuild zu beeinflussen)."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    _clear_rebuild_progress(instance)
    return {"ok": True}


@app.get("/api/rebuild-resume-info/{instance}")
async def rebuild_resume_info(instance: str):
    """Gibt Info über gespeicherten Rebuild-Fortschritt zurück."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    progress = _load_rebuild_progress(instance)
    if not progress:
        return {"has_progress": False}
    return {
        "has_progress": True,
        "processed": progress.get("processed", 0),
        "total_entries": progress.get("total_entries", 0),
        "paused_at": progress.get("paused_at"),
        "error": progress.get("error", ""),
    }


@app.get("/api/rebuild-progress/{instance}")
async def rebuild_progress(instance: str, request: Request):
    """SSE-Stream mit Rebuild-Fortschritt."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    async def generator():
        while True:
            if await request.is_disconnected():
                break
            state = _rebuild.get(instance, {})
            done    = state.get("done", 0)
            total   = state.get("total", 0)
            status  = state.get("status", "idle")
            elapsed = time.time() - state.get("started", time.time())
            eta_s   = int((total - done) * (elapsed / done)) if done > 0 else None
            yield f"data: {json.dumps({'done': done, 'total': total, 'status': status, 'eta_s': eta_s, 'error': state.get('error',''), 'errors': state.get('errors', 0), 'skipped_trivial': state.get('skipped_trivial', 0)})}\n\n"
            if status in ("done", "error", "idle", "cancelled"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/fetch-models")
async def fetch_models(request: Request):
    """
    Fragt verfügbare Modelle eines LLM-Providers ab.
    Body: {"type": "anthropic"|"ollama"|"custom", "url": "...", "key": "..."}
    Returns: {"models": ["model-id", ...]}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    type_ = (body.get("type") or "").strip()
    url   = (body.get("url")  or "").strip()
    key   = (body.get("key")  or "").strip()

    # Bekannte Anthropic-Modelle als Fallback
    _ANTHROPIC_KNOWN = [
        "claude-opus-4-6", "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-5", "claude-sonnet-4-5",
        "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    ]

    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if type_ == "ollama":
                target = url or "http://localhost:11434"
                r = await client.get(f"{target}/api/tags")
                models = [m["name"] for m in r.json().get("models", [])]
                return {"models": models}

            elif type_ == "minimax":
                # MiniMax: Anthropic-kompatible API
                return {"models": ["MiniMax-M2.5", "MiniMax-Text-01"]}

            elif type_ == "anthropic":
                # Wenn custom URL mit minimax → minimax-Modelle zurückgeben
                if url and "minimax" in url.lower():
                    return {"models": ["MiniMax-M2.5", "MiniMax-Text-01"]}
                # Wenn kein API-Key → Fallback-Liste
                if not key:
                    return {"models": _ANTHROPIC_KNOWN, "fallback": True}
                headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
                try:
                    r = await client.get("https://api.anthropic.com/v1/models", headers=headers)
                    if r.status_code == 200:
                        models = [m["id"] for m in r.json().get("data", [])]
                        return {"models": models}
                except Exception:
                    pass
                return {"models": _ANTHROPIC_KNOWN, "fallback": True}

            elif type_ == "openai":
                target = url or "https://api.openai.com"
                headers = {"Authorization": f"Bearer {key}"}
                r = await client.get(f"{target}/v1/models", headers=headers)
                if r.status_code == 200:
                    models = sorted([m["id"] for m in r.json().get("data", [])])
                    return {"models": models}
                return {"models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o1-mini", "o3-mini"], "fallback": True}

            elif type_ == "gemini":
                return {"models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-2.0-flash-lite"], "fallback": True}

            else:
                return {"models": [], "manual": True}
    except Exception as e:
        return {"models": [], "error": str(e)[:200]}


# ── Embedding-Modell-Konstanten ────────────────────────────────────────────────
_OPENAI_EMBEDDINGS = [
    {"id": "text-embedding-3-small", "dims": 1536},
    {"id": "text-embedding-3-large", "dims": 3072},
    {"id": "text-embedding-ada-002", "dims": 1536},
]
_GEMINI_EMBEDDINGS = [
    {"id": "models/gemini-embedding-001", "dims": 3072},
    {"id": "models/text-embedding-004", "dims": 768},
]
_FASTEMBED_MODELS = [
    {"id": "BAAI/bge-small-en-v1.5", "dims": 384, "is_embed": True},
    {"id": "BAAI/bge-m3", "dims": 1024, "is_embed": True},
]
_OLLAMA_EMBED_PATTERN = re.compile(r"embed|bge|minilm|nomic|mxbai|snowflake|arctic", re.I)
_OLLAMA_DIMS = {
    "bge-m3": 1024, "nomic-embed-text": 768, "all-minilm": 384,
    "bge-small-en-v1.5": 384, "mxbai-embed-large": 1024,
    "snowflake-arctic-embed": 1024,
}


@app.post("/api/fetch-embedding-models")
async def fetch_embedding_models(request: Request):
    """
    Gibt Embedding-Modelle für einen Provider zurück.
    Body: {"type": "ollama"|"openai"|"gemini"|"fastembed", "url": "...", "key": "..."}
    Returns: {"models": [{"id": "model-id", "dims": 768}, ...]}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    type_ = (body.get("type") or "").strip()
    url   = (body.get("url")  or "").strip()
    key   = (body.get("key")  or "").strip()

    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if type_ == "ollama":
                target = url or "http://localhost:11434"
                r = await client.get(f"{target}/api/tags")
                all_models = [m["name"] for m in r.json().get("models", [])]
                # Embedding-Modelle priorisieren, Rest auch anbieten
                embed_models = []
                for m in all_models:
                    base = m.split(":")[0]
                    dims = _OLLAMA_DIMS.get(base, 0)
                    is_embed = bool(_OLLAMA_EMBED_PATTERN.search(m))
                    embed_models.append({"id": m, "dims": dims, "is_embed": is_embed})
                # Embedding-Modelle zuerst
                embed_models.sort(key=lambda x: (not x["is_embed"], x["id"]))
                return {"models": embed_models}

            elif type_ == "openai":
                # Versuche dynamisch, Fallback auf bekannte Liste
                target = url or "https://api.openai.com"
                if key:
                    try:
                        headers = {"Authorization": f"Bearer {key}"}
                        r = await client.get(f"{target}/v1/models", headers=headers)
                        if r.status_code == 200:
                            api_models = [m["id"] for m in r.json().get("data", [])
                                          if "embed" in m["id"].lower()]
                            if api_models:
                                models = []
                                for m in sorted(api_models):
                                    known = next((e for e in _OPENAI_EMBEDDINGS if e["id"] == m), None)
                                    models.append({"id": m, "dims": known["dims"] if known else 0})
                                return {"models": models}
                    except Exception:
                        pass
                return {"models": _OPENAI_EMBEDDINGS, "fallback": True}

            elif type_ == "gemini":
                return {"models": _GEMINI_EMBEDDINGS, "fallback": True}

            elif type_ == "fastembed":
                return {"models": _FASTEMBED_MODELS}

            else:
                return {"models": []}
    except Exception as e:
        return {"models": [], "error": str(e)[:200]}


@app.post("/api/test-embedding")
async def test_embedding(request: Request):
    """Testet ein Embedding-Modell mit einem kurzen Text und gibt Dimensions zurück."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    type_ = (body.get("type") or "").strip()
    url   = (body.get("url")  or "").strip()
    key   = (body.get("key")  or "").strip()
    model = (body.get("model") or "").strip()

    if not model:
        return {"ok": False, "error": "Kein Modell angegeben"}

    import httpx
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if type_ == "ollama":
                target = url or "http://localhost:11434"
                r = await client.post(f"{target}/api/embed", json={"model": model, "input": "Test embedding"})
                if r.status_code != 200:
                    return {"ok": False, "error": f"Ollama Fehler: {r.status_code} {r.text[:100]}"}
                data = r.json()
                embeddings = data.get("embeddings", [[]])
                dims = len(embeddings[0]) if embeddings and embeddings[0] else 0

            elif type_ == "openai":
                target = url or "https://api.openai.com"
                r = await client.post(
                    f"{target}/v1/embeddings",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "input": "Test embedding"},
                )
                if r.status_code != 200:
                    return {"ok": False, "error": f"OpenAI Fehler: {r.status_code} {r.text[:100]}"}
                data = r.json()
                emb = data.get("data", [{}])[0].get("embedding", [])
                dims = len(emb)

            elif type_ == "gemini":
                # Google AI Embedding API
                api_url = f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={key}"
                r = await client.post(api_url, json={
                    "model": model,
                    "content": {"parts": [{"text": "Test embedding"}]},
                })
                if r.status_code != 200:
                    return {"ok": False, "error": f"Gemini Fehler: {r.status_code} {r.text[:100]}"}
                data = r.json()
                emb = data.get("embedding", {}).get("values", [])
                dims = len(emb)

            elif type_ == "fastembed":
                # Lokales Embedding via fastembed – kein externer Service
                try:
                    from fastembed import TextEmbedding
                    fe_model = model or "BAAI/bge-small-en-v1.5"
                    embedding_model = TextEmbedding(model_name=fe_model)
                    embeddings = list(embedding_model.embed(["Test embedding"]))
                    dims = len(embeddings[0]) if embeddings else 0
                except ImportError:
                    return {"ok": False, "error": "fastembed nicht installiert"}
                except Exception as fe_err:
                    return {"ok": False, "error": f"FastEmbed Fehler: {str(fe_err)[:100]}"}

            else:
                return {"ok": False, "error": f"Unbekannter Provider-Typ: {type_}"}

            elapsed = int((time.time() - start) * 1000)
            return {"ok": True, "dims": dims, "time_ms": elapsed}

    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ── User-Management ───────────────────────────────────────────────────────────

_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
    "tr": "Turkish",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
}


def _render_claude_md(template_name: str, display_name: str, user_id: str, ha_user: str = "", language: str = "de") -> str:
    """Generiert CLAUDE.md aus Template mit Platzhalter-Ersetzung."""
    tpl_path = TEMPLATES_DIR / f"{template_name}.md"
    if not tpl_path.exists():
        tpl_path = TEMPLATES_DIR / "user.md"
    content = tpl_path.read_text(encoding="utf-8")
    content = content.replace("{{DISPLAY_NAME}}", display_name)
    content = content.replace("{{USER_ID}}", user_id)
    content = content.replace("{{HA_USER}}", ha_user or user_id)
    response_language = _LANGUAGE_NAMES.get(language, language)
    content = content.replace("{{RESPONSE_LANGUAGE}}", response_language)
    return content


def _find_free_port(existing_ports: list[int]) -> int:
    """Nächsten freien Port ab 8001 finden."""
    port = 8001
    while port in existing_ports:
        port += 1
    return port


@app.get("/api/users")
async def get_users():
    """User-Liste mit Agent-Status."""
    cfg = load_config()
    users = cfg.get("users", [])
    result = []
    for u in users:
        status = _agent_manager.agent_status(u["id"])
        result.append({**u, "container_status": status})
    return result


@app.post("/api/users")
async def create_user(request: Request):
    """
    Legt neuen User an:
    1. ID-Validierung
    2. Port vergeben
    3. CLAUDE.md aus Template generieren
    4. Container starten via Docker SDK
    5. User in config.json speichern
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    uid = (body.get("id") or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,29}$", uid):
        raise HTTPException(400, "ID muss [a-z0-9-], max 30 Zeichen, nicht mit - beginnen")
    if uid in _SYSTEM_USER_IDS:
        raise HTTPException(409, f"'{uid}' ist eine reservierte System-ID")

    cfg = load_config()
    existing = [u["id"] for u in cfg.get("users", [])]
    if uid in existing:
        raise HTTPException(409, f"User '{uid}' existiert bereits")

    # Port vergeben
    used_ports = [u.get("api_port", 0) for u in cfg.get("users", [])]
    port = _find_free_port(used_ports)

    # Default-LLMs aus Config
    default_primary = cfg.get("llms", [{}])[0].get("id", "") if cfg.get("llms") else ""

    # User-Objekt aufbauen
    user: dict = {
        "id":                  uid,
        "display_name":        body.get("display_name") or uid.capitalize(),
        "role":                body.get("role", "user"),
        "language":            body.get("language", "de"),
        "primary_llm":         body.get("primary_llm", default_primary),
        "fallback_llm":        body.get("fallback_llm", ""),
        "ha_user":             body.get("ha_user", uid),
        "whatsapp_phone":      body.get("whatsapp_phone", ""),
        "api_port":            port,
        "container_name":      f"haana-instanz-{uid}-1",
        "claude_md_template":  body.get("claude_md_template", "admin" if body.get("role") == "admin" else "user"),
        "caldav_url": "", "caldav_user": "", "caldav_pass": "",
        "imap_host": "", "imap_port": 993, "imap_user": "", "imap_pass": "",
        "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "",
    }

    # CLAUDE.md generieren
    claude_md_dir = INST_DIR / uid
    claude_md_dir.mkdir(parents=True, exist_ok=True)
    claude_md_content = _render_claude_md(
        user["claude_md_template"], user["display_name"], uid, user["ha_user"],
        user.get("language", "de")
    )
    (claude_md_dir / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")

    # Agent starten
    result = await _agent_manager.start_agent(user, cfg)

    # User in config speichern (auch wenn Container-Start fehlschlägt)
    cfg.setdefault("users", []).append(user)
    save_config(cfg)

    # Rebuild-State erweitern
    _rebuild[uid] = {"status": "idle", "done": 0, "total": 0, "started": 0.0, "error": ""}

    return {"ok": True, "user": user, "container": result}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: str, request: Request):
    """User-Felder aktualisieren. Container wird neu gestartet wenn relevante Felder geändert."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    cfg = load_config()
    users = cfg.get("users", [])
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, f"User '{user_id}' nicht gefunden")

    restart_fields = {"primary_llm", "fallback_llm", "ha_user", "role", "claude_md_template", "language"}
    needs_restart = any(k in body and body[k] != user.get(k) for k in restart_fields)

    user.update({k: v for k, v in body.items() if k not in ("id", "api_port", "container_name")})
    save_config(cfg)

    # CLAUDE.md neu generieren wenn Template, Name oder Sprache geändert
    if "display_name" in body or "claude_md_template" in body or "ha_user" in body or "language" in body:
        claude_md_content = _render_claude_md(
            user["claude_md_template"], user["display_name"], user_id,
            user.get("ha_user", user_id), user.get("language", "de")
        )
        (INST_DIR / user_id / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")

    container_result = None
    if needs_restart:
        container_result = await _agent_manager.start_agent(user, cfg)

    return {"ok": True, "user": user, "restarted": needs_restart, "container": container_result}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str):
    """User löschen: Container stoppen + entfernen, CLAUDE.md-Dir löschen, config speichern."""
    cfg = load_config()
    users = cfg.get("users", [])
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, f"User '{user_id}' nicht gefunden")
    if user.get("system"):
        raise HTTPException(403, "System-Instanzen können nicht gelöscht werden")

    # Agent stoppen + entfernen
    remove_result = await _agent_manager.remove_agent(user_id)
    container_removed = remove_result.get("ok", False)

    # CLAUDE.md-Verzeichnis löschen
    import shutil
    inst_path = INST_DIR / user_id
    if inst_path.exists():
        shutil.rmtree(inst_path, ignore_errors=True)

    # User aus config entfernen
    cfg["users"] = [u for u in users if u["id"] != user_id]
    save_config(cfg)

    _rebuild.pop(user_id, None)

    return {"ok": True, "container_removed": container_removed}


@app.post("/api/users/{user_id}/restart")
async def restart_user_container(user_id: str):
    """Agent für einen User neu starten."""
    cfg = load_config()
    user = next((u for u in cfg.get("users", []) if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, f"User '{user_id}' nicht gefunden")
    result = await _agent_manager.start_agent(user, cfg)
    return {"ok": result.get("ok", False), "container": result}


@app.post("/api/users/{user_id}/stop")
async def stop_user_container(user_id: str):
    """Agent für einen User stoppen."""
    cfg = load_config()
    user = next((u for u in cfg.get("users", []) if u["id"] == user_id), None)
    if not user:
        raise HTTPException(404, f"User '{user_id}' nicht gefunden")
    return await _agent_manager.stop_agent(user_id)


def _get_agent_url(instance: str) -> str:
    """Agent-URL: AgentManager oder Fallback aus AGENT_URLS/Config."""
    if _agent_manager:
        url = _agent_manager.agent_url(instance)
        if url:
            return url
    # Fallback für statische Instanzen
    if instance in AGENT_URLS:
        return AGENT_URLS[instance]
    cfg = load_config()
    user = next((u for u in cfg.get("users", []) if u["id"] == instance), None)
    if user:
        return f"http://{user.get('container_name', f'haana-instanz-{instance}-1')}:{user['api_port']}"
    return ""


@app.get("/api/agent-health/{instance}")
async def agent_health(instance: str):
    """Prüft ob ein Agent-Container erreichbar ist."""
    if instance not in get_all_instances():
        raise HTTPException(404)
    agent_url = _get_agent_url(instance)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{agent_url}/health")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Claude Auth Management ────────────────────────────────────────────────────

CLAUDE_AUTH_DIR = Path("/claude-auth")       # gemountet via docker-compose
CLAUDE_AUTH_HOST = Path("/home/haana/.claude")  # Host-Pfad (für Referenz)

@app.get("/api/claude-auth/status")
async def claude_auth_status():
    """Prüft ob gültige Claude OAuth-Credentials vorliegen."""
    creds_file = CLAUDE_AUTH_DIR / ".credentials.json"
    if not creds_file.exists():
        return {"ok": False, "status": "no_credentials", "detail": "Keine Credentials gefunden"}
    try:
        creds = json.loads(creds_file.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return {"ok": False, "status": "no_token", "detail": "Kein Access-Token"}
        expires_at = oauth.get("expiresAt", 0) / 1000
        now = time.time()
        if now > expires_at:
            hours_ago = (now - expires_at) / 3600
            return {"ok": False, "status": "expired", "detail": f"Token abgelaufen (vor {hours_ago:.1f}h)"}
        hours_left = (expires_at - now) / 3600
        return {"ok": True, "status": "valid", "detail": f"Token gültig (noch {hours_left:.1f}h)",
                "expires_in_hours": round(hours_left, 1)}
    except Exception as e:
        return {"ok": False, "status": "error", "detail": str(e)[:200]}


@app.post("/api/claude-auth/refresh")
async def claude_auth_refresh():
    """Versucht den OAuth-Token per Refresh-Token zu erneuern.
    Nutzt einen laufenden Agent-Container um den CLI-Befehl auszuführen."""
    if not _docker_client:
        return {"ok": False, "detail": "Docker nicht verfügbar"}

    # Finde einen laufenden Agent-Container
    try:
        containers = _docker_client.containers.list(
            filters={"status": "running", "name": "haana-instanz"})
        if not containers:
            return {"ok": False, "detail": "Kein laufender Agent-Container gefunden"}
        container = containers[0]

        # auth status prüfen
        result = container.exec_run(
            cmd=["/usr/local/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude",
                 "auth", "status"],
            user="haana", environment={"HOME": "/home/haana"})
        status_out = result.output.decode("utf-8", errors="replace").strip()

        try:
            status_data = json.loads(status_out.split("\n")[0])
        except Exception:
            status_data = {}

        if status_data.get("loggedIn"):
            return {"ok": True, "detail": "Bereits eingeloggt", "status": status_data}

        # Token ist abgelaufen - Credentials-Datei neu von laufender Session kopieren
        # Falls eine Claude Code Session auf dem Host läuft, hat sie den Token refreshed
        return {"ok": False, "detail": "Token abgelaufen. Bitte manuell erneuern (siehe Anleitung).",
                "status": status_data}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/api/claude-auth/upload")
async def claude_auth_upload(request: Request):
    """Credentials-Datei hochladen (JSON mit claudeAiOauth)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    creds = body.get("credentials")
    if not creds or not isinstance(creds, dict):
        raise HTTPException(400, "Feld 'credentials' fehlt oder ungültig")

    # Validierung: muss claudeAiOauth mit accessToken enthalten
    oauth = creds.get("claudeAiOauth", {})
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        raise HTTPException(400, "Credentials müssen claudeAiOauth mit accessToken und refreshToken enthalten")

    creds_file = CLAUDE_AUTH_DIR / ".credentials.json"
    try:
        CLAUDE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
        creds_file.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        # Permissions für Container-User
        os.chmod(creds_file, 0o600)
        import subprocess
        subprocess.run(["chown", "1000:1000", str(creds_file)], check=False)
        return {"ok": True, "detail": "Credentials gespeichert. Container müssen neu gestartet werden."}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


# ── Claude OAuth Login Flow (setup-token) ────────────────────────────────────
# Nutzt `claude setup-token` statt `claude auth login`.
# setup-token erzeugt einen langlebigen Token (~1 Jahr) der in headless/Container-
# Umgebungen funktioniert. Der Flow: URL anzeigen → User autorisiert im Browser →
# Code wird angezeigt → User gibt Code ein → Token wird gespeichert.

# Stores active login session: {pid, fd, tmp_home}
_oauth_login_session: dict | None = None


def _cleanup_oauth_session():
    """Kill any running oauth login process."""
    global _oauth_login_session
    if _oauth_login_session:
        try:
            os.kill(_oauth_login_session["pid"], signal.SIGKILL)
            os.waitpid(_oauth_login_session["pid"], os.WNOHANG)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(_oauth_login_session["fd"])
        except OSError:
            pass
        _oauth_login_session = None


def _start_oauth_login_sync():
    """Blocking: spawn `claude setup-token`, extract OAuth URL."""
    import shutil as _shutil
    if not _shutil.which("claude"):
        return {"ok": False, "detail": "Claude CLI nicht installiert. Bitte API Key verwenden oder 'npm install -g @anthropic-ai/claude-code' im Container ausführen."}

    global _oauth_login_session

    _cleanup_oauth_session()

    tmp_home = "/tmp/claude-oauth-login"
    import shutil
    if os.path.exists(tmp_home):
        shutil.rmtree(tmp_home)
    os.makedirs(f"{tmp_home}/.claude", exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = tmp_home
    # TERM=dumb disables TUI mode so setup-token accepts stdin input
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"

    pid, fd = pty.fork()
    if pid == 0:
        # Set wide terminal to avoid URL wrapping
        import struct, fcntl, termios
        try:
            winsize = struct.pack("HHHH", 50, 500, 0, 0)
            fcntl.ioctl(1, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass
        for k, v in env.items():
            os.environ[k] = v
        os.execvp("claude", ["claude", "setup-token"])
        os._exit(1)

    # Set PTY window size on parent side too
    import struct, fcntl, termios
    try:
        winsize = struct.pack("HHHH", 50, 500, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    # Read output until we get the URL
    output = b""
    end_time = time.time() + 25
    while time.time() < end_time:
        r, _, _ = select.select([fd], [], [], 1)
        if r:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                output += data
                if b"prompted" in output or b"Paste" in output:
                    break
            except OSError:
                break

    text = output.decode("utf-8", errors="replace")
    # Strip ANSI escape codes and control sequences
    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    # Remove all CR/LF to join wrapped URL lines
    flat = re.sub(r"[\r\n]+", "", clean)

    url_match = re.search(r"(https://claude\.ai/oauth/authorize[^\s]*)", flat)
    if not url_match:
        _cleanup_oauth_session()
        return {"ok": False, "detail": f"Could not extract OAuth URL."}

    auth_url = url_match.group(1)
    # state is the last query param; trim trailing prompt text
    state_match = re.search(r"state=([A-Za-z0-9\-_]{43})", auth_url)
    if state_match:
        auth_url = auth_url[:state_match.end()]

    _oauth_login_session = {
        "pid": pid, "fd": fd, "tmp_home": tmp_home,
        "url": auth_url,
    }
    return {"ok": True, "url": auth_url}


@app.post("/api/claude-auth/login/start")
async def claude_auth_login_start():
    """Start OAuth login: spawns 'claude setup-token', returns the auth URL."""
    return await asyncio.to_thread(_start_oauth_login_sync)


def _complete_oauth_login_sync(code: str):
    """Blocking: send authorization code to claude setup-token via PTY stdin."""
    global _oauth_login_session

    if not _oauth_login_session:
        return {"ok": False, "detail": "No active login session. Start login first."}

    fd = _oauth_login_session["fd"]
    tmp_home = _oauth_login_session["tmp_home"]

    # Set PTY to raw mode so special chars like # pass through unprocessed
    import tty
    try:
        tty.setraw(fd)
    except Exception:
        pass

    # Write the code to the PTY (stdin of claude setup-token)
    try:
        os.write(fd, code.encode("utf-8"))
        time.sleep(0.3)
        os.write(fd, b"\r")
    except OSError as e:
        _cleanup_oauth_session()
        return {"ok": False, "detail": f"Could not send code to CLI: {e}"}

    # Wait for process to finish and read output (setup-token does token exchange)
    # Token exchange with Anthropic can take 5-10s, so we must NOT break on idle.
    pty_text = ""
    end_time = time.time() + 30
    while time.time() < end_time:
        try:
            r, _, _ = select.select([fd], [], [], 2)
            if r:
                data = os.read(fd, 8192)
                if not data:
                    break
                pty_text += data.decode("utf-8", errors="replace")
                logger.info(f"setup-token output chunk: {repr(data[:200])}")
                # Check for completion indicators
                lower = pty_text.lower()
                if "error" in lower or "invalid" in lower or "retry" in lower:
                    time.sleep(1)  # give time for full error message
                    break
                if "success" in lower or "authenticated" in lower or "logged in" in lower or "token saved" in lower:
                    time.sleep(2)  # give time to write credentials
                    break
            # else: no data yet - keep waiting (token exchange takes time)
        except OSError:
            break

    # Also wait for the process to exit and capture remaining output
    try:
        r, _, _ = select.select([fd], [], [], 3)
        if r:
            remaining = os.read(fd, 8192)
            if remaining:
                pty_text += remaining.decode("utf-8", errors="replace")
                logger.info(f"setup-token remaining output: {repr(remaining[:200])}")
    except OSError:
        pass

    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", pty_text)
    clean = re.sub(r"[\r\n]+", " ", clean).strip()

    # Log full output for debugging
    clean_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", pty_text)
    logger.info(f"setup-token full output: {repr(clean_log[:500])}")

    # Look for credentials in tmp home - check multiple possible locations
    creds_saved = False
    tmp_creds = Path(tmp_home) / ".claude" / ".credentials.json"
    # setup-token might also write to .claude.json or other locations
    alt_creds_paths = [
        Path(tmp_home) / ".claude" / ".credentials.json",
        Path(tmp_home) / ".claude" / "credentials.json",
        Path(tmp_home) / ".credentials.json",
    ]
    for p in alt_creds_paths:
        if p.is_file():
            logger.info(f"setup-token: Found credentials at {p}")
            tmp_creds = p
            break
    else:
        # List what files actually exist in tmp_home
        try:
            import subprocess as _sp2
            ls_result = _sp2.run(["find", tmp_home, "-type", "f"], capture_output=True, text=True, timeout=5)
            logger.info(f"setup-token: Files in {tmp_home}: {ls_result.stdout.strip()}")
        except Exception:
            pass

    if tmp_creds.is_file():
        try:
            creds_data = tmp_creds.read_text(encoding="utf-8")
            creds = json.loads(creds_data)
            if creds.get("claudeAiOauth", {}).get("accessToken"):
                CLAUDE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
                dest = CLAUDE_AUTH_DIR / ".credentials.json"
                dest.write_text(creds_data, encoding="utf-8")
                os.chmod(dest, 0o600)
                import subprocess as _sp
                _sp.run(["chown", "1000:1000", str(dest)], check=False)
                creds_saved = True
                logger.info("setup-token: Credentials in CLAUDE_AUTH_DIR gespeichert")
        except Exception as e:
            logger.error(f"setup-token: Credential copy failed: {e}")

    if creds_saved:
        _cleanup_oauth_session()
        return {"ok": True, "detail": "Login successful. Long-lived token saved."}

    # Fallback: setup-token might print a token string to stdout instead of writing a file
    # Look for token patterns like sk-ant-oat01-... in the output
    token_match = re.search(r"(sk-ant-[a-zA-Z0-9_-]{20,})", pty_text)
    if token_match:
        token_str = token_match.group(1)
        logger.info(f"setup-token: Found token in stdout output (len={len(token_str)})")
        # Build credentials JSON from the token
        creds_data = json.dumps({
            "claudeAiOauth": {
                "accessToken": token_str,
                "refreshToken": "",
                "expiresAt": 0,  # setup-token tokens are long-lived
                "scopes": ["user:inference", "user:profile"],
            }
        })
        try:
            CLAUDE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
            dest = CLAUDE_AUTH_DIR / ".credentials.json"
            dest.write_text(creds_data, encoding="utf-8")
            os.chmod(dest, 0o600)
            import subprocess as _sp
            _sp.run(["chown", "1000:1000", str(dest)], check=False)
            _cleanup_oauth_session()
            logger.info("setup-token: Token aus stdout in CLAUDE_AUTH_DIR gespeichert")
            return {"ok": True, "detail": "Login successful. Long-lived token saved."}
        except Exception as e:
            logger.error(f"setup-token: Token save failed: {e}")

    _cleanup_oauth_session()

    # Check for error messages
    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", pty_text)
    clean = re.sub(r"[\r\n]+", " ", clean).strip()
    if "error" in clean.lower() or "invalid" in clean.lower():
        detail = re.sub(r"[^\x20-\x7e]", "", clean).strip()
        # Compress multiple spaces
        detail = re.sub(r" {2,}", " ", detail)[:200]
        return {"ok": False, "detail": f"Login fehlgeschlagen: {detail}"}

    return {"ok": False, "detail": "Credentials nicht gefunden. Bitte Login erneut starten."}


@app.post("/api/claude-auth/login/complete")
async def claude_auth_login_complete(request: Request):
    """Complete OAuth login: send the authorization code via PTY stdin."""
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        return {"ok": False, "detail": "Authorization code missing"}
    try:
        return await asyncio.to_thread(_complete_oauth_login_sync, code)
    except Exception as e:
        logger.exception("OAuth complete error")
        return {"ok": False, "detail": str(e)}


# ── Provider-scoped OAuth Endpoints ──────────────────────────────────────────

@app.get("/api/claude-auth/status/{provider_id}")
async def claude_auth_status_provider(provider_id: str):
    """Prüft ob gültige Claude OAuth-Credentials für einen Provider vorliegen."""
    cfg = load_config()
    prov = next((p for p in cfg.get("providers", []) if p["id"] == provider_id), None)
    if not prov:
        raise HTTPException(404, "Provider nicht gefunden")
    oauth_dir = Path(prov.get("oauth_dir", f"/data/claude-auth/{provider_id}"))
    creds_file = oauth_dir / ".credentials.json"
    if not creds_file.exists():
        return {"ok": False, "status": "no_credentials", "detail": "Keine Credentials gefunden"}
    try:
        creds = json.loads(creds_file.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return {"ok": False, "status": "no_token", "detail": "Kein Access-Token"}
        expires_at = oauth.get("expiresAt", 0) / 1000
        now = time.time()
        if expires_at > 0 and now > expires_at:
            hours_ago = (now - expires_at) / 3600
            return {"ok": False, "status": "expired", "detail": f"Token abgelaufen (vor {hours_ago:.1f}h)"}
        if expires_at > 0:
            hours_left = (expires_at - now) / 3600
            days_left = hours_left / 24
            if days_left > 30:
                return {"ok": True, "status": "valid",
                        "detail": f"Token gültig (noch {days_left:.0f} Tage)",
                        "expires_in_hours": round(hours_left, 1)}
            return {"ok": True, "status": "valid", "detail": f"Token gültig (noch {hours_left:.1f}h)",
                    "expires_in_hours": round(hours_left, 1)}
        # No expiry set = long-lived token
        return {"ok": True, "status": "valid", "detail": "Token gültig (langlebig)"}
    except Exception as e:
        return {"ok": False, "status": "error", "detail": str(e)[:200]}


@app.post("/api/claude-auth/login/start/{provider_id}")
async def claude_auth_login_start_provider(provider_id: str):
    """Start OAuth login for a specific provider."""
    return await asyncio.to_thread(_start_oauth_login_sync)


@app.post("/api/claude-auth/login/complete/{provider_id}")
async def claude_auth_login_complete_provider(provider_id: str, request: Request):
    """Complete OAuth login for a specific provider: send the authorization code."""
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        return {"ok": False, "detail": "Authorization code missing"}

    try:
        result = await asyncio.to_thread(_complete_oauth_login_sync, code)
    except Exception as e:
        logger.exception("OAuth complete (provider) error")
        return {"ok": False, "detail": str(e)}

    # Copy credentials to provider-specific directory (auch bei Fehler versuchen,
    # da der erste complete-Aufruf funktioniert haben könnte)
    cfg = load_config()
    prov = next((p for p in cfg.get("providers", []) if p["id"] == provider_id), None)
    if prov:
        oauth_dir = Path(prov.get("oauth_dir", f"/data/claude-auth/{provider_id}"))
        global_creds = CLAUDE_AUTH_DIR / ".credentials.json"
        if global_creds.exists():
            try:
                import shutil
                oauth_dir.mkdir(parents=True, exist_ok=True)
                dest = oauth_dir / ".credentials.json"
                shutil.copy2(str(global_creds), str(dest))
                os.chmod(dest, 0o600)
                import subprocess
                subprocess.run(["chown", "1000:1000", str(dest)], check=False)
                logger.info(f"OAuth credentials kopiert: {global_creds} → {dest}")
                # Nur als Erfolg melden wenn die Credentials tatsächlich gültig sind
                if not result.get("ok"):
                    creds = json.loads(dest.read_text(encoding="utf-8"))
                    oauth = creds.get("claudeAiOauth", {})
                    expires_at = oauth.get("expiresAt", 0) / 1000
                    if oauth.get("accessToken") and (expires_at == 0 or expires_at > time.time()):
                        result = {"ok": True, "detail": "Login successful. Credentials saved."}
            except Exception as e:
                logger.error(f"OAuth credential copy failed: {e}")

    return result


@app.post("/api/claude-auth/upload/{provider_id}")
async def claude_auth_upload_provider(provider_id: str, request: Request):
    """Upload credentials for a specific provider."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    creds = body.get("credentials")
    if not creds or not isinstance(creds, dict):
        raise HTTPException(400, "Feld 'credentials' fehlt oder ungültig")

    oauth = creds.get("claudeAiOauth", {})
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        raise HTTPException(400, "Credentials müssen claudeAiOauth mit accessToken und refreshToken enthalten")

    cfg = load_config()
    prov = next((p for p in cfg.get("providers", []) if p["id"] == provider_id), None)
    if not prov:
        raise HTTPException(404, "Provider nicht gefunden")

    oauth_dir = Path(prov.get("oauth_dir", f"/data/claude-auth/{provider_id}"))
    try:
        oauth_dir.mkdir(parents=True, exist_ok=True)
        creds_file = oauth_dir / ".credentials.json"
        creds_file.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        os.chmod(creds_file, 0o600)
        import subprocess
        subprocess.run(["chown", "1000:1000", str(creds_file)], check=False)
        return {"ok": True, "detail": "Credentials gespeichert. Container müssen neu gestartet werden."}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


# ── SSE: Echtzeit-Konversationen ──────────────────────────────────────────────

@app.get("/api/events/{instance}")
async def sse_events(instance: str, request: Request):
    """
    Server-Sent Events: streamt neue Konversationszeilen sobald sie erscheinen.
    Pollt alle 2 Sekunden die aktuelle Tages-Log-Datei.
    """
    if instance not in get_all_instances():
        raise HTTPException(404, "Instanz nicht gefunden")

    async def event_generator():
        last_pos = 0

        # Bestehende Zeilen beim Connect überspringen (nur neue senden)
        today_path = LOG_ROOT / "conversations" / instance / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        if today_path.exists():
            last_pos = today_path.stat().st_size

        yield f"data: {json.dumps({'type': 'connected', 'instance': instance})}\n\n"

        while True:
            if await request.is_disconnected():
                break

            # Tages-Datei kann sich von Tag zu Tag ändern
            today = datetime.now().strftime("%Y-%m-%d")
            log_path = LOG_ROOT / "conversations" / instance / f"{today}.jsonl"

            if log_path.exists():
                size = log_path.stat().st_size
                if size > last_pos:
                    with log_path.open("r", encoding="utf-8") as f:
                        f.seek(last_pos)
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    record = json.loads(line)
                                    yield f"data: {json.dumps({'type': 'conversation', 'record': record})}\n\n"
                                except json.JSONDecodeError:
                                    pass
                    last_pos = log_path.stat().st_size
            else:
                last_pos = 0  # Neuer Tag, Position zurücksetzen

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Dream Process Integration ────────────────────────────────────────────────

def _build_dream_config(cfg: dict) -> dict:
    """Baut die memory_config dict für DreamProcess aus der HAANA-Konfiguration."""
    dream_cfg = cfg.get("dream", {})
    llm_id = dream_cfg.get("llm") or cfg.get("memory", {}).get("extraction_llm", "")
    llm, provider = _resolve_llm(llm_id, cfg)

    return {
        "qdrant_url": cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333"),
        "ollama_url": _find_ollama_url(cfg),
        "extract_type": provider.get("type", "ollama"),
        "extract_url": provider.get("url", ""),
        "extract_key": provider.get("key", ""),
        "model": llm.get("model", ""),
        "similarity_threshold": 0.9,
    }


async def _run_dream(instance: str, cfg: dict):
    """Führt den Dream-Prozess für eine Instanz aus."""
    from core.dream import DreamProcess

    _dream_state[instance] = {"status": "running", "started": time.time()}

    try:
        memory_cfg = _build_dream_config(cfg)
        dream = DreamProcess(memory_cfg, str(LOG_ROOT))

        # Scopes: aus Config oder Default-Scopes des Users
        scopes = cfg.get("dream", {}).get("scopes") or []
        if not scopes:
            scopes = [f"{instance}_memory", "household_memory"]

        t_start = time.monotonic()

        # DreamProcess.run() ist async und nimmt einen einzelnen Scope
        total_consolidated = 0
        total_cleaned = 0
        last_summary = ""
        for scope in scopes:
            report = await dream.run(instance, scope)
            total_consolidated += report.consolidated
            total_cleaned += report.cleaned
            if report.summary:
                last_summary = report.summary

        duration = time.monotonic() - t_start

        # Tagebuch-Eintrag speichern
        if last_summary:
            from core.logger import log_dream_summary
            log_dream_summary(
                instance=instance,
                date=datetime.now().strftime("%Y-%m-%d"),
                summary=last_summary,
                consolidated=total_consolidated,
                contradictions=total_cleaned,
                duration_s=duration,
            )

        _dream_state[instance] = {
            "status": "done",
            "finished": time.time(),
            "report": {
                "summary": last_summary,
                "consolidated": total_consolidated,
                "contradictions": total_cleaned,
                "duration_s": round(duration, 1),
            },
        }
    except Exception as e:
        logger.error(f"Dream-Prozess Fehler für {instance}: {e}", exc_info=True)
        _dream_state[instance] = {"status": "error", "error": str(e)[:200]}


async def _dream_scheduler():
    """Prüft minütlich ob es Zeit für den Dream-Prozess ist."""
    while True:
        await asyncio.sleep(60)
        cfg = load_config()
        dream_cfg = cfg.get("dream", {})
        if not dream_cfg.get("enabled"):
            continue

        now = datetime.now()
        schedule = dream_cfg.get("schedule", "02:00")
        try:
            hour, minute = map(int, schedule.split(":"))
        except (ValueError, AttributeError):
            continue

        if now.hour == hour and now.minute == minute:
            # Für jeden User mit Write-Scopes den Dream starten
            for user in cfg.get("users", []):
                instance = user["id"]
                if _dream_state.get(instance, {}).get("status") != "running":
                    asyncio.create_task(_run_dream(instance, cfg))
            # Warte 61 Sekunden um Doppel-Trigger zu vermeiden
            await asyncio.sleep(61)


@app.post("/api/dream/run/{instance}")
async def dream_run(instance: str):
    """Dream-Prozess sofort triggern."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    if _dream_state.get(instance, {}).get("status") == "running":
        return {"ok": False, "error": "Dream-Prozess läuft bereits"}

    cfg = load_config()
    asyncio.create_task(_run_dream(instance, cfg))
    return {"ok": True, "status": "started"}


@app.get("/api/dream/status/{instance}")
async def dream_status(instance: str):
    """Aktuellen Dream-Status abfragen."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    state = _dream_state.get(instance, {"status": "idle"})
    result = {"status": state.get("status", "idle")}

    if "finished" in state:
        result["last_run"] = datetime.fromtimestamp(
            state["finished"], tz=timezone.utc
        ).isoformat()
    if "report" in state:
        result["report"] = state["report"]
    if "error" in state:
        result["error"] = state["error"]

    return result


@app.get("/api/dream/logs/{instance}")
async def dream_logs(instance: str, request: Request):
    """Dream-Tagebuch-Einträge lesen."""
    if instance not in get_all_instances():
        raise HTTPException(404)

    params = dict(request.query_params)
    date_filter = params.get("date", "")
    try:
        limit = int(params.get("limit", "30"))
    except (ValueError, TypeError):
        limit = 30

    dream_dir = LOG_ROOT / "dream" / instance

    if not dream_dir.exists():
        return []

    records: list[dict] = []

    if date_filter:
        # Bestimmter Tag — Path-Traversal verhindern
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_filter):
            raise HTTPException(400, "Ungültiges Datumsformat")
        fpath = dream_dir / f"{date_filter}.jsonl"
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    else:
        # Neueste N Einträge über alle Tage
        files = sorted(dream_dir.glob("*.jsonl"), reverse=True)
        for fpath in files:
            try:
                lines = fpath.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
                if len(records) >= limit:
                    break
            if len(records) >= limit:
                break

    return records[:limit]


@app.get("/api/dream/config")
async def dream_config():
    """Dream-Konfiguration lesen."""
    cfg = load_config()
    return cfg.get("dream", DEFAULT_CONFIG["dream"])


# ── API: Log Management (Export, Delete, Rebuild) ──────────────────────────────

@app.get("/api/logs/export/{instance}")
async def export_user_logs(instance: str):
    """Erstellt ein ZIP aller Konversations-Logs + Dream-Tagebuch einer Instanz."""
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")
    import io
    import zipfile

    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Konversations-Logs
        conv_dir = LOG_ROOT / "conversations" / instance
        if conv_dir.exists():
            for f in sorted(conv_dir.glob("*.jsonl")):
                zf.write(f, f"conversations/{instance}/{f.name}")
                file_count += 1

        # Dream-Tagebuch
        dream_dir = LOG_ROOT / "dream" / instance
        if dream_dir.exists():
            for f in sorted(dream_dir.glob("*.jsonl")):
                zf.write(f, f"dream/{instance}/{f.name}")
                file_count += 1

        # Metadaten
        meta = {
            "export_date": datetime.now(timezone.utc).isoformat(),
            "instance": instance,
            "file_count": file_count,
        }
        zf.writestr("metadata.json", json.dumps(meta, indent=2))

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"haana-export-{instance}-{ts}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.delete("/api/logs/user/{instance}")
async def delete_user_data(instance: str, confirm: str = ""):
    """Löscht ALLE Logs und Qdrant-Memories für eine Instanz.

    Erfordert ?confirm=true als Sicherheitsabfrage.
    """
    if confirm != "true":
        raise HTTPException(
            400,
            "Sicherheitsabfrage: ?confirm=true erforderlich. "
            "Diese Aktion löscht alle Daten für diese Instanz unwiderruflich.",
        )
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")

    import shutil
    deleted_files = 0
    deleted_dirs: list[str] = []

    # 1. Konversations-Logs
    conv_dir = LOG_ROOT / "conversations" / instance
    if conv_dir.exists():
        deleted_files += sum(1 for _ in conv_dir.glob("*.jsonl"))
        shutil.rmtree(conv_dir, ignore_errors=True)
        deleted_dirs.append("conversations")

    # 2. Dream-Tagebuch
    dream_dir = LOG_ROOT / "dream" / instance
    if dream_dir.exists():
        deleted_files += sum(1 for _ in dream_dir.glob("*.jsonl"))
        shutil.rmtree(dream_dir, ignore_errors=True)
        deleted_dirs.append("dream")

    # 3. System-Logs mit Instance-Referenz (llm-calls, tool-calls, memory-ops)
    #    Diese sind nicht pro Instance aufgeteilt, daher Einträge filtern
    for cat in ("llm-calls", "tool-calls", "memory-ops"):
        cat_dir = LOG_ROOT / cat
        if not cat_dir.exists():
            continue
        for f in cat_dir.glob("*.jsonl"):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
                filtered = []
                removed = 0
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("instance") == instance:
                            removed += 1
                            continue
                    except json.JSONDecodeError:
                        pass
                    filtered.append(line)
                if removed > 0:
                    deleted_files += removed
                    if filtered:
                        f.write_text("\n".join(filtered) + "\n", encoding="utf-8")
                    else:
                        f.unlink()
            except Exception:
                pass
        if cat not in deleted_dirs and deleted_files > 0:
            deleted_dirs.append(cat)

    # 4. Extraction-Index
    from core.logger import _extraction_index_dir
    idx_file = _extraction_index_dir() / f"{instance}.json"
    if idx_file.exists():
        idx_file.unlink()

    # 5. Rebuild-Progress
    _clear_rebuild_progress(instance)

    # 6. Qdrant-Memories löschen
    deleted_vectors = 0
    import httpx
    cfg = load_config()
    qdrant_url = cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333")
    # Scopes: {instance}_memory + household_memory (nur Punkte des Users)
    scopes_to_clean = [f"{instance}_memory"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Eigenen Scope komplett löschen
            for scope in scopes_to_clean:
                try:
                    r = await client.delete(f"{qdrant_url}/collections/{scope}")
                    if r.status_code == 200:
                        deleted_vectors += 1
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[LogDelete] Qdrant-Cleanup für '{instance}' fehlgeschlagen: {e}")

    return {
        "ok": True,
        "instance": instance,
        "deleted_files": deleted_files,
        "deleted_categories": deleted_dirs,
        "deleted_qdrant_collections": scopes_to_clean if deleted_vectors > 0 else [],
    }


@app.delete("/api/logs/day/{instance}/{date}")
async def delete_day_log(instance: str, date: str):
    """Löscht die Log-Datei eines bestimmten Tages.

    Löscht NICHT automatisch zugehörige Memories (User entscheidet).
    """
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "Ungültiges Datumsformat (erwartet YYYY-MM-DD)")

    path = LOG_ROOT / "conversations" / instance / f"{date}.jsonl"
    if not path.exists():
        raise HTTPException(404, f"Keine Log-Datei für {date} gefunden")

    entries = sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
    path.unlink()

    # Extraction-Index für diesen Tag entfernen
    from core.logger import _load_extraction_index, _save_extraction_index
    index = _load_extraction_index(instance)
    if date in index:
        del index[date]
        _save_extraction_index(instance, index)

    return {"ok": True, "instance": instance, "date": date, "deleted_entries": entries}


@app.post("/api/logs/rebuild/{instance}/{date}")
async def rebuild_day_memories(instance: str, date: str):
    """Re-extrahiert Memories aus der Log-Datei eines bestimmten Tages.

    1. Löscht bestehende Memories die aus diesem Tag extrahiert wurden
    2. Re-extrahiert aus der Log-Datei
    """
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "Ungültiges Datumsformat (erwartet YYYY-MM-DD)")

    path = LOG_ROOT / "conversations" / instance / f"{date}.jsonl"
    if not path.exists():
        raise HTTPException(404, f"Keine Log-Datei für {date} gefunden")

    # Agent-Erreichbarkeit prüfen
    agent_url = _get_agent_url(instance)
    if not agent_url:
        raise HTTPException(503, f"Keine Agent-URL für '{instance}'")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{agent_url}/health")
            if not r.is_success:
                raise HTTPException(503, f"Agent '{instance}' nicht erreichbar")
    except httpx.ConnectError:
        raise HTTPException(503, f"Agent '{instance}' nicht erreichbar")

    # Einträge laden
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not _is_trivial_entry(rec):
                entries.append(rec)
        except json.JSONDecodeError:
            pass

    if not entries:
        return {"ok": True, "instance": instance, "date": date,
                "total": 0, "detail": "Keine relevanten Einträge"}

    # Async Rebuild starten
    total = len(entries)
    _rebuild_day_state = {"status": "running", "done": 0, "total": total, "errors": 0}

    async def _run():
        async with httpx.AsyncClient(timeout=120.0) as client:
            for rec in entries:
                try:
                    r = await client.post(
                        f"{agent_url}/rebuild-entry",
                        json={
                            "user": rec.get("user", ""),
                            "assistant": rec.get("assistant", ""),
                        },
                    )
                    if not r.is_success:
                        _rebuild_day_state["errors"] += 1
                except Exception:
                    _rebuild_day_state["errors"] += 1
                _rebuild_day_state["done"] += 1
            _rebuild_day_state["status"] = "done"

            # Extraction-Index aktualisieren
            from core.logger import update_extraction_index
            update_extraction_index(instance, date, str(path))

    asyncio.create_task(_run())
    return {"ok": True, "instance": instance, "date": date,
            "total": total, "status": "started"}


@app.post("/api/logs/check-rebuild/{instance}")
async def check_rebuild_changed(instance: str, auto_rebuild: str = ""):
    """Vergleicht Log-Dateien mit dem Extraction-Index.

    Findet neue oder geänderte Dateien seit der letzten Extraktion.
    Mit ?auto_rebuild=true werden geänderte Dateien automatisch re-extrahiert.
    """
    if instance not in get_all_instances():
        raise HTTPException(404, f"Instanz '{instance}' nicht gefunden")

    from core.logger import get_changed_log_files
    changed = get_changed_log_files(instance)

    if not changed:
        return {"ok": True, "instance": instance, "changed": [],
                "total_changed": 0, "auto_rebuild": False}

    do_rebuild = auto_rebuild == "true"
    rebuild_started = 0

    if do_rebuild:
        agent_url = _get_agent_url(instance)
        if not agent_url:
            return {"ok": False, "error": f"Keine Agent-URL für '{instance}'",
                    "changed": changed, "total_changed": len(changed)}

        # Agent-Erreichbarkeit prüfen
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{agent_url}/health")
                if not r.is_success:
                    return {"ok": False, "error": f"Agent '{instance}' nicht erreichbar",
                            "changed": changed, "total_changed": len(changed)}
        except Exception as e:
            return {"ok": False, "error": f"Agent nicht erreichbar: {str(e)[:100]}",
                    "changed": changed, "total_changed": len(changed)}

        # Rebuild für jede geänderte Datei triggern
        for item in changed:
            date = item["date"]
            fpath = Path(item["path"])
            if not fpath.exists():
                continue

            entries = []
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not _is_trivial_entry(rec):
                        entries.append(rec)
                except json.JSONDecodeError:
                    pass

            if not entries:
                continue

            async def _rebuild_file(ents, dt, fp, a_url):
                async with httpx.AsyncClient(timeout=120.0) as client:
                    for rec in ents:
                        try:
                            await client.post(
                                f"{a_url}/rebuild-entry",
                                json={
                                    "user": rec.get("user", ""),
                                    "assistant": rec.get("assistant", ""),
                                },
                            )
                        except Exception:
                            pass
                    from core.logger import update_extraction_index
                    update_extraction_index(instance, dt, str(fp))

            asyncio.create_task(_rebuild_file(entries, date, fpath, agent_url))
            rebuild_started += 1

    return {
        "ok": True,
        "instance": instance,
        "changed": changed,
        "total_changed": len(changed),
        "auto_rebuild": do_rebuild,
        "rebuild_started": rebuild_started,
    }


# ── MS3: Claude Code Terminal ─────────────────────────────────────────────────

@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    await _ws_terminal(websocket, load_config, _auth.is_authenticated)


@app.post("/api/terminal/set-provider")
async def terminal_set_provider(request: Request):
    body = await request.json()
    return await _term_set_provider(body.get("provider_id", ""), load_config)


@app.get("/api/terminal/status")
async def terminal_status():
    return await _term_status()


# ── MS5: Git-Integration ───────────────────────────────────────────────────────

@app.get("/api/git/status")
async def api_git_status():
    return await _git.git_status()


@app.post("/api/git/pull")
async def api_git_pull(request: Request):
    return await _git.git_pull()


@app.post("/api/git/push")
async def api_git_push(request: Request):
    return await _git.git_push()


@app.post("/api/git/connect")
async def api_git_connect(request: Request):
    body = await request.json()
    url = body.get("url", "")
    token = body.get("token", "")
    if not url.startswith(("https://", "http://")):
        return {"ok": False, "error": "URL muss mit https:// beginnen"}
    return await _git.git_connect(url, token, load_config, save_config)


@app.get("/api/git/log")
async def api_git_log():
    return await _git.git_log()
