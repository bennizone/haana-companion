"""
HAANA Terminal – WebSocket-PTY-Bridge für Claude Code

Routen (werden in main.py eingebunden):
  WS   /ws/terminal                  → PTY-Bridge zu tmux-Session
  POST /api/terminal/set-provider    → Provider-Env in tmux-Session setzen
  GET  /api/terminal/status          → tmux + claude Verfügbarkeit prüfen
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import select
import struct
import subprocess
import termios

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

TMUX_SESSION = "haana-dev"


# ── tmux-Helpers ──────────────────────────────────────────────────────────────

def _ensure_tmux_session() -> bool:
    """Stellt sicher dass tmux-Session 'haana-dev' existiert."""
    check = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    if check.returncode == 0:
        return True
    create = subprocess.run(
        ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-x", "220", "-y", "50"],
        capture_output=True,
    )
    return create.returncode == 0


def _get_provider_env(provider: dict) -> dict:
    """Gibt Env-Vars für Claude Code basierend auf Provider-Typ zurück."""
    if not provider:
        return {}
    provider_type = provider.get("type", "")
    auth_method = provider.get("auth_method", "")
    if provider_type == "anthropic":
        if auth_method == "oauth":
            return {"CLAUDE_CONFIG_DIR": provider.get("oauth_dir", "/claude-auth")}
        else:
            # api_key oder Default
            return {"ANTHROPIC_API_KEY": provider.get("key", "")}
    else:
        # Andere Provider (OpenAI-compat etc.)
        env = {}
        if provider.get("url"):
            env["ANTHROPIC_BASE_URL"] = provider["url"]
        if provider.get("key"):
            env["ANTHROPIC_API_KEY"] = provider["key"]
        return env


def _find_provider_by_id(config: dict, provider_id: str) -> dict | None:
    """Sucht Provider in der Config anhand der ID."""
    providers = config.get("llm_providers", [])
    for p in providers:
        if str(p.get("id", "")) == str(provider_id) or p.get("name", "") == provider_id:
            return p
    return None


# ── Auth-Helper ───────────────────────────────────────────────────────────────

def _ws_is_authenticated(websocket: WebSocket, auth_fn) -> bool:
    """
    Prüft ob die WebSocket-Verbindung authentifiziert ist.

    FastAPI WebSocket hat dieselben .cookies und .headers wie Request,
    daher kann auth_fn (is_authenticated) direkt damit aufgerufen werden.
    Fallback: ?token= Query-Param wird als Cookie-Wert simuliert.
    """
    # Direkt via WebSocket (hat .cookies + .headers wie Request)
    try:
        if auth_fn(websocket):
            return True
    except Exception:
        pass

    # Fallback: ?token= Query-Parameter gegen Admin-Token prüfen
    import secrets as _secrets
    import auth as _auth_mod
    token = websocket.query_params.get("token")
    if token:
        try:
            expected = _auth_mod.get_admin_token()
            return _secrets.compare_digest(token, expected)
        except Exception:
            pass
    return False


# ── WebSocket-PTY-Bridge ──────────────────────────────────────────────────────

async def ws_terminal(websocket: WebSocket, load_config_fn, auth_fn):
    """
    WebSocket-PTY-Bridge. Wird von main.py aufgerufen.

    Ablauf:
    1. Auth prüfen: Cookie 'haana_session' oder Query-Param 'token'
    2. tmux-Session sicherstellen
    3. PTY öffnen: master_fd, slave_fd = pty.openpty()
    4. subprocess.Popen(["tmux", "attach-session", "-t", TMUX_SESSION], ...)
    5. Bidirektionale asyncio-Loop:
       - WS → PTY: ws.receive_bytes() → os.write(master_fd)
       - PTY → WS: run_in_executor(select.select) → os.read(master_fd) → ws.send_bytes()
    6. Bei Disconnect: PTY schließen, tmux-Session bleibt bestehen
    """
    # 1. Auth prüfen
    if not _ws_is_authenticated(websocket, auth_fn):
        await websocket.accept()
        await websocket.close(code=4403, reason="Unauthorized")
        return

    await websocket.accept()

    # 2. Config laden und Provider-Env bestimmen
    provider_env: dict = {}
    try:
        config = load_config_fn()
        # Optionaler Provider via Query-Param
        provider_id = websocket.query_params.get("provider_id")
        if provider_id:
            provider = _find_provider_by_id(config, provider_id)
            if provider:
                provider_env = _get_provider_env(provider)
    except Exception as exc:
        logger.warning("terminal: Konnte Config nicht laden: %s", exc)

    # 3. tmux-Session sicherstellen
    if not _ensure_tmux_session():
        await websocket.send_bytes(b"\r\n[HAANA] Fehler: tmux-Session konnte nicht erstellt werden.\r\n")
        await websocket.close()
        return

    # 4. PTY öffnen
    master_fd, slave_fd = pty.openpty()

    env = {**os.environ, **provider_env}

    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", TMUX_SESSION],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )

    # slave_fd wird vom Child-Prozess genutzt, im Parent schließen
    os.close(slave_fd)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def pty_to_ws():
        """Liest PTY-Output und sendet an WebSocket."""
        try:
            while not stop_event.is_set():
                try:
                    ready, _, _ = await loop.run_in_executor(
                        None,
                        lambda: select.select([master_fd], [], [], 0.1),
                    )
                except (ValueError, OSError):
                    break

                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        await websocket.send_bytes(data)
                    except OSError:
                        break
                    except Exception as exc:
                        logger.debug("terminal: pty_to_ws Fehler: %s", exc)
                        break

                if proc.poll() is not None:
                    break
        finally:
            stop_event.set()

    async def ws_to_pty():
        """Empfängt WebSocket-Daten und schreibt in PTY."""
        try:
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    elif msg["type"] == "websocket.receive":
                        if msg.get("text"):  # JSON-Text-Frame (z.B. resize)
                            try:
                                data = json.loads(msg["text"])
                                if data.get("type") == "resize":
                                    cols = int(data.get("cols", 80))
                                    rows = int(data.get("rows", 24))
                                    # PTY-Größe setzen
                                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                                struct.pack("HHHH", rows, cols, 0, 0))
                                    # tmux-Fenstergröße auch anpassen
                                    subprocess.run(["tmux", "resize-window", "-t", TMUX_SESSION,
                                                    "-x", str(cols), "-y", str(rows)],
                                                   capture_output=True)
                            except Exception:
                                pass
                        elif msg.get("bytes"):  # Binary-Frame (Tastatureingabe)
                            os.write(master_fd, msg["bytes"])
                except WebSocketDisconnect:
                    break
                except OSError:
                    break
                except Exception as exc:
                    logger.debug("terminal: ws_to_pty Fehler: %s", exc)
                    break
        finally:
            stop_event.set()

    # 5. Bidirektionale Loop
    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    except Exception as exc:
        logger.debug("terminal: gather beendet: %s", exc)
    finally:
        # 6. Cleanup: PTY schließen, tmux-Session bleibt bestehen
        stop_event.set()
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        logger.info("terminal: WebSocket-Verbindung getrennt (tmux-Session bleibt bestehen)")


# ── REST-Endpoints ────────────────────────────────────────────────────────────

async def set_provider(provider_id: str, load_config_fn) -> dict:
    """Setzt Provider-Env-Vars in der tmux-Session via 'tmux setenv'."""
    if not provider_id:
        return {"ok": False, "error": "Keine Provider-ID angegeben"}

    try:
        config = load_config_fn()
    except Exception as exc:
        return {"ok": False, "error": f"Config-Fehler: {exc}"}

    provider = _find_provider_by_id(config, provider_id)
    if not provider:
        return {"ok": False, "error": f"Provider '{provider_id}' nicht gefunden"}

    env_vars = _get_provider_env(provider)

    # tmux-Session sicherstellen
    _ensure_tmux_session()

    # Env-Vars in tmux-Session setzen
    for key, value in env_vars.items():
        result = subprocess.run(
            ["tmux", "setenv", "-t", TMUX_SESSION, key, value],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning("terminal: tmux setenv %s fehlgeschlagen: %s", key, result.stderr)

    provider_name = provider.get("name", provider_id)
    logger.info("terminal: Provider '%s' in tmux-Session gesetzt (%d Env-Vars)", provider_name, len(env_vars))

    return {"ok": True, "provider": provider_name, "env_vars_set": list(env_vars.keys())}


async def get_status() -> dict:
    """Prüft tmux-Session und claude-Verfügbarkeit."""
    # tmux-Session prüfen
    check = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    session_active = check.returncode == 0

    # claude-Verfügbarkeit prüfen
    claude_available = False
    which_result = subprocess.run(
        ["which", "claude"],
        capture_output=True,
    )
    if which_result.returncode == 0:
        claude_available = True
    elif os.path.exists("/usr/local/bin/claude"):
        claude_available = True

    return {
        "session_active": session_active,
        "claude_available": claude_available,
        "tmux_session": TMUX_SESSION,
    }
