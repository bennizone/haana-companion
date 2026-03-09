"""
HAANA Memory – Mem0 + Qdrant Wrapper

Drei Scopes mit separaten Qdrant-Collections:
  benni_memory  – Bennies persönliche Erinnerungen
  domi_memory   – Domis persönliche Erinnerungen
  household_memory    – gemeinsamer Haushaltskontext

LLM für Memory-Extraktion: Ollama (kein API-Key nötig).
Embedder: Ollama bge-m3, FastEmbed (lokal/CPU) oder OpenAI/Gemini.
Wenn kein Ollama verfügbar: Memory deaktiviert mit Warn-Log.

Sliding Window:
  Letzte N Nachrichten / M Minuten bleiben im lokalen Window-Buffer.
  Einträge die das Window verlassen werden async zu Qdrant extrahiert.
  Bei Extraktions-Fehler bleibt der Eintrag im Window (kein Datenverlust).

Persistenz:
  Window wird nach jeder Nachricht als JSON gespeichert (data/context/).
  Beim Start: JSON laden → pending Einträge sofort extrahieren.
  Bei Absturz: maximal die letzte Nachricht geht verloren.

Konfiguration via Env:
  HAANA_WINDOW_SIZE     – max Nachrichten im Window (Standard: 20)
  HAANA_WINDOW_MINUTES  – max Alter in Minuten (Standard: 60)
"""

import asyncio
import json
import os
import logging
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import core.logger as haana_log

logger = logging.getLogger(__name__)

VALID_SCOPES = {"benni_memory", "domi_memory", "household_memory"}

# Schreibberechtigungen pro Instanz
_WRITE_SCOPES: dict[str, set[str]] = {
    "benni":       {"benni_memory", "household_memory"},
    "domi":        {"domi_memory", "household_memory"},
    "ha-assist":   set(),
    "ha-advanced": {"household_memory"},
}

# Leseberechtigungen pro Instanz
_READ_SCOPES: dict[str, set[str]] = {
    "benni":       {"benni_memory", "household_memory"},
    "domi":        {"domi_memory", "household_memory"},
    "ha-assist":   {"benni_memory", "domi_memory", "household_memory"},
    "ha-advanced": {"benni_memory", "domi_memory", "household_memory"},
}


def _get_qdrant_host_port(qdrant_url: str = "") -> tuple[str, int]:
    """Parst QDRANT_URL in (host, port)."""
    url = qdrant_url or os.environ.get("QDRANT_URL", "http://qdrant:6333")
    url = url.replace("https://", "").replace("http://", "")
    host, _, port_str = url.partition(":")
    port = int(port_str) if port_str else 6333
    return host, port


CUSTOM_FACT_EXTRACTION_PROMPT = """\
Du bist ein Fakten-Extraktor für ein Haushalts-Assistenzsystem.
Extrahiere alle relevanten Fakten aus dem Gespräch zwischen User und Assistant.

WICHTIG:
- Berücksichtige BEIDE Seiten – sowohl User-Nachrichten als auch Assistant-Antworten.
- Der Assistant fasst oft Informationen zusammen oder bestätigt Fakten – diese sind genauso relevant.
- Keine Spekulation: Schreibe nur gesicherte Fakten, kein "vermutlich" oder "wahrscheinlich".
- Verknüpfe zusammengehörige Fakten (z.B. "Mystique ist eine Maine Coon" statt getrennt "Katze: Mystique" + "Rasse: Maine Coon").
- Ignoriere kurzfristige Aktionen (z.B. "Licht anschalten", "Test bestätigen") – nur dauerhafte Fakten.

Arten von Fakten die du extrahieren sollst:
1. Persönliche Daten: Namen, Geburtstage, Wohnort, Beruf
2. Vorlieben und Gewohnheiten: Essen, Trinken, Hobbys, Routinen
3. Haushalt: Mitbewohner, Haustiere, Geräte, Wohnung
4. Pläne und Termine: Urlaub, Verabredungen, Vorhaben
5. Korrekturen: Wenn etwas als falsch markiert wird, extrahiere die RICHTIGE Information

Beispiele:

User: was weißt du über mich?
Assistant: Du bist Benni, geboren am 1. Juli 1983. Du trinkst morgens gerne Kaffee.
Output: {"facts": ["Name ist Benni", "Geboren am 1. Juli 1983", "Trinkt morgens gerne Kaffee"]}

User: das stimmt nicht, ich trinke keinen Kaffee
Assistant: Entschuldigung, ich korrigiere das.
Output: {"facts": ["Trinkt keinen Kaffee"]}

User: Mystique ist eine Maine Coon, 3 Jahre alt
Assistant: Maine Coons sind tolle Katzen!
Output: {"facts": ["Mystique ist eine Maine Coon", "Mystique ist 3 Jahre alt"]}

User: Hallo
Assistant: Hi! Wie kann ich helfen?
Output: {"facts": []}

User: Mach das Licht in der Küche an
Assistant: Erledigt!
Output: {"facts": []}

Gib die Fakten als JSON zurück. Nur das JSON-Objekt mit dem Key "facts", nichts anderes.
WICHTIG: "facts" muss eine FLACHE LISTE von Strings sein, z.B. {"facts": ["Fakt 1", "Fakt 2"]}.
KEINE verschachtelten Objekte, KEINE Kategorien, KEINE Dicts — nur einfache Strings.
Erkenne die Sprache des Users und schreibe die Fakten in derselben Sprache.\
"""


