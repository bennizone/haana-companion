ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache curl bash

# Ollama Binary (CPU-only, minimal)
RUN curl -fsSL https://ollama.com/install.sh | sh

COPY haana-addons/haana-ollama-cpu/rootfs/ /
