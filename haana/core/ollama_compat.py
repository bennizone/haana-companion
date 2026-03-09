"""
HAANA Fake-Ollama-API – Ollama-kompatible Endpoints für HA Voice Integration

Stellt HAANA als Ollama-Server dar, sodass Home Assistants eingebaute
Ollama-Integration direkt verbinden kann – keine HACS-Abhängigkeit nötig.

Architektur: Universeller LLM-Proxy mit Tool-Calling-Support und Delegation.
- Empfängt Requests im Ollama-Format (von HA)
- Injiziert Memory-Kontext (Qdrant Embeddings)
- Übersetzt und leitet an beliebigen Provider weiter (Ollama, Anthropic, OpenAI, MiniMax, Gemini)
- Übersetzt Tool-Calls zurück ins Ollama-Format (für HA)
- Delegation: ha-assist kann komplexe Anfragen an ha-advanced weiterleiten ([DELEGATE]-Marker)
- Agent-Routing: Reguläre User-Agents (benni, domi, ...) werden als Modelle exponiert
  und Nachrichten direkt an die Agent-API geroutet (System-Prompt/Kontext wird abgeschnitten)

Endpoints:
  GET  /api/tags     → Listet konfigurierte Modelle
  POST /api/chat     → Chat-Completion mit Tool-Support
  POST /api/show     → Modell-Details
  GET  /api/version  → Health-Check / Version
  GET  /api/ps       → Laufende "Modelle"
"""

import json
import logging
import os
import time
import uuid
from typing import Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

DELEGATION_MARKER = "[DELEGATE]"

_DELEGATION_INSTRUCTIONS = """
IMPORTANT: You are a fast voice assistant. For complex questions that you cannot answer directly, respond ONLY with the exact text "[DELEGATE]" (nothing else).
Delegate when the question is about: weather, forecasts, calendar, appointments, recipes, cooking, complex explanations ("how does X work", "why does X happen"), general knowledge, or anything you are unsure about.
Do NOT delegate for: controlling devices (lights, switches, climate), reading sensor states, simple status queries, timers, or simple household questions.
"""

_MODEL_META = {
    "format": "haana",
    "family": "haana",
    "parameter_size": "0B",
    "quantization_level": "none",
}


def _strip_tag(model_name: str) -> str:
    """Entfernt :latest oder andere Tags vom Modellnamen."""
    if ":" in model_name:
        return model_name.split(":")[0]
    return model_name


def _extract_messages(messages: list[dict]) -> tuple[str, str]:
    """Extrahiert System-Prompt und letzten User-Message.
    Returns: (system_prompt, user_message)
    """
    system_prompt = ""
    user_message = ""
    for msg in messages:
        if msg.get("role") == "system":
            system_prompt = msg.get("content", "")
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_message = msg.get("content", "")
            break
    return system_prompt, user_message


# ══════════════════════════════════════════════════════════════════════════════
# Memory-Lookup (Ollama Embedding → Qdrant)
# ══════════════════════════════════════════════════════════════════════════════