def _build_mem0_config(collection_name: str, *,
                       qdrant_url: str = "",
                       ollama_url: str = "",
                       memory_llm: str = "",
                       embed_model: str = "",
                       embed_dims: int = 0,
                       embed_type: str = "ollama",
                       embed_url: str = "",
                       embed_key: str = "",
                       extract_url: str = "",
                       extract_key: str = "",
                       extract_type: str = "ollama") -> Optional[dict]:
    """
    Erstellt vollständige Mem0-Konfiguration für einen Scope.
    Gibt None zurück wenn kein LLM-Backend verfügbar ist.
    Parameter überschreiben Env-Vars (für InProcess-Modus mit mehreren Agents).

    extract_type steuert den LLM-Provider für Mem0's Extraktion.
    embed_type steuert den Embedding-Provider:
      - "ollama": Lokales Embedding via Ollama (default)
      - "openai": OpenAI-kompatible API (auch für Custom-Endpoints)
      - "gemini": Google Gemini Embeddings
      - "fastembed"/"local": Lokales CPU-Embedding via fastembed (kein externer Service)
    """
    host, port = _get_qdrant_host_port(qdrant_url)
    ollama_url = ollama_url or os.environ.get("OLLAMA_URL", "").strip()
    extract_url  = extract_url  or os.environ.get("HAANA_EXTRACT_URL", "").strip()
    extract_key  = extract_key  or os.environ.get("HAANA_EXTRACT_KEY", "").strip()
    extract_type = extract_type or os.environ.get("HAANA_EXTRACT_PROVIDER_TYPE", "ollama").strip()

    memory_llm  = memory_llm  or os.environ.get("HAANA_MEMORY_MODEL", "ministral-3-32k:3b")
    embed_model = embed_model or os.environ.get("HAANA_EMBEDDING_MODEL",  "bge-m3")
    embed_dims  = embed_dims  or int(os.environ.get("HAANA_EMBEDDING_DIMS", "1024"))
    embed_type  = embed_type  or os.environ.get("HAANA_EMBED_PROVIDER_TYPE", "ollama").strip()
    embed_url   = embed_url   or os.environ.get("HAANA_EMBED_URL", "").strip()
    embed_key   = embed_key   or os.environ.get("HAANA_EMBED_KEY", "").strip()

    # LLM-Konfiguration je nach Provider-Typ
    if extract_type == "ollama":
        if not ollama_url:
            logger.warning(
                f"[{collection_name}] OLLAMA_URL nicht gesetzt. "
                "Memory-Extraktion erfordert ein LLM-Backend. "
                "Memory für diesen Scope deaktiviert."
            )
            return None
        llm_config = {
            "provider": "ollama",
            "config": {
                "model": memory_llm,
                "ollama_base_url": ollama_url,
                "temperature": 0.1,
            },
        }
        llm_label = f"Ollama/{memory_llm} @ {ollama_url}"
    elif extract_type in ("anthropic", "minimax"):
        # OAuth: Kein API-Key, aber CLI-Extraction möglich → Dummy-Key für Mem0 Init
        use_cli = not extract_key and os.environ.get("HAANA_EXTRACT_OAUTH_DIR", "").strip()
        if not extract_key and not use_cli:
            logger.warning(
                f"[{collection_name}] Kein API-Key für {extract_type}. "
                "Memory für diesen Scope deaktiviert."
            )
            return None
        if use_cli:
            extract_key = "cli-oauth-placeholder"
        llm_cfg = {
            "model": memory_llm,
            "api_key": extract_key,
            "temperature": 0.1,
        }
        if extract_url:
            llm_cfg["anthropic_base_url"] = extract_url
        llm_config = {"provider": "anthropic", "config": llm_cfg}
        llm_label = f"{extract_type}/{memory_llm} @ {extract_url or 'api.anthropic.com'}"
    elif extract_type == "openai":
        if not extract_key:
            logger.warning(
                f"[{collection_name}] Kein API-Key für OpenAI. "
                "Memory für diesen Scope deaktiviert."
            )
            return None
        llm_cfg = {
            "model": memory_llm,
            "api_key": extract_key,
            "temperature": 0.1,
        }
        if extract_url:
            llm_cfg["openai_base_url"] = extract_url
        llm_config = {"provider": "openai", "config": llm_cfg}
        llm_label = f"OpenAI/{memory_llm} @ {extract_url or 'api.openai.com'}"
    elif extract_type == "gemini":
        if not extract_key:
            logger.warning(
                f"[{collection_name}] Kein API-Key für Gemini. "
                "Memory für diesen Scope deaktiviert."
            )
            return None
        llm_config = {
            "provider": "gemini",
            "config": {
                "model": memory_llm,
                "api_key": extract_key,
                "temperature": 0.1,
            },
        }
        llm_label = f"Gemini/{memory_llm}"
    else:
        logger.warning(f"[{collection_name}] Unbekannter extract_type: {extract_type}")
        return None

    # Embedder-Konfiguration je nach Provider-Typ
    if embed_type == "ollama":
        if not ollama_url:
            logger.warning(
                f"[{collection_name}] OLLAMA_URL nicht gesetzt. "
                "Embeddings erfordern Ollama. Memory deaktiviert."
            )
            return None
        embedder_config = {
            "provider": "ollama",
            "config": {
                "model": embed_model,
                "ollama_base_url": ollama_url,
                "embedding_dims": embed_dims,
            },
        }
        embed_label = f"Ollama/{embed_model} @ {ollama_url}"
    elif embed_type == "openai":
        if not embed_key:
            logger.warning(f"[{collection_name}] Kein API-Key für OpenAI Embeddings. Memory deaktiviert.")
            return None
        embed_cfg = {
            "model": embed_model or "text-embedding-3-small",
            "api_key": embed_key,
            "embedding_dims": embed_dims,
        }
        if embed_url:
            embed_cfg["openai_base_url"] = embed_url
        embedder_config = {"provider": "openai", "config": embed_cfg}
        embed_label = f"OpenAI/{embed_model} @ {embed_url or 'api.openai.com'}"
    elif embed_type == "gemini":
        if not embed_key:
            logger.warning(f"[{collection_name}] Kein API-Key für Gemini Embeddings. Memory deaktiviert.")
            return None
        embedder_config = {
            "provider": "gemini",
            "config": {
                "model": embed_model or "models/text-embedding-004",
                "api_key": embed_key,
                "embedding_dims": embed_dims,
            },
        }
        embed_label = f"Gemini/{embed_model}"
    elif embed_type in ("fastembed", "local"):
        # Lokales CPU-Embedding via fastembed – kein externer Service nötig
        fastembed_model = embed_model or "BAAI/bge-small-en-v1.5"
        fastembed_dims = embed_dims or 384
        embedder_config = {
            "provider": "fastembed",
            "config": {
                "model": fastembed_model,
                "embedding_dims": fastembed_dims,
            },
        }
        embed_label = f"FastEmbed/{fastembed_model} (lokal)"
    else:
        logger.warning(f"[{collection_name}] Unbekannter embed_type: {embed_type}")
        return None

    config = {
        "custom_fact_extraction_prompt": CUSTOM_FACT_EXTRACTION_PROMPT,
        "llm": llm_config,
        "embedder": embedder_config,
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "host": host,
                "port": port,
                "embedding_model_dims": fastembed_dims if embed_type in ("fastembed", "local") else embed_dims,
            },
        },
    }

    logger.debug(
        f"[{collection_name}] Mem0 config: "
        f"LLM={llm_label}, "
        f"Embedder={embed_label} (dims={embed_dims}), Qdrant={host}:{port}"
    )
    return config


# ── Sliding Window ─────────────────────────────────────────────────────────────

@dataclass
class _WindowEntry:
    user: str
    assistant: str
    scope: Optional[str]
    # Wall-clock (time.time()) für Persistenz über Neustarts hinweg
    timestamp: float = field(default_factory=time.time)
    extracting: bool = False  # True = Hintergrund-Task läuft gerade
    classify_retries: int = 0  # Fehlversuche bei Scope-Klassifikation
    already_extracted: bool = False  # True = bereits via add_immediate() extrahiert


