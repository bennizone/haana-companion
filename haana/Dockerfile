ARG BUILD_FROM
FROM ${BUILD_FROM}

# System-Pakete
RUN apk add --no-cache \
    python3 \
    py3-pip \
    curl \
    jq \
    bash

# Qdrant Binary (statisch gelinkt)
ARG QDRANT_VERSION=1.13.6
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "amd64" ]; then ARCH="x86_64"; \
    elif [ "$TARGETARCH" = "arm64" ]; then ARCH="aarch64"; \
    else ARCH="x86_64"; fi && \
    curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-${ARCH}-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin/ qdrant

# Python-Abhaengigkeiten
COPY admin-interface/requirements.txt /tmp/admin-req.txt
COPY requirements.txt /tmp/core-req.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /tmp/admin-req.txt \
    -r /tmp/core-req.txt && \
    rm /tmp/*.txt

# Anwendungscode
COPY core/ /app/core/
COPY admin-interface/ /app/admin-interface/
COPY skills/ /app/skills/
RUN mkdir -p /app/instanzen/templates
COPY instanzen/templates/ /app/instanzen/templates/

# S6 Service-Definitionen
COPY haana-addons/haana/rootfs/ /

WORKDIR /app/admin-interface
