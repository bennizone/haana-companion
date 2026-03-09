FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tmux für Terminal-Session-Persistenz (Claude Code)
RUN apt-get update && apt-get install -y --no-install-recommends tmux git && rm -rf /var/lib/apt/lists/*

COPY . .

ENV PYTHONUNBUFFERED=1
ENV HAANA_ADMIN_PORT=8080

EXPOSE ${HAANA_ADMIN_PORT}

CMD uvicorn main:app --host 0.0.0.0 --port ${HAANA_ADMIN_PORT}