class ConversationWindow:
    """
    Sliding Window für die lokale Konversationshistorie.

    Regeln:
      - Mindestens min_messages Einträge bleiben immer im Window
      - Einträge die weder in den letzten max_messages noch in den letzten
        max_age_minutes liegen → Overflow → async zu Qdrant extrahieren
      - Bei Extraktions-Fehler: Eintrag bleibt (kein Datenverlust)
    """

    def __init__(
        self,
        max_messages: int = 20,
        max_age_minutes: int = 60,
        min_messages: int = 5,
    ):
        self.max_messages = max_messages
        self.max_age_minutes = max_age_minutes
        self.min_messages = min_messages
        self._entries: list[_WindowEntry] = []

    def add(self, user: str, assistant: str, scope: str) -> list[_WindowEntry]:
        """Fügt Eintrag hinzu. Gibt Overflow-Kandidaten zurück."""
        self._entries.append(_WindowEntry(user=user, assistant=assistant, scope=scope))
        return self._get_overflow()

    def _get_overflow(self) -> list[_WindowEntry]:
        """
        Bestimmt Einträge die das Window verlassen sollen.
        Ein Eintrag verlässt das Window wenn er KEINER der drei Bedingungen entspricht:
          - in_count: unter den letzten max_messages
          - in_time:  jünger als max_age_minutes
          - in_min:   unter den letzten min_messages (Safety-Floor)
        """
        now = time.time()
        max_age_sec = self.max_age_minutes * 60
        n = len(self._entries)
        overflow = []

        for i, entry in enumerate(self._entries):
            if entry.extracting:
                continue

            # Abstand vom neuesten Eintrag (0 = neuester)
            pos_from_newest = n - 1 - i

            in_count = pos_from_newest < self.max_messages
            in_time  = (now - entry.timestamp) <= max_age_sec
            in_min   = pos_from_newest < self.min_messages

            if not (in_count or in_time or in_min):
                entry.extracting = True
                overflow.append(entry)

        return overflow

    def mark_extracted(self, entry: _WindowEntry):
        """Entfernt erfolgreich extrahierten Eintrag."""
        try:
            self._entries.remove(entry)
        except ValueError:
            pass

    def mark_failed(self, entry: _WindowEntry):
        """Extraktion fehlgeschlagen – Eintrag bleibt im Window."""
        entry.extracting = False

    def size(self) -> int:
        return len(self._entries)

    # ── Persistenz ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialisiert Window-Zustand als dict (JSON-kompatibel)."""
        return {
            "version": 1,
            "saved_at": time.time(),
            "config": {
                "max_messages": self.max_messages,
                "max_age_minutes": self.max_age_minutes,
                "min_messages": self.min_messages,
            },
            "entries": [
                {
                    "user": e.user,
                    "assistant": e.assistant,
                    "scope": e.scope,  # None wenn noch nicht klassifiziert
                    "timestamp": e.timestamp,
                    # War der Task aktiv als gespeichert wurde? → pending auf nächsten Start
                    "pending_extraction": e.extracting,
                    "classify_retries": e.classify_retries,
                    "already_extracted": e.already_extracted,
                }
                for e in self._entries
            ],
        }

    def from_dict(self, d: dict) -> list[_WindowEntry]:
        """
        Stellt Window-Zustand aus dict wieder her.
        Gibt Liste der Einträge zurück die sofort extrahiert werden sollen:
          - Einträge die beim letzten Speichern extracting=True waren
          - Einträge die jetzt durch Overflow das Window verlassen würden
        """
        self._entries.clear()
        immediately_pending: list[_WindowEntry] = []

        for item in d.get("entries", []):
            entry = _WindowEntry(
                user=item["user"],
                assistant=item["assistant"],
                scope=item.get("scope"),  # None wenn noch nicht klassifiziert
                timestamp=item["timestamp"],
                extracting=False,
                classify_retries=item.get("classify_retries", 0),
                already_extracted=item.get("already_extracted", False),
            )
            self._entries.append(entry)
            if item.get("pending_extraction", False):
                entry.extracting = True
                immediately_pending.append(entry)

        # Zusätzlich: Overflow aus aktuellem Stand neu berechnen
        # (z.B. wenn max_messages seit letztem Start verkleinert wurde)
        new_overflow = self._get_overflow()
        immediately_pending.extend(new_overflow)

        return immediately_pending


# ── HaanaMemory ────────────────────────────────────────────────────────────────

def _load_scopes(instance_name: str) -> tuple[set[str], set[str]]:
    """
    Liest Write/Read-Scopes aus Env-Variablen oder fällt auf Hardcoded-Defaults zurück.
    HAANA_WRITE_SCOPES / HAANA_READ_SCOPES: kommagetrennte Scope-Namen.
    Erlaubt dynamisch erstellte User-Instanzen ohne Code-Änderungen.
    """
    write_env = os.environ.get("HAANA_WRITE_SCOPES")
    read_env  = os.environ.get("HAANA_READ_SCOPES")
    write = (
        {s.strip() for s in write_env.split(",") if s.strip()}
        if write_env is not None
        else _WRITE_SCOPES.get(instance_name, set())
    )
    read = (
        {s.strip() for s in read_env.split(",") if s.strip()}
        if read_env is not None
        else _READ_SCOPES.get(instance_name, set())
    )
    return write, read


class _RateLimiter:
    """Einfacher Token-Bucket Rate-Limiter (Thread-safe)."""

    def __init__(self, max_per_minute: int):
        import threading
        self._interval = 60.0 / max_per_minute if max_per_minute > 0 else 0.0
        self._rpm = max_per_minute
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        """Blockiert bis der nächste Request erlaubt ist."""
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                delay = self._next_allowed - now
                time.sleep(delay)
            self._next_allowed = max(time.monotonic(), self._next_allowed) + self._interval


class _NoopLimiter:
    """Dummy-Limiter der nichts tut (für Provider ohne Rate-Limit)."""
    def wait(self):
        pass


# Shared Rate-Limiters Registry (key = "provider_type:rpm")
_limiter_registry: dict[str, _RateLimiter] = {}
_NOOP_LIMITER = _NoopLimiter()

# Standard-Embedding-Limiter (Gemini Free Tier: 100/min)
_gemini_embed_limiter = _RateLimiter(max_per_minute=90)


def _get_llm_limiter(rpm: int) -> object:
    """Holt oder erstellt einen shared Rate-Limiter für die gegebene RPM."""
    if rpm <= 0:
        return _NOOP_LIMITER
    key = str(rpm)
    if key not in _limiter_registry:
        _limiter_registry[key] = _RateLimiter(max_per_minute=rpm)
        logger.info(f"Rate-Limiter erstellt: {rpm} RPM")
    return _limiter_registry[key]



def _find_claude_cli() -> Optional[str]:
    """Findet den Pfad zur Claude CLI Binary."""
    import shutil
    # Bundled im SDK
    sdk_path = Path("/usr/local/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude")
    if sdk_path.is_file():
        return str(sdk_path)
    # Im PATH
    found = shutil.which("claude")
    return found


def _call_anthropic_direct(llm_instance, *args, **kwargs) -> str:
    """Ruft Anthropic-kompatible API direkt auf und extrahiert TextBlock.

    Workaround für Mem0-Bug: generate_response crasht bei ThinkingBlock
    (MiniMax und andere Modelle mit Extended Thinking).
    """
    messages = kwargs.get("messages", args[0] if args else [])
    system_message = ""
    filtered = []
    for msg in messages:
        if isinstance(msg, dict):
            if msg.get("role") == "system":
                system_message = msg.get("content", "")
            else:
                filtered.append(msg)
    params = {
        "model": llm_instance.config.model,
        "messages": filtered,
        "max_tokens": getattr(llm_instance.config, "max_tokens", 2000),
    }
    if system_message:
        params["system"] = system_message
    response = llm_instance.client.messages.create(**params)
    # TextBlock finden (ThinkingBlocks überspringen)
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    # Fallback: ersten Block als String
    return str(response.content[0]) if response.content else ""


