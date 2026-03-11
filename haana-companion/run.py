"""
HAANA Companion Addon — run.py

Minimaler aiohttp-Proxy-Server, der den HAANA-Stack in HA einbindet.
- Liest Optionen aus /data/options.json (HA Addon Standard)
- Handshake mit HAANA beim Start (ping + register)
- GET /api/status: Erreichbarkeit des HAANA-Stacks
- Alle anderen Requests werden transparent an HAANA_URL proxiert
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


async def _is_ha_admin(ha_user_token: str, session: aiohttp.ClientSession) -> bool:
    """Prueft via HA Supervisor ob der User Admin-Rechte hat."""
    if not ha_user_token or not SUPERVISOR_TOKEN:
        # Kein HA-Kontext (z.B. direkte API-Nutzung) — durchlassen
        return True
    try:
        async with session.post(
            "http://supervisor/auth",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            json={"token": ha_user_token},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"HA Auth-Check: HTTP {resp.status} — kein Admin")
                return False
            data = await resp.json()
            return data.get("is_admin", False)
    except Exception as e:
        logger.warning(f"HA Auth-Check fehlgeschlagen: {e} — Zugriff verweigert")
        return False


def _load_options() -> dict:
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


async def _handshake(haana_url: str, token: str, session: aiohttp.ClientSession) -> bool:
    """Ping + Register beim HAANA-Stack."""
    headers = {"Authorization": f"Bearer {token}"}

    # Ping
    try:
        async with session.get(f"{haana_url}/api/companion/ping", headers=headers) as r:
            if r.status != 200:
                logger.warning(f"Companion-Ping fehlgeschlagen: HTTP {r.status}")
                return False
            logger.info("Companion-Ping erfolgreich")
    except Exception as e:
        logger.warning(f"Companion-Ping Fehler: {e}")
        return False

    # Register
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    ha_url = "http://supervisor/core"
    try:
        async with session.post(
            f"{haana_url}/api/companion/register",
            headers=headers,
            json={"ha_url": ha_url, "ha_token": supervisor_token},
        ) as r:
            if r.status == 200:
                logger.info("Companion-Register erfolgreich")
            else:
                text = await r.text()
                logger.warning(f"Companion-Register Fehler: HTTP {r.status} — {text[:100]}")
    except Exception as e:
        logger.warning(f"Companion-Register Fehler: {e}")

    return True


async def _check_haana_reachable(haana_url: str, token: str, session: aiohttp.ClientSession) -> bool:
    """Prueft ob HAANA erreichbar ist."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            f"{haana_url}/api/companion/ping",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            return r.status == 200
    except Exception:
        return False


def create_app(haana_url: str, token: str) -> web.Application:
    app = web.Application()

    async def status_handler(request: web.Request) -> web.Response:
        async with aiohttp.ClientSession() as session:
            reachable = await _check_haana_reachable(haana_url, token, session)
        return web.json_response({"haana_reachable": reachable})

    async def proxy_handler(request: web.Request) -> web.Response:
        path = request.match_info.get("path", "")
        target_url = f"{haana_url}/{path}"
        if request.query_string:
            target_url = f"{target_url}?{request.query_string}"

        # WebSocket-Upgrade ablehnen
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return web.Response(status=501, text="WebSocket wird vom Companion nicht unterstuetzt")

        # Ingress-Basis-Pfad aus HA-Header lesen
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")

        # HA Admin-Check: nur wenn Request ueber HA Ingress kommt
        if ingress_path:  # ingress_path ist bereits aus X-Ingress-Path gelesen
            raw_auth = request.headers.get("Authorization", "")
            ha_user_token = raw_auth.removeprefix("Bearer ").strip()
            async with aiohttp.ClientSession() as auth_session:
                if not await _is_ha_admin(ha_user_token, auth_session):
                    logger.warning(f"Zugriff verweigert: User ist kein HA-Admin")
                    return web.Response(
                        status=403,
                        content_type="text/html",
                        text="<h2>Zugriff verweigert</h2><p>Nur Home Assistant Admins haben Zugriff auf HAANA.</p>",
                    )

        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "authorization", "content-length")
        }
        headers["Authorization"] = f"Bearer {token}"

        try:
            body = await request.read()
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=body or None,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    resp_body = await resp.read()
                    content_type = resp.headers.get("Content-Type", "")
                    resp_headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
                    }

                    # HTML-Antworten: absolute Pfade für Ingress rewriten
                    if ingress_path and "text/html" in content_type:
                        try:
                            html = resp_body.decode("utf-8", errors="replace")
                            # <base> Tag in <head> injizieren
                            base_tag = f'<base href="{ingress_path}/">'
                            html = html.replace("<head>", f"<head>{base_tag}", 1)
                            # Absolute href/src Pfade rewriten (für ältere Browser ohne base-Tag Support)
                            html = html.replace('href="/', f'href="{ingress_path}/')
                            html = html.replace("href='/", f"href='{ingress_path}/")
                            html = html.replace('src="/', f'src="{ingress_path}/')
                            html = html.replace("src='/", f"src='{ingress_path}/")
                            # fetch/axios URL-Rewrites im JS (für API-Calls)
                            html = html.replace("fetch('/", f"fetch('{ingress_path}/")
                            resp_body = html.encode("utf-8")
                        except Exception as e:
                            logger.warning(f"[proxy] HTML-Rewrite fehlgeschlagen: {e}")

                    return web.Response(
                        status=resp.status,
                        headers=resp_headers,
                        body=resp_body,
                    )
        except aiohttp.ClientConnectorError as e:
            logger.error(f"[proxy] HAANA nicht erreichbar: {e}")
            return web.Response(status=502, text=f"HAANA nicht erreichbar: {e}")
        except Exception as e:
            logger.error(f"[proxy] Fehler: {e}")
            return web.Response(status=500, text=f"Proxy-Fehler: {e}")

    app.router.add_get("/api/status", status_handler)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    return app


async def main() -> None:
    try:
        options = _load_options()
    except Exception as e:
        logger.error(f"Optionen konnten nicht gelesen werden: {e}")
        sys.exit(1)

    haana_url = options.get("haana_url", "").rstrip("/")
    token = options.get("companion_token", "")

    if not haana_url:
        logger.error("haana_url ist nicht konfiguriert")
        sys.exit(1)
    if not token:
        logger.error("companion_token ist nicht konfiguriert")
        sys.exit(1)

    logger.info(f"HAANA Companion startet — HAANA_URL={haana_url} Port={PORT}")

    async with aiohttp.ClientSession() as session:
        ok = await _handshake(haana_url, token, session)
        if not ok:
            logger.warning("Handshake fehlgeschlagen — Companion laeuft trotzdem weiter")

    app = create_app(haana_url, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Companion laeuft auf Port {PORT}")

    # Laufen bis SIGTERM
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
