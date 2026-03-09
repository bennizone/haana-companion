# HAANA Local LLM (CPU)

CPU-only Ollama server for HAANA embeddings and memory extraction.
Enables HAANA to work without a GPU server or external API key.

## Configuration

| Option | Description |
|--------|-------------|
| `embedding_model` | Model for vector embeddings (nomic-embed-text, all-minilm, bge-m3) |
| `llm_model` | Model for memory extraction (qwen3:0.6b, qwen3:1.7b) |

### Embedding Models

| Model | Size | Dims | Notes |
|-------|------|------|-------|
| nomic-embed-text | ~274 MB | 768 | Recommended: Good quality for DE + EN |
| all-minilm | ~22 MB | 384 | Minimal: Very fast, primarily English |
| bge-m3 | ~1.2 GB | 1024 | Best quality, needs more RAM |

### LLM Models

| Model | Size | Notes |
|-------|------|-------|
| qwen3:0.6b | ~397 MB | Recommended: Good speed/quality ratio |
| qwen3:1.7b | ~700 MB | Better quality, needs more RAM and CPU |

## Usage

1. Install this add-on
2. Start it (models are downloaded on first start)
3. In HAANA, add an Ollama provider with URL: `http://haana-ollama-cpu:11434`

## Important Notes

- CPU inference is slow but functional for async tasks (embeddings, memory extraction)
- Estimated RAM usage: 2-4 GB depending on model choice
- Models are stored persistently in `/data` and included in HA backups
- Changing the embedding model requires recreating Qdrant collections (different dimensions)