async def _memory_search(
    query: str,
    *,
    qdrant_url: str,
    ollama_url: str,
    embed_model: str,
    collections: list[str],
    limit: int = 5,
) -> str:
    """Schlanker Memory-Lookup: Embedding via Ollama → Qdrant Query."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{ollama_url.rstrip('/')}/api/embeddings",
            json={"model": embed_model, "prompt": query},
        )
        r.raise_for_status()
        embedding = r.json().get("embedding", [])
        if not embedding:
            return ""

        all_results: list[tuple[float, str, str]] = []
        for coll in collections:
            try:
                r = await client.post(
                    f"{qdrant_url.rstrip('/')}/collections/{coll}/points/query",
                    json={"query": embedding, "limit": limit, "with_payload": True},
                )
                if r.status_code != 200:
                    continue
                for point in r.json().get("result", {}).get("points", []):
                    score = point.get("score", 0)
                    payload = point.get("payload", {})
                    content = payload.get("memory") or payload.get("data", "")
                    if content and score > 0.3:
                        all_results.append((score, coll, content))
            except Exception:
                continue

    if not all_results:
        return ""
    all_results.sort(reverse=True)
    return "\n".join(f"[{scope}] {content}" for _, scope, content in all_results[:10])


# ══════════════════════════════════════════════════════════════════════════════
# Tool-Format-Übersetzung: Ollama ↔ Anthropic ↔ OpenAI
# ══════════════════════════════════════════════════════════════════════════════

def _ollama_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Konvertiert Ollama/OpenAI Tool-Definitionen → Anthropic Format."""
    result = []
    for tool in tools:
        fn = tool.get("function", tool)
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _ollama_msgs_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Konvertiert Ollama Messages → Anthropic Format (system separat).

    Returns: (system_text, anthropic_messages)
    """
    system_text = ""
    anthropic_msgs = []

    for msg in messages:
        role = msg.get("role", "")

        if role == "system":
            system_text = msg.get("content", "")

        elif role == "assistant":
            content_blocks = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            if content_blocks:
                anthropic_msgs.append({"role": "assistant", "content": content_blocks})

        elif role == "tool":
            # Tool-Result → Anthropic: user message mit tool_result content
            tool_call_id = msg.get("tool_call_id", "")
            # Finde die zugehörige tool_use ID aus der letzten Assistant-Message
            if not tool_call_id and anthropic_msgs:
                last = anthropic_msgs[-1]
                if last.get("role") == "assistant":
                    for block in last.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_call_id = block.get("id", "")
            anthropic_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": msg.get("content", ""),
                }],
            })

        elif role == "user":
            anthropic_msgs.append({"role": "user", "content": msg.get("content", "")})

    return system_text, anthropic_msgs


def _anthropic_response_to_ollama(data: dict, model: str) -> dict:
    """Konvertiert Anthropic API Response → Ollama-Format (inkl. Tool-Calls)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
    content_blocks = data.get("content", [])
    stop_reason = data.get("stop_reason", "end_turn")

    text_parts = []
    tool_calls = []
    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    message = {"role": "assistant", "content": " ".join(text_parts).strip()}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "model": model,
        "created_at": now,
        "message": message,
        "done": stop_reason != "tool_use",
        "done_reason": "stop" if stop_reason != "tool_use" else "tool_calls",
    }


