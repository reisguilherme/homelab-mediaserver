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

Com o cadastro de expansão instalado, o painel mostra dois cards, SSD e HD USB.
Cada um apresenta total, usado físico, livre utilizável, restante da fila,
disponibilidade para novos downloads, categorias e idade da amostra. A ausência,
troca de identidade, montagem somente leitura ou evidência vencida deixa somente
o card afetado indisponível; CPU, memória e rede continuam atualizando.

As categorias são medidas nas raízes físicas, nunca na visão mergerfs. Hardlinks
são deduplicados dentro de cada disco. Usado físico exclui blocos reservados ainda
livres; livre utilizável considera somente blocos disponíveis ao serviço.
Fila e disponibilidade aparecem como “—” quando o controlador não publicou prova
atual correspondente ao UUID do disco. Não interpretar esse símbolo como fila
vazia. O contrato está em [status v2](../contracts/status-v2.md).

```bash
docker compose ps host-metrics telemetry
docker compose logs --tail=100 host-metrics telemetry
df -hT /srv/data
```

A telemetria recebe mídia/snapshots somente leitura; não publica segredos.
O monitor qBit usa loopback e [Tailscale Serve](../installation.md#acesso-pelo-tailscale).
Use encaminhamento TCP e abra `http://IP_TAILSCALE:18080` ou o hostname
MagicDNS na mesma porta. Confira `tailscale serve status` se o card não abrir.
O painel também está disponível em `/ui/status`; `/` abre o mesmo dashboard.

O painel não executa comandos de administração. Para falhas e estados
da fila consulte [diagnóstico](../troubleshooting.md).
