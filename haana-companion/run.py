"""
HAANA Companion Addon — run.py

SSO-Gateway: Prüft HA-Admin-Status, holt Einmal-Token von HAANA,
leitet Browser direkt zu HAANA weiter. Kein Proxy.
"""

import asyncio
import json
import logging
import os
import sys
from urllib.parse import urlparse as _urlparse

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("haana-companion")

OPTIONS_FILE = "/data/options.json"
PORT = 8099
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _load_options() -> dict:
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


async def _detect_ha_url(session: aiohttp.ClientSession) -> str:
    """Ermittelt die HA-URL automatisch über den Supervisor."""
    if not SUPERVISOR_TOKEN:
        return ""
    try:
        async with session.get(
            "http://supervisor/core/info",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
            ip = data.get("data", {}).get("ip_address", "")
            if ip:
                url = f"http://{ip}:8123"
                logger.info(f"HA-URL automatisch erkannt: {url}")
                return url
    except Exception as e:
        logger.warning(f"HA-URL Erkennung fehlgeschlagen: {e}")
    return ""


async def _fetch_ha_persons(session: aiohttp.ClientSession) -> list:
    """Holt Person-Entitaeten aus HA via Supervisor-Proxy."""
    if not SUPERVISOR_TOKEN:
        return []
    try:
        async with session.get(
            "http://supervisor/core/api/states",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                logger.warning(f"HA States-Abfrage fehlgeschlagen: HTTP {r.status}")
                return []
            states = await r.json()
            persons = []
            for state in states:
                eid = state.get("entity_id", "")
                if eid.startswith("person."):
                    uid = eid[len("person."):]
                    name = state.get("attributes", {}).get("friendly_name", uid)
                    persons.append({"id": eid, "uid": uid, "display_name": name})
            logger.info(f"HA Personen geladen: {len(persons)}")
            return persons
    except Exception as e:
        logger.warning(f"HA Personen-Abfrage fehlgeschlagen: {e}")
        return []


async def _check_ha_mcp_addon(session: aiohttp.ClientSession, ha_ip: str) -> dict:
    """Prueft ob das ha-mcp Addon installiert und aktiv ist."""
    if not SUPERVISOR_TOKEN:
        return {"installed": False}
    try:
        async with session.get(
            "http://supervisor/addons/ha_mcp/info",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 404:
                return {"installed": False}
            if resp.status != 200:
                logger.warning(f"ha-mcp Addon-Info fehlgeschlagen: HTTP {resp.status}")
                return {"installed": False}
            data = await resp.json()
            addon = data.get("data", data)
            state = addon.get("state", "")
            running = state == "started"
            url = f"http://{ha_ip}:9583" if ha_ip else ""
            logger.info(f"ha-mcp Addon gefunden: state={state}")
            return {"installed": True, "running": running, "url": url}
    except Exception as e:
        logger.warning(f"ha-mcp Addon-Check fehlgeschlagen: {e}")
        return {"installed": False}


async def _handshake(haana_url: str, token: str, ha_url: str, ha_ip: str, session: aiohttp.ClientSession) -> None:
    """Ping + Register beim HAANA-Stack."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get(f"{haana_url}/api/companion/ping", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                logger.info("Companion-Ping erfolgreich")
            else:
                logger.warning(f"Companion-Ping fehlgeschlagen: HTTP {r.status}")
    except Exception as e:
        logger.warning(f"Companion-Ping Fehler: {e}")
        return

    ha_persons = await _fetch_ha_persons(session)
    ha_mcp_info = await _check_ha_mcp_addon(session, ha_ip)
    supervisor_token = SUPERVISOR_TOKEN
    try:
        async with session.post(
            f"{haana_url}/api/companion/register",
            headers=headers,
            json={
                "ha_url": ha_url,
                "ha_token": supervisor_token,
                "ha_persons": ha_persons,
                "ha_mcp": ha_mcp_info,
            },
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                logger.info("Companion-Register erfolgreich")
            else:
                logger.warning(f"Companion-Register Fehler: HTTP {r.status}")
    except Exception as e:
        logger.warning(f"Companion-Register Fehler: {e}")


async def _is_ha_admin(ha_user_token: str, session: aiohttp.ClientSession) -> bool:
    """Prüft via HA Supervisor ob der User Admin-Rechte hat."""
    if not ha_user_token or not SUPERVISOR_TOKEN:
        return True  # Kein HA-Kontext — durchlassen
    try:
        async with session.post(
            "http://supervisor/auth",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            json={"token": ha_user_token},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"HA Auth-Check: HTTP {resp.status}")
                return False
            data = await resp.json()
            return data.get("is_admin", False)
    except Exception as e:
        logger.warning(f"HA Auth-Check fehlgeschlagen: {e}")
        return False


async def _ws_person_watcher(haana_url: str, token: str) -> None:
    """WebSocket-Loop: Abonniert HA state_changed Events fuer person.* Entitaeten."""
    backoff = 5
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                logger.info("HA WebSocket: Verbinde zu ws://supervisor/core/websocket")
                async with session.ws_connect(
                    "ws://supervisor/core/websocket",
                    headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as ws:
                    backoff = 5  # Reset bei erfolgreicher Verbindung

                    # Schritt 1: auth_required abwarten
                    auth_req = await ws.receive_json()
                    if auth_req.get("type") != "auth_required":
                        logger.warning(f"HA WebSocket: Unerwartete erste Nachricht: {auth_req.get('type')}")
                        continue

                    # Schritt 2: Auth senden
                    await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})

                    # Schritt 3: auth_ok abwarten
                    auth_resp = await ws.receive_json()
                    if auth_resp.get("type") != "auth_ok":
                        logger.warning(f"HA WebSocket: Auth fehlgeschlagen: {auth_resp.get('type')}")
                        continue
                    logger.info("HA WebSocket: Auth erfolgreich")

                    # Schritt 4: state_changed Events abonnieren
                    await ws.send_json({
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    })

                    # Bestätigung abwarten
                    sub_resp = await ws.receive_json()
                    if not sub_resp.get("success"):
                        logger.warning(f"HA WebSocket: subscribe_events fehlgeschlagen: {sub_resp}")
                        continue
                    logger.info("HA WebSocket: state_changed abonniert — warte auf person.* Events")

                    # Schritt 5: Event-Loop
                    async for raw in ws:
                        if raw.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event_msg = raw.json()
                            except Exception:
                                continue
                            if event_msg.get("type") != "event":
                                continue
                            event_data = event_msg.get("event", {}).get("data", {})
                            entity_id = event_data.get("entity_id", "")
                            if not entity_id.startswith("person."):
                                continue
                            logger.info(f"HA WebSocket: person-Event fuer {entity_id} — aktualisiere Personen")
                            try:
                                persons = await _fetch_ha_persons(session)
                                headers = {"Authorization": f"Bearer {token}"}
                                async with session.post(
                                    f"{haana_url}/api/companion/refresh-persons",
                                    headers=headers,
                                    json={"ha_persons": persons},
                                    timeout=aiohttp.ClientTimeout(total=5),
                                ) as r:
                                    if r.status == 200:
                                        logger.info(f"HA WebSocket: Personen aktualisiert ({len(persons)} Eintraege)")
                                    else:
                                        logger.warning(f"HA WebSocket: refresh-persons HTTP {r.status}")
                            except Exception as push_err:
                                logger.warning(f"HA WebSocket: Push zu HAANA fehlgeschlagen: {push_err}")
                        elif raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            logger.warning("HA WebSocket: Verbindung geschlossen")
                            break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"HA WebSocket: Fehler — {e} — Reconnect in {backoff}s")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


def create_app(haana_url: str, token: str) -> web.Application:
    app = web.Application()

    async def sso_handler(request: web.Request) -> web.Response:
        ingress_path = request.headers.get("X-Ingress-Path", "")

        # HA Admin-Check (nur bei Ingress-Requests)
        if ingress_path:
            raw_auth = request.headers.get("Authorization", "")
            ha_user_token = raw_auth.removeprefix("Bearer ").strip()
            async with aiohttp.ClientSession() as auth_session:
                if not await _is_ha_admin(ha_user_token, auth_session):
                    logger.warning("Zugriff verweigert: kein HA-Admin")
                    return web.Response(
                        status=403,
                        content_type="text/html",
                        text="<h2>Zugriff verweigert</h2><p>Nur Home Assistant Admins haben Zugriff auf HAANA.</p>",
                    )

        # SSO-Token bei HAANA holen
        try:
            async with aiohttp.ClientSession() as session_sso:
                async with session_sso.post(
                    f"{haana_url}/api/companion/sso",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"SSO-Token Fehler: HTTP {resp.status} — {text[:200]}")
                        return web.Response(status=502, text="HAANA SSO nicht verfügbar.")
                    data = await resp.json()
                    sso_token = data["sso_token"]
        except Exception as e:
            logger.error(f"HAANA nicht erreichbar: {e}")
            return web.Response(status=502, text=f"HAANA nicht erreichbar: {e}")

        redirect_url = f"{haana_url}/api/auth/sso?token={sso_token}"
        logger.info(f"SSO-Redirect → {haana_url}/api/auth/sso?token=***")
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HAANA</title>
<style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f5f5}}
.box{{text-align:center;background:white;padding:32px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1)}}
a{{display:inline-block;margin-top:16px;padding:12px 24px;background:#0073e6;color:white;border-radius:8px;text-decoration:none;font-size:1rem}}</style>
</head>
<body>
<div class="box">
  <h2>HAANA Admin</h2>
  <p>HAANA öffnet sich in einem neuen Tab.</p>
  <a href="{redirect_url}" target="_blank" id="link">HAANA öffnen →</a>
</div>
<script>window.open({repr(redirect_url)}, '_blank'); document.getElementById('link').textContent='HAANA erneut öffnen →';</script>
</body></html>"""
        return web.Response(content_type="text/html", text=html)

    app.router.add_route("*", "/", sso_handler)
    app.router.add_route("*", "/{path:.*}", sso_handler)
    return app


async def main() -> None:
    try:
        options = _load_options()
    except Exception as e:
        logger.error(f"Optionen konnten nicht gelesen werden: {e}")
        sys.exit(1)

    haana_url = options.get("haana_url", "").rstrip("/")
    token = options.get("companion_token", "")
    ha_url = options.get("ha_url", "").rstrip("/")
    if not ha_url:
        async with aiohttp.ClientSession() as detect_session:
            ha_url = await _detect_ha_url(detect_session)
        if not ha_url:
            logger.warning("ha_url nicht konfiguriert und automatische Erkennung fehlgeschlagen — HA-Integration deaktiviert")

    if not haana_url:
        logger.error("haana_url ist nicht konfiguriert")
        sys.exit(1)
    if not token:
        logger.error("companion_token ist nicht konfiguriert")
        sys.exit(1)

    logger.info(f"HAANA Companion startet — HAANA_URL={haana_url} Port={PORT}")

    # ha_ip fuer ha-mcp URL extrahieren
    ha_ip = ""
    if ha_url:
        parsed = _urlparse(ha_url)
        ha_ip = parsed.hostname or ""

    async with aiohttp.ClientSession() as session:
        await _handshake(haana_url, token, ha_url, ha_ip, session)

    app = create_app(haana_url, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Companion SSO-Gateway läuft auf Port {PORT}")

    # WebSocket Person-Watcher als Background-Task (nur mit Supervisor-Token)
    ws_task = None
    if SUPERVISOR_TOKEN:
        ws_task = asyncio.create_task(_ws_person_watcher(haana_url, token))
        logger.info("HA WebSocket Person-Watcher gestartet")
    else:
        logger.info("Kein SUPERVISOR_TOKEN — WebSocket Person-Watcher deaktiviert")

    try:
        await asyncio.Event().wait()
    finally:
        if ws_task:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