def _openai_response_to_ollama(data: dict, model: str) -> dict:
    """Konvertiert OpenAI API Response → Ollama-Format (inkl. Tool-Calls)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
    choice = data.get("choices", [{}])[0] if data.get("choices") else {}
    msg = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    message = {"role": "assistant", "content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        tool_calls = []
        for tc in msg["tool_calls"]:
            tool_calls.append({
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            })
        message["tool_calls"] = tool_calls

    return {
        "model": model,
        "created_at": now,
        "message": message,
        "done": finish != "tool_calls",
        "done_reason": "stop" if finish != "tool_calls" else "tool_calls",
    }


# ══════════════════════════════════════════════════════════════════════════════
# OAuth-Token-Lookup
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_api_key(provider: dict) -> str:
    """Gibt den API-Key für einen Provider zurück (direkt oder via OAuth-Token)."""
    if provider.get("key"):
        return provider["key"]
    if provider.get("auth_method") == "oauth" and provider.get("oauth_dir"):
        creds_path = os.path.join(provider["oauth_dir"], ".credentials.json")
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            if token:
                return token
        except Exception as e:
            logger.warning("[ollama-compat] OAuth-Token nicht lesbar (%s): %s", creds_path, e)
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Universeller LLM-Call (mit Tool-Support)
# ══════════════════════════════════════════════════════════════════════════════

async def _call_llm(
    provider_type: str,
    url: str,
    model: str,
    messages: list[dict],
    api_key: str = "",
    tools: list[dict] | None = None,
) -> dict:
    """Ruft das LLM auf und gibt die Antwort im Ollama-Format zurück.

    Unterstützt Tool-Calling für alle Provider-Typen.
    Returns: Ollama-kompatibles Response-Dict.
    """
    if provider_type == "ollama":
        endpoint = f"{url.rstrip('/')}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        headers = {}

    elif provider_type in ("anthropic", "minimax"):
        base = url.rstrip("/") if url else (
            "https://api.minimax.io/anthropic" if provider_type == "minimax"
            else "https://api.anthropic.com"
        )
        endpoint = f"{base}/v1/messages"
        system_text, api_msgs = _ollama_msgs_to_anthropic(messages)
        payload = {
            "model": model,
            "max_tokens": 1024,
            "messages": api_msgs or [{"role": "user", "content": ""}],
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = _ollama_tools_to_anthropic(tools)
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if api_key and api_key.startswith("sk-ant-oat"):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key

    else:
        # OpenAI-kompatibel (OpenAI, Gemini, Custom)
        endpoint = f"{url.rstrip('/')}/v1/chat/completions"
        payload = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        headers = {"content-type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(endpoint, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()

    # Response → Ollama-Format konvertieren
    if provider_type == "ollama":
        # Ollama-Response ist schon fast im richtigen Format
        msg = data.get("message", {})
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
        has_tool_calls = bool(msg.get("tool_calls"))
        return {
            "model": model,
            "created_at": now,
            "message": msg,
            "done": not has_tool_calls,
            "done_reason": "tool_calls" if has_tool_calls else "stop",
        }
    elif provider_type in ("anthropic", "minimax"):
        return _anthropic_response_to_ollama(data, model)
    else:
        return _openai_response_to_ollama(data, model)


# ══════════════════════════════════════════════════════════════════════════════
# Ollama-Format Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_response(text: str, model: str, elapsed: float, stream: bool):
    if stream:
        return StreamingResponse(
            _stream_response(text, model, elapsed),
            media_type="application/x-ndjson",
        )
    return _text_response(text, model, elapsed)


def _text_response(text: str, model: str, elapsed: float) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
    return {
        "model": model,
        "created_at": now,
        "message": {"role": "assistant", "content": text},
        "done": True,
        "done_reason": "stop",
        "total_duration": int(elapsed * 1e9),
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": len(text.split()) if text else 0,
        "eval_duration": int(elapsed * 1e9),
    }


def _raw_response(ollama_resp: dict, elapsed: float) -> dict:
    """Ergänzt ein Ollama-Response-Dict um Timing-Felder."""
    ollama_resp.setdefault("total_duration", int(elapsed * 1e9))
    ollama_resp.setdefault("load_duration", 0)
    ollama_resp.setdefault("prompt_eval_count", 0)
    ollama_resp.setdefault("prompt_eval_duration", 0)
    text = ollama_resp.get("message", {}).get("content", "")
    ollama_resp.setdefault("eval_count", len(text.split()) if text else 0)
    ollama_resp.setdefault("eval_duration", int(elapsed * 1e9))
    return ollama_resp


async def _stream_response(text: str, model: str, elapsed: float):
    """NDJSON-Stream: Wort für Wort, dann Done-Message."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
    if text:
        words = text.split(" ")
        for i, word in enumerate(words):
            token = word if i == 0 else f" {word}"
            yield json.dumps({
                "model": model, "created_at": now,
                "message": {"role": "assistant", "content": token},
                "done": False,
            }) + "\n"
    yield json.dumps({
        "model": model, "created_at": now,
        "message": {"role": "assistant", "content": ""},
        "done": True, "done_reason": "stop",
        "total_duration": int(elapsed * 1e9), "load_duration": 0,
        "prompt_eval_count": 0, "prompt_eval_duration": 0,
        "eval_count": len(text.split()) if text else 0,
        "eval_duration": int(elapsed * 1e9),
    }) + "\n"



# ══════════════════════════════════════════════════════════════════════════════
# Delegation: ha-assist → ha-advanced
# ══════════════════════════════════════════════════════════════════════════════

