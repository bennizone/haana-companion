"""
HAANA Agent – Claude Agent SDK Basis (ClaudeSDKClient)

Verwendet ClaudeSDKClient für bidirektionale Kommunikation mit einem einzigen
persistenten claude-Subprocess. Kein Subprocess-Start pro Nachricht →
kein ~5s Startup-Overhead nach der ersten Verbindung.

Authentifizierung läuft über die gebundelte Claude Code CLI
(Claude.ai Subscription oder API-Key in der CLI konfiguriert).

Custom Tools werden als MCP-Server eingebunden (Phase 2+).
"""

import json
import os
import re
import asyncio
import logging
import signal
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    CLINotFoundError,
    CLIConnectionError,
    ProcessError,
    CLIJSONDecodeError,
)
from claude_agent_sdk.types import McpHttpServerConfig, McpSSEServerConfig
from core.memory import HaanaMemory
import core.logger as haana_log

logger = logging.getLogger(__name__)

# Trigger-Wörter für explizite Memory-Speicherung bei Voice
_MEMORY_TRIGGERS = (
    "merk dir", "merke dir", "merken",
    "vergiss nicht", "speicher", "speichere",
    "erinner dich", "remember", "denk dran",
    "notier", "notiere",
)


def _should_extract_memory(user_message: str, channel: str) -> bool:
    """Prüft ob Memory-Extraktion stattfinden soll.

    ha_voice: Nur bei expliziten 'merke dir'-Befehlen.
    Alle anderen Channels: Immer extrahieren.
    """
    if channel != "ha_voice":
        return True
    msg_lower = user_message.lower()
    return any(trigger in msg_lower for trigger in _MEMORY_TRIGGERS)


def _is_explicit_memory_request(user_message: str) -> bool:
    """Erkennt ob der User explizit etwas ins Memory schreiben will."""
    lower = user_message.lower()
    patterns = (
        "merk dir", "merke dir", "merken:", "vergiss nicht",
        "remember that", "remember this", "don't forget",
        "speicher dir", "speichere das", "speicher das",
        "notier dir", "notiere dir", "notiere das",
        "behalte im kopf",
    )
    return any(p in lower for p in patterns)


def _extract_date_references(message: str) -> list[str]:
    """Extrahiert Datumsreferenzen aus einer Nachricht. Gibt Liste von YYYY-MM-DD zurück."""
    dates = []
    today = date.today()
    lower = message.lower()

    if "gestern" in lower or "yesterday" in lower:
        dates.append((today - timedelta(days=1)).isoformat())
    if "vorgestern" in lower:
        dates.append((today - timedelta(days=2)).isoformat())

    # "heute" / "today"
    if "heute" in lower or "today" in lower:
        dates.append(today.isoformat())

    # Explizite Datumsangaben: "am 1.2.", "am 01.02.", "am 1.2.2026"
    # Kontextwörter verhindern False Positives wie "1.2 Millionen"
    for m in re.finditer(
        r'(?:am|vom|den|seit|bis|ab)\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})?', lower
    ):
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            dates.append(date(year, month, day).isoformat())
        except ValueError:
            pass

    return dates


def _load_dream_summaries(instance: str, date_refs: list[str]) -> str:
    """Lädt Dream-Zusammenfassungen für die angegebenen Daten."""
    from core.logger import _log_root as _get_log_root
    log_root = _get_log_root()
    dream_dir = log_root / "dream" / instance
    if not dream_dir.exists():
        return ""

    parts = []
    for d in date_refs:
        fpath = dream_dir / f"{d}.jsonl"
        if not fpath.exists():
            continue
        try:
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    summary = entry.get("summary", "")
                    if summary:
                        parts.append(f"[dream-diary] {d}: {summary}")
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    return "\n".join(parts)


