ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache nodejs npm

COPY whatsapp-bridge/ /app/whatsapp-bridge/
WORKDIR /app/whatsapp-bridge
RUN npm ci --omit=dev

COPY haana-addons/haana-whatsapp/rootfs/ /
