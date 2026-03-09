"""
HAANA Agent Process Manager – Abstraktion für Agent-Lifecycle

Zwei Implementierungen:
  DockerAgentManager   – Standalone/Dev: Container via Docker SDK (wie bisher)
  InProcessAgentManager – Add-on: Agents als Python-Objekte im selben Prozess

Auto-Detection via HAANA_MODE env oder Docker-Socket-Verfügbarkeit.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _get_default_media_dir() -> Path:
    """Media-Verzeichnis: HAANA_MEDIA_DIR > /media/haana > /data."""
    env = os.environ.get("HAANA_MEDIA_DIR", "").strip()
    if env:
        return Path(env)
    default = Path("/media/haana")
    if default.exists():
        return default
    return Path("/data")


def _get_default_log_dir() -> Path:
    """Log-Verzeichnis: HAANA_LOG_DIR > {MEDIA_DIR}/logs."""
    env = os.environ.get("HAANA_LOG_DIR", "").strip()
    if env:
        return Path(env)
    return _get_default_media_dir() / "logs"


@runtime_checkable
class AgentManager(Protocol):
    """Protocol für Agent-Lifecycle-Management."""

    async def start_agent(self, user: dict, cfg: dict) -> dict:
        """Startet einen Agent für einen User. Gibt {"ok": bool, ...} zurück."""
        ...

    async def stop_agent(self, instance: str, force: bool = False) -> dict:
        """Stoppt einen Agent. force=True → sofortiges Beenden."""
        ...

    async def restart_agent(self, instance: str) -> dict:
        """Neustart eines Agents (stop + start mit aktueller Config)."""
        ...

    def agent_status(self, instance: str) -> str:
        """Status: 'running', 'exited', 'absent', 'unknown'."""
        ...

    def agent_url(self, instance: str) -> str:
        """HTTP-URL zum Agent-API Endpunkt."""
        ...

    def list_agents(self) -> dict[str, str]:
        """Alle bekannten Agents mit Status. {instance: status}."""
        ...

    def get_agent(self, instance: str):
        """Gibt das Agent-Objekt zurück (InProcess) oder None (Docker)."""
        ...

    async def remove_agent(self, instance: str) -> dict:
        """Agent vollständig entfernen (Container/Prozess + Cleanup)."""
        ...


def _build_fallback_env(f_llm: dict, f_prov: dict, ollama_url: str, cfg: dict) -> dict:
    """Baut HAANA_FALLBACK_* Env-Vars für den Fallback-LLM auf.

    Diese Vars werden vom Agent gelesen und bei Auth/Connection-Fehlern
    für einen automatischen Retry mit dem Fallback-LLM genutzt.
    """
    fb = {
        "HAANA_FALLBACK_MODEL": f_llm.get("model", ""),
    }

    ptype = f_prov.get("type", "")
    fb["HAANA_FALLBACK_PROVIDER_TYPE"] = ptype

    if ptype == "minimax":
        fb["HAANA_FALLBACK_BASE_URL"] = f_prov.get("url") or "https://api.minimax.io/anthropic"
        fb["HAANA_FALLBACK_AUTH_TOKEN"] = f_prov.get("key", "")
        fb["HAANA_FALLBACK_API_KEY"] = ""
    elif ptype == "ollama":
        ollama_base = (f_prov.get("url") or ollama_url or "http://localhost:11434").rstrip("/")
        fb["HAANA_FALLBACK_BASE_URL"] = ollama_base
        fb["HAANA_FALLBACK_AUTH_TOKEN"] = "ollama"
        fb["HAANA_FALLBACK_API_KEY"] = ""
    elif ptype == "openai":
        fb["HAANA_FALLBACK_API_KEY"] = f_prov.get("key", "")
        fb["HAANA_FALLBACK_BASE_URL"] = f_prov.get("url", "")
    elif ptype == "gemini":
        fb["HAANA_FALLBACK_API_KEY"] = f_prov.get("key", "")
        fb["HAANA_FALLBACK_BASE_URL"] = ""
    elif ptype == "anthropic":
        fb["HAANA_FALLBACK_API_KEY"] = f_prov.get("key", "")
        fb["HAANA_FALLBACK_BASE_URL"] = f_prov.get("url", "")
        # OAuth
        if not f_prov.get("key") and f_prov.get("auth_method") == "oauth" and f_prov.get("oauth_dir"):
            fb["HAANA_FALLBACK_OAUTH_DIR"] = f_prov["oauth_dir"]

    # OAuth-Suche: Drittanbieter brauchen trotzdem Anthropic-Auth für Claude CLI
    if ptype in ("gemini", "openai", "ollama"):
        for prov in cfg.get("providers", []):
            if prov.get("type") == "anthropic" and prov.get("auth_method") == "oauth" and prov.get("oauth_dir"):
                fb["HAANA_FALLBACK_OAUTH_DIR"] = prov["oauth_dir"]
                break

    return fb


def _build_agent_env(user: dict, cfg: dict, resolve_llm_fn, find_ollama_url_fn) -> dict:
    """Baut die Env-Vars für einen Agent-Prozess auf.

    Gemeinsame Logik für Docker- und InProcess-Manager.
    resolve_llm_fn: (llm_id, cfg) -> (llm_dict, provider_dict)
    find_ollama_url_fn: (cfg) -> str
    """
    uid = user["id"]
    api_port = user.get("api_port", 8001)
    write_scopes = f"{uid}_memory,household_memory"
    read_scopes = f"{uid}_memory,household_memory"

    primary_llm_id = user.get("primary_llm", "")
    fallback_llm_id = user.get("fallback_llm", "")
    extract_llm_id = cfg.get("memory", {}).get("extraction_llm", "")
    p_llm, p_prov = resolve_llm_fn(primary_llm_id, cfg)
    f_llm, f_prov = resolve_llm_fn(fallback_llm_id, cfg)
    e_llm, e_prov = resolve_llm_fn(extract_llm_id, cfg)

    ollama_url = find_ollama_url_fn(cfg)
    emb = cfg.get("embedding", {})

    # Extraction-Provider bestimmen (kann sich vom Primary-Provider unterscheiden)
    extract_type = e_prov.get("type", "ollama")
    extract_url = e_prov.get("url", "")
    extract_key = e_prov.get("key", "")
    extract_oauth_dir = ""
    # Anthropic OAuth: API-Key hat Vorrang (Messages API braucht Key, nicht OAuth)
    # Wenn auch Key vorhanden → Key verwenden. Sonst OAuth-Dir als Fallback-Info.
    if extract_type == "anthropic" and not extract_key:
        if e_prov.get("auth_method") == "oauth" and e_prov.get("oauth_dir"):
            extract_oauth_dir = e_prov["oauth_dir"]
    # Extraction-LLM RPM (aus LLM-Config, 0 = kein Limit)
    extract_rpm = str(e_llm.get("rpm", 0))
    # Ollama: URL kommt aus OLLAMA_URL

    # Embedding-Provider bestimmen
    embed_provider_id = emb.get("provider_id", "")
    embed_type = "ollama"
    embed_url = ""
    embed_key = ""
    if embed_provider_id == "__local__":
        embed_type = "fastembed"
    elif embed_provider_id:
        for prov in cfg.get("providers", []):
            if prov.get("id") == embed_provider_id:
                embed_type = prov.get("type", "ollama")
                if embed_type in ("openai", "gemini"):
                    embed_url = prov.get("url", "")
                    embed_key = prov.get("key", "")
                break

    mem_cfg = cfg.get("memory", {})

    env = {
        "HAANA_INSTANCE":        uid,
        "HAANA_API_PORT":        str(api_port),
        "HAANA_MEDIA_DIR":       os.environ.get("HAANA_MEDIA_DIR", str(_get_default_media_dir())),
        "HAANA_LOG_DIR":         os.environ.get("HAANA_LOG_DIR", str(_get_default_log_dir())),
        "HAANA_WRITE_SCOPES":    write_scopes,
        "HAANA_READ_SCOPES":     read_scopes,
        "HAANA_MODEL":           p_llm.get("model", "claude-sonnet-4-6"),
        "HAANA_MEMORY_MODEL":    e_llm.get("model", "ministral-3-32k:3b"),
        "HAANA_WINDOW_SIZE":     str(mem_cfg.get("window_size", 20)),
        "HAANA_WINDOW_MINUTES":  str(mem_cfg.get("window_minutes", 60)),
        "HAANA_EMBEDDING_MODEL": emb.get("model", "bge-m3"),
        "HAANA_EMBEDDING_DIMS":  str(emb.get("dims", 1024)),
        "QDRANT_URL":            cfg.get("services", {}).get("qdrant_url", "http://qdrant:6333"),
        "OLLAMA_URL":            ollama_url,
        "HA_URL":                cfg.get("services", {}).get("ha_url", ""),
        "HA_TOKEN":              cfg.get("services", {}).get("ha_token", ""),
        # Extraction-Provider (für Memory-LLM, kann sich von Ollama unterscheiden)
        "HAANA_EXTRACT_URL":           extract_url,
        "HAANA_EXTRACT_KEY":           extract_key,
        "HAANA_EXTRACT_PROVIDER_TYPE": extract_type,
        "HAANA_EXTRACT_OAUTH_DIR":     extract_oauth_dir,
        "HAANA_EXTRACT_RPM":           extract_rpm,
        "HAANA_EXTRACT_THINK":         str(e_llm.get("think", "")).lower(),
        "HAANA_CONTEXT_ENRICHMENT":    str(mem_cfg.get("context_enrichment", False)).lower(),
        "HAANA_CONTEXT_BEFORE":        str(mem_cfg.get("context_before", 3)),
        "HAANA_CONTEXT_AFTER":         str(mem_cfg.get("context_after", 2)),
        # Embedding-Provider (kann sich von Ollama unterscheiden)
        "HAANA_EMBED_PROVIDER_TYPE":   embed_type,
        "HAANA_EMBED_URL":             embed_url,
        "HAANA_EMBED_KEY":             embed_key,
    }

    # Fallback-LLM Env-Vars (für automatisches Umschalten bei Auth/Connection-Fehlern)
    if f_llm and f_prov:
        fb_env = _build_fallback_env(f_llm, f_prov, ollama_url, cfg)
        env.update(fb_env)

    # HA MCP-Server URL
    services = cfg.get("services", {})
    if services.get("ha_mcp_enabled"):
        ha_mcp_type = services.get("ha_mcp_type", "extended")
        ha_mcp_url = services.get("ha_mcp_url", "").strip()
        if not ha_mcp_url:
            ha_url = services.get("ha_url", "").rstrip("/")
            if ha_url and ha_mcp_type == "builtin":
                ha_mcp_url = f"{ha_url}/mcp_server/sse"
        if ha_mcp_url:
            env["HA_MCP_URL"] = ha_mcp_url
            env["HA_MCP_TYPE"] = ha_mcp_type

    # Provider-spezifische Env-Vars
    is_minimax = p_prov.get("type") == "minimax"
    if is_minimax:
        env["ANTHROPIC_BASE_URL"] = p_prov.get("url") or "https://api.minimax.io/anthropic"
        env["ANTHROPIC_AUTH_TOKEN"] = p_prov.get("key", "")
        env["ANTHROPIC_MODEL"] = p_llm.get("model", "MiniMax-M2.5")
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    elif p_prov.get("type") == "ollama":
        # Ollama: Anthropic-kompatiblen Endpoint nutzen (offizielle Ollama-Doku)
        # https://docs.ollama.com/integrations/claude-code
        ollama_base = (p_prov.get("url") or ollama_url or "http://localhost:11434").rstrip("/")
        env["ANTHROPIC_BASE_URL"] = ollama_base
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
    elif p_prov.get("type") == "openai":
        if p_prov.get("key"):
            env["OPENAI_API_KEY"] = p_prov["key"]
        if p_prov.get("url"):
            env["OPENAI_BASE_URL"] = p_prov["url"]
        env["OPENAI_MODEL"] = p_llm.get("model", "gpt-4o")
    elif p_prov.get("type") == "gemini":
        if p_prov.get("key"):
            env["GEMINI_API_KEY"] = p_prov["key"]
        env["GEMINI_MODEL"] = p_llm.get("model", "gemini-2.0-flash")
    else:
        if p_prov.get("key"):
            env["ANTHROPIC_API_KEY"] = p_prov["key"]
        if p_prov.get("url"):
            env["ANTHROPIC_BASE_URL"] = p_prov["url"]

    return env


class DockerAgentManager:
    """Agent-Management via Docker SDK (Standalone/Development-Modus)."""

    def __init__(self, docker_client, *, host_base: str, data_volume: str,
                 compose_network: str, agent_image: str,
                 media_volume: str = "",
                 resolve_llm_fn, find_ollama_url_fn):
        self._client = docker_client
        self._host_base = host_base
        self._data_volume = data_volume
        self._media_volume = media_volume or os.environ.get("HAANA_MEDIA_VOLUME", "haana_haana-media")
        self._compose_network = compose_network
        self._agent_image = agent_image
        self._resolve_llm = resolve_llm_fn
        self._find_ollama_url = find_ollama_url_fn
        self._port_cache: dict[str, int] = {}  # instance -> api_port

    def _get_image(self, instance: str = "") -> str:
        """Agent-Image auto-detektieren. Bevorzugt Image mit passendem Namen."""
        if not self._client:
            return self._agent_image
        try:
            # Erst: exaktes Image für diese Instanz suchen
            if instance:
                for tag in [f"haana-instanz-{instance}:latest", f"haana-instanz-{instance}"]:
                    try:
                        self._client.images.get(tag)
                        return tag
                    except Exception:
                        pass
            # Fallback: Image von einem laufenden Instanz-Container
            containers = self._client.containers.list(all=True)
            for c in containers:
                if "instanz-" in c.name or "haana-instanz" in c.name:
                    return c.image.tags[0] if c.image.tags else self._agent_image
        except Exception:
            pass
        return self._agent_image

    def _get_network(self) -> str:
        """Docker-Netzwerk auto-detektieren."""
        if not self._client:
            return self._compose_network
        for net_name in [self._compose_network, "haana-default", "haana_default", "bridge"]:
            try:
                self._client.networks.get(net_name)
                return net_name
            except Exception:
                pass
        return self._compose_network

    def _container_name(self, user_or_instance) -> str:
        if isinstance(user_or_instance, dict):
            return user_or_instance.get("container_name", f"haana-instanz-{user_or_instance['id']}-1")
        return f"haana-instanz-{user_or_instance}-1"

    async def start_agent(self, user: dict, cfg: dict) -> dict:
        if not self._client:
            return {"ok": False, "error": "Docker nicht verfügbar (kein Socket gemountet?)"}

        uid = user["id"]
        api_port = user["api_port"]
        self._port_cache[uid] = api_port
        container_name = self._container_name(user)

        env = _build_agent_env(user, cfg, self._resolve_llm, self._find_ollama_url)
        image = self._get_image(uid)
        network = self._get_network()

        # Host-Pfade
        host_claude_md = f"{self._host_base}/instanzen/{uid}/CLAUDE.md"
        host_skills_data = f"{self._host_base}/data/skills"
        host_skills_app  = f"{self._host_base}/skills"
        host_claude_config = "/home/haana/.claude"

        # Skills: /data/skills/ bevorzugen (update-resistent), Fallback auf /app/skills/
        data_skills_path = Path(host_skills_data)
        if data_skills_path.exists() and any(data_skills_path.iterdir()):
            active_skills_host = host_skills_data
            logger.debug(f"[Docker] Skills aus /data/skills/ für Agent '{uid}'")
        else:
            active_skills_host = host_skills_app
            logger.debug(f"[Docker] Skills aus /app/skills/ (Fallback) für Agent '{uid}'")

        volumes = {
            host_claude_md:       {"bind": "/app/CLAUDE.md",  "mode": "ro"},
            active_skills_host:   {"bind": "/app/skills",     "mode": "ro"},
            self._data_volume:    {"bind": "/data",           "mode": "rw"},
            self._media_volume:   {"bind": "/media/haana",    "mode": "rw"},
        }

        # Provider aus env rekonstruieren für OAuth-Mount
        p_prov = self._resolve_llm(user.get("primary_llm", ""), cfg)[1]
        is_minimax = p_prov.get("type") == "minimax"
        is_oauth = p_prov.get("type") == "anthropic" and p_prov.get("auth_method") == "oauth"

        # OAuth-Credentials finden: Primär-Provider oder irgendeinen Anthropic-OAuth-Provider
        oauth_dir = None
        if is_oauth and p_prov.get("oauth_dir"):
            oauth_dir = p_prov["oauth_dir"]
        elif p_prov.get("type") in ("gemini", "openai", "ollama"):
            # Drittanbieter-Provider: Claude CLI braucht trotzdem Anthropic-Auth.
            # Ersten Anthropic-OAuth-Provider mit Credentials suchen.
            for prov in cfg.get("providers", []):
                if prov.get("type") == "anthropic" and prov.get("auth_method") == "oauth" and prov.get("oauth_dir"):
                    oauth_dir = prov["oauth_dir"]
                    break

        if oauth_dir:
            env["HAANA_OAUTH_DIR"] = oauth_dir
        elif not is_minimax and p_prov.get("type") not in ("openai", "gemini", "ollama"):
            volumes[host_claude_config] = {"bind": "/home/haana/.claude", "mode": "rw"}
            claude_json_host = Path("/root/.claude.json")
            try:
                has_claude_json = claude_json_host.is_file()
            except PermissionError:
                has_claude_json = False
            if has_claude_json:
                volumes[str(claude_json_host)] = {"bind": "/home/haana/.claude.json", "mode": "rw"}

        try:
            # Alten Container entfernen
            try:
                old = self._client.containers.get(container_name)
                old.stop(timeout=5)
                old.remove()
            except Exception:
                pass

            container = self._client.containers.run(
                image,
                name=container_name,
                environment=env,
                volumes=volumes,
                ports={f"{api_port}/tcp": api_port},
                network=network,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
            )
            return {"ok": True, "container_id": container.short_id, "container_name": container_name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    async def stop_agent(self, instance: str, force: bool = False) -> dict:
        if not self._client:
            return {"ok": False, "error": "Docker nicht verfügbar"}
        container_name = self._container_name(instance)
        try:
            c = self._client.containers.get(container_name)
            if force:
                c.kill()
            else:
                c.stop(timeout=10)
            return {"ok": True, "container": container_name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def restart_agent(self, instance: str) -> dict:
        if not self._client:
            return {"ok": False, "error": "Docker nicht verfügbar"}
        container_name = self._container_name(instance)
        try:
            c = self._client.containers.get(container_name)
            c.restart(timeout=10)
            return {"ok": True, "container": container_name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def agent_status(self, instance: str) -> str:
        if not self._client:
            return "unknown"
        container_name = self._container_name(instance)
        try:
            c = self._client.containers.get(container_name)
            return c.status
        except Exception:
            return "absent"

    def agent_url(self, instance: str) -> str:
        container_name = self._container_name(instance)
        port = self._port_cache.get(instance)
        if port is None and self._client:
            # Port aus laufendem Container lesen und cachen
            try:
                c = self._client.containers.get(container_name)
                env_dict = dict(e.split("=", 1) for e in (c.attrs.get("Config", {}).get("Env") or []) if "=" in e)
                port = int(env_dict.get("HAANA_API_PORT", 8001))
                self._port_cache[instance] = port
            except Exception:
                port = 8001
        return f"http://{container_name}:{port or 8001}"

    def get_agent(self, instance: str):
        """Gibt None zurück – im Docker-Modus kein direkter Agent-Zugriff."""
        return None

    def list_agents(self) -> dict[str, str]:
        if not self._client:
            return {}
        result = {}
        try:
            containers = self._client.containers.list(all=True)
            for c in containers:
                if "instanz-" in c.name or "haana-instanz" in c.name:
                    # Extract instance name from container name
                    name = c.name.replace("haana-instanz-", "").replace("-1", "")
                    result[name] = c.status
        except Exception:
            pass
        return result

    async def remove_agent(self, instance: str) -> dict:
        if not self._client:
            return {"ok": False, "error": "Docker nicht verfügbar"}
        container_name = self._container_name(instance)
        try:
            c = self._client.containers.get(container_name)
            c.stop(timeout=5)
            c.remove()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}


class InProcessAgentManager:
    """Agent-Management im selben Prozess (HA Add-on Modus).

    Agents laufen als HaanaAgent-Objekte mit eigener FastAPI Sub-App.
    """

    def __init__(self, *, main_app, resolve_llm_fn, find_ollama_url_fn,
                 inst_dir: Path, data_root: Path):
        self._agents: dict[str, object] = {}  # instance -> HaanaAgent
        self._api_apps: dict[str, object] = {}  # instance -> FastAPI sub-app
        self._main_app = main_app
        self._resolve_llm = resolve_llm_fn
        self._find_ollama_url = find_ollama_url_fn
        self._inst_dir = inst_dir
        self._data_root = data_root

    async def start_agent(self, user: dict, cfg: dict) -> dict:
        uid = user["id"]

        # Bereits laufend? Erst stoppen
        if uid in self._agents:
            await self.stop_agent(uid)

        env = _build_agent_env(user, cfg, self._resolve_llm, self._find_ollama_url)

        # Env-Vars setzen für diesen Agent (In-Process: teilen sich os.environ)
        # Wir setzen die Vars temporär für die Agent-Initialisierung
        old_env = {}
        for k, v in env.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            from core.agent import HaanaAgent
            from core.api import create_api

            agent = HaanaAgent(uid)
            await agent.startup()
            api = create_api(agent)

            self._agents[uid] = agent
            self._api_apps[uid] = api

            # Sub-App mounten
            prefix = f"/agent/{uid}"
            self._main_app.mount(prefix, api)
            logger.info(f"[InProcess] Agent '{uid}' gestartet unter {prefix}")

            return {"ok": True, "mode": "in-process", "url": prefix}
        except Exception as e:
            logger.error(f"[InProcess] Agent '{uid}' Start fehlgeschlagen: {e}")
            return {"ok": False, "error": str(e)[:300]}
        finally:
            # Env wiederherstellen
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    async def stop_agent(self, instance: str, force: bool = False) -> dict:
        agent = self._agents.pop(instance, None)
        self._api_apps.pop(instance, None)
        if not agent:
            return {"ok": False, "error": f"Agent '{instance}' nicht aktiv"}

        try:
            if hasattr(agent, 'shutdown'):
                await agent.shutdown()
        except Exception as e:
            logger.warning(f"[InProcess] Shutdown '{instance}': {e}")

        # Sub-App aus Routes entfernen
        prefix = f"/agent/{instance}"
        self._main_app.routes[:] = [
            r for r in self._main_app.routes
            if not (hasattr(r, 'path') and r.path.startswith(prefix))
        ]

        logger.info(f"[InProcess] Agent '{instance}' gestoppt")
        return {"ok": True}

    async def restart_agent(self, instance: str) -> dict:
        # In-Process: restart erfordert user+cfg, Aufrufer muss start_agent nutzen
        if instance in self._agents:
            await self.stop_agent(instance)
            return {"ok": True, "restarted": False, "detail": "Agent gestoppt. start_agent mit user+cfg nötig."}
        return {"ok": False, "error": f"Agent '{instance}' nicht aktiv"}

    def agent_status(self, instance: str) -> str:
        if instance in self._agents:
            return "running"
        return "absent"

    def agent_url(self, instance: str) -> str:
        if instance in self._agents:
            return f"http://localhost:8080/agent/{instance}"
        return ""

    def get_agent(self, instance: str):
        """Gibt das HaanaAgent-Objekt zurück (InProcess-Modus)."""
        return self._agents.get(instance)

    def list_agents(self) -> dict[str, str]:
        return {k: "running" for k in self._agents}

    async def remove_agent(self, instance: str) -> dict:
        return await self.stop_agent(instance)


def detect_mode() -> str:
    """Erkennt den Betriebsmodus: 'standalone' oder 'addon'."""
    mode = os.environ.get("HAANA_MODE", "auto")
    if mode != "auto":
        return mode
    # Auto-detect: Docker-Socket vorhanden → standalone
    if Path("/var/run/docker.sock").exists():
        return "standalone"
    return "addon"


def create_agent_manager(mode: str, *, main_app=None, docker_client=None,
                         resolve_llm_fn=None, find_ollama_url_fn=None,
                         **kwargs) -> AgentManager:
    """Factory: Erstellt den passenden AgentManager."""
    if mode == "standalone":
        return DockerAgentManager(
            docker_client,
            host_base=kwargs.get("host_base", os.environ.get("HAANA_HOST_BASE", "/opt/haana")),
            data_volume=kwargs.get("data_volume", os.environ.get("HAANA_DATA_VOLUME", "haana_haana-data")),
            media_volume=kwargs.get("media_volume", os.environ.get("HAANA_MEDIA_VOLUME", "haana_haana-media")),
            compose_network=kwargs.get("compose_network", os.environ.get("HAANA_COMPOSE_NETWORK", "haana_default")),
            agent_image=kwargs.get("agent_image", os.environ.get("HAANA_AGENT_IMAGE", "haana-instanz-benni")),
            resolve_llm_fn=resolve_llm_fn,
            find_ollama_url_fn=find_ollama_url_fn,
        )
    else:
        return InProcessAgentManager(
            main_app=main_app,
            resolve_llm_fn=resolve_llm_fn,
            find_ollama_url_fn=find_ollama_url_fn,
            inst_dir=kwargs.get("inst_dir", Path(os.environ.get("HAANA_INST_DIR", "/app/instanzen"))),
            data_root=kwargs.get("data_root", Path(os.environ.get("HAANA_DATA_DIR", "/data"))),
        )
