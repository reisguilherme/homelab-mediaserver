# Configurar os serviços

Comece pelo [guia de instalação](../installation.md). O serviço init prepara
diretórios/configurações ausentes e o operator conecta as APIs, aplica
preferências e confirma valores. Bancos existentes e IDs são preservados.

```bash
docker compose run --rm operator config plan --env-file /project/.env --in-container
docker compose run --rm operator config apply --env-file /project/.env --in-container
docker compose restart control-api control-worker download-gateway telemetry host-metrics
docker compose run --rm operator config verify --env-file /project/.env --in-container
```

## Recursos gerenciados

| Serviço | Configuração coordenada |
|---|---|
| qBittorrent | Downloads/seeding, banda, conexões, slots e limites de seed |
| Sonarr/Radarr | Rootfolders `/data/media/tv` e `/data/media/movies`, perfil HomeServer, qualidades e cliente HomeServer Gateway |
| Prowlarr | Conexões Arr, indexadores declarados e vínculo com proxy Byparr |
| Bazarr | Conexões Arr, providers e idiomas |
| Jellyfin | Bibliotecas por path, conta inicial e permissão opcional de exclusão |
| Seerr | Jellyfin, bibliotecas, conexões Arr e perfis pelos IDs reais |

Sonarr/Radarr usam o gateway como cliente; a API qBit é interna. Completed
Download Handling fica desabilitado e hardlinks habilitados. O worker coordena
validação, legenda e importação antes de publicar a mídia.

Contas existentes não recebem reset silencioso. Perfil/biblioteca estrangeiros
não são renomeados ou removidos. Credenciais ausentes/incorretas exigem preencher
o `.env` com os valores reais e repetir o apply.

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
mas ficam desativadas. UIndex é avaliado primeiro pelo worker; 1337x serve de
alternativa se não houver release elegível ou houver poucos seeds anunciados.

Quando Byparr está habilitado, o operator vincula proxy e indexadores pela
tag gerenciada `homeserver-byparr`. Um proxy sem tag correspondente pode
existir e nunca ser escolhido nas buscas. HTTP 429 exige revisar esse vínculo
e respeitar o retry da fonte.

O perfil HomeServer habilita busca interativa e mantém RSS/busca automática
desabilitados, porque o worker conduz as aquisições com capacidade real e
janela de episódios e importação sequencial. Esses controles não significam
ausência de indexadores.

## Configurar legendas

Informe as credenciais dos providers habilitados:

```dotenv
HOMESERVER_SUBTITLE_PROVIDERS="bazarr,subdl"
HOMESERVER_BAZARR_PROVIDERS="subdl,opensubtitlescom"
HOMESERVER_SUBDL_API_KEY=""
HOMESERVER_OPENSUBTITLES_USERNAME=""
HOMESERVER_OPENSUBTITLES_PASSWORD=""
```

Preencha os valores vazios se usar esses provedores. Provider habilitado sem
credenciais não comprova disponibilidade. A prioridade é pt-BR mesma release,
pt-BR edição compatível, inglês mesma release e inglês edição compatível.
Original pt-BR pode dispensar legenda somente com evidência; valor vazio em
`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` desativa a dispensa.

O filtro de duração ajuda a distinguir versões base/extended/director cut,
com margem para créditos. Uma legenda de outra release pode ser aceita pela
edição; isso não garante sincronismo perfeito.

## Painéis e verificação

Os [cards do painel](status-dashboard.md) abrem os serviços nas portas fixas
do [README](../../README.md#acessar). Acompanhe fontes/importação nos Arr,
indexadores no Prowlarr e peers/velocidade no monitor qBit. O proxy qBit é
somente leitura, inclusive quando sua interface mostra botões de alteração.

Um torrent completo ainda pode aguardar validação, legenda ou importação.
Consulte [diagnóstico](../troubleshooting.md) para o estado detalhado.
