"""
HAANA Dream Process – Nächtliche Memory-Konsolidierung

Läuft als periodischer Task (typisch nachts) und:
  1. Konsolidiert ähnliche/redundante Memory-Einträge (Cosine Similarity > Threshold)
  2. Erstellt Tages-Zusammenfassungen der Konversationen
  3. Entfernt widersprüchliche Einträge

Nutzt Qdrant REST API direkt für Reads, Mem0 für Writes (Konsistenz mit HaanaMemory).
LLM-Calls über _call_extract_llm-Pattern (gleicher Provider wie Memory-Extraktion).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class DreamReport:
    """Ergebnis eines Dream-Durchlaufs."""
    instance: str
    consolidated: int = 0
    summarized: bool = False
    cleaned: int = 0
    duration_s: float = 0.0
    summary: str = ""
    errors: list[str] = field(default_factory=list)


# ── LLM-Call Helper ──────────────────────────────────────────────────────────

def _call_llm(
    prompt: str,
    *,
    extract_type: str,
    extract_url: str,
    extract_key: str,
    ollama_url: str,
    model: str,
    timeout: int = 90,
) -> Optional[str]:
    """
    Ruft das konfigurierte Extraction-LLM auf (gleicher Pattern wie HaanaMemory).
    Gibt Antwort als String zurück oder None bei Fehler.
    """
    import requests as req

    try:
        r = None
        if extract_type == "ollama":
            url = extract_url or ollama_url
            if not url:
                return None
            r = req.post(
                f"{url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=timeout,
            )
            if r.status_code == 200:
                return r.json().get("response", "").strip()
        elif extract_type in ("anthropic", "minimax"):
            url = extract_url or "https://api.anthropic.com"
            if not extract_key:
                return None
            r = req.post(
                f"{url}/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": extract_key,
                },
                json={
                    "model": model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )
            if r.status_code == 200:
                for block in r.json().get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "").strip()
        elif extract_type == "openai":
            url = extract_url or "https://api.openai.com/v1"
            if not extract_key:
                return None
            r = req.post(
                f"{url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {extract_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        elif extract_type == "gemini":
            if not extract_key:
                return None
            api_url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model}:generateContent?key={extract_key}"
            )
            r = req.post(
                api_url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1},
                },
                timeout=timeout,
            )
            if r.status_code == 200:
                candidates = r.json().get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()

        if r is not None and r.status_code != 200:
            logger.warning(f"Dream LLM HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Dream LLM Fehler: {e}")

    return None


# ── Qdrant Helpers ───────────────────────────────────────────────────────────

def _qdrant_scroll(
    qdrant_url: str,
    collection: str,
    limit: int = 100,
    offset: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Liest Punkte aus einer Qdrant-Collection via Scroll-API.
    Gibt (points, next_offset) zurück.
    """
    body: dict = {"limit": limit, "with_payload": True, "with_vector": True}
    if offset is not None:
        body["offset"] = offset

    try:
        r = httpx.post(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll",
            json=body,
            timeout=30.0,
        )
        if r.status_code != 200:
            return [], None
        data = r.json().get("result", {})
        points = data.get("points", [])
        next_offset = data.get("next_page_offset")
        return points, next_offset
    except Exception as e:
        logger.warning(f"Qdrant scroll Fehler ({collection}): {e}")
        return [], None


def _qdrant_get_all_points(qdrant_url: str, collection: str) -> list[dict]:
    """Liest alle Punkte einer Collection via paginiertem Scroll."""
    all_points = []
    offset = None
    while True:
        points, next_offset = _qdrant_scroll(qdrant_url, collection, limit=100, offset=offset)
        all_points.extend(points)
        if not next_offset or not points:
            break
        offset = next_offset
    return all_points


