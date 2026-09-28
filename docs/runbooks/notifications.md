# Alertas e telemetria

O painel HTTP e os snapshots do host funcionam sem CYD ou broker MQTT.
Consulte [status](status-dashboard.md) e [guia do operador](../operator-guide.md).

Alertas são opcionais: configure `HOMESERVER_ALERTS_ENABLED=true` e
`HOMESERVER_ALERT_WEBHOOK_URL` no `.env` privado. O supervisor envia um POST
JSON somente a esse destino explicitamente configurado, usando timeout
`HTTP_TIMEOUT_SECONDS`. Não há integração Telegram implícita.

A outbox SQLite persistente em `APPDATA_ROOT/telemetry/notifications.sqlite`
deduplica por evento, objeto e geração. Retry exponencial começa em
`ALERT_RETRY_SECONDS` e é limitado a uma hora. Stack bloqueada/degradada e
backup ausente/antigo produzem alertas; falha de entrega não impede reprodução.
`BACKUP_STALE_HOURS` define a idade máxima; zero desabilita o limiar de idade.

Payloads e logs ocultam credenciais. Um timeout após o destinatário aceitar
pode duplicar a entrega; o destinatário deve tratar geração como idempotente.
Falha de webhook é observada na outbox sem publicar a URL secreta em logs.
