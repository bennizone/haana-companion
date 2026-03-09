"""
HAANA Git-Integration

Funktionen für Git-Operationen auf dem HAANA-Repository.
Token wird NIEMALS in stdout/logs ausgegeben.
"""

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(os.environ.get("HAANA_REPO_ROOT", "/data"))


def _run(args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    """Führt einen Git-Befehl aus und gibt das Ergebnis zurück."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=timeout,
    )


def _mask_output(text: str) -> str:
    """Maskiert Token-URLs in git-Ausgaben (z.B. https://ghp_xxx@github.com)."""
    return re.sub(r'https?://[^@\s/]+@', 'https://***@', text)


def _is_git_repo() -> bool:
    """Prüft ob REPO_ROOT existiert und ein Git-Repository ist."""
    if not REPO_ROOT.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-dir"],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode == 0


async def git_status() -> dict:
    """Gibt Branch, dirty-Status, ahead/behind und Remote-URL zurück."""
    if not _is_git_repo():
        return {"branch": None, "dirty": False, "ahead": 0, "behind": 0, "remote": None, "configured": False}

    loop = asyncio.get_running_loop()

    def _collect():
        branch_result = _run(["git", "-C", str(REPO_ROOT), "branch", "--show-current"])
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        porcelain_result = _run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"])
        dirty = bool(porcelain_result.stdout.strip()) if porcelain_result.returncode == 0 else False

        ahead = 0
        behind = 0
        try:
            ahead_result = _run(["git", "-C", str(REPO_ROOT), "rev-list", "--count", "@{u}..HEAD"])
            if ahead_result.returncode == 0:
                ahead = int(ahead_result.stdout.strip())
        except (ValueError, Exception):
            pass
        try:
            behind_result = _run(["git", "-C", str(REPO_ROOT), "rev-list", "--count", "HEAD..@{u}"])
            if behind_result.returncode == 0:
                behind = int(behind_result.stdout.strip())
        except (ValueError, Exception):
            pass

        remote = ""
        try:
            remote_result = _run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"])
            if remote_result.returncode == 0:
                # Token aus URL entfernen bevor es zurückgegeben wird
                raw_url = remote_result.stdout.strip()
                remote = _mask_token_in_url(raw_url)
        except Exception:
            pass

        return {
            "branch": branch,
            "dirty": dirty,
            "ahead": ahead,
            "behind": behind,
            "remote": remote,
        }

    return await loop.run_in_executor(None, _collect)


async def git_pull() -> dict:
    """Führt git pull aus und gibt den Output zurück."""
    if not _is_git_repo():
        return {"ok": False, "output": "Kein Git-Repository konfiguriert"}

    loop = asyncio.get_running_loop()

    def _do_pull():
        result = _run(["git", "-C", str(REPO_ROOT), "pull"], timeout=60)
        ok = result.returncode == 0
        output = result.stdout + result.stderr
        return {"ok": ok, "output": _mask_output(output.strip())}

    return await loop.run_in_executor(None, _do_pull)


async def git_push() -> dict:
    """Führt git push aus und gibt den Output zurück."""
    if not _is_git_repo():
        return {"ok": False, "output": "Kein Git-Repository konfiguriert"}

    loop = asyncio.get_running_loop()

    def _do_push():
        result = _run(["git", "-C", str(REPO_ROOT), "push"], timeout=60)
        ok = result.returncode == 0
        output = result.stdout + result.stderr
        return {"ok": ok, "output": _mask_output(output.strip())}

    return await loop.run_in_executor(None, _do_push)


async def git_connect(url: str, token: str, load_config_fn, save_config_fn) -> dict:
    """
    Setzt remote origin mit eingebettetem Token und speichert in config.json.
    Token wird NICHT in Logs ausgegeben.
    """
    loop = asyncio.get_running_loop()

    def _do_connect():
        # Token in URL einbetten
        url_with_token = _embed_token_in_url(url, token)

        result = _run(
            ["git", "-C", str(REPO_ROOT), "remote", "set-url", "origin", url_with_token],
            timeout=15,
        )

        if result.returncode != 0:
            # Fehlermeldung maskieren (Token darf nicht erscheinen)
            err_msg = _mask_token(result.stderr.strip(), token)
            logger.error("git remote set-url fehlgeschlagen: %s", err_msg)
            return {"ok": False, "output": err_msg}

        # Token und URL in config speichern
        try:
            cfg = load_config_fn()
            cfg["git_token"] = token
            cfg["git_remote_url"] = url  # Ohne Token speichern
            save_config_fn(cfg)
        except Exception as exc:
            logger.error("Fehler beim Speichern der Git-Config: %s", exc)
            return {"ok": False, "output": f"Config-Fehler: {exc}"}

        logger.info("Git remote gesetzt auf: %s", _mask_token_in_url(url_with_token))
        return {"ok": True, "output": "Remote erfolgreich gesetzt."}

    return await loop.run_in_executor(None, _do_connect)


async def git_log() -> list:
    """Gibt die letzten 10 Commits zurück."""
    if not _is_git_repo():
        return []

    loop = asyncio.get_running_loop()

    def _get_log():
        result = _run(
            [
                "git", "-C", str(REPO_ROOT),
                "log", "--oneline", "-10",
                "--format=%H|%s|%ad|%an",
                "--date=short",
            ]
        )
        if result.returncode != 0:
            logger.error("git log fehlgeschlagen: %s", result.stderr.strip())
            return []

        entries = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                entries.append({
                    "hash": parts[0],
                    "msg": parts[1],
                    "date": parts[2],
                    "author": parts[3],
                })
        return entries

    return await loop.run_in_executor(None, _get_log)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _embed_token_in_url(url: str, token: str) -> str:
    """Bettet einen Token in eine HTTPS-URL ein: https://token@host/path."""
    if not token:
        return url
    # Vorhandene Credentials entfernen
    url = re.sub(r"https?://[^@]*@", lambda m: m.group(0).split("://")[0] + "://", url)
    if url.startswith("https://"):
        return f"https://{token}@{url[len('https://'):]}"
    if url.startswith("http://"):
        return f"http://{token}@{url[len('http://'):]}"
    return url


def _mask_token(text: str, token: str) -> str:
    """Ersetzt Token im Text durch '***'."""
    if token and token in text:
        return text.replace(token, "***")
    return text


def _mask_token_in_url(url: str) -> str:
    """Entfernt eingebetteten Token aus einer URL."""
    return re.sub(r"(https?://)([^@]+)@", r"\1", url)