def _qdrant_delete_points(qdrant_url: str, collection: str, point_ids: list) -> bool:
    """Löscht Punkte aus einer Qdrant-Collection."""
    if not point_ids:
        return True
    try:
        r = httpx.post(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/delete",
            json={"points": point_ids},
            timeout=30.0,
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Qdrant delete Fehler ({collection}): {e}")
        return False


def _qdrant_update_payload(
    qdrant_url: str, collection: str, point_id, payload: dict
) -> bool:
    """Aktualisiert das Payload eines Punktes."""
    try:
        r = httpx.post(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/payload",
            json={"payload": payload, "points": [point_id]},
            timeout=30.0,
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Qdrant update Fehler ({collection}): {e}")
        return False


# ── Cosine Similarity ────────────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Berechnet Cosine Similarity zwischen zwei Vektoren."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def _find_similar_pairs(
    points: list[dict], threshold: float = 0.9
) -> list[tuple[dict, dict, float]]:
    """
    Findet Paare von Punkten mit Cosine Similarity über dem Threshold.
    Gibt sortierte Liste (höchste Similarity zuerst) zurück.
    """
    pairs = []
    n = len(points)
    if n < 2:
        return pairs

    # Vektoren extrahieren für Batch-Berechnung
    vectors = []
    valid_points = []
    for p in points:
        vec = p.get("vector")
        if vec and isinstance(vec, list) and len(vec) > 0:
            vectors.append(vec)
            valid_points.append(p)

    if len(valid_points) < 2:
        return pairs

    # Matrix-Berechnung für Effizienz
    mat = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    normed = mat / norms
    sim_matrix = normed @ normed.T

    for i in range(len(valid_points)):
        for j in range(i + 1, len(valid_points)):
            sim = float(sim_matrix[i, j])
            if sim >= threshold:
                pairs.append((valid_points[i], valid_points[j], sim))

    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


# ── Merge Prompt ─────────────────────────────────────────────────────────────

_MERGE_PROMPT = """\
Du bist ein Memory-Konsolidierer. Fasse die folgenden ähnlichen Memory-Einträge \
zu EINEM prägnanten Eintrag zusammen. Behalte alle wichtigen Fakten bei, \
entferne Redundanzen.

Einträge:
{entries}

Antworte NUR mit dem zusammengefassten Text (ein Satz oder kurzer Absatz). \
Keine Erklärung, kein JSON, nur der zusammengefasste Fakt.\
"""

_CONTRADICTION_PROMPT = """\
Analysiere die folgenden Memory-Einträge und finde Widersprüche. \
Zwei Einträge widersprechen sich, wenn sie gegensätzliche Aussagen über \
dasselbe Thema machen (z.B. "trinkt gerne Kaffee" vs "trinkt keinen Kaffee").

Einträge (Format: ID | Text):
{entries}

Antworte als JSON-Liste der IDs die entfernt werden sollen (die veralteten/falschen). \
Behalte jeweils den neueren/wahrscheinlich korrekteren Eintrag.
Bei Korrekturen (z.B. "das stimmt nicht, ich trinke keinen Kaffee") ist die \
Korrektur korrekt und der alte Eintrag falsch.

Wenn es keine Widersprüche gibt, antworte mit: []
Antworte NUR mit der JSON-Liste, nichts anderes. Beispiel: ["id1", "id2"]\
"""

_SUMMARY_PROMPT = """\
Erstelle eine kurze Zusammenfassung der folgenden Konversationen vom {date}. \
Fasse die wichtigsten Themen, Anfragen und Ergebnisse zusammen. \
Maximal 3-5 Sätze.

Konversationen:
{conversations}

Antworte NUR mit der Zusammenfassung, keine Überschriften oder Formatierung.\
"""


# ── DreamProcess ─────────────────────────────────────────────────────────────

class DreamProcess:
    """
    Nächtlicher Konsolidierungs-Prozess für HAANA Memory.

    Konfiguration via memory_config dict:
      - qdrant_url: Qdrant HTTP URL
      - ollama_url: Ollama API URL (für Embedding/LLM)
      - extract_type: LLM-Provider-Typ (ollama, anthropic, openai, gemini, minimax)
      - extract_url: LLM API URL
      - extract_key: API-Key
      - model: LLM-Modell-Name
      - similarity_threshold: Schwellwert für Konsolidierung (default 0.9)
    """

    def __init__(self, memory_config: dict, log_root: str):
        self._qdrant_url = memory_config.get("qdrant_url", "http://qdrant:6333")
        self._ollama_url = memory_config.get("ollama_url", "")
        self._extract_type = memory_config.get("extract_type", "ollama")
        self._extract_url = memory_config.get("extract_url", "")
        self._extract_key = memory_config.get("extract_key", "")
        self._model = memory_config.get("model", "ministral-3-32k:3b")
        self._similarity_threshold = memory_config.get("similarity_threshold", 0.9)
        self._log_root = Path(log_root)

    def _llm_call(self, prompt: str) -> Optional[str]:
        """Ruft das konfigurierte LLM auf."""
        return _call_llm(
            prompt,
            extract_type=self._extract_type,
            extract_url=self._extract_url,
            extract_key=self._extract_key,
            ollama_url=self._ollama_url,
            model=self._model,
        )

    async def run(self, instance: str, scope: str) -> DreamReport:
        """Führt den kompletten Dream-Prozess für eine Instanz/Scope durch."""
        report = DreamReport(instance=instance)
        start = time.monotonic()

        logger.info(f"[Dream] Start für {instance}/{scope}")

        # 1. Konsolidierung
        try:
            report.consolidated = await self._consolidate_memories(instance, scope)
        except Exception as e:
            msg = f"Konsolidierung fehlgeschlagen: {e}"
            logger.error(f"[Dream] {msg}", exc_info=True)
            report.errors.append(msg)

        # 2. Tages-Zusammenfassung
        try:
            summary = await self._create_daily_summary(instance)
            if summary:
                report.summarized = True
                report.summary = summary
        except Exception as e:
            msg = f"Zusammenfassung fehlgeschlagen: {e}"
            logger.error(f"[Dream] {msg}", exc_info=True)
            report.errors.append(msg)

        # 3. Widersprüche bereinigen
        try:
            report.cleaned = await self._cleanup_contradictions(instance, scope)
        except Exception as e:
            msg = f"Widerspruchsbereinigung fehlgeschlagen: {e}"
            logger.error(f"[Dream] {msg}", exc_info=True)
            report.errors.append(msg)

        report.duration_s = round(time.monotonic() - start, 2)
        logger.info(
            f"[Dream] Fertig für {instance}/{scope} | "
            f"konsolidiert={report.consolidated} | "
            f"zusammengefasst={report.summarized} | "
            f"bereinigt={report.cleaned} | "
            f"dauer={report.duration_s}s | "
            f"fehler={len(report.errors)}"
        )
        return report

    async def _consolidate_memories(self, instance: str, scope: str) -> int:
        """
        Findet ähnliche Memory-Einträge und merged sie.
        Gibt Anzahl der konsolidierten (gelöschten) Einträge zurück.
        """
        loop = asyncio.get_running_loop()

        # Alle Punkte laden
        points = await loop.run_in_executor(
            None, _qdrant_get_all_points, self._qdrant_url, scope
        )
        if len(points) < 2:
            logger.debug(f"[Dream] {scope}: Nur {len(points)} Einträge, nichts zu konsolidieren")
            return 0

        logger.info(f"[Dream] {scope}: {len(points)} Einträge geladen, suche ähnliche Paare...")

        # Ähnliche Paare finden
        pairs = await loop.run_in_executor(
            None, _find_similar_pairs, points, self._similarity_threshold
        )
        if not pairs:
            logger.debug(f"[Dream] {scope}: Keine ähnlichen Paare gefunden")
            return 0

        logger.info(f"[Dream] {scope}: {len(pairs)} ähnliche Paare gefunden")

        # Merge-Gruppen bilden (Union-Find-artig: verbundene Komponenten)
        merged_ids: set = set()
        consolidated = 0

        for p1, p2, sim in pairs:
            id1 = p1["id"]
            id2 = p2["id"]

            # Bereits verarbeitet?
            if id1 in merged_ids or id2 in merged_ids:
                continue

            text1 = _get_memory_text(p1)
            text2 = _get_memory_text(p2)

            if not text1 or not text2:
                continue

            # LLM-Merge
            prompt = _MERGE_PROMPT.format(entries=f"1. {text1}\n2. {text2}")
            merged_text = await loop.run_in_executor(None, self._llm_call, prompt)

            if not merged_text:
                logger.debug(f"[Dream] Merge fehlgeschlagen für {id1}/{id2}")
                continue

            # Alten Eintrag mit gemergetem Text aktualisieren, zweiten löschen
            success = _qdrant_update_payload(
                self._qdrant_url, scope, id1,
                {"memory": merged_text, "data": merged_text,
                 "dream_merged": True, "dream_ts": datetime.now().isoformat()},
            )
            if success:
                _qdrant_delete_points(self._qdrant_url, scope, [id2])
                merged_ids.add(id1)
                merged_ids.add(id2)
                consolidated += 1
                logger.debug(
                    f"[Dream] Merged: '{text1[:40]}' + '{text2[:40]}' → '{merged_text[:60]}'"
                )

        return consolidated

    async def _create_daily_summary(
        self, instance: str, date: Optional[str] = None
    ) -> str:
        """
        Erstellt eine Zusammenfassung der Tages-Konversationen.
        Liest JSONL-Logs und fasst sie via LLM zusammen.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        log_path = self._log_root / "conversations" / instance / f"{date}.jsonl"
        if not log_path.exists():
            logger.debug(f"[Dream] Kein Log für {instance}/{date}")
            return ""

        # Konversationen laden
        conversations = []
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    user = entry.get("user", "")
                    assistant = entry.get("assistant", "")
                    if user or assistant:
                        conversations.append(f"User: {user}\nAssistant: {assistant}")
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning(f"[Dream] Log lesen fehlgeschlagen ({log_path}): {e}")
            return ""

        if not conversations:
            return ""

        # Konversationen auf max ~4000 Zeichen kürzen (LLM-Kontextlimit)
        conv_text = ""
        for conv in conversations:
            if len(conv_text) + len(conv) > 4000:
                conv_text += "\n[... weitere Konversationen gekürzt ...]"
                break
            conv_text += f"\n---\n{conv}"

        prompt = _SUMMARY_PROMPT.format(date=date, conversations=conv_text.strip())

        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(None, self._llm_call, prompt)

        if summary:
            logger.info(f"[Dream] Zusammenfassung für {instance}/{date}: {summary[:100]}...")

        return summary or ""

    async def _cleanup_contradictions(self, instance: str, scope: str) -> int:
        """
        Entfernt widersprüchliche Memory-Einträge.
        Gibt Anzahl der entfernten Einträge zurück.
        """
        loop = asyncio.get_running_loop()

        points = await loop.run_in_executor(
            None, _qdrant_get_all_points, self._qdrant_url, scope
        )
        if len(points) < 2:
            return 0

        # Alle Einträge als Text mit ID formatieren
        entries_text = ""
        id_map: dict[str, str] = {}  # str_id -> real_id
        for i, p in enumerate(points):
            text = _get_memory_text(p)
            if not text:
                continue
            str_id = str(p["id"])
            id_map[str_id] = p["id"]
            entries_text += f"{str_id} | {text}\n"

        if not entries_text.strip():
            return 0

        # Auf max ~4000 Zeichen begrenzen (Batch-weise bei großen Collections)
        if len(entries_text) > 4000:
            logger.warning(f"[Dream] {scope}: Contradiction-Check auf 4000 Zeichen gekürzt ({len(entries_text)} total)")
            entries_text = entries_text[:4000] + "\n[... gekürzt ...]"

        prompt = _CONTRADICTION_PROMPT.format(entries=entries_text.strip())
        answer = await loop.run_in_executor(None, self._llm_call, prompt)

        if not answer:
            return 0

        # JSON-Liste der zu löschenden IDs parsen
        try:
            # Bereinigung: Manchmal gibt das LLM Markdown-Codeblocks zurück
            cleaned = answer.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            to_delete = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.debug(f"[Dream] Contradiction-Antwort nicht parsbar: {answer[:200]}")
            return 0

        if not isinstance(to_delete, list) or not to_delete:
            return 0

        # IDs auflösen und löschen
        real_ids = []
        for str_id in to_delete:
            str_id = str(str_id)
            if str_id in id_map:
                real_ids.append(id_map[str_id])

        if not real_ids:
            return 0

        success = _qdrant_delete_points(self._qdrant_url, scope, real_ids)
        if success:
            logger.info(f"[Dream] {len(real_ids)} widersprüchliche Einträge aus {scope} entfernt")
            return len(real_ids)

        return 0


def _get_memory_text(point: dict) -> str:
    """Extrahiert den Memory-Text aus einem Qdrant-Punkt."""
    payload = point.get("payload", {})
    return payload.get("memory") or payload.get("data", "")
