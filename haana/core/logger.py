"""
HAANA Logger – Strukturiertes JSONL-Logging

Kategorien:
  conversations/{instance}/YYYY-MM-DD.jsonl  – vollständige Konversationen
  llm-calls/YYYY-MM-DD.jsonl                 – jeder LLM-Call mit Metriken
  memory-ops/YYYY-MM-DD.jsonl                – Memory-Reads und -Writes
  tool-calls/YYYY-MM-DD.jsonl                – Tool-Aufrufe mit Parametern
  dream/{instance}/YYYY-MM-DD.jsonl          – Dream-Tagebuch

Format: JSONL (eine JSON-Zeile pro Event), täglich rotiert, nie gelöscht.
Qdrant kann jederzeit aus den Logs rekonstruiert werden.

Speicherpfade:
  /data/                    – Konfiguration, Kontext (immer gebackupt)
  /media/haana/             – Logs, Qdrant-Daten (groß, wachsend)
  HAANA_MEDIA_DIR           – Standard: /media/haana (HA Addon), Fallback: /data
  HAANA_LOG_DIR             – Standard: {MEDIA_DIR}/logs
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)


def get_media_dir() -> Path:
    """Gibt das Media-Verzeichnis zurück.

    Priorität:
      1. HAANA_MEDIA_DIR Env-Var
      2. /media/haana (falls vorhanden, HA Addon)
      3. /data (Fallback für Dev/Docker)
    """
    env = os.environ.get("HAANA_MEDIA_DIR", "").strip()
    if env:
        return Path(env)
    default = Path("/media/haana")
    if default.exists():
        return default
    return Path("/data")


def _log_root() -> Path:
    env = os.environ.get("HAANA_LOG_DIR", "").strip()
    if env:
        return Path(env)
    return get_media_dir() / "logs"


def _write(category: str, sub: Optional[str], record: dict) -> None:
    """Schreibt einen Record als JSONL-Zeile (append, thread-safe genug für JSONL)."""
    today = datetime.now().strftime("%Y-%m-%d")
    root = _log_root()
    path = (root / category / sub / f"{today}.jsonl") if sub else (root / category / f"{today}.jsonl")
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _logger.error(f"[HaanaLogger] Schreiben nach {path} fehlgeschlagen: {e}")


# ── Öffentliche API ───────────────────────────────────────────────────────────

def log_conversation(
    instance: str,
    channel: str,          # "repl" | "webchat" | "whatsapp" | "ha_app"
    user_message: str,
    assistant_response: str,
    latency_s: float,
    memory_used: bool = False,
    memory_hits: int = 0,
    tool_calls: Optional[list[dict]] = None,
    model: Optional[str] = None,
    memory_results: Optional[list[str]] = None,
    memory_extracted: bool = False,
) -> None:
    """Loggt eine vollständige Konversationsrunde."""
    record = {
        "instance": instance,
        "channel": channel,
        "user": user_message,
        "assistant": assistant_response,
        "latency_s": round(latency_s, 3),
        "memory_used": memory_used,
        "memory_hits": memory_hits,
        "tool_calls": tool_calls or [],
    }
    if model:
        record["model"] = model
    if memory_results:
        record["memory_results"] = memory_results
    if memory_extracted:
        record["memory_extracted"] = True
    _write("conversations", instance, record)


def log_memory_op(
    instance: str,
    op: str,               # "read" | "write"
    scope: str,
    query: Optional[str] = None,
    results_count: Optional[int] = None,
    content_preview: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None,
) -> None:
    """Loggt eine Memory-Read- oder -Write-Operation."""
    _write("memory-ops", None, {
        "instance": instance,
        "op": op,
        "scope": scope,
        "query": query,
        "results_count": results_count,
        "content_preview": (content_preview or "")[:300] or None,
        "success": success,
        "error": error,
    })


def log_tool_call(
    instance: str,
    tool_name: str,
    tool_input: Any,
    latency_s: Optional[float] = None,
    success: bool = True,
    error: Optional[str] = None,
) -> None:
    """Loggt einen Tool-Aufruf."""
    _write("tool-calls", None, {
        "instance": instance,
        "tool": tool_name,
        "input": str(tool_input)[:500] if tool_input is not None else None,
        "latency_s": round(latency_s, 3) if latency_s is not None else None,
        "success": success,
        "error": error,
    })


def log_dream_summary(
    instance: str,
    date: str,
    summary: str,
    consolidated: int,
    contradictions: int,
    duration_s: float,
) -> None:
    """Speichert eine Dream-Tages-Zusammenfassung als JSONL-Eintrag.

    Speicherpfad: {LOG_ROOT}/dream/{instance}/YYYY-MM-DD.jsonl
    """
    root = _log_root()
    path = root / "dream" / instance / f"{date}.jsonl"
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance": instance,
        "date": date,
        "summary": summary,
        "consolidated": consolidated,
        "contradictions": contradictions,
        "duration_s": round(duration_s, 2),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _logger.error(f"[HaanaLogger] Dream-Log schreiben nach {path} fehlgeschlagen: {e}")


def list_instances() -> list[str]:
    """Gibt alle Instanzen zurück für die Konversations-Logs existieren."""
    conv_dir = _log_root() / "conversations"
    if not conv_dir.exists():
        return []
    return sorted(p.name for p in conv_dir.iterdir() if p.is_dir())


# ── Extraction Index ──────────────────────────────────────────────────────────


def _extraction_index_dir() -> Path:
    return _log_root() / ".extraction-index"


def _load_extraction_index(instance: str) -> dict:
    """Lädt den Extraction-Index für eine Instanz."""
    path = _extraction_index_dir() / f"{instance}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_extraction_index(instance: str, index: dict) -> None:
    """Speichert den Extraction-Index für eine Instanz."""
    d = _extraction_index_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{instance}.json"
    try:
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _logger.error(f"[HaanaLogger] Extraction-Index schreiben fehlgeschlagen: {e}")


def update_extraction_index(instance: str, date: str, log_file_path: str) -> None:
    """Aktualisiert den Extraction-Index nach erfolgreicher Memory-Extraktion.

    Berechnet MD5 der Log-Datei und speichert Hash + Zeitstempel.
    """
    try:
        file_hash = hashlib.md5(
            Path(log_file_path).read_bytes()
        ).hexdigest()
    except Exception as e:
        _logger.error(f"[HaanaLogger] MD5-Berechnung fehlgeschlagen für {log_file_path}: {e}")
        return

    index = _load_extraction_index(instance)
    index[date] = {
        "hash": file_hash,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_extraction_index(instance, index)


def get_changed_log_files(instance: str) -> list[dict]:
    """Vergleicht aktuelle Log-Dateien mit dem Extraction-Index.

    Returns Liste von dicts: {"date": "YYYY-MM-DD", "status": "new"|"changed", "path": "..."}
    """
    index = _load_extraction_index(instance)
    conv_dir = _log_root() / "conversations" / instance
    if not conv_dir.exists():
        return []

    changed = []
    for fpath in sorted(conv_dir.glob("*.jsonl")):
        date = fpath.stem
        try:
            current_hash = hashlib.md5(fpath.read_bytes()).hexdigest()
        except Exception:
            continue

        entry = index.get(date)
        if entry is None:
            changed.append({"date": date, "status": "new", "path": str(fpath)})
        elif entry.get("hash") != current_hash:
            changed.append({"date": date, "status": "changed", "path": str(fpath)})

    return changed
