# Preparação dos serviços

O Compose base não publica portas e não contém caminhos do host. Use
`compose.dev.yaml` para fixtures/loopback e `compose.prod.yaml` somente depois
da auditoria, guarda de UUID e criação explícita de `/etc/homeserver/server.env`.

Antes da primeira aquisição, configurar uma instância de Sonarr e Radarr,
desabilitar busca/RSS/grabs autônomos e apontar os Arr apenas para o gateway.
O qBittorrent permanece na rede `transfer`; não publicar sua API na LAN.

Validar a versão real das imagens e preencher digests no manifesto de release.
Tags do arquivo `config/versions.env` são referências de desenvolvimento, não
prova de compatibilidade ou de segurança da produção.

## Legenda pt-BR antes da aquisição

No Bazarr, manter o perfil `pb` (Português Brasil) e habilitar provedores
testados. O SubDL exige uma chave de API; o OpenSubtitles.com exige conta válida.
O Bazarr procura legendas depois que a mídia está na biblioteca e não substitui
a verificação anterior ao download.

O worker usa a mesma chave SubDL em `/etc/homeserver/subdl.key`, arquivo de uma
linha criado no servidor com dono `root` e modo `0600`. O Compose monta o arquivo
somente no `control-worker` em `/run/secrets/subdl.key`; nunca colocar a chave no
repositório, manifesto ou saída de diagnóstico. Criar o arquivo e executar um
backup consistente antes de implantar a release que requer essa montagem.

O worker só aceita uma legenda SRT `BR_PT` para o ID TMDb e nome exato da release
(após normalizar pontuação); para séries, temporada e episódio também devem
coincidir. Falha de API, legenda inválida ou ausência de correspondência mantém
o pedido aguardando fonte. A legenda é persistida no SQLite antes do permit e
instalada na biblioteca somente após a importação validada. A inspeção manual de
sincronismo e da tradução continua necessária no Jellyfin.

Legendas SRT em Windows-1252 são convertidas para UTF-8 após a validação da
estrutura; arquivos sem tempos SRT válidos continuam inelegíveis. Clipes em
`Sample/` ou com nome terminado em `sample` não contam como segundo vídeo do
torrent e não são selecionados para download. Um segundo vídeo principal ainda
bloqueia o candidato.

Se o Seerr mostrar `Requested`, conferir a reserva no controlador, o permit do
gateway e as filas Sonarr/Radarr e qBittorrent. O estado do Seerr só avança após
a importação. Pedidos `waiting_space` aguardam capacidade real no disco, sem
liberação manual do gateway.