def _inject_delegation_instructions(messages: list[dict]) -> list[dict]:
    """Fügt Delegation-Instructions in den System-Prompt ein."""
    result = []
    has_system = False
    for msg in messages:
        if msg.get("role") == "system":
            has_system = True
            enriched = msg.copy()
            enriched["content"] = msg["content"] + "\n" + _DELEGATION_INSTRUCTIONS
            result.append(enriched)
        else:
            result.append(msg)
    if not has_system:
        result.insert(0, {"role": "system", "content": _DELEGATION_INSTRUCTIONS.strip()})
    return result


async def _handle_delegation(
    target_instance: str,
    user_message: str,
    *,
    get_agent_url: Callable,
) -> str | None:
    """Delegiert an den laufenden Agent. Returns Antwort-Text oder None bei Fehler."""
    agent_url = get_agent_url(target_instance)
    if not agent_url:
        logger.warning("[ollama-compat] Delegation: Agent '%s' nicht erreichbar", target_instance)
        return None

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{agent_url.rstrip('/')}/chat",
                json={"message": user_message, "channel": "ha_voice"},
            )
            r.raise_for_status()
            return r.json().get("response", "")
    except Exception as e:
        logger.error("[ollama-compat] Delegation an %s fehlgeschlagen: %s", target_instance, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════════

def create_ollama_router(
    *,
    get_config: Callable,
    resolve_llm: Callable,
    find_ollama_url: Callable,
    get_agent_url: Callable | None = None,
) -> APIRouter:
    """Erstellt Ollama-kompatiblen Router — universeller LLM-Proxy mit Tool-Support."""
    router = APIRouter(tags=["ollama-compat"])

    def _is_enabled() -> bool:
        return get_config().get("ollama_compat", {}).get("enabled", False)

    def _exposed_models() -> list[str]:
        models = get_config().get("ollama_compat", {}).get("exposed_models", [])
        return models if models else ["ha-assist", "ha-advanced"]

    def _all_user_ids() -> list[str]:
        return [u["id"] for u in get_config().get("users", [])]

    def _is_agent_model(instance: str) -> bool:
        """True wenn instance ein regulärer User-Agent ist (kein Proxy-Modell)."""
        return instance not in _exposed_models() and instance in _all_user_ids()

    def _resolve_user_llm(instance: str) -> tuple[dict, dict, dict]:
        cfg = get_config()
        user = next((u for u in cfg.get("users", []) if u["id"] == instance), {})
        if not user:
            return {}, {}, {}
        llm, prov = resolve_llm(user.get("primary_llm", ""), cfg)
        return user, llm, prov

    def _model_entry(instance: str) -> dict:
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
        return {
            "name": f"{instance}:latest", "model": f"{instance}:latest",
            "modified_at": now, "size": 0, "digest": f"haana-{instance}",
            "details": {**_MODEL_META, "families": ["haana"]},
        }

    @router.get("/api/tags")
    async def ollama_tags():
        if not _is_enabled():
            return JSONResponse({"models": []}, status_code=200)
        models = []
        # Proxy-Modelle (exposed_models mit LLM-Config)
        for inst in _exposed_models():
            user, llm, prov = _resolve_user_llm(inst)
            if user and llm:
                models.append(_model_entry(inst))
        # Reguläre User-Agents (direkt an Agent-API geroutet)
        for uid in _all_user_ids():
            if uid not in _exposed_models():
                models.append(_model_entry(uid))
        return {"models": models}

    @router.post("/api/chat")
    async def ollama_chat(request: Request):
        if not _is_enabled():
            return JSONResponse({"error": "Ollama-Compat nicht aktiviert"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Ungültiges JSON"}, status_code=400)

        model_raw = body.get("model", "")
        instance = _strip_tag(model_raw)
        messages = body.get("messages", [])
        tools = body.get("tools")
        stream = body.get("stream", True)

        # ── Agent-Modell: direkt an laufenden Agent routen ──
        if _is_agent_model(instance):
            if not get_agent_url:
                return JSONResponse({"error": f"Agent-Routing nicht verfügbar"}, status_code=503)
            _, user_message = _extract_messages(messages)
            if not user_message:
                return _make_response("", model_raw, 0.0, stream)
            t_start = time.monotonic()
            agent_text = await _handle_delegation(
                instance, user_message, get_agent_url=get_agent_url,
            )
            elapsed = time.monotonic() - t_start
            if agent_text is None:
                return JSONResponse({"error": f"Agent '{instance}' nicht erreichbar"}, status_code=503)
            logger.info(
                "[ollama-compat] agent %s: %.2fs | Q: %s | A: %s",
                instance, elapsed, user_message[:80], agent_text[:120],
            )
            return _make_response(agent_text, model_raw, elapsed, stream)

        exposed = _exposed_models()
        if instance not in exposed:
            return JSONResponse({"error": f"Modell '{model_raw}' nicht verfügbar"}, status_code=404)

        user, llm, prov = _resolve_user_llm(instance)
        if not llm or not prov:
            return JSONResponse({"error": f"Kein LLM für '{instance}' konfiguriert"}, status_code=503)

        system_prompt, user_message = _extract_messages(messages)

        # Kein User-Message und kein Tool-Result → leere Antwort
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not user_message and not has_tool_result:
            return _make_response("", model_raw, 0.0, stream)

        # Memory-Lookup (nur beim ersten Turn, nicht bei Tool-Result-Follow-ups)
        memories = ""
        if user_message and not has_tool_result:
            cfg = get_config()
            emb = cfg.get("embedding", {})
            embed_model = emb.get("model", "bge-m3")
            qdrant_url = cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333")
            ollama_embed_url = find_ollama_url(cfg) or "http://localhost:11434"
            collections = [f"{instance}_memory", "household_memory"]
            try:
                memories = await _memory_search(
                    user_message, qdrant_url=qdrant_url,
                    ollama_url=ollama_embed_url, embed_model=embed_model,
                    collections=collections,
                )
            except Exception as e:
                logger.warning(f"[ollama-compat] Memory-Suche fehlgeschlagen: {e}")

        # Memories in System-Prompt injizieren
        if memories:
            enriched_messages = []
            for msg in messages:
                if msg.get("role") == "system":
                    enriched = msg.copy()
                    enriched["content"] = (
                        msg["content"]
                        + f"\n\nAdditional context from household memory:\n{memories}"
                    )
                    enriched_messages.append(enriched)
                else:
                    enriched_messages.append(msg)
            messages = enriched_messages

        # Delegation-Check: Hat diese Instanz ein Delegationsziel?
        cfg = get_config()
        delegation_map = cfg.get("ollama_compat", {}).get("delegation", {})
        delegation_target = delegation_map.get(instance, "")

        # Delegation-Instructions in System-Prompt injizieren
        if delegation_target and not has_tool_result:
            messages = _inject_delegation_instructions(messages)

        # LLM-Provider bestimmen
        provider_type = prov.get("type", "ollama")
        model_name = llm.get("model", "")
        api_key = _resolve_api_key(prov)

        if provider_type == "ollama":
            llm_url = prov.get("url") or find_ollama_url(cfg) or "http://localhost:11434"
        elif provider_type == "minimax":
            llm_url = prov.get("url") or "https://api.minimax.io/anthropic"
        elif provider_type == "anthropic":
            llm_url = prov.get("url") or "https://api.anthropic.com"
        else:
            llm_url = prov.get("url", "")

        t_start = time.monotonic()
        try:
            ollama_resp = await _call_llm(
                provider_type=provider_type, url=llm_url,
                model=model_name, messages=messages,
                api_key=api_key, tools=tools,
            )
        except Exception as e:
            logger.error(f"[ollama-compat] LLM-Fehler ({provider_type}): {e}")
            return _make_response("Entschuldigung, es ist ein Fehler aufgetreten.", model_raw, time.monotonic() - t_start, stream)

        elapsed = time.monotonic() - t_start
        resp_text = ollama_resp.get("message", {}).get("content", "")

        # ── Delegation: [DELEGATE] erkannt → an laufenden Agent weiterleiten ──
        if delegation_target and resp_text.strip().startswith(DELEGATION_MARKER) and get_agent_url:
            delegate_text = await _handle_delegation(
                delegation_target, user_message,
                get_agent_url=get_agent_url,
            )
            if delegate_text is not None:
                d_elapsed = time.monotonic() - t_start
                logger.info(
                    "[ollama-compat] %s → %s (delegated): %.2fs | Q: %s | A: %s",
                    instance, delegation_target, d_elapsed,
                    user_message[:80] if user_message else "(tool-result)",
                    delegate_text[:120],
                )
                if stream:
                    return _make_response(delegate_text, model_raw, d_elapsed, True)
                return _text_response(delegate_text, model_raw, d_elapsed)
            # Delegation fehlgeschlagen → Fallback auf eigene Antwort
            logger.warning("[ollama-compat] Delegation %s → %s fehlgeschlagen, Fallback", instance, delegation_target)

        # Modell-Name im Response auf den Alias setzen
        ollama_resp["model"] = model_raw

        # Logging
        has_tc = bool(ollama_resp.get("message", {}).get("tool_calls"))
        _mem_count = len(memories.splitlines()) if memories else 0
        _q = user_message[:80] if user_message else "(tool-result)"
        logger.info(
            "[ollama-compat] %s (%s/%s): %.2fs | memories=%d | tools=%s | Q: %s | A: %s",
            instance, provider_type, model_name, elapsed, _mem_count,
            "yes" if has_tc else "no", _q, resp_text[:120],
        )

        # Tool-Call Response: direkt als JSON (kein Streaming)
        if has_tc:
            return _raw_response(ollama_resp, elapsed)

        # Text Response: Streaming oder Single
        if stream:
            return _make_response(resp_text, model_raw, elapsed, True)
        return _raw_response(ollama_resp, elapsed)

    @router.post("/api/show")
    async def ollama_show(request: Request):
        if not _is_enabled():
            return JSONResponse({"error": "Nicht aktiviert"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Ungültiges JSON"}, status_code=400)
        model_raw = body.get("name", body.get("model", ""))
        instance = _strip_tag(model_raw)
        if instance not in _exposed_models() and not _is_agent_model(instance):
            return JSONResponse({"error": f"Modell '{model_raw}' nicht gefunden"}, status_code=404)
        return {
            "modelfile": f'FROM {instance}\nSYSTEM "HAANA Voice {instance}"',
            "parameters": "", "template": "{{ .System }}\n{{ .Prompt }}",
            "details": {**_MODEL_META, "families": ["haana"]},
            "model_info": {"general.architecture": "haana", "general.name": f"HAANA {instance}"},
        }

    @router.get("/api/version")
    async def ollama_version():
        return {"version": "0.9.0-haana"}

    @router.get("/api/ps")
    async def ollama_ps():
        if not _is_enabled():
            return {"models": []}
        models = []
        for inst in _exposed_models():
            user, llm, prov = _resolve_user_llm(inst)
            if user and llm:
                models.append({
                    "name": f"{inst}:latest", "model": f"{inst}:latest",
                    "size": 0, "digest": f"haana-{inst}",
                    "details": _MODEL_META,
                    "expires_at": "2099-12-31T23:59:59Z", "size_vram": 0,
                })
        # Reguläre User-Agents
        for uid in _all_user_ids():
            if uid not in _exposed_models():
                models.append({
                    "name": f"{uid}:latest", "model": f"{uid}:latest",
                    "size": 0, "digest": f"haana-{uid}",
                    "details": _MODEL_META,
                    "expires_at": "2099-12-31T23:59:59Z", "size_vram": 0,
                })
        return {"models": models}

    return router
