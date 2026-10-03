# Fontes paradas e temporadas

O Seerr acompanha o pedido; qBittorrent mostra a transferência real. Um episódio
concluído pode aguardar a importação porque falta um episódio anterior. Essa
ordem de publicação não limita os downloads a um arquivo por vez.

O worker combina a busca do episódio com resultados nativos da temporada,
mantendo a identidade da série e do episódio verificada pelo Sonarr. A busca
da temporada é reutilizada por até cinco minutos. Resultados frescos do episódio
prevalecem sobre decisões antigas da temporada.

UIndex é a primeira fonte. O fallback considera a ausência de candidatos
elegíveis ou seeds abaixo de `HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS`.
Resolução e idioma continuam prioritários; seeds precedem a afinidade de família.
O prefixo no nome do arquivo não comprova a origem nem a disponibilidade do swarm.

Um magnet pode anunciar muitos seeds e ter um arquivo `.torrent` ausente ou
corrompido no cache HTTP. Nesse caso, o worker usa uma sessão libtorrent isolada
somente para metadados: até 45 segundos, 16 MiB e nenhum payload de mídia. O hash,
os caminhos e os tamanhos continuam sendo inspecionados antes da admissão.
Falha na recuperação aparece nos logs como `Peer metadata unavailable`.

Com `HOMESERVER_SERIES_PREFER_SEASON_PACK=true`, um pack completo que passe pelos
filtros de qualidade também pode recuperar fontes individuais que permaneceram
sem peers e sem progresso por `HOMESERVER_SOURCE_STALL_SECONDS`. Essa recuperação
exige resposta positiva de disponibilidade dos trackers, confirma novamente o
estado dos originais e conserva seus arquivos parciais. Fontes concluídas,
protegidas, em progresso ou com uma troca em curso são preservadas. A capacidade
inclui o pack inteiro e os compromissos já existentes; episódios duplicados dentro
do pack ocupam espaço e não são apagados automaticamente.

A mudança dos vínculos é atômica. Metadados são persistidos antes da troca; após
reinício, uma autorização válida pode ser retomada. Se expirar antes do envio,
uma nova aquisição precisa passar novamente pela verificação de capacidade.
As importações dos episódios continuam sequenciais.

`HOMESERVER_SOURCE_SLOW_REPLACEMENT_ENABLED=false` mantém transferências lentas
que progridem. Um candidato de teste ativo que não recebe nenhum byte é rejeitado
após `HOMESERVER_SOURCE_STALL_SECONDS`; espera por vaga ou pausa manual não conta
como falha do swarm. A avaliação desse candidato não bloqueia os próximos
episódios da janela de downloads.

Para acompanhar:

```bash
docker compose logs --tail=150 control-worker
docker compose logs --tail=100 download-gateway
```

Compare seeds conectados e progresso no monitor qBit com os seeds anunciados na
busca do Sonarr. Contagens de indexadores e trackers não garantem que um peer
esteja alcançável ou transferindo naquele momento. Não contorne o gateway para
forçar um download: isso perde a validação de espaço e os vínculos de importação.

Referência técnica: [modo de metadados do libtorrent](https://libtorrent.org/manual-ref.html#magnet-links).
