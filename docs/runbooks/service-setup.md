# Preparação dos serviços

O Compose base não publica portas e não contém caminhos do host. Use
`compose.dev.yaml` para fixtures/loopback e `compose.prod.yaml` somente depois
da auditoria, guarda de UUID e criação explícita de `/etc/homeserver/server.env`.

Antes da primeira aquisição, configurar uma instância de Sonarr e Radarr,
desabilitar busca/RSS/grabs autônomos e apontar os Arr apenas para o gateway.
O qBittorrent permanece na rede `transfer`; não publicar sua API na LAN.

## Porta de peers do qBittorrent

Em produção, o Compose publica somente a porta de transferência P2P do
qBittorrent, em TCP e UDP, no endereço `LAN_BIND_IP`. Sem esse endereço, a
publicação fica restrita a `127.0.0.1`. O padrão é a porta `6881`; para usar
outra, definir `QBITTORRENT_PEER_PORT` em `/etc/homeserver/server.env` antes do
deploy. A mesma porta é configurada no cliente via `TORRENTING_PORT`. Não usar
`TAILSCALE_BIND_IP` para essa publicação nem expor a WebUI/API `8080` na LAN.

Após o deploy, conferir a porta publicada e as preferências de conexão do
qBittorrent: a porta de escuta deve ser a mesma em TCP e UDP.

```bash
docker ps --filter name=homeserver-qbittorrent --format '{{.Ports}}'
```

Publicar a porta no host não cria uma regra de entrada no roteador nem garante
peers de entrada da Internet; a velocidade ainda depende dos seeds da release.

## Troca de fonte sem progresso

O worker mede o progresso de cada filme e do primeiro episódio pendente de cada
série. Só procura outra fonte após 30 minutos sem avanço, sem seeds conectados
e com velocidade zero, ou após uma hora com média abaixo de 1 MiB/s. Um
episódio pausado por ordem cronológica não entra nessa avaliação. A alternativa
precisa ter seeds reportados, qualidade permitida, metadados verificáveis,
legenda elegível, caminho de arquivo distinto e espaço livre suficiente para
seu tamanho exato. O gateway para a fonte anterior, verifica a parada e só
então autoriza a nova. Os arquivos parciais antigos permanecem no disco.

Há no máximo uma troca automática por filme ou episódio para evitar que várias
fontes parciais consumam o armazenamento. Se a segunda fonte também parar ou
não existir candidata segura, verificar os logs do worker e os seeds nos
indexadores; escolher outra fonte manualmente exige uma nova análise de espaço
e dos arquivos parciais preservados.

Se a resposta ao envio da nova fonte se perder, o worker consulta o qBittorrent
e confirma o permit apenas quando hash, categoria, destino e tamanho coincidirem
com o manifesto persistido. Caso o torrent continue ausente após uma operação
incerta, a fonte antiga permanece parada e o worker registra a necessidade de
reconciliação manual, sem repetir um efeito cujo resultado não foi comprovado.

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
