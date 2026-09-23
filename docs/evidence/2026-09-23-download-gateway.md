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

## Limite da validação

Não houve pedido de mídia seguido de busca, seleção, download, inspeção, importação e reprodução completos. O worker atual apenas transforma pedidos aprovados do Seerr em reservas; ele ainda não busca releases, confere metadados e idioma, emite permissões para um candidato real, reconcilia importação ou cancela reservas quando pedidos somem. A remoção da reserva do teste precisou ser feita pelo operador. A aquisição automática permanece desativada até esse fluxo e sua reconciliação passarem em teste de ponta a ponta com conteúdo autorizado. O perfil Arr limita resolução nominal, mas não substitui as verificações de áudio, legenda e tamanho por arquivo exigidas pelo design.
