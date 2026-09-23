# Gateway de downloads — validação de implantação

Release de código `6a784a64d13787e8c41c8729939aa25ba9e06ba7`, implantada no servidor de testes em 23/09/2026. O manifesto e o artefato foram validados antes da troca, e o Compose de produção passou em `config --quiet` com o override local. As credenciais e os arquivos de estado permanecem somente no servidor.

## Verificações concluídas

- Sonarr e Radarr usam `download-gateway:8081` como cliente qBittorrent; ambos retornaram zero erros em `/api/v3/downloadclient/test` após o deploy.
- Apenas o gateway alcança a API do qBittorrent na rede `transfer`. Sonarr não resolve o nome do qBittorrent e a conexão direta ao IP da rede `transfer` expira. O qBittorrent mantém uma rede separada para saída à Internet.
- Um torrent sintético de 123 bytes, com reserva e autorização persistentes, atravessou o gateway e apareceu no qBittorrent com hash, tamanho e destino esperados. O teste não iniciou transferência de mídia; o torrent sintético e seus estados foram encerrados após a verificação.
- Seerr conecta aos dois Arr. Ambos são servidores padrão, com `preventSearch=true` e perfil único `HomeServer 1080p-2160p`, sem upgrades. Busca RSS e busca automática dos indexadores sincronizados continuam desativadas.
- Um pedido aprovado de *Big Buck Bunny* (TMDb 10378) entrou no Radarr com o perfil esperado e gerou uma reserva de 50 GB pelo worker. O Seerr expõe aprovação como `status=2`, não como `isApproved`; o adaptador e seu teste de contrato foram corrigidos. A consulta do worker usa `filter=approved` para paginar sem perder pedidos.
- O pedido, o cadastro vazio no Radarr e os registros de reserva do teste foram removidos. O filme não tinha arquivo associado, e a pasta de torrents continha zero arquivos.
- Depois da release, a API respondeu `200` em `/health/ready`, o gateway respondeu `200` em `/health/live`, e o worker completou ciclos sem repetir o erro `Event loop is closed`.
- O backup Restic de configuração terminou com `Result=success` e `ExecMainStatus=0`; o stack voltou a `active/running`.
- O repositório Restic criptografado foi sincronizado para `backup/restic` no desktop e passou em `restic check`. A tarefa do Windows `HomeServer Backup Pull` ficou pronta para execução diária às 04:30 (horário de São Paulo).

## Limite da validação

Esta primeira validação não cobriu um download completo nem a importação de mídia. O estado acima descreve a release `6a784a6`, antes da aquisição controlada. O perfil Arr limita resolução nominal, mas não substitui as verificações de áudio, legenda e tamanho por arquivo exigidas pelo design.

## Atualização: aquisição controlada de filmes

A release `2d2f35a2bf385637b058ba13a6bc3c98130646a6` foi implantada em 23/09/2026 com manifesto e artefato validados, Compose de produção verificado e backup Restic consistente sincronizado ao desktop. O smoke de produção passou após usar o endereço Tailscale configurado no servidor. A configuração de *Completed Download Handling* do Radarr foi desativada antes de habilitar a aquisição, para que o controlador valide o arquivo antes da importação.

O pedido real `seerr:2`, filme TMDb `1101383`, permaneceu aprovado e com reserva de 50 GB. A busca do Radarr apresentou uma release YTS 1080p com `.torrent` v1 e legenda `por.srt`. O worker inspecionou os metadados, verificou hash, tamanho total, arquivo de vídeo e legenda, e emitiu um único permit persistente para a categoria `radarr` e o destino `/data/torrents`. A primeira tentativa de grab falhou porque o Radarr envia `stopped=False` no formulário e o gateway aceitava apenas `false`; o hotfix aceita ambas as capitalizações sem aceitar `true`, e retoma o mesmo permit. O gateway registrou `POST /api/v2/torrents/add` com HTTP 200, o permit está `confirmed`, e o Radarr mostra o filme na fila com estado `downloading / ok`. O qBittorrent reportou os arquivos selecionados com os nomes e tamanhos esperados. Nenhum registro de importação foi criado enquanto o torrent está incompleto.

O worker agora acompanha a conclusão pelo gateway, compara arquivos e tamanhos com o qBittorrent, valida resolução/áudio via FFprobe e o formato da legenda portuguesa, e solicita `DownloadedMoviesScan` ao Radarr uma vez por reserva. Após o Radarr reconhecer o filme, o worker garante uma legenda `.pt` na biblioteca por hardlink e encerra a reserva. Essa etapa final ainda depende da conclusão do download real para validação operacional. Séries/Sonarr e o cancelamento automático de pedidos ausentes ainda não usam essa automação de aquisição.
