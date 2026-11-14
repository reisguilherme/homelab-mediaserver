# Configurar os serviços

Comece pelo [guia de instalação](../installation.md). O serviço init prepara
diretórios/configurações ausentes e o operator conecta as APIs, aplica
preferências e confirma valores. Bancos existentes são reutilizados; a migração
dos perfis de qualidade é descrita abaixo.

```bash
docker compose run --rm operator config plan --env-file /project/.env --in-container
docker compose run --rm operator config apply --env-file /project/.env --in-container
docker compose restart control-api control-worker download-gateway telemetry host-metrics
docker compose run --rm operator config verify --env-file /project/.env --in-container
```

## Recursos gerenciados

Os painéis ficam sob `/jellyfin/`, `/sonarr/`, `/radarr/`, `/prowlarr/`,
`/bazarr/`, `/qbittorrent/` e `/status/` no IP LAN, IP Tailscale ou MagicDNS.
O proxy `service-proxy` usa HTTP 80 e preserva a autenticação nativa. `/seer`
é um atalho para `:5055` no mesmo host; essa versão não suporta Base URL.
O operator gerencia os prefixos nativos e as URLs das conexões entre serviços.
Quando uma mudança indicar reinício, reinicie o serviço afetado e repita
apply/verify. Para migrar URLs de uma instalação existente, siga a
[sequência de reinício e reconciliação](../installation.md#caminhos-dos-serviços-em-uma-instalação-existente).
A API de controle não recebe um caminho público nesse proxy.

| Serviço | Configuração coordenada |
|---|---|
| qBittorrent | Downloads/seeding, banda, conexões, slots e limites de seed |
| Sonarr/Radarr | Rootfolders `/data/media/tv` e `/data/media/movies`, três perfis `Max Quality`, `Standard` e `Dublado` em cada serviço e cliente HomeServer Gateway |
| Prowlarr | Conexões Arr, indexadores declarados e vínculo com proxy Byparr |
| Bazarr | Conexões Arr, providers e idiomas |
| Jellyfin | Bibliotecas por path, administrador, permissões de edição/coleções/legendas e coleções automáticas de filmes |
| Seerr | Jellyfin, bibliotecas, conexões Arr e perfis pelos IDs reais |

Sonarr/Radarr usam o gateway como cliente; a API qBit é interna. Completed
Download Handling fica desabilitado e hardlinks habilitados. O worker coordena
validação e importação. Filmes podem aparecer no Jellyfin enquanto a recuperação
de legendas continua; séries mantêm a exigência de legenda e a ordem de publicação.
Uma autorização explícita por título permite publicar uma série com legendas
pendentes; o controlador conserva a ordem e continua a recuperação por episódio.
A conexão Radarr `HomeServer Jellyfin` atualiza a biblioteca após importações,
upgrades e renomeações, inclusive quando mergerfs não propaga eventos de arquivos.

O Seerr usa `HOMESERVER_DOWNLOAD_DEFAULT_PROFILE="Standard"` por padrão nas
conexões de filmes e séries. Escolha outro perfil nas opções avançadas do pedido
ou no cadastro do título no Arr. `Max Quality` prefere UHD Remux, UHD Blu-ray,
UHD WEB-DL, FHD Remux, FHD Blu-ray e FHD WEB-DL, nessa ordem. `Standard` aceita
somente FHD Blu-ray e FHD WEB-DL. `Dublado` usa essas mesmas duas qualidades,
mas compara primeiro dual pt-BR + original, pt-BR e original, na ordem de
`HOMESERVER_DUBBED_AUDIO_PREFERENCES`. No Radarr, Dublado usa idioma `Any` e os
outros perfis usam original. O mínimo de seeds para qualquer perfil é
`HOMESERVER_RELEASE_MIN_SEEDERS="5"`.

O Custom Format gerenciado `HomeServer Dublado Search` declara os idiomas
português e português brasileiro disponíveis no Radarr, com score 1 apenas no
perfil Dublado. Isso expande a busca nativa para títulos traduzidos; não aumenta
o score mínimo nem exige esse formato para permitir o fallback original.
Standard e Max Quality mantêm suas pontuações. A seleção continua dependendo
da evidência de áudio avaliada pelo worker: um título traduzido ou o idioma
original cadastrado do filme não comprovam uma faixa na release.

O apply migra associações do antigo `HomeServer Animation` para `Dublado` e dos
demais perfis obsoletos para o padrão configurado, inicialmente `Standard`,
antes de remover suas definições.
Arquivos e torrents permanecem. Animações novas respeitam a seleção explícita;
alterar o perfil só orienta buscas futuras, sem iniciar upgrades ou trocas automáticas.

Senhas de contas existentes não recebem reset silencioso. A conta Jellyfin
indicada em `HOMESERVER_ADMIN_USERNAME` recebe administração e permissões de
gerenciar coleções, legendas e letras; outras políticas e usuários são preservados.
`HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION` mantém a preferência explícita de exclusão.
Bibliotecas existentes não são
renomeadas ou removidas. A migração de qualidade substitui todos os perfis fora
dos três nomes gerenciados, depois de reassociar seus filmes e séries.
Credenciais ausentes/incorretas exigem preencher o `.env` com os valores reais
e repetir o apply.

## Coleções de filmes

`HOMESERVER_JELLYFIN_AUTO_COLLECTIONS="true"` é o padrão. O operator aplica a
opção nativa somente à biblioteca de filmes em `/data/media/movies`, incluindo
bibliotecas existentes com nome diferente. Séries, bibliotecas manuais em outros
paths e a biblioteca de coleções mantêm suas opções.

Execute uma varredura da biblioteca no Jellyfin para criar os agrupamentos
identificados pelo TMDb com pelo menos dois filmes locais. Mantenha a tarefa
periódica de varredura habilitada nas tarefas agendadas do Jellyfin para incluir
futuras importações. Coleções personalizadas podem ser administradas pelo painel;
excluir uma coleção ou remover um filme dela preserva os arquivos.

O worker preenche as capas oficiais ausentes das coleções identificadas pelos
filmes, com `HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED="true"`. A verificação
ocorre a cada 300 segundos; ela preserva capas manuais e coleções bloqueadas.
Uma coleção recém-criada recebe a capa na próxima reconciliação depois da
varredura. Coleções vazias ou com identidades ambíguas precisam de ajuste manual.

## Declarar indexadores

O default `HOMESERVER_PROWLARR_INDEXERS="[]"` deixa as fontes vazias.
Essa chave recebe uma lista JSON de definições suportadas pela versão nativa
do Prowlarr. `name`, `implementation` e `definitionName` identificam a fonte;
`fields` informa valores da definição, como URL ou credencial. Exemplo de
sintaxe para uma definição Cardigann:

```dotenv
HOMESERVER_PROWLARR_INDEXERS='[{"name":"UIndex","implementation":"Cardigann","definitionName":"uindex","fields":[{"name":"baseUrl","value":"https://uindex.org/"}]}]'
HOMESERVER_BYPARR_ENABLED="true"
```

Confira a definição e os campos na instância Prowlarr antes de usar outra fonte.
Nomes/fields desconhecidos retornam unsupported; não invente campos. Preserve
credenciais de trackers diretamente nesse JSON privado, quando necessárias.
Teste a fonte no Prowlarr e confirme sua sincronização no Sonarr/Radarr.
O padrão `RELEASE_INDEXER_PRIORITY=uindex,1337x` habilita somente essas duas
fontes nos três aplicativos. Outras definições existentes são preservadas,
mas ficam desativadas. O worker compara candidatas elegíveis das duas fontes;
a qualidade do perfil precede seeds e, no Dublado, áudio precede qualidade.

Quando Byparr está habilitado, o operator vincula proxy e indexadores pela
tag gerenciada `homeserver-byparr`. Um proxy sem tag correspondente pode
existir e nunca ser escolhido nas buscas. HTTP 429 exige revisar esse vínculo
e respeitar o retry da fonte.

Os indexadores gerenciados habilitam busca interativa e mantêm RSS/busca automática
desabilitados, porque o worker conduz as aquisições com capacidade real e
janela de episódios e importação sequencial. Esses controles não significam
ausência de indexadores.

## Configurar legendas

Informe as credenciais dos providers habilitados:

```dotenv
HOMESERVER_SUBTITLE_PROVIDERS="bazarr,subdl"
HOMESERVER_BAZARR_PROVIDERS="subdl,opensubtitlescom"
HOMESERVER_BAZARR_USE_SONARR="true"
HOMESERVER_SUBDL_API_KEY=""
HOMESERVER_OPENSUBTITLES_USERNAME=""
HOMESERVER_OPENSUBTITLES_PASSWORD=""
```

Preencha os valores vazios se usar esses provedores. Provider habilitado sem
credenciais não comprova disponibilidade. A prioridade é pt-BR mesma release,
pt-BR edição compatível, inglês mesma release e inglês edição compatível.
Original pt-BR pode dispensar legenda somente com evidência nas demais mídias;
valor vazio em `SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` desativa a dispensa.
Dublado e animações sempre seguem essa busca, inclusive com original pt-BR. O perfil
gerenciado do Bazarr mantém `audio_exclude=false`; dublagem não dispensa legenda.

Para séries com busca exclusivamente pelo SubDL do controlador, configure
`HOMESERVER_BAZARR_USE_SONARR="false"`. O reconciliador desativa a integração
Sonarr e o perfil padrão automático de séries no Bazarr, mantendo Radarr,
providers e configuração de filmes. Preserve `subdl` em `SUBTITLE_PROVIDERS`
e a credencial `SUBDL_API_KEY`. A opção permanece `true` por padrão.
Reinicie o Bazarr após aplicar a configuração para remover jobs de séries
registrados anteriormente; confirme que os jobs de sincronização, busca e
indexação de episódios não aparecem no painel de tarefas antes de liberar
aquisições. O job compartilhado de upgrade continua atendendo aos filmes.

O filtro de duração ajuda a distinguir versões base/extended/director cut,
com margem para créditos. Uma legenda de outra release pode ser aceita pela
edição; isso não garante sincronismo perfeito.

## Painéis e verificação

Os [cards do painel](status-dashboard.md) abrem os serviços nos caminhos
do [README](../../README.md#acessar). Acompanhe fontes/importação nos Arr,
indexadores no Prowlarr e fila/peers/velocidade no painel qBit. O proxy qBit
permite todas as ações da interface nativa após login, inclusive alterações
manuais da fila e das preferências.

Um torrent completo ainda pode aguardar validação ou importação. Em séries,
também pode aguardar legenda ou episódios anteriores; filmes podem receber a
legenda depois de aparecer no Jellyfin.
Consulte [diagnóstico](../troubleshooting.md) para o estado detalhado.