class HaanaAgent:
    def __init__(self, instance_name: str):
        self.instance = instance_name

        # Env-Snapshot für Subprocess (InProcess-Modus: env wird nach __init__ restored)
        self._env = dict(os.environ)

        # Modellname für Logging und CLI --model Parameter
        self.model: Optional[str] = self._env.get("HAANA_MODEL") or None
        self._cli_model: Optional[str] = self.model
        # Drittanbieter (nicht Ollama): model=None → CLI nutzt Env-Vars
        # Ollama: _cli_model bleibt auf HAANA_MODEL (wie `claude --model <name>`)
        # MiniMax: ANTHROPIC_MODEL gesetzt → model=None
        if self._env.get("OPENAI_MODEL") or self._env.get("GEMINI_MODEL"):
            self._cli_model = None
        elif self._env.get("ANTHROPIC_MODEL"):
            # MiniMax: CLI nutzt ANTHROPIC_MODEL aus env
            self._cli_model = None
        self.session_id: Optional[str] = None

        # Config aus config.json nachladen wenn Env-Vars fehlen
        # (z.B. bei docker-compose Start ohne Process Manager)
        if not self._env.get("HAANA_FALLBACK_MODEL") or not self._env.get("HAANA_OAUTH_DIR"):
            self._load_config_env()

        # Fallback-LLM: Wird aktiviert bei Auth/Connection-Fehlern
        self._fallback_available = bool(self._env.get("HAANA_FALLBACK_MODEL"))
        self._fallback_active = False

        # Credential-Watcher: Merkt sich mtime der Credentials-Datei.
        # Bei Änderung wird Fallback automatisch zurückgesetzt.
        self._creds_path: Optional[Path] = None
        self._creds_mtime: float = 0.0
        oauth_dir_init = self._env.get("HAANA_OAUTH_DIR")
        if oauth_dir_init:
            cp = Path(oauth_dir_init) / ".credentials.json"
            if cp.exists():
                self._creds_path = cp
                self._creds_mtime = cp.stat().st_mtime

        # OAuth: Credentials aus Data-Volume symlinken.
        # Nicht bei Ollama/MiniMax — dort übernimmt ANTHROPIC_AUTH_TOKEN die Auth.
        oauth_dir = self._env.get("HAANA_OAUTH_DIR")
        _has_token_auth = bool(self._env.get("ANTHROPIC_AUTH_TOKEN"))
        if oauth_dir and not _has_token_auth:
            src = Path(oauth_dir) / ".credentials.json"
            dst = Path.home() / ".claude" / ".credentials.json"
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # Erst Symlink versuchen, bei Read-Only Filesystem kopieren
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    dst.symlink_to(src)
                    logger.info(f"[{instance_name}] OAuth credentials verlinkt: {src}")
                except OSError:
                    # Read-only mount → Datei kopieren statt symlinken
                    import shutil
                    try:
                        shutil.copy2(str(src), str(dst))
                        logger.info(f"[{instance_name}] OAuth credentials kopiert: {src} → {dst}")
                    except Exception as e2:
                        logger.warning(f"[{instance_name}] OAuth credentials weder link noch kopie möglich: {e2}")

        # Arbeitsverzeichnis = Verzeichnis mit CLAUDE.md der Instanz.
        # Claude Code CLI lädt CLAUDE.md automatisch als Projektkontext.
        for candidate in [
            Path("/app"),                         # Container: CLAUDE.md via Volume
            Path(f"instanzen/{instance_name}"),   # Lokal: direktes Instanzverzeichnis
        ]:
            if (candidate / "CLAUDE.md").exists():
                self.cwd = candidate
                logger.info(f"[{instance_name}] cwd={self.cwd.resolve()}")
                break
        else:
            raise FileNotFoundError(
                f"CLAUDE.md nicht gefunden für Instanz '{instance_name}'. "
                "Erwartet: /app/CLAUDE.md (Container) oder "
                f"instanzen/{instance_name}/CLAUDE.md (lokal)."
            )

        # Memory-Layer (Mem0 + Qdrant + Sliding Window)
        self.memory = HaanaMemory(instance_name)

        # Pfad für Window-Persistenz
        data_dir = self._env.get("HAANA_DATA_DIR", "data")
        self._context_path = Path(data_dir) / "context" / f"{instance_name}.json"

        # MCP-Server für Custom Tools (Phase 2+: HA, Trilium, Kalender, ...)
        self._mcp_servers: dict = {}

        # Home Assistant MCP – automatisch einbinden wenn konfiguriert
        ha_mcp_url = self._env.get("HA_MCP_URL")
        if ha_mcp_url:
            ha_mcp_type = self._env.get("HA_MCP_TYPE", "extended")
            ha_token = self._env.get("HA_TOKEN", "")
            if ha_mcp_type == "builtin":
                # Built-in HA MCP Server (SSE transport, Bearer auth)
                self._mcp_servers["home-assistant"] = McpSSEServerConfig(
                    type="sse",
                    url=ha_mcp_url,
                    headers={"Authorization": f"Bearer {ha_token}"} if ha_token else {},
                )
            else:
                # ha-mcp Add-on (streamable HTTP, auth via private URL path)
                self._mcp_servers["home-assistant"] = McpHttpServerConfig(
                    type="http",
                    url=ha_mcp_url,
                )
            logger.info(f"[{instance_name}] HA MCP-Server registriert: {ha_mcp_type} @ {ha_mcp_url}")

        # Erlaubte Built-in-Tools (Phase 1: Basis)
        self._allowed_tools: list[str] = [
            "Read",
            "Write",
            "Bash",
            "Glob",
            "Grep",
        ]

        # Persistenter Client – lazy initialisiert beim ersten run_async()
        self._client: Optional[ClaudeSDKClient] = None

    # ── Tool-Verwaltung ───────────────────────────────────────────────────────

    def register_mcp_server(self, name: str, server_config: object):
        """Registriert einen MCP-Server mit Custom Tools."""
        self._mcp_servers[name] = server_config
        logger.info(f"[{self.instance}] MCP-Server registriert: {name}")

    def allow_tools(self, tools: list[str]):
        """Fügt weitere erlaubte Tools hinzu."""
        self._allowed_tools.extend(tools)
        logger.debug(f"[{self.instance}] Tools erweitert: {tools}")

    # ── Fallback-LLM ────────────────────────────────────────────────────────

    def _load_config_env(self):
        """Liest fehlende Env-Vars aus config.json nach.

        Wird beim Start aufgerufen wenn Agents via docker-compose gestartet werden
        (ohne _build_agent_env aus dem Process Manager). Lädt:
        - Fallback-LLM Konfiguration
        - OAuth-Directory für Primary Provider
        """
        import json as _json
        config_path = Path(self._env.get("HAANA_CONF_FILE", "/data/config/config.json"))
        if not config_path.exists():
            return

        try:
            cfg = _json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[{self.instance}] config.json nicht lesbar: {e}")
            return

        # User mit passender instance finden
        user = None
        for u in cfg.get("users", []):
            if u.get("id") == self.instance:
                user = u
                break
        if not user:
            return

        # OAuth-Dir für Primary Provider setzen (wenn nicht schon gesetzt)
        if not self._env.get("HAANA_OAUTH_DIR"):
            primary_llm_id = user.get("primary_llm", "")
            p_llm = next((l for l in cfg.get("llms", []) if l.get("id") == primary_llm_id), None)
            if p_llm:
                p_prov = next((p for p in cfg.get("providers", []) if p.get("id") == p_llm.get("provider_id")), None)
                if p_prov and p_prov.get("type") == "anthropic" and p_prov.get("auth_method") == "oauth":
                    oauth_dir = p_prov.get("oauth_dir", "")
                    if oauth_dir:
                        self._env["HAANA_OAUTH_DIR"] = oauth_dir
                        logger.info(f"[{self.instance}] OAuth-Dir aus config.json: {oauth_dir}")

        # Fallback-LLM laden (wenn nicht schon gesetzt)
        if not self._env.get("HAANA_FALLBACK_MODEL") and user.get("fallback_llm"):
            fb_llm_id = user["fallback_llm"]
            fb_llm = next((l for l in cfg.get("llms", []) if l.get("id") == fb_llm_id), None)
            if fb_llm:
                fb_prov = next((p for p in cfg.get("providers", []) if p.get("id") == fb_llm.get("provider_id")), None)
                if fb_prov:
                    from core.process_manager import _build_fallback_env
                    ollama_url = self._env.get("OLLAMA_URL", "")
                    fb_env = _build_fallback_env(fb_llm, fb_prov, ollama_url, cfg)
                    self._env.update(fb_env)
                    logger.info(
                        f"[{self.instance}] Fallback-LLM aus config.json: "
                        f"{fb_llm.get('model')} ({fb_prov.get('type')})"
                    )

    @staticmethod
    def _is_fallback_error(error) -> bool:
        """Prüft ob ein Fehler einen Fallback-Wechsel rechtfertigt.

        Relevante Fehler: Auth-Fehler (401/403), Connection-Fehler,
        Prozess-Abbrüche die auf Provider-Probleme hindeuten.
        """
        err_str = str(error).lower()
        # Auth-bezogene Fehlermuster
        auth_patterns = (
            "401", "403", "unauthorized", "forbidden",
            "authentication", "auth", "invalid api key",
            "invalid_api_key", "api key", "token",
            "permission denied", "access denied",
            "overloaded", "rate limit", "rate_limit",
            "quota", "billing", "insufficient",
        )
        return any(p in err_str for p in auth_patterns)

    async def _activate_fallback(self):
        """Wechselt auf den Fallback-LLM.

        Schließt den aktuellen Client, setzt die Env-Vars auf Fallback-Werte
        und markiert den Fallback als aktiv. Beim nächsten _ensure_connected()
        wird der Client mit den neuen Env-Vars erstellt.
        """
        if self._fallback_active or not self._fallback_available:
            return False

        await self.close()  # Client schließen

        fb_model = self._env.get("HAANA_FALLBACK_MODEL", "")
        fb_type = self._env.get("HAANA_FALLBACK_PROVIDER_TYPE", "")
        fb_base_url = self._env.get("HAANA_FALLBACK_BASE_URL", "")
        fb_auth_token = self._env.get("HAANA_FALLBACK_AUTH_TOKEN", "")
        fb_api_key = self._env.get("HAANA_FALLBACK_API_KEY", "")
        fb_oauth_dir = self._env.get("HAANA_FALLBACK_OAUTH_DIR", "")

        logger.warning(
            f"[{self.instance}] Wechsle auf Fallback-LLM: {fb_model} "
            f"(Provider: {fb_type})"
        )

        # Alte Provider-Env-Vars entfernen
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                     "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                     "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                     "GEMINI_API_KEY", "GEMINI_MODEL"):
            self._env.pop(key, None)

        # Neue Provider-Env-Vars setzen
        self.model = fb_model
        self._env["HAANA_MODEL"] = fb_model

        if fb_type == "minimax":
            self._env["ANTHROPIC_BASE_URL"] = fb_base_url
            self._env["ANTHROPIC_AUTH_TOKEN"] = fb_auth_token
            self._env["ANTHROPIC_MODEL"] = fb_model
            self._cli_model = None
        elif fb_type == "ollama":
            self._env["ANTHROPIC_BASE_URL"] = fb_base_url
            self._env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
            self._cli_model = fb_model
        elif fb_type == "openai":
            if fb_api_key:
                self._env["OPENAI_API_KEY"] = fb_api_key
            if fb_base_url:
                self._env["OPENAI_BASE_URL"] = fb_base_url
            self._env["OPENAI_MODEL"] = fb_model
            self._cli_model = None
        elif fb_type == "gemini":
            if fb_api_key:
                self._env["GEMINI_API_KEY"] = fb_api_key
            self._env["GEMINI_MODEL"] = fb_model
            self._cli_model = None
        else:
            # anthropic / custom
            if fb_api_key:
                self._env["ANTHROPIC_API_KEY"] = fb_api_key
            if fb_base_url:
                self._env["ANTHROPIC_BASE_URL"] = fb_base_url
            self._cli_model = fb_model

        # OAuth für Fallback
        if fb_oauth_dir and not fb_auth_token:
            self._env["HAANA_OAUTH_DIR"] = fb_oauth_dir

        # Session zurücksetzen (neuer Provider = neue Session)
        self.session_id = None
        self._fallback_active = True

        logger.info(f"[{self.instance}] Fallback-LLM aktiviert: {fb_model}")
        return True

    # ── Verbindungsverwaltung ─────────────────────────────────────────────────

    def _build_options(self) -> ClaudeAgentOptions:
        """Erstellt ClaudeAgentOptions. CLAUDECODE wird entfernt damit der
        Subprocess-Agent in einer Claude Code Session starten kann."""
        subprocess_env = dict(self._env)
        subprocess_env.pop("CLAUDECODE", None)
        return ClaudeAgentOptions(
            cwd=self.cwd,
            model=self._cli_model,
            max_turns=20,
            allowed_tools=self._allowed_tools,
            permission_mode="bypassPermissions",
            mcp_servers=self._mcp_servers if self._mcp_servers else {},
            setting_sources=["project"],
            env=subprocess_env,
        )

    async def _ensure_connected(self):
        """Stellt sicher dass der persistente Subprocess läuft. Lazy-Init.
        Prüft auch ob sich Credentials geändert haben → Fallback zurücksetzen."""
        # Credential-Watcher: Bei Token-Änderung Fallback automatisch zurücksetzen
        if self._fallback_active and self._creds_path:
            try:
                new_mtime = self._creds_path.stat().st_mtime
                if new_mtime > self._creds_mtime:
                    self._creds_mtime = new_mtime
                    logger.info(
                        f"[{self.instance}] Credentials geändert – "
                        f"setze Fallback zurück auf primäres LLM"
                    )
                    await self.close()
                    self._fallback_active = False
                    # Fallback-Env-Vars entfernen (wurden von _activate_fallback gesetzt)
                    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                                "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                                "GEMINI_API_KEY", "GEMINI_MODEL"):
                        self._env.pop(key, None)
                    # Primäre Env-Vars wiederherstellen
                    self._load_config_env()
                    # Credentials neu symlinken
                    src = self._creds_path
                    dst = Path.home() / ".claude" / ".credentials.json"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if dst.exists() or dst.is_symlink():
                            dst.unlink()
                        dst.symlink_to(src)
                    except OSError:
                        import shutil
                        shutil.copy2(str(src), str(dst))
            except (OSError, FileNotFoundError):
                pass

        if self._client is None:
            options = self._build_options()
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            logger.info(f"[{self.instance}] Claude subprocess gestartet")

    async def close(self):
        """Schließt nur den persistenten Subprocess (kein Memory-Flush)."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.debug(f"[{self.instance}] Disconnect-Fehler (ignoriert): {e}")
            finally:
                self._client = None
            logger.info(f"[{self.instance}] Claude subprocess geschlossen")

    async def _run_with_fallback_notice(self, user_message: str, channel: str,
                                         memory_context: str, prompt: str) -> str:
        """Führt einen Agent-Turn mit dem Fallback-LLM aus und fügt Hinweis hinzu."""
        # Neuen Client mit Fallback-Env aufbauen
        await self._ensure_connected()

        response_parts: list[str] = []
        tool_calls_log: list[dict] = []
        t_start = time.monotonic()

        await self._client.query(prompt)

        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        logger.info(
                            f"[{self.instance}] Tool-Aufruf (Fallback): {block.name}"
                        )
                        tool_calls_log.append({
                            "tool": block.name,
                            "input": str(block.input)[:300],
                        })
                        haana_log.log_tool_call(
                            instance=self.instance,
                            tool_name=block.name,
                            tool_input=block.input,
                        )
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    self.session_id = message.session_id
                if message.result and not response_parts:
                    response_parts.append(str(message.result))

        elapsed = time.monotonic() - t_start
        response_text = "".join(response_parts).strip()

        logger.info(
            f"[{self.instance}] Fallback-Antwort in {elapsed:.2f}s "
            f"(model={self.model})"
        )

        # Memory speichern
        if response_text and _should_extract_memory(user_message, channel):
            await self.memory.add_conversation_async(user_message, response_text)

        memory_lines = memory_context.splitlines() if memory_context else []
        haana_log.log_conversation(
            instance=self.instance,
            channel=channel,
            user_message=user_message,
            assistant_response=response_text,
            latency_s=elapsed,
            memory_used=bool(memory_context),
            memory_hits=self.memory._last_search_hits,
            tool_calls=tool_calls_log,
            model=self.model,
            memory_results=memory_lines,
        )

        # Fallback-Hinweis voranstellen (nicht bei Voice-Channels)
        notice = ""
        if channel not in ("whatsapp_voice", "ha_voice"):
            notice = f"[Fallback-LLM aktiv: {self.model}] "

        return notice + (response_text or "[Keine Antwort]")

    # ── Startup / Shutdown ────────────────────────────────────────────────────

    async def startup(self):
        """
        Startet den Agenten:
        1. Gespeicherten Window-Context laden
        2. Pending Extraktionen vom letzten Lauf sofort nachextrahieren
        """
        pending = await self.memory.load_context(self._context_path)
        if pending > 0:
            logger.info(
                f"[{self.instance}] {pending} Einträge aus letzter Session "
                "werden nachträglich extrahiert..."
            )
            await self.memory.flush_pending(timeout=60.0)
            # Context nach Extraktion aktualisieren
            self.memory.save_context(self._context_path)

    async def shutdown(self, timeout: float = 60.0):
        """
        Sauberes Shutdown:
        1. Laufende Extraktions-Tasks abwarten (nicht neue starten!)
        2. Window-Context speichern (Einträge bleiben erhalten für nächsten Start)
        3. Subprocess schließen

        Context-Preservation: Der Window wird NICHT geflusht, damit bei einem
        Restart die Unterhaltung nahtlos weitergeht. Extraktion passiert nur
        via normalen Overflow (max_messages / max_age_minutes).
        """
        pending = self.memory.pending_count()
        if pending > 0:
            print(f"  Warte auf {pending} laufende Extraktionen...", flush=True)
            cancelled = await self.memory.flush_pending(timeout=timeout)
            if cancelled > 0:
                logger.warning(
                    f"[{self.instance}] {cancelled} Extraktionen nach "
                    f"{timeout}s abgebrochen und im Context-File gespeichert."
                )

        remaining = self.memory._window.size()
        if remaining > 0:
            logger.info(
                f"[{self.instance}] Shutdown: {remaining} Einträge im Window "
                "bleiben erhalten (Context-Preservation)"
            )

        self.memory.save_context(self._context_path)
        await self.close()

    # ── Haupt-Loop ────────────────────────────────────────────────────────────

    async def run_async(self, user_message: str, channel: str = "repl") -> str:
        """
        Führt einen Agent-Turn aus.

        1. Relevante Memories aus Qdrant suchen
        2. Memory-Kontext dem Prompt voranstellen
        3. Prompt an persistenten Subprocess senden (kein neuer Prozess!)
        4. Text aus AssistantMessage-Blöcken sammeln
        5. Session-ID für Kontinuität merken
        6. Konversation non-blocking ins Sliding Window schreiben
        7. Vollständige Konversation + Tool-Calls ins Log schreiben
        """
        # Memory: relevanten Kontext laden (in Executor – blockiert Event-Loop nicht)
        loop = asyncio.get_running_loop()
        memory_context = await loop.run_in_executor(None, self.memory.search, user_message)

        # Dream-Tagebuch: bei Datumsreferenzen passende Zusammenfassungen laden
        date_refs = _extract_date_references(user_message)
        if date_refs:
            dream_context = _load_dream_summaries(self.instance, date_refs)
            if dream_context:
                memory_context = (memory_context + "\n" + dream_context) if memory_context else dream_context

        parts = []
        if memory_context:
            parts.append(f"<relevante_erinnerungen>\n{memory_context}\n</relevante_erinnerungen>")
            logger.debug(f"[{self.instance}] Memory-Kontext: {len(memory_context)} Zeichen")
        if channel in ("whatsapp_voice", "ha_voice"):
            parts.append(
                "<hinweis>Diese Nachricht kam als Sprachnachricht. "
                "Deine Antwort wird per Text-to-Speech vorgelesen. "
                "Antworte daher ohne Emojis, ohne Markdown-Formatierung, ohne Sonderzeichen. "
                "Schreibe natürlich und gesprächig, als würdest du sprechen. "
                "Halte dich kurz und prägnant.</hinweis>"
            )
        parts.append(user_message)
        prompt = "\n\n".join(parts)

        # Verbindung sicherstellen (lazy init oder nach Fehler)
        try:
            await self._ensure_connected()
        except CLINotFoundError:
            logger.error("Claude Code CLI nicht gefunden.")
            return (
                "Fehler: Claude Code CLI nicht gefunden. "
                "Bitte installieren: curl -fsSL https://claude.ai/install.sh | bash"
            )

        # Prompt senden und Antwort empfangen
        response_parts: list[str] = []
        tool_calls_log: list[dict] = []
        t_start = time.monotonic()

        try:
            await self._client.query(prompt)

            async for message in self._client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_parts.append(block.text)
                            logger.debug(
                                f"[{self.instance}] TextBlock: {block.text[:80]}..."
                            )
                        elif isinstance(block, ToolUseBlock):
                            logger.info(
                                f"[{self.instance}] Tool-Aufruf: {block.name} "
                                f"| input={str(block.input)[:120]}"
                            )
                            tool_calls_log.append({
                                "tool": block.name,
                                "input": str(block.input)[:300],
                            })
                            haana_log.log_tool_call(
                                instance=self.instance,
                                tool_name=block.name,
                                tool_input=block.input,
                            )
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        self.session_id = message.session_id
                        logger.debug(f"[{self.instance}] Session: {self.session_id}")
                    if message.is_error:
                        logger.error(
                            f"[{self.instance}] ResultMessage Fehler: {message.result}"
                        )
                        # Auth-Fehler in ResultMessage → Fallback versuchen
                        if (self._fallback_available and not self._fallback_active
                                and self._is_fallback_error(message.result)):
                            switched = await self._activate_fallback()
                            if switched:
                                logger.info(f"[{self.instance}] Retry mit Fallback-LLM (ResultMessage-Fehler)...")
                                try:
                                    return await self._run_with_fallback_notice(
                                        user_message, channel, memory_context, prompt
                                    )
                                except Exception as retry_err:
                                    logger.error(
                                        f"[{self.instance}] Fallback-Retry fehlgeschlagen: {retry_err}"
                                    )
                                    return (
                                        f"Fehler: Primary-LLM Fehler ({message.result}). "
                                        f"Fallback-LLM ebenfalls fehlgeschlagen: {retry_err}"
                                    )
                    # Fallback: manche Provider liefern Text in ResultMessage statt TextBlock
                    if message.result and not response_parts:
                        logger.info(f"[{self.instance}] Text aus ResultMessage übernommen")
                        response_parts.append(str(message.result))

        except CLINotFoundError:
            self._client = None
            logger.error("Claude Code CLI nicht gefunden.")
            return "Fehler: Claude Code CLI nicht gefunden."
        except (CLIConnectionError, ProcessError) as e:
            self._client = None
            is_process = isinstance(e, ProcessError)
            if is_process:
                logger.error(f"[{self.instance}] CLI-Prozess Fehler (exit {e.exit_code}): {e}")
            else:
                logger.error(f"[{self.instance}] Verbindungsfehler: {e}")

            # Fallback-LLM versuchen bei Auth/Connection-Fehlern
            if self._fallback_available and not self._fallback_active and self._is_fallback_error(e):
                switched = await self._activate_fallback()
                if switched:
                    logger.info(f"[{self.instance}] Retry mit Fallback-LLM...")
                    try:
                        return await self._run_with_fallback_notice(
                            user_message, channel, memory_context, prompt
                        )
                    except Exception as retry_err:
                        logger.error(f"[{self.instance}] Fallback-Retry fehlgeschlagen: {retry_err}")
                        return (
                            f"Fehler: Primary-LLM nicht erreichbar ({e}). "
                            f"Fallback-LLM ebenfalls fehlgeschlagen: {retry_err}"
                        )

            if is_process:
                return f"Fehler: Agent-Prozess beendet mit Code {e.exit_code}."
            return "Fehler: Verbindung zum Agent verloren. Nächste Nachricht startet neu."
        except CLIJSONDecodeError as e:
            logger.error(f"[{self.instance}] JSON-Parse-Fehler: {e}")
            return "Fehler: Ungültige Antwort vom Agent."

        elapsed = time.monotonic() - t_start
        logger.info(f"[{self.instance}] Antwort in {elapsed:.2f}s")

        response_text = "".join(response_parts).strip()

        if not response_text:
            logger.warning(
                f"[{self.instance}] Leere Antwort vom Modell "
                f"(model={self.model}, {elapsed:.1f}s)"
            )

        # Memory: Konversation async im Hintergrund speichern (non-blocking)
        # ha_voice: Nur bei expliziten "merke dir"-Befehlen extrahieren
        memory_extracted = False
        if response_text and _should_extract_memory(user_message, channel):
            if _is_explicit_memory_request(user_message):
                # Explicit Memory: sofort in Mem0 schreiben, dann ins Window
                # (mit already_extracted=True → keine erneute Extraktion)
                success = await self.memory.add_immediate(user_message, response_text)
                await self.memory.add_conversation_async(
                    user_message, response_text, already_extracted=success
                )
                memory_extracted = True
            else:
                await self.memory.add_conversation_async(user_message, response_text)

        # Memory-Ergebnisse als Liste (für Log-Anzeige im UI)
        memory_lines = memory_context.splitlines() if memory_context else []

        # Strukturiertes Log schreiben
        haana_log.log_conversation(
            instance=self.instance,
            channel=channel,
            user_message=user_message,
            assistant_response=response_text,
            latency_s=elapsed,
            memory_used=bool(memory_context),
            memory_hits=self.memory._last_search_hits,
            tool_calls=tool_calls_log,
            model=self.model,
            memory_results=memory_lines,
            memory_extracted=memory_extracted,
        )

        result = response_text or "[Keine Antwort]"

        # Fallback-Hinweis bei aktiver Fallback-Nutzung (nicht bei Voice)
        if self._fallback_active and channel not in ("whatsapp_voice", "ha_voice"):
            result = f"[Fallback-LLM aktiv: {self.model}] " + result

        return result

    def run(self, user_message: str) -> str:
        """Synchroner Wrapper für run_async() – für einfache Skripte."""
        return asyncio.run(self.run_async(user_message))


# ── CLI / REPL für lokale Tests ───────────────────────────────────────────────

def _setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _repl(agent: HaanaAgent):
    """
    REPL-Schleife für lokale Tests.

    Befehle:
      /exit       – Sauberes Shutdown (pending Extraktionen abwarten, dann beenden)
      exit / quit – wie /exit
      Ctrl+C      – wie /exit

    Signal-Handler:
      SIGTERM / SIGINT → setzt shutdown_event → REPL beendet sich sauber
    """
    print(f"\nHAANA [{agent.instance}] – REPL (ClaudeSDKClient, persistenter Subprocess)")
    print(f"cwd:     {agent.cwd.resolve()}")
    print(f"context: {agent._context_path}")
    print("Beenden mit /exit, exit, quit oder Ctrl+C\n")

    shutdown_event = asyncio.Event()

    # Signal-Handler: SIGTERM + SIGINT → setzt shutdown_event
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, OSError):
            # Windows unterstützt add_signal_handler nicht
            pass

    async def _do_shutdown():
        """Shutdown-Sequenz mit Status-Output."""
        pending = agent.memory.pending_count()
        if pending > 0:
            print(f"\n  Extrahiere noch {pending} Einträge...", flush=True)
        await agent.shutdown(timeout=30.0)
        print("Tschüss!")

    try:
        while not shutdown_event.is_set():
            # Non-blocking input: gibt den Event-Loop frei für pending async Tasks
            try:
                user_input = await asyncio.to_thread(input, "Du: ")
                user_input = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                break

            # Shutdown-Signal kam während input() lief
            if shutdown_event.is_set():
                break

            # Leere Eingabe → ignorieren
            if not user_input:
                continue

            # /exit Befehl
            if user_input.lower() in ("/exit", "exit", "quit"):
                break

            t0 = time.monotonic()
            response = await agent.run_async(user_input)
            elapsed = time.monotonic() - t0
            print(f"HAANA ({elapsed:.2f}s): {response}\n")

            # Context nach jeder Nachricht persistieren (atomares JSON-Write)
            agent.memory.save_context(agent._context_path)

    finally:
        await _do_shutdown()


async def _main():
    _setup_logging()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    instance = os.environ.get("HAANA_INSTANCE", "benni")

    try:
        agent = HaanaAgent(instance)
    except FileNotFoundError as e:
        print(f"Fehler: {e}")
        return

    # Startup: Context laden, pending Einträge aus letzter Session extrahieren
    await agent.startup()

    api_port = int(os.environ.get("HAANA_API_PORT", "0"))
    if api_port:
        # API-Modus: nur HTTP-Server starten (kein REPL – kein TTY im Container)
        import uvicorn
        from core.api import create_api
        api_app = create_api(agent)
        api_host = os.environ.get("HAANA_API_HOST", "0.0.0.0")
        config = uvicorn.Config(
            api_app, host=api_host, port=api_port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)
        logger.info(f"[{instance}] API-Server startet auf {api_host}:{api_port}")

        # Graceful shutdown bei SIGTERM/SIGINT
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, OSError):
                pass

        async def _run_server():
            await server.serve()
            stop_event.set()

        async def _wait_for_stop():
            await stop_event.wait()
            server.should_exit = True

        await asyncio.gather(_run_server(), _wait_for_stop())
        await agent.shutdown(timeout=30.0)
    else:
        await _repl(agent)


if __name__ == "__main__":
    asyncio.run(_main())