def _call_claude_cli(prompt: str, model: str, timeout: int = 60) -> Optional[str]:
    """Ruft die Claude CLI auf (nutzt OAuth-Credentials automatisch).

    Gibt die Antwort als String zurück oder None bei Fehler.
    """
    cli = _find_claude_cli()
    if not cli:
        logger.debug("Claude CLI nicht gefunden")
        return None
    import subprocess
    try:
        result = subprocess.run(
            [cli, "-p", "--model", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            logger.debug(f"Claude CLI stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logger.warning(f"Claude CLI Timeout ({timeout}s)")
    except Exception as e:
        logger.debug(f"Claude CLI Fehler: {e}")
    return None


class HaanaMemory:
    def __init__(self, instance_name: str):
        self.instance = instance_name
        self.write_scopes, self.read_scopes = _load_scopes(instance_name)

        # Env-Snapshot für Runtime-Zugriffe (InProcess: env wird nach __init__ restored)
        self._qdrant_url   = os.environ.get("QDRANT_URL", "http://qdrant:6333")
        self._ollama_url   = os.environ.get("OLLAMA_URL", "").strip()
        self._memory_model = os.environ.get("HAANA_MEMORY_MODEL", "ministral-3-32k:3b")
        self._embed_model  = os.environ.get("HAANA_EMBEDDING_MODEL", "bge-m3")
        self._embed_dims   = int(os.environ.get("HAANA_EMBEDDING_DIMS", "1024"))
        self._embed_type   = os.environ.get("HAANA_EMBED_PROVIDER_TYPE", "ollama").strip()
        self._embed_url    = os.environ.get("HAANA_EMBED_URL", "").strip()
        self._embed_key    = os.environ.get("HAANA_EMBED_KEY", "").strip()

        # Extraction-Provider (kann Ollama, MiniMax, Anthropic, OpenAI etc. sein)
        self._extract_url  = os.environ.get("HAANA_EXTRACT_URL", "").strip()
        self._extract_key  = os.environ.get("HAANA_EXTRACT_KEY", "").strip()
        self._extract_type = os.environ.get("HAANA_EXTRACT_PROVIDER_TYPE", "ollama").strip()

        # Anthropic OAuth: Messages API akzeptiert keine OAuth-Tokens,
        # aber die Claude CLI nutzt OAuth automatisch → CLI als Fallback.
        self._extract_oauth_dir = os.environ.get("HAANA_EXTRACT_OAUTH_DIR", "").strip()
        self._use_cli_extraction = False
        if self._extract_type == "anthropic" and not self._extract_key:
            if self._extract_oauth_dir and _find_claude_cli():
                self._use_cli_extraction = True
                logger.info(
                    f"[{instance_name}] Extraction-LLM nutzt Claude CLI (OAuth) "
                    f"mit Modell {self._memory_model}"
                )
            elif self._extract_oauth_dir:
                logger.warning(
                    f"[{instance_name}] Anthropic-Extraction: OAuth vorhanden aber "
                    f"Claude CLI nicht gefunden. Memory-Extraktion deaktiviert."
                )

        # Ollama Thinking-Modus: "true"/"false"/"" (leer = nicht setzen)
        _think_raw = os.environ.get("HAANA_EXTRACT_THINK", "").strip().lower()
        self._extract_think: Optional[bool] = (
            True if _think_raw == "true" else False if _think_raw == "false" else None
        )

        # Rate-Limiter für Extraction-LLM (0 = kein Limit)
        extract_rpm = int(os.environ.get("HAANA_EXTRACT_RPM", "0"))
        self._extract_limiter = _get_llm_limiter(extract_rpm)

        # Kontext-Anreicherung (löst Pronomen/Bezüge vor Extraktion auf)
        self._context_enrichment = os.environ.get(
            "HAANA_CONTEXT_ENRICHMENT", "false"
        ).lower() in ("true", "1", "yes")

        # Kontext-Fenster für Extraktion (Nachrichten vor/nach der aktuellen)
        self._context_before = int(os.environ.get("HAANA_CONTEXT_BEFORE", "3"))
        self._context_after = int(os.environ.get("HAANA_CONTEXT_AFTER", "2"))

        # Lazy-loaded Mem0-Instanzen pro Scope (None = nicht verfügbar)
        self._memories: dict[str, object] = {}

        # Sliding Window (Konfiguration aus Env)
        self._window = ConversationWindow(
            max_messages=int(os.environ.get("HAANA_WINDOW_SIZE", "20")),
            max_age_minutes=int(os.environ.get("HAANA_WINDOW_MINUTES", "60")),
            min_messages=5,
        )

        # Tracking laufender Extraktions-Tasks (für flush_pending / shutdown)
        self._pending_tasks: set[asyncio.Task] = set()

        # Anzahl Memory-Treffer aus letztem search()-Aufruf (für Logging)
        self._last_search_hits: int = 0

        # Rate-Limit-Tracking: aufeinanderfolgende Fehler zählen
        self._consecutive_write_errors: int = 0
        self._rate_limit_warned: bool = False

        logger.info(
            f"[{instance_name}] Memory init | "
            f"write={sorted(self.write_scopes)} | "
            f"read={sorted(self.read_scopes)} | "
            f"window={self._window.max_messages}msg / "
            f"{self._window.max_age_minutes}min / min=5 | "
            f"context={self._context_before}+{self._context_after}"
        )

    def _check_collection_dims(self, scope: str, expected_dims: int):
        """Prüft ob eine Qdrant-Collection die erwarteten Dimensionen hat.

        Falls Mismatch: Collection löschen, damit Mem0 sie mit korrekten Dims neu erstellt.
        """
        try:
            import httpx
            resp = httpx.get(f"{self._qdrant_url}/collections/{scope}", timeout=5.0)
            if resp.status_code != 200:
                return  # Collection existiert nicht → wird von Mem0 neu erstellt
            data = resp.json()
            vectors_cfg = data.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            current_dims = vectors_cfg.get("size", 0)
            if current_dims and current_dims != expected_dims:
                logger.warning(
                    f"[{self.instance}] Embedding-Dimensions-Mismatch in '{scope}': "
                    f"Collection hat {current_dims}d, erwartet {expected_dims}d. "
                    f"Collection wird gelöscht und neu erstellt."
                )
                del_resp = httpx.delete(f"{self._qdrant_url}/collections/{scope}", timeout=10.0)
                if del_resp.status_code == 200:
                    logger.info(f"[{self.instance}] Collection '{scope}' gelöscht (Rebuild nötig).")
                else:
                    logger.error(f"[{self.instance}] Collection '{scope}' löschen fehlgeschlagen: {del_resp.text}")
        except Exception as e:
            logger.debug(f"[{self.instance}] Collection-Dims-Check fehlgeschlagen: {e}")

    def _get_memory(self, scope: str):
        """Lazy-load einer Mem0 Memory-Instanz für einen Scope."""
        if scope not in self._memories:
            config = _build_mem0_config(
                scope,
                qdrant_url=self._qdrant_url,
                ollama_url=self._ollama_url,
                memory_llm=self._memory_model,
                embed_model=self._embed_model,
                embed_dims=self._embed_dims,
                embed_type=self._embed_type,
                embed_url=self._embed_url,
                embed_key=self._embed_key,
                extract_url=self._extract_url,
                extract_key=self._extract_key,
                extract_type=self._extract_type,
            )
            if config is None:
                self._memories[scope] = None
                return None

            try:
                # Dimensions-Mismatch erkennen: Falls Collection existiert aber
                # andere Dimensionen hat (z.B. nach Embedding-Wechsel), löschen & neu anlegen.
                self._check_collection_dims(scope, self._embed_dims)

                from mem0 import Memory
                mem = Memory.from_config(config)
                # Mem0-Bug: AnthropicLLM ignoriert anthropic_base_url beim Client-Erstellen.
                # Fix: base_url nachträglich setzen (nötig für MiniMax/kompatible APIs).
                if self._extract_url and hasattr(mem.llm, "client"):
                    _c = mem.llm.client
                    if hasattr(_c, "_base_url"):
                        import httpx as _hx
                        _url = self._extract_url.rstrip("/") + "/"
                        _c._base_url = _hx.URL(_url)
                        logger.info(f"[{scope}] Anthropic base_url → {self._extract_url}")
                # Ollama think-Modus: client.chat() um think-Parameter erweitern
                if self._extract_think is not None and self._extract_type == "ollama":
                    _think_val = self._extract_think
                    _orig_chat = mem.llm.client.chat
                    _think_num_predict = 8192 if _think_val else None
                    def _chat_with_think(**kw):
                        kw["think"] = _think_val
                        if _think_val and _think_num_predict:
                            kw.setdefault("options", {})["num_predict"] = _think_num_predict
                        return _orig_chat(**kw)
                    mem.llm.client.chat = _chat_with_think
                    logger.info(f"[{scope}] Ollama think={_think_val}")
                # Monkeypatch: LLM-Antworten sanitizen (manche LLMs geben Dicts statt Strings)
                # + Rate-Limit-Retry (429 → Backoff bis zu 3 Versuche)
                _orig_generate = mem.llm.generate_response
                _inst = self.instance
                _limiter = self._extract_limiter
                _cli_mode = self._use_cli_extraction
                _cli_model = self._memory_model
                # Provider mit Extended Thinking: Mem0 crasht bei ThinkingBlock → direkte API
                _THINKING_PROVIDERS = ("minimax",)
                _has_thinking = self._extract_type in _THINKING_PROVIDERS
                def _sanitized_generate(*args, **kwargs):
                    import json as _json
                    _limiter.wait()
                    # CLI-Extraction: Claude CLI statt SDK aufrufen
                    if _cli_mode:
                        prompt_text = ""
                        if args:
                            prompt_text = str(args[0])
                        elif "messages" in kwargs:
                            # Mem0 übergibt messages als Liste
                            for m in kwargs["messages"]:
                                if hasattr(m, "content"):
                                    prompt_text += m.content + "\n"
                                elif isinstance(m, dict):
                                    prompt_text += m.get("content", "") + "\n"
                        resp = _call_claude_cli(prompt_text.strip(), _cli_model, timeout=90)
                        if resp is None:
                            raise RuntimeError("Claude CLI returned no response")
                        return resp
                    _max_retries = 3
                    for _attempt in range(_max_retries):
                        try:
                            # ThinkingBlock-Modelle (MiniMax): Mem0 crasht bei content[0].text
                            # → Direkt API aufrufen und TextBlock extrahieren
                            if _has_thinking:
                                resp = _call_anthropic_direct(mem.llm, *args, **kwargs)
                            else:
                                resp = _orig_generate(*args, **kwargs)
                            break
                        except Exception as _rate_err:
                            _err_str = str(_rate_err)
                            if "429" in _err_str or "RESOURCE_EXHAUSTED" in _err_str:
                                _wait = min(15 * (2 ** _attempt), 60)
                                logger.warning(
                                    f"[{_inst}] Mem0 LLM Rate-Limit (429), "
                                    f"Retry {_attempt + 1}/{_max_retries} in {_wait}s"
                                )
                                time.sleep(_wait)
                                if _attempt == _max_retries - 1:
                                    raise
                            else:
                                raise
                    try:
                        from mem0.utils.helper import remove_code_blocks
                        cleaned = remove_code_blocks(resp)
                        parsed = _json.loads(cleaned)
                        changed = False
                        # Facts-Liste: Dicts -> Strings
                        if "facts" in parsed and isinstance(parsed["facts"], list):
                            sanitized = []
                            for f in parsed["facts"]:
                                if isinstance(f, str):
                                    sanitized.append(f)
                                elif isinstance(f, dict):
                                    for k, v in f.items():
                                        if isinstance(v, dict):
                                            for k2, v2 in v.items():
                                                sanitized.append(f"{k2}: {v2}")
                                        else:
                                            sanitized.append(f"{k}: {v}")
                                else:
                                    sanitized.append(str(f))
                            parsed["facts"] = sanitized
                            changed = True
                        # Memory-Actions: Strings -> Dicts mit text+event
                        if "memory" in parsed and isinstance(parsed["memory"], list):
                            sanitized_mem = []
                            for item in parsed["memory"]:
                                if isinstance(item, str):
                                    sanitized_mem.append({"text": item, "event": "ADD"})
                                elif isinstance(item, dict):
                                    sanitized_mem.append(item)
                                else:
                                    sanitized_mem.append({"text": str(item), "event": "ADD"})
                            parsed["memory"] = sanitized_mem
                            changed = True
                        if changed:
                            return _json.dumps(parsed)
                    except Exception:
                        pass
                    return resp
                mem.llm.generate_response = _sanitized_generate
                # Gemini Embedding Rate-Limiter (100/min Free Tier)
                if self._embed_type == "gemini":
                    _orig_embed = mem.embedding_model.embed
                    def _rate_limited_embed(*args, **kwargs):
                        _gemini_embed_limiter.wait()
                        return _orig_embed(*args, **kwargs)
                    mem.embedding_model.embed = _rate_limited_embed
                self._memories[scope] = mem
                logger.info(f"[{self.instance}] Memory-Instanz '{scope}' bereit.")
            except Exception as e:
                logger.error(
                    f"[{self.instance}] Memory-Init '{scope}' fehlgeschlagen: {e}",
                    exc_info=True,
                )
                self._memories[scope] = None

        return self._memories[scope]

    def _resolve_scope(self, assistant_response: str, scope: Optional[str]) -> Optional[str]:
        """
        Bestimmt den Ziel-Scope für einen Memory-Write.

        1. Explizit übergeben → direkt verwenden
        2. Scope-Name in Agentenantwort erkannt → verwenden
        3. LLM-Klassifikation via Ollama → personal vs. household
        4. None → Klassifikation fehlgeschlagen, Eintrag bleibt im Window
        """
        if scope is not None:
            return scope

        # Versuch 1: Scope aus Agentenantwort lesen
        match = re.search(
            r"\b(benni_memory|domi_memory|household_memory)\b",
            assistant_response,
        )
        if match and match.group(1) in self.write_scopes:
            scope = match.group(1)
            logger.debug(f"[{self.instance}] Scope aus Agentenantwort: '{scope}'")
            return scope

        # Versuch 2: LLM-Klassifikation (nur wenn household_memory schreibbar)
        if "household_memory" in self.write_scopes:
            classified = self._classify_scope_via_llm(assistant_response)
            if classified:
                logger.debug(f"[{self.instance}] Scope via LLM: '{classified}'")
                return classified

        # Nur ein Write-Scope → eindeutig, kein LLM nötig
        if len(self.write_scopes) == 1:
            scope = next(iter(self.write_scopes))
            logger.debug(f"[{self.instance}] Scope eindeutig (einziger Write-Scope): '{scope}'")
            return scope

        # Keine Klassifikation möglich → None (Eintrag bleibt im Window)
        logger.debug(f"[{self.instance}] Scope-Klassifikation nicht möglich, bleibt pending")
        return None

    def _classify_scope_via_llm(self, text: str) -> Optional[str]:
        """
        Fragt das Extraction-LLM ob die Information persönlich oder haushaltsbezogen ist.
        Nutzt denselben Provider wie die Memory-Extraktion (_call_extract_llm).
        """
        personal_scope = next(
            (s for s in self.write_scopes if s != "household_memory"),
            None,
        )
        if not personal_scope:
            return None

        prompt = (
            "Klassifiziere die folgende Information in genau eine Kategorie:\n"
            f"- PERSONAL: betrifft nur eine einzelne Person ({self.instance}), "
            "z.B. Geburtstag, Beruf, Vorlieben, persönliche Termine, Urlaub\n"
            "- HOUSEHOLD: betrifft den gemeinsamen Haushalt, z.B. Haustiere, "
            "Wohnung, gemeinsame Geräte, Familienmitglieder, Mitbewohner\n\n"
            f"Text:\n{text[:500]}\n\n"
            "Antworte mit genau einem Wort: PERSONAL oder HOUSEHOLD"
        )

        answer = self._call_extract_llm(prompt)
        if not answer:
            return None
        answer = answer.strip().upper()
        if "HOUSEHOLD" in answer:
            return "household_memory"
        if "PERSONAL" in answer:
            return personal_scope
        logger.warning(
            f"[{self.instance}] Scope-Klassifikation: unerwartete Antwort '{answer[:50]}'"
        )

        return None

    # ── Lesen ─────────────────────────────────────────────────────────────────

    def search(self, query: str, scopes: Optional[list[str]] = None) -> str:
        """
        Sucht in allen lesbaren Scopes nach relevantem Kontext.
        Gibt formatierten String zurück: "[scope] erinnerung\n..."
        Leerer String wenn nichts gefunden oder Memory nicht verfügbar.
        """
        if scopes is None:
            scopes = list(self.read_scopes)

        all_results: list[tuple[float, str, str]] = []

        for scope in scopes:
            if scope not in self.read_scopes:
                logger.warning(f"[{self.instance}] Lesezugriff auf '{scope}' verweigert.")
                continue

            mem = self._get_memory(scope)
            if mem is None:
                continue

            try:
                user_id = scope.replace("_memory", "")
                results = mem.search(query=query, user_id=user_id, limit=5)
                for r in results.get("results", []):
                    content = r.get("memory") or r.get("content", "")
                    score = float(r.get("score", 0))
                    if content:
                        all_results.append((score, scope, content))
            except Exception as e:
                logger.error(
                    f"[{self.instance}] Memory-Suche in '{scope}' fehlgeschlagen: {e}"
                )

        if not all_results:
            self._last_search_hits = 0
            haana_log.log_memory_op(
                instance=self.instance, op="read",
                scope=",".join(scopes), query=query[:200],
                results_count=0,
            )
            return ""

        all_results.sort(reverse=True)
        top = all_results[:10]
        lines = [f"[{scope}] {content}" for _, scope, content in top]
        result_str = "\n".join(lines)

        self._last_search_hits = len(top)
        haana_log.log_memory_op(
            instance=self.instance, op="read",
            scope=",".join(scopes), query=query[:200],
            results_count=len(top),
            content_preview=result_str,
        )
        return result_str

    # ── Schreiben (synchron, für Thread-Executor) ──────────────────────────────

    def add(
        self,
        messages: list[dict],
        scope: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Schreibt Konversation synchron in Mem0/Qdrant.
        Wird vom async Extraktions-Task im Thread-Executor aufgerufen.
        """
        if scope not in VALID_SCOPES:
            logger.error(f"[{self.instance}] Ungültiger Scope: '{scope}'")
            return False

        if scope not in self.write_scopes:
            logger.error(
                f"[{self.instance}] Schreibzugriff auf '{scope}' verweigert "
                f"(erlaubt: {sorted(self.write_scopes)})"
            )
            return False

        mem = self._get_memory(scope)
        if mem is None:
            return False

        try:
            user_id = scope.replace("_memory", "")
            content_preview = " | ".join(m.get("content", "")[:100] for m in messages)
            result = mem.add(
                messages=messages,
                user_id=user_id,
                infer=True,
                metadata=metadata or {},
            )
            logger.info(
                f"[{self.instance}] Memory gespeichert | "
                f"scope={scope} | user_id={user_id} | result={result}"
            )
            haana_log.log_memory_op(
                instance=self.instance, op="write",
                scope=scope,
                content_preview=content_preview,
                success=True,
            )
            # Fehler-Zähler zurücksetzen bei Erfolg
            self._consecutive_write_errors = 0
            self._rate_limit_warned = False
            return True
        except Exception as e:
            self._consecutive_write_errors += 1
            is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if is_rate_limit and not self._rate_limit_warned:
                logger.error(
                    f"[{self.instance}] ⚠ RATE LIMIT: Memory-Extraktion schlägt "
                    f"wegen API-Rate-Limit fehl (429). Nachrichten bleiben im "
                    f"Window und gehen nicht verloren. Bitte API-Plan prüfen oder "
                    f"Extraction-LLM wechseln (z.B. Ollama lokal)."
                )
                self._rate_limit_warned = True
            elif self._consecutive_write_errors >= 5 and not self._rate_limit_warned:
                logger.error(
                    f"[{self.instance}] ⚠ Memory-Extraktion fehlgeschlagen "
                    f"({self._consecutive_write_errors}x in Folge). "
                    f"Nachrichten bleiben im Window. Bitte Logs prüfen."
                )
                self._rate_limit_warned = True
            logger.error(
                f"[{self.instance}] Memory-Write in '{scope}' fehlgeschlagen: {e}",
                exc_info=True,
            )
            haana_log.log_memory_op(
                instance=self.instance, op="write",
                scope=scope, success=False, error=str(e),
            )
            return False

    # ── Async Extraktion ───────────────────────────────────────────────────────

    def _track_task(self, task: asyncio.Task):
        """Registriert einen Task und entfernt ihn automatisch bei Abschluss."""
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    _CLASSIFY_MAX_RETRIES = 5

    def _build_extraction_context(self, entry: _WindowEntry,
                                   context_before: int = 3,
                                   context_after: int = 2) -> str:
        """
        Baut Kontext-String mit umgebenden Nachrichten für die Extraktion.
        Die Ziel-Nachricht wird mit >>> markiert.
        Gibt leeren String zurück wenn kein Kontext verfügbar.
        """
        entries = self._window._entries
        try:
            idx = entries.index(entry)
        except ValueError:
            return ""

        start = max(0, idx - context_before)
        end = min(len(entries), idx + context_after + 1)

        # Kein Kontext wenn nur die Ziel-Nachricht vorhanden
        if start == idx and end == idx + 1:
            return ""

        lines = []
        for i in range(start, end):
            e = entries[i]
            if i == idx:
                lines.append(f"\n>>> AKTUELLE NACHRICHT:")
                lines.append(f"[User]: {e.user}")
                lines.append(f"[Assistant]: {e.assistant}")
            else:
                lines.append(f"[User]: {e.user}")
                lines.append(f"[Assistant]: {e.assistant}")

        return "\n".join(lines)

    _ENRICH_PROMPT = (
        "Schreibe die AKTUELLE NACHRICHT (markiert mit >>>) so um, dass sie "
        "ohne Kontext verständlich ist. Löse alle Pronomen und Bezüge auf.\n"
        "Gib NUR die umgeschriebene Nachricht zurück im Format:\n"
        "User: <umgeschriebene User-Nachricht>\n"
        "Assistant: <umgeschriebene Assistant-Antwort>\n\n"
        "Regeln:\n"
        "- Ersetze 'er/sie/es/das/die' durch konkrete Namen/Begriffe\n"
        "- Behalte alle Fakten bei, füge keine neuen hinzu\n"
        "- Kürze lange Antworten auf die faktisch relevanten Teile\n"
        "- Wenn nichts aufzulösen ist, gib die Nachricht unverändert zurück\n"
    )

    _RATE_LIMIT_MAX_RETRIES = 3

    def _call_extract_llm(self, prompt: str) -> Optional[str]:
        """
        Ruft das konfigurierte Extraction-LLM auf (Ollama oder API-Provider).
        Gibt die Antwort als String zurück oder None bei Fehler.
        Bei 429 Rate-Limit: Retry mit Backoff (bis zu 3 Versuche).
        Unterstützt OAuth-Token für Anthropic (wenn kein API-Key).
        """
        import requests as req

        # CLI-Extraction für OAuth (kein Retry-Loop nötig, CLI macht eigenes Auth)
        if self._use_cli_extraction:
            self._extract_limiter.wait()
            result = _call_claude_cli(prompt, self._memory_model)
            if result is None:
                logger.warning(f"[{self.instance}] Claude CLI Extraction fehlgeschlagen")
            return result

        for attempt in range(self._RATE_LIMIT_MAX_RETRIES):
            try:
                self._extract_limiter.wait()
                r = None
                if self._extract_type == "ollama":
                    url = self._extract_url or self._ollama_url
                    if not url:
                        return None
                    r = req.post(
                        f"{url}/api/generate",
                        json={"model": self._memory_model, "prompt": prompt, "stream": False},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        return r.json().get("response", "").strip()
                elif self._extract_type in ("anthropic", "minimax"):
                    url = self._extract_url or "https://api.anthropic.com"
                    if not self._extract_key:
                        logger.warning(f"[{self.instance}] Kein API-Key für Anthropic Extraction")
                        return None
                    headers = {
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self._extract_key,
                    }
                    r = req.post(
                        f"{url}/v1/messages",
                        headers=headers,
                        json={
                            "model": self._memory_model,
                            "max_tokens": 1024,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                        timeout=60,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        for block in data.get("content", []):
                            if block.get("type") == "text":
                                return block.get("text", "").strip()
                elif self._extract_type == "openai":
                    url = self._extract_url or "https://api.openai.com/v1"
                    r = req.post(
                        f"{url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._extract_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self._memory_model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.1,
                        },
                        timeout=60,
                    )
                    if r.status_code == 200:
                        return r.json()["choices"][0]["message"]["content"].strip()
                elif self._extract_type == "gemini":
                    api_url = (
                        f"https://generativelanguage.googleapis.com/v1beta/"
                        f"models/{self._memory_model}:generateContent"
                        f"?key={self._extract_key}"
                    )
                    r = req.post(
                        api_url,
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.1},
                        },
                        timeout=60,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                return parts[0].get("text", "").strip()

                # Rate-Limit: Retry mit Backoff
                if r is not None and r.status_code == 429:
                    wait = min(15 * (2 ** attempt), 60)
                    logger.warning(
                        f"[{self.instance}] Extract-LLM Rate-Limit (429), "
                        f"Retry {attempt + 1}/{self._RATE_LIMIT_MAX_RETRIES} in {wait}s"
                    )
                    time.sleep(wait)
                    continue

                # Anderer Fehler: loggen und abbrechen
                if r is not None and r.status_code != 200:
                    logger.warning(
                        f"[{self.instance}] Extract-LLM HTTP {r.status_code}: "
                        f"{r.text[:200]}"
                    )
                    return None

            except Exception as e:
                logger.debug(f"[{self.instance}] _call_extract_llm Fehler: {e}")
                return None

        logger.error(
            f"[{self.instance}] Extract-LLM Rate-Limit nach "
            f"{self._RATE_LIMIT_MAX_RETRIES} Retries nicht aufgelöst"
        )
        return None

    def _enrich_with_context(self, entry: _WindowEntry) -> tuple[str, str]:
        """
        Reichert eine Nachricht mit Kontext an: Löst Pronomen auf und
        macht die Nachricht selbsterklärend.

        Nur aktiv wenn self._context_enrichment = True.
        Gibt (enriched_user, enriched_assistant) zurück.
        Fallback: Original-Texte wenn kein Kontext oder LLM-Fehler.
        """
        if not self._context_enrichment:
            return entry.user, entry.assistant

        context = self._build_extraction_context(
            entry,
            context_before=self._context_before,
            context_after=self._context_after,
        )
        if not context:
            return entry.user, entry.assistant

        answer = self._call_extract_llm(f"{self._ENRICH_PROMPT}\n{context}")
        if not answer:
            return entry.user, entry.assistant

        enriched_user = entry.user
        enriched_assistant = entry.assistant

        for line in answer.split("\n"):
            line = line.strip()
            if line.lower().startswith("user:"):
                enriched_user = line[5:].strip()
            elif line.lower().startswith("assistant:"):
                enriched_assistant = line[10:].strip()

        if not enriched_user:
            enriched_user = entry.user
        if not enriched_assistant:
            enriched_assistant = entry.assistant

        logger.debug(
            f"[{self.instance}] Kontext-Anreicherung: "
            f"'{entry.user[:40]}' → '{enriched_user[:40]}'"
        )
        return enriched_user, enriched_assistant

    async def _extract_entry(self, entry: _WindowEntry):
        """
        Extrahiert einen Window-Eintrag async zu Qdrant.
        Läuft im Thread-Executor → blockiert den Event-Loop nicht.
        Bei Fehler: Eintrag bleibt im Window (kein Datenverlust).
        Bei fehlendem Scope: Klassifikation nochmal versuchen, bei Dauerfehler Admins warnen.
        """
        # Bereits via add_immediate() extrahiert → nur aus Window entfernen
        if entry.already_extracted:
            self._window.mark_extracted(entry)
            logger.debug(
                f"[{self.instance}] Überspringe Extraktion (already_extracted) | "
                f"window={self._window.size()}"
            )
            return

        # Scope noch nicht klassifiziert → nochmal versuchen
        if entry.scope is None:
            entry.scope = self._resolve_scope(entry.assistant, None)
            if entry.scope is None:
                entry.classify_retries += 1
                self._window.mark_failed(entry)
                if entry.classify_retries >= self._CLASSIFY_MAX_RETRIES:
                    logger.error(
                        f"[{self.instance}] Scope-Klassifikation nach "
                        f"{entry.classify_retries} Versuchen fehlgeschlagen. "
                        f"Eintrag bleibt im Window. Bitte Ollama-Verbindung prüfen. "
                        f"Text: {entry.user[:80]}..."
                    )
                else:
                    logger.warning(
                        f"[{self.instance}] Scope-Klassifikation Versuch "
                        f"{entry.classify_retries}/{self._CLASSIFY_MAX_RETRIES} "
                        f"fehlgeschlagen, Retry beim nächsten Overflow"
                    )
                return

        # Optionale Kontext-Anreicherung
        loop = asyncio.get_running_loop()
        enriched_user, enriched_assistant = await loop.run_in_executor(
            None, self._enrich_with_context, entry
        )

        messages = [
            {"role": "user",      "content": enriched_user},
            {"role": "assistant", "content": enriched_assistant},
        ]
        loop = asyncio.get_running_loop()
        try:
            success = await loop.run_in_executor(None, self.add, messages, entry.scope)
            if success:
                self._window.mark_extracted(entry)
                logger.debug(
                    f"[{self.instance}] Async-Extraktion OK | "
                    f"scope={entry.scope} | window={self._window.size()}"
                )
            else:
                self._window.mark_failed(entry)
                logger.warning(
                    f"[{self.instance}] Async-Extraktion fehlgeschlagen | "
                    f"scope={entry.scope} | Eintrag bleibt im Window"
                )
        except Exception as e:
            self._window.mark_failed(entry)
            logger.error(f"[{self.instance}] Async-Extraktion Fehler: {e}", exc_info=True)

    def _schedule_extraction(self, entry: _WindowEntry):
        """Erstellt und trackt einen Extraktions-Task."""
        task = asyncio.create_task(self._extract_entry(entry))
        self._track_task(task)

    async def add_immediate(self, user_message: str, assistant_response: str, scope: Optional[str] = None):
        """Sofortige Memory-Extraktion für explizite 'merke dir' Anfragen.
        Bypassed das Sliding Window — schreibt direkt via Mem0."""
        resolved_scope = self._resolve_scope(assistant_response, scope)
        if resolved_scope is None:
            # Fallback: ersten Write-Scope nehmen
            if self.write_scopes:
                resolved_scope = next(iter(self.write_scopes))
            else:
                logger.warning(f"[{self.instance}] Kein Write-Scope für immediate memory")
                return False

        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_response},
        ]

        loop = asyncio.get_running_loop()
        try:
            success = await loop.run_in_executor(None, self.add, messages, resolved_scope)
        except Exception as e:
            logger.error(f"[{self.instance}] add_immediate Fehler: {e}", exc_info=True)
            return False
        if success:
            logger.info(f"[{self.instance}] Explicit memory write → {resolved_scope}")
        return success

    async def add_conversation_async(
        self,
        user_message: str,
        assistant_response: str,
        scope: Optional[str] = None,
        already_extracted: bool = False,
    ):
        """
        Fügt Konversation zum Sliding Window hinzu und extrahiert Overflow async.

        Non-blocking: kehrt sofort zurück. Mem0-Write (LLM-Inferenz + Embedding)
        läuft als asyncio.Task im Hintergrund, ohne den Event-Loop zu blockieren.
        ha-assist (keine write_scopes) → no-op.

        already_extracted: Wenn True, wird der Eintrag im Window als bereits
        extrahiert markiert (z.B. nach add_immediate()). Bei Overflow wird er
        nur aus dem Window entfernt, ohne erneute Mem0-Extraktion.
        """
        if not self.write_scopes:
            return

        scope = self._resolve_scope(assistant_response, scope)
        overflow = self._window.add(user_message, assistant_response, scope)

        # Flag auf den soeben hinzugefügten Eintrag setzen
        if already_extracted and self._window._entries:
            self._window._entries[-1].already_extracted = True

        for entry in overflow:
            self._schedule_extraction(entry)

        logger.debug(
            f"[{self.instance}] Window +1 | "
            f"scope={scope} | size={self._window.size()} | "
            f"overflow={len(overflow)} | pending_tasks={len(self._pending_tasks)}"
        )

    # ── Task-Verwaltung ────────────────────────────────────────────────────────

    def pending_count(self) -> int:
        """Anzahl laufender Extraktions-Tasks."""
        return len(self._pending_tasks)

    async def flush_all(self, timeout: float = 60.0) -> int:
        """
        Extrahiert ALLE Window-Einträge zu Qdrant (für sauberes Shutdown).

        Nicht nur Overflow-Kandidaten, sondern wirklich alles was noch im
        Window sitzt → danach ist das Window leer und Qdrant vollständig.
        Gibt Anzahl der Tasks zurück die nach timeout abgebrochen wurden.
        """
        if not self.write_scopes:
            return 0

        # Alle Einträge die noch nicht extrahiert werden, jetzt einplanen
        to_extract = [e for e in self._window._entries if not e.extracting]
        for entry in to_extract:
            entry.extracting = True
            self._schedule_extraction(entry)

        if to_extract:
            logger.info(
                f"[{self.instance}] flush_all: {len(to_extract)} Einträge → Qdrant"
            )

        return await self.flush_pending(timeout=timeout)

    async def flush_pending(self, timeout: float = 30.0) -> int:
        """
        Wartet auf alle laufenden Extraktions-Tasks.
        Gibt Anzahl der Tasks zurück die nach timeout abgebrochen wurden.
        """
        if not self._pending_tasks:
            return 0

        tasks = set(self._pending_tasks)
        logger.info(f"[{self.instance}] Warte auf {len(tasks)} Extraktions-Tasks...")

        done, still_pending = await asyncio.wait(tasks, timeout=timeout)

        for task in still_pending:
            task.cancel()

        if still_pending:
            logger.warning(
                f"[{self.instance}] {len(still_pending)} Tasks nach {timeout}s abgebrochen"
            )

        return len(still_pending)

    # ── Persistenz ─────────────────────────────────────────────────────────────

    def save_context(self, path: Path):
        """
        Schreibt den aktuellen Window-Zustand als JSON.
        Wird nach jeder Nachricht aufgerufen → bei Absturz geht max. 1 Nachricht verloren.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self._window.to_dict()
            # Atomares Schreiben via temp-Datei → kein korruptes JSON bei Absturz
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            logger.debug(
                f"[{self.instance}] Context gespeichert | "
                f"{self._window.size()} Einträge → {path}"
            )
        except Exception as e:
            logger.error(f"[{self.instance}] Context-Speichern fehlgeschlagen: {e}")

    async def load_context(self, path: Path) -> int:
        """
        Lädt Window-Zustand aus JSON und plant pending Einträge zur Extraktion ein.
        Gibt Anzahl der sofort gestarteten Extraktions-Tasks zurück.
        Kein Fehler wenn Datei nicht existiert (erster Start).
        """
        if not path.exists():
            logger.info(f"[{self.instance}] Kein gespeicherter Context gefunden ({path})")
            return 0

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[{self.instance}] Context-Laden fehlgeschlagen: {e}")
            return 0

        pending_entries = self._window.from_dict(data)

        for entry in pending_entries:
            self._schedule_extraction(entry)

        saved_at = data.get("saved_at", 0)
        age_min = (time.time() - saved_at) / 60

        logger.info(
            f"[{self.instance}] Context geladen | "
            f"{self._window.size()} Einträge | "
            f"gespeichert vor {age_min:.1f}min | "
            f"{len(pending_entries)} pending → sofortige Extraktion"
        )
        return len(pending_entries)
