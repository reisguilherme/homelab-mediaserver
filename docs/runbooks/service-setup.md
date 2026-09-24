# Preparação dos serviços

O Compose base não publica portas e não contém caminhos do host. Use
`compose.dev.yaml` para fixtures/loopback e `compose.prod.yaml` somente depois
da auditoria, guarda de UUID e criação explícita de `/etc/homeserver/server.env`.

Antes da primeira aquisição, configurar uma instância de Sonarr e Radarr,
desabilitar busca/RSS/grabs autônomos e apontar os Arr apenas para o gateway.
O qBittorrent permanece na rede `transfer`; não publicar sua API na LAN.

## Painéis web nativos

Com o Tailscale conectado, acessar `http://<tailscale-hostname>:8989/` (Sonarr),
`http://<tailscale-hostname>:7878/` (Radarr) e
`http://<tailscale-hostname>:18080/` (qBittorrent). Consultar o hostname e as
portas ativas no servidor com `sudo tailscale serve status`. O Tailscale Serve
encaminha as portas para os binds em loopback; esses painéis não são publicados
na LAN. As credenciais ficam somente na configuração do servidor.

Usar Sonarr/Radarr para acompanhar buscas, fila e importações, e o qBittorrent
para acompanhar a transferência. Novos downloads continuam passando pelo
controlador e gateway; não adicionar torrents ou alterar a fila manualmente
pelo painel do qBittorrent.

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
caminho de arquivo distinto e espaço livre suficiente para seu tamanho exato.
O gateway para a fonte anterior, verifica a parada e só
então autoriza a nova. Os arquivos parciais antigos permanecem no disco.
Para episódios sem sidecar, a nova fonte também exige SRT elegível no preflight.

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

## Legendas e importação

No Bazarr, manter o perfil `pb` (Português Brasil) e habilitar provedores
testados. O SubDL exige uma chave de API; o OpenSubtitles.com exige conta válida.
O Bazarr procura legendas depois que a mídia está na biblioteca. A ausência de
legenda obtida antes do download não dispensa a validação na importação.

O worker usa a mesma chave SubDL em `/etc/homeserver/subdl.key`, arquivo de uma
linha criado no servidor com dono `root` e modo `0600`. O Compose monta o arquivo
somente no `control-worker` em `/run/secrets/subdl.key`; nunca colocar a chave no
repositório, manifesto ou saída de diagnóstico. Criar o arquivo e executar um
backup consistente antes de implantar a release que requer essa montagem.

Para filmes, a aquisição não consulta o SubDL. Após validar o vídeo baixado,
o finalizador consulta o serviço pelo ID TMDb verificado e escolhe
SRT válido nesta ordem: pt-BR da mesma release, pt-BR de outra release com
duração compatível, inglês da mesma release e inglês de outra release com
duração compatível. Uma release diferente precisa ter edição/corte compatível
e cobertura temporal do SRT próxima à duração medida do vídeo, com margem
para créditos finais. O SubDL V1 usa `EN` sem garantir o dialeto en-US.
Artefatos de filmes criados pelo preflight anterior são ignorados nas novas
importações. Para séries sem sidecar, permanece o preflight: checar
temporada e episódio, preferir release exata e, depois, mesmo título, episódio
e família de fonte; persistir o SRT antes do permit. Para filmes sem legenda
elegível, o finalizador mantém o vídeo fora da biblioteca até obter uma
legenda válida ou confirmar áudio original pt-BR. Conferir sincronismo e
tradução no Jellyfin.

Legendas SRT em Windows-1252 são convertidas para UTF-8 após a validação da
estrutura; arquivos sem tempos SRT válidos continuam inelegíveis. Clipes em
`Sample/` ou com nome terminado em `sample` não contam como segundo vídeo do
torrent e não são selecionados para download. Um segundo vídeo principal ainda
bloqueia o candidato.

Se o Seerr mostrar `Requested`, conferir a reserva no controlador, o permit do
gateway e as filas Sonarr/Radarr e qBittorrent. O estado do Seerr só avança após
a importação. Pedidos `waiting_space` aguardam capacidade real no disco, sem
liberação manual do gateway.
