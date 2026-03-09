# HAANA - Home Assistant Advanced Nano Assistant

AI Household Assistant with persistent memory, powered by Claude Code SDK.

## Installation

1. Add repository: Settings > Add-ons > Add-on Store > Menu > Repositories
2. Enter: `https://github.com/benpro-haana/haana-addons`
3. Install "HAANA" add-on
4. Configure API key or Ollama URL in add-on settings
5. Start the add-on

## Configuration

| Option | Description |
|--------|-------------|
| `anthropic_api_key` | Anthropic API key (optional if using Ollama) |
| `ollama_url` | URL to Ollama server, e.g. `http://10.0.0.5:11434` |
| `ha_mcp_url` | Home Assistant MCP server URL (optional) |

## Data Storage

- **Config & Memory** (`/data`): Automatically included in HA backups
- **Conversation Logs** (`/media/haana/logs`): Included when media backup is enabled

## Optional Add-ons

- **HAANA Local LLM (CPU)**: CPU-only Ollama for embeddings and memory extraction
- **HAANA WhatsApp**: WhatsApp bridge for chat via WhatsApp
