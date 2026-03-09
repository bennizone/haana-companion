"""
HAANA Proaktive Benachrichtigungen

FastAPI-Router fuer proaktive Nachrichten an User, ausgeloest durch
Home Assistant Automationen (Webhooks).

Flow:
  1. HA-Automation sendet POST an /api/notify/webhook
  2. Nachricht wird an den laufenden Agent gesendet (via Agent-Chat-Endpoint)
  3. Agent-Antwort wird an den gewuenschten Channel weitergeleitet (WhatsApp etc.)

Mount in main.py:
  from core.notify import create_notify_router
  router = create_notify_router(get_agent_url_fn, get_config_fn)
  app.include_router(router)
"""

import logging
import time
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

# Prioritaeten bestimmen Verhalten
PRIORITIES = ("low", "normal", "high", "critical")


def create_notify_router(
    get_agent_url: Callable[[str], Optional[str]],
    get_config: Callable[[], dict],
) -> APIRouter:
    """Erstellt den Notify-Router.

    Args:
        get_agent_url: Funktion (instance_id) -> Agent-HTTP-URL oder None.
        get_config: Funktion () -> aktuelle HAANA-Config (fuer WhatsApp-Bridge-URL etc.).
    """
    router = APIRouter(prefix="/api/notify", tags=["notify"])

    def _get_whatsapp_bridge_url() -> Optional[str]:
        """WhatsApp-Bridge-URL aus Config oder Env ableiten."""
        cfg = get_config()
        services = cfg.get("services", {})
        url = services.get("whatsapp_bridge_url", "").strip()
        if url:
            return url.rstrip("/")
        # Fallback: Docker-Compose Standard
        import os
        env_url = os.environ.get("WHATSAPP_BRIDGE_URL", "").strip()
        if env_url:
            return env_url.rstrip("/")
        return None

    def _find_user_jid(instance: str) -> Optional[str]:
        """WhatsApp-JID fuer eine Instanz aus Config suchen."""
        cfg = get_config()
        for user in cfg.get("users", []):
            if user.get("id") == instance:
                jid = user.get("whatsapp_jid", "").strip()
                return jid if jid else None
        return None

    @router.post("/webhook")
    async def notify_webhook(request: Request):
        """Webhook fuer HA-Automationen.

        Erwartet JSON:
          {
            "instance": "benni",           # Pflicht: Agent-Instanz
            "message": "Waschmaschine fertig!",  # Pflicht: Nachricht/Event-Beschreibung
            "event": "washer_done",        # Optional: Event-Typ (fuer Logging)
            "channel": "whatsapp",         # Optional: Ziel-Channel (default: whatsapp)
            "priority": "normal"           # Optional: low/normal/high/critical
          }

        Der Agent erhaelt die Nachricht als System-Notification und formuliert
        eine passende Antwort, die dann an den User gesendet wird.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Ungueltiges JSON")

        instance = (body.get("instance") or "").strip()
        message = (body.get("message") or "").strip()
        event = body.get("event", "generic")
        channel = body.get("channel", "whatsapp")
        priority = body.get("priority", "normal")

        if not instance:
            raise HTTPException(400, "instance ist Pflichtfeld")
        if not message:
            raise HTTPException(400, "message ist Pflichtfeld")
        if priority not in PRIORITIES:
            raise HTTPException(400, f"priority muss einer von {PRIORITIES} sein")

        logger.info(
            f"[notify] Webhook: instance={instance} event={event} "
            f"channel={channel} priority={priority} msg={message[:80]}"
        )

        # 1. Agent-Antwort holen
        agent_url = get_agent_url(instance)
        if not agent_url:
            raise HTTPException(
                503,
                f"Keine Agent-URL fuer Instanz '{instance}' verfuegbar. Agent laeuft nicht?",
            )

        # Prompt fuer den Agent: klar als System-Notification markiert
        agent_prompt = (
            f"[SYSTEM-NOTIFICATION | Event: {event} | Prioritaet: {priority}]\n"
            f"{message}\n\n"
            "Formuliere eine kurze, freundliche Benachrichtigung fuer den User. "
            "Halte dich kurz und praegnant."
        )

        t_start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{agent_url}/chat",
                    json={"message": agent_prompt, "channel": channel},
                )
                r.raise_for_status()
                agent_response = r.json().get("response", "")
        except httpx.TimeoutException:
            logger.error(f"[notify] Agent-Timeout fuer {instance}")
            raise HTTPException(504, f"Agent '{instance}' antwortet nicht (Timeout)")
        except httpx.HTTPStatusError as e:
            logger.error(f"[notify] Agent-Fehler: {e.response.status_code}")
            raise HTTPException(502, f"Agent-Fehler: HTTP {e.response.status_code}")
        except Exception as e:
            logger.error(f"[notify] Agent nicht erreichbar: {e}")
            raise HTTPException(502, f"Agent '{instance}' nicht erreichbar: {e}")

        elapsed = time.monotonic() - t_start
        logger.info(
            f"[notify] Agent-Antwort in {elapsed:.2f}s: {agent_response[:80]}"
        )

        # 2. Antwort an Channel senden
        delivery_result = {"sent": False, "channel": channel}

        if channel == "whatsapp":
            delivery_result = await _deliver_whatsapp(
                instance, agent_response, priority
            )
        else:
            # Andere Channels (webchat etc.): Antwort wird nur zurueckgegeben
            delivery_result = {"sent": True, "channel": channel, "note": "response_only"}

        return {
            "ok": True,
            "instance": instance,
            "event": event,
            "agent_response": agent_response,
            "delivery": delivery_result,
            "elapsed_s": round(elapsed, 2),
        }

    async def _deliver_whatsapp(
        instance: str, message: str, priority: str
    ) -> dict:
        """Sendet eine Nachricht via WhatsApp-Bridge an den User.

        Die Bridge muss den POST /send Endpoint unterstuetzen.
        Falls die Bridge (noch) keinen /send Endpoint hat, wird ein
        Hinweis zurueckgegeben.
        """
        bridge_url = _get_whatsapp_bridge_url()
        if not bridge_url:
            return {
                "sent": False,
                "channel": "whatsapp",
                "error": "WhatsApp-Bridge-URL nicht konfiguriert",
            }

        jid = _find_user_jid(instance)
        if not jid:
            return {
                "sent": False,
                "channel": "whatsapp",
                "error": f"Keine WhatsApp-JID fuer Instanz '{instance}' konfiguriert",
            }

        # JID normalisieren: sicherstellen dass @s.whatsapp.net Suffix vorhanden
        if "@" not in jid:
            jid = f"{jid}@s.whatsapp.net"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{bridge_url}/send",
                    json={
                        "jid": jid,
                        "message": message,
                        "priority": priority,
                    },
                )
                r.raise_for_status()
                return {"sent": True, "channel": "whatsapp", "jid": jid}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Bridge hat keinen /send Endpoint → Feature muss noch aktiviert werden
                return {
                    "sent": False,
                    "channel": "whatsapp",
                    "error": "Bridge /send Endpoint nicht verfuegbar. "
                    "Siehe docs/NOTIFICATIONS.md fuer Setup-Anleitung.",
                }
            return {
                "sent": False,
                "channel": "whatsapp",
                "error": f"Bridge HTTP {e.response.status_code}",
            }
        except Exception as e:
            logger.error(f"[notify] WhatsApp-Zustellung fehlgeschlagen: {e}")
            return {
                "sent": False,
                "channel": "whatsapp",
                "error": str(e)[:200],
            }

    @router.get("/health")
    async def notify_health():
        """Health-Check fuer den Notify-Service."""
        bridge_url = _get_whatsapp_bridge_url()
        bridge_ok = False
        if bridge_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{bridge_url}/status")
                    bridge_ok = r.is_success and r.json().get("status") == "connected"
            except Exception:
                pass

        return {
            "ok": True,
            "whatsapp_bridge_configured": bridge_url is not None,
            "whatsapp_bridge_connected": bridge_ok,
        }

    return router
