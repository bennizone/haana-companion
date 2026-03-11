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


async def _handshake(haana_url: str, token: str, ha_url: str, session: aiohttp.ClientSession) -> None:
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

    supervisor_token = SUPERVISOR_TOKEN
    try:
        async with session.post(
            f"{haana_url}/api/companion/register",
            headers=headers,
            json={"ha_url": ha_url, "ha_token": supervisor_token},
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
<html><head><meta charset="utf-8"><title>HAANA</title></head>
<body>
<script>
try {{ window.top.location = {repr(redirect_url)}; }}
catch(e) {{ window.location = {repr(redirect_url)}; }}
</script>
<p>Weiterleitung zu HAANA... <a href="{redirect_url}">Hier klicken</a> falls nichts passiert.</p>
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

    if not haana_url:
        logger.error("haana_url ist nicht konfiguriert")
        sys.exit(1)
    if not token:
        logger.error("companion_token ist nicht konfiguriert")
        sys.exit(1)
    if not ha_url:
        logger.warning("ha_url ist nicht konfiguriert — Home Assistant URL wird nicht an HAANA registriert")

    logger.info(f"HAANA Companion startet — HAANA_URL={haana_url} Port={PORT}")

    async with aiohttp.ClientSession() as session:
        await _handshake(haana_url, token, ha_url, session)

    app = create_app(haana_url, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Companion SSO-Gateway läuft auf Port {PORT}")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
