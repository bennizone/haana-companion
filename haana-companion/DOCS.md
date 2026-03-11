# HAANA Companion

Verbindet Home Assistant mit dem HAANA AI-Stack auf einem Proxmox LXC.

## Voraussetzungen

- HAANA-Stack laeuft auf einem Proxmox LXC (Docker Compose)
- HAANA Admin-Interface ist vom HA-Host erreichbar

## Einrichtung

1. HAANA Companion Addon installieren
2. `haana_url`: URL des HAANA Admin-Interfaces (z.B. `http://192.168.1.100:8080`)
3. `companion_token`: Token aus dem HAANA Admin-Interface (Einstellungen > Companion)
4. Addon starten — der Handshake registriert HA automatisch beim HAANA-Stack

## Ingress

Das HAANA Admin-UI wird direkt in der HA Sidebar eingebunden.

## HA Voice

Ollama-URL manuell in HA Voice Assistants eintragen: `http://<lxc-ip>:11435`
