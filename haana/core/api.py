"""
HAANA Agent HTTP-API

Stellt einen HTTP- und WebSocket-Endpunkt für einen Agent bereit.
Wird gestartet wenn HAANA_API_PORT gesetzt ist (parallel zum REPL).

Endpunkte:
  GET  /health        → {"ok": true, "instance": "benni"}
  POST /chat          → {"message": "...", "channel": "webchat"} → {"response": "..."}
  WS   /ws            → bidirektionale Konversation (für Webchat-Live-Chat)
"""

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


def create_api(agent) -> FastAPI:
    """Erstellt die FastAPI-App für eine Agent-Instanz."""
    api = FastAPI(title=f"HAANA {agent.instance}", docs_url=None, redoc_url=None)

    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST", "GET"],
        allow_headers=["*"],
    )

    @api.get("/health")
    async def health():
        return {
            "ok": True,
            "instance": agent.instance,
            "window_size": agent.memory._window.size(),
            "pending_extractions": agent.memory.pending_count(),
        }

    @api.post("/chat")
    async def chat(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Ungültiges JSON")
        message = (body.get("message") or "").strip()
        channel = body.get("channel", "webchat")
        if not message:
            raise HTTPException(400, "message darf nicht leer sein")
        logger.info(f"[{agent.instance}] API /chat | channel={channel} | {message[:80]}")
        response = await agent.run_async(message, channel=channel)
        return {"response": response, "instance": agent.instance}

    @api.post("/rebuild-entry")
    async def rebuild_entry(request: Request):
        """
        Fügt einen einzelnen Konversations-Eintrag direkt in Qdrant ein.
        Wird vom Admin-Interface für den Memory-Rebuild aus Logs aufgerufen.
        Läuft synchron (kein Sliding Window, kein LLM-Skip – volle Mem0-Extraktion).
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Ungültiges JSON")

        user_msg  = (body.get("user")      or "").strip()
        asst_msg  = (body.get("assistant") or "").strip()
        scope_req = body.get("scope")

        if not user_msg and not asst_msg:
            raise HTTPException(400, "user oder assistant fehlt")

        # Scope bestimmen: explizit > aus Agentenantwort > persönlicher Fallback
        if scope_req and scope_req in agent.memory.write_scopes:
            scope = scope_req
        else:
            scope = agent.memory._resolve_scope(asst_msg, None)

        if scope is None:
            return {"ok": False, "error": "Scope-Klassifikation fehlgeschlagen (Ollama erreichbar?)"}

        messages = [
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": asst_msg},
        ]
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, agent.memory.add, messages, scope)
        return {"ok": success, "scope": scope}

    @api.websocket("/ws")
    async def websocket_chat(ws: WebSocket):
        await ws.accept()
        logger.info(f"[{agent.instance}] WebSocket verbunden")
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({"type": "error", "text": "Ungültiges JSON"}))
                    continue

                message = (msg.get("message") or "").strip()
                if not message:
                    continue

                # Typing-Indikator sofort senden
                await ws.send_text(json.dumps({"type": "typing"}))

                response = await agent.run_async(message, channel="webchat")
                await ws.send_text(json.dumps({"type": "response", "text": response}))

        except WebSocketDisconnect:
            logger.info(f"[{agent.instance}] WebSocket getrennt")

    return api
