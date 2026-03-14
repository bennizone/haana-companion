# Changelog

## 2.0.0
- Vereinfacht auf SSO-Gateway + HA-Admin-Check
- Entfernt: Handshake, Person-Watcher, MCP-Erkennung, ha_url Konfigurationsfeld
- HAANA LXC holt HA-Daten jetzt selbst via konfigurierter HA URL + Long-Lived Token
- Nur noch benötigt: haana_url + companion_token

## 1.0.0
- Initial release
- Ingress proxy to HAANA Admin UI
- Automatic token handshake with HAANA LXC
