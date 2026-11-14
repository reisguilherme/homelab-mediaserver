# Painel web de status

Abra `http://IP_DO_SERVIDOR/status/`, pela LAN, IP Tailscale ou MagicDNS.
O painel mostra CPU, memória, velocidade de rede, capacidade e estado dos
serviços. Cards abaixo das métricas abrem os painéis nativos pelos caminhos de cada serviço.

O container `host-metrics` lê o host e produz snapshots a cada
`HOMESERVER_METRICS_INTERVAL_SECONDS` (default 5). O navegador consulta
`/status/api/v1/status`. A primeira amostra CPU/rede ainda não tem taxa; dados antigos
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

“Restante da fila” mostra os bytes pendentes dos downloads ativos. Fontes
paradas não têm um campo próprio nos cards; seus compromissos continuam
considerados pelo controlador até a limpeza comprovada.

A barra de cada disco representa seu total físico, com filmes, séries, torrents,
outros, espaço livre utilizável e **Reserva do filesystem**. Essa reserva
corresponde a blocos livres que o sistema de arquivos mantém indisponíveis aos
serviços: a diferença entre total, usado físico e livre utilizável. A fila e a
margem de segurança dos downloads são calculadas separadamente. O tooltip da
legenda explica a reserva. A legenda identifica as cores e o texto informa as
proporções. Sem categorias atuais, a barra mostra usado, livre e reserva do
filesystem com aviso de detalhamento indisponível. Sem capacidade válida ou com
dados antigos, o gráfico é ocultado; um disco indisponível não parece vazio.

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
Abra `http://IP_DO_SERVIDOR/status/` ou a raiz `/`. Os mesmos caminhos funcionam
no IP Tailscale e no MagicDNS; os cards usam o host atual, sem IP fixo no HTML.
O painel antigo em `:8081` continua disponível. `/qbittorrent/` atravessa o
proxy administrativo e preserva autenticação, cookies e proteção de origem.
O acesso antigo em `:18080` pode continuar usando
[Tailscale Serve](../installation.md#acesso-pelo-tailscale).
Seerr é a exceção: seu card `/seer` redireciona ao mesmo host em `:5055`, sem
reescrever o aplicativo para uma subpasta não suportada.

O card **Downloads** abre `/status/ui/downloads`, protegido por `ADMIN_USERNAME` e
`ADMIN_PASSWORD` do `.env`. A lista mostra progresso, velocidade, seeds e disco;
**Trocar fonte** enfileira uma solicitação manual vinculada ao torrent exibido.
O worker mantém a fonte atual sem alternativa compatível ou espaço disponível.
Torrents completos, solicitações canceladas e packs com importação já iniciada
ficam protegidos. Nenhum token interno é enviado ao navegador.
Para falhas e estados da fila consulte [diagnóstico](../troubleshooting.md).
