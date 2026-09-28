# Painel web de status

Abra `http://IP_DO_SERVIDOR:8081`, pela LAN ou pelo IP Tailscale.
O painel mostra CPU, memória, velocidade de rede, capacidade e estado dos
serviços. Cards abaixo das métricas abrem os painéis nativos nas portas fixas.

O container `host-metrics` lê o host e produz snapshots a cada
`HOMESERVER_METRICS_INTERVAL_SECONDS` (default 5). O navegador consulta
`/api/v1/status`. A primeira amostra CPU/rede ainda não tem taxa; dados antigos
aparecem como stale/unavailable. Ausência não é exibida como zero.

Categorias medem blocos físicos de filmes, séries e torrents, sem contar
hardlinks duas vezes. Filmes têm prioridade, depois séries e torrents;
Outros inclui o restante do filesystem e seus metadados. A primeira coleta
pode mostrar Calculando pastas.

```bash
docker compose ps host-metrics telemetry
docker compose logs --tail=100 host-metrics telemetry
df -hT /srv/data
```

A telemetria recebe mídia/snapshots somente leitura; não publica segredos.
O monitor qBit usa loopback e [Tailscale Serve](../installation.md#acesso-pelo-tailscale).
O hostname MagicDNS do servidor permite alcançá-lo na tailnet; confira o
endereço informado pelo Serve se o card não abrir.

O painel não executa comandos de administração. Para falhas e estados
da fila consulte [diagnóstico](../troubleshooting.md).
