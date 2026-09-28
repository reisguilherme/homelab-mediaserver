# Painel web de status

Abra `HOMESERVER_STATUS_PUBLIC_URL` no navegador autorizado. O painel é somente
leitura e usa cards com as URLs públicas dos serviços; todas ficam no `.env`.
Os binds padrão são loopback; para acesso fora de casa, use o IP Tailscale nas
portas administrativas. Não há dependência de CYD/MQTT.

`homeserver-metrics.service` produz snapshots host/capacidade a cada
`METRICS_INTERVAL_SECONDS` (padrão 5). O navegador consulta `/api/v1/status`
a cada cinco segundos. Primeira amostra CPU/rede não tem taxa; dados antigos
mostram estado stale/unavailable, nunca um zero inventado.

A telemetria recebe `RUN_ROOT` e `/data` somente leitura. Categorias medem os
blocos físicos de filmes, séries e torrents sem contar hardlinks duas vezes.
Filmes têm prioridade, depois séries e torrents; Outros inclui o restante do
volume e metadados. A primeira coleta mostra Calculando pastas.

O rodapé mostra idade/estado do último backup verificado somente quando o
snapshot do host é recente. Falta de prova não é exibida como backup válido.
O painel não recebe segredos, acesso ao Docker ou mutações administrativas.

Para falhas consulte `systemctl status homeserver-metrics.service`, as idades
dos snapshots e [troubleshooting](../troubleshooting.md). `/health/live`
prova processo; readiness exige condições de operação.
