# Alertas

O painel e as métricas funcionam sem display CYD ou broker MQTT.
Consulte [status](status-dashboard.md) e [operação](../operator-guide.md).

Alertas são opcionais. No `.env` privado:

```dotenv
HOMESERVER_ALERTS_ENABLED="true"
HOMESERVER_ALERT_WEBHOOK_URL="https://seu-destino/endpoint"
HOMESERVER_ALERT_RETRY_SECONDS="60"
```

O supervisor envia JSON somente ao destino configurado. A outbox SQLite
persistente deduplica eventos e faz retry exponencial; falha de entrega não
impede reprodução. Reinicie os consumidores após o apply conforme
[o guia do operador](../operator-guide.md#aplicar-uma-mudança).

Payloads/logs ocultam credenciais. Timeout após aceitação pelo destino pode
gerar nova entrega, então o destinatário deve tratar a identidade/geração
do evento de forma idempotente.
