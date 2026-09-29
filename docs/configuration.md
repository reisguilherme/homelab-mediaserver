# Configuração

Copie [`.env.example`](../.env.example) para `.env`. Ele contém preferências
editáveis e credenciais da instalação. Caminhos, portas, UID/GID e URLs internas
são defaults do código/Compose, para que a operação cotidiana se concentre no
comportamento do servidor. Não há arquivos de segredo `*_FILE`.

O catálogo tipado em `homeserver_common.settings` é a referência para nomes,
defaults, tipos e unidades. Consulte-o sem revelar credenciais:

```bash
docker compose run --rm operator config catalog
docker compose run --rm operator config validate --env-file /project/.env
docker compose run --rm operator config show --redacted --env-file /project/.env
```

## Sintaxe e aplicação

Cada linha contém `HOMESERVER_CHAVE=value`. Linhas vazias e comentários iniciados
por `#` são ignorados. O loader trata o conteúdo como dados: não expande `$`,
não executa comandos e não interpreta comentários ao final de valores. Uma
chave duplicada ou desconhecida é erro de validação.

Valores sem aspas perdem espaços externos; aspas simples delimitam texto
literal. Aspas duplas usam escapes JSON. O exemplo usa strings JSON:

```dotenv
HOMESERVER_MOVIE_RESOLUTIONS="2160,1080"
HOMESERVER_SERIES_RESOLUTIONS="1080"
HOMESERVER_ADMIN_PASSWORD="preencha sua senha"
HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=""
```

Não execute `source .env` nem `eval`. Guarde o arquivo com permissão `0600`;
ele está ignorado pelo Git. Os consumidores rodam UID 1000 e precisam ler o
arquivo; preserve esse owner ou adapte a identidade estrutural no Compose.
O operator escreve API keys adotadas no mesmo
arquivo, sem criar arquivos de segredo separados. Veja
[o procedimento de alteração](operator-guide.md#aplicar-uma-mudança).
Os Arr e o Seerr usam o perfil `HomeServer`. Em uma instalação antiga que ainda
associe mídias a outro perfil, selecione `HomeServer` no editor de séries/filmes
para aplicar também os filtros nativos a esses cadastros.

## Downloads e seeding

| Parâmetro, com prefixo `HOMESERVER_` | Default | Unidade e efeito |
|---|---|---|
| `DOWNLOAD_MAX_ACTIVE` | 10 | Downloads simultâneos globais |
| `SERIES_DOWNLOAD_WINDOW` | 10 | Janela de episódios ainda não baixados por série |
| `SEED_MAX_ACTIVE` | -1 | Torrents em seeding; -1 deixa a quantidade ilimitada |
| `TORRENT_MAX_ACTIVE` | -1 | Total ativo nativo qBit; -1 deixa a quantidade ilimitada |
| `QUEUE_IGNORE_SLOW_TORRENTS` | false | Downloads lentos também contam no limite global |
| `UPLOAD_LIMIT_MBIT` | 20 | Mbit/s; zero é ilimitado |
| `DOWNLOAD_LIMIT_MBIT` | 0 | Mbit/s; zero é ilimitado |
| `TORRENT_MAX_CONNECTIONS` | 500 | Conexões globais |
| `TORRENT_MAX_CONNECTIONS_PER_TORRENT` | 100 | Conexões por torrent |
| `UPLOAD_SLOTS`, `UPLOAD_SLOTS_PER_TORRENT` | 20, 4 | Slots de upload |
| `SEED_RATIO_LIMIT` | -1 | Ratio; -1 desativa o limite |
| `SEED_TIME_LIMIT_MINUTES`, `SEED_INACTIVE_LIMIT_MINUTES` | -1, -1 | Minutos; -1 desativa |

Mbit/s é decimal: 20 Mbit/s equivale a 2.500.000 bytes/s. qBit pode arredondar
para KiB inteiros; o read-back mostra o valor efetivo. O total ativo deve
comportar os limites individuais; se seeding é ilimitado, o total também deve
ser ilimitado. Os limites de ratio/tempo ficam desligados por padrão: a banda
de upload limita o envio, sem restringir a quantidade de arquivos disponíveis.
Contribuir com seeding depende de upload,
peers e disco disponíveis, não somente do limite configurado.

Capacidade considera tamanho solicitado, bytes já baixados, bytes ainda
pendentes e reservas concorrentes no filesystem de mídia. Não existe reserva
fixa de 80 GB por filme nem teto estático de tamanho.

## Qualidade e fontes lentas

| Parâmetro | Default | Efeito |
|---|---|---|
| `MOVIE_RESOLUTIONS` | 2160,1080 | Filmes: 4K com fallback em 1080p |
| `SERIES_RESOLUTIONS` | 1080 | Séries: somente 1080p |
| `MEDIA_SOURCES` | remux,bluray,webdl | Fontes aceitas; WEBRip/TV ficam excluídas |
| `RELEASE_INDEXER_PRIORITY` | uindex,1337x | Fontes nativas habilitadas e ordem de seleção |
| `INDEXER_FALLBACK_MIN_SEEDERS` | 5 | Consultar alternativas quando a fonte principal tem menos seeds anunciados |
| `SERIES_PREFER_SEASON_PACK` | true | Preferir torrent de temporada completa elegível com seeds suficientes |
| `SERIES_RELEASE_AFFINITY` | true | Favorecer o mesmo indexador e família de releases saudáveis na série |
| `PREFER_DOLBY_VISION`, `PREFER_ATMOS` | true, true | Preferências de vídeo/áudio |
| `QUALITY_MIN_MIB_PER_MIN_720` | 10 | Piso MiB/min do vídeo principal |
| `QUALITY_MIN_MIB_PER_MIN_1080` | 20 | Piso MiB/min do vídeo principal |
| `QUALITY_MIN_MIB_PER_MIN_2160` | 50 | Piso MiB/min do vídeo principal |
| `AUTOMATIC_UPGRADES` | false | Atualizações automáticas após aquisição |
| `SOURCE_SLOW_REPLACEMENT_ENABLED` | false | Troca por velocidade desativada; zero progresso ainda pode acionar recuperação |
| `SOURCE_SLOW_WINDOW_SECONDS`, `SOURCE_STALL_SECONDS` | 300, 300 | Janela de lentidão ou falta de progresso |
| `SOURCE_MIN_RATE_KIB` | 1024 | KiB/s; limiar de lentidão |
| `SOURCE_PROBE_SECONDS` | 60 | Medição da candidata |
| `SOURCE_MIN_TIME_GAIN_PERCENT` | 20 | Melhoria mínima do ETA medido |

A seleção avalia UIndex primeiro. Quando não encontra uma release elegível ou
ela tem menos de cinco seeds anunciados, avalia 1337x. As buscas HTTP nativas
dos Arr podem consultar ambos; a inspeção de metadados e a escolha respeitam
essa prioridade. Dentro da fonte, maior resolução permitida e idioma original
declarado precedem seeds; fonte/Dolby Vision/Atmos desempatarão candidatos.
O piso MiB/min continua obrigatório. Filmes elegíveis podem passar à frente por seeds.
Séries podem baixar em paralelo dentro de `SERIES_DOWNLOAD_WINDOW`, respeitando
o limite global. A janela contém os primeiros episódios ainda não baixados:
um episódio sem fonte ou lento não impede buscar os seguintes dessa janela.
Um torrent confirmado com zero bytes restantes no qBit libera sua vaga mesmo
quando aguarda a importação. A janela avança para os próximos episódios e pode
alcançar as temporadas seguintes já solicitadas. Sem evidência atual do qBit,
o episódio continua ocupando a vaga.
Importação/Jellyfin mantém temporada/episódio em ordem: E7 pronto espera E5/E6;
S2 pode baixar quando couber na janela, mas aguarda S1 para ser importada.
Episódios desmonitorados ou futuros conhecidos não bloqueiam a cadeia.
Um episódio completo aguardando importação não conta como download ativo.
Seeds anunciados não garantem velocidade real.

Séries favorecem a mesma família (grupo de release e origem, como AMZN/DSNP)
quando ela tem seeds suficientes; a afinidade não prende a série a uma fonte
fraca. Um pacote precisa conter todos os episódios esperados da temporada,
com um vídeo válido por episódio e piso de qualidade individual. É uma única
transferência e uma única reserva do tamanho total, incluindo episódios já
baixados separadamente se o pacote os contém. Os vínculos desses episódios
preservam os arquivos existentes. Cada importação aponta somente ao arquivo
do próximo episódio, mantendo a ordem mesmo quando o pacote baixa em paralelo.
Pacotes com caminhos que colidem com uma fonte preservada são recusados;
nesses casos a aquisição continua por episódio.
Ao excluir um episódio pelo Jellyfin, o fluxo remove sua entrada e arquivo
da biblioteca e dos serviços. Um pacote compartilhado permanece no qBit
enquanto serve outros episódios; o espaço físico dessa fonte só é liberado
depois que todos os episódios vinculados forem explicitamente excluídos.
Nesse momento o gateway revalida o pacote e remove seu torrent e arquivo.

Magnets têm seus trackers incorporados aos metadados verificados antes do
envio ao qBit. O conteúdo identificado pelo infohash permanece igual.

Os pisos recusam encodes muito pequenos, usando bytes do vídeo principal e
duração declarada, sem somar samples/sidecars. Tamanho não garante qualidade
visual. Zero desativa o piso daquela resolução; duração desconhecida não
comprova um piso habilitado. Essa regra vale também para candidatas de failover,
sem apagar arquivos existentes.

Com `SOURCE_SLOW_REPLACEMENT_ENABLED=false`, a velocidade baixa não dispara
troca. Se a flag for habilitada, após cinco minutos lento o worker busca e mede uma candidata
mantendo o download atual. Promoção exige qualidade/edição compatíveis,
capacidade conjunta e previsão de término melhor. Pausas por operador/capacidade
e torrents completos não disparam troca. Ausência sustentada de progresso
continua sujeita à recuperação após `SOURCE_STALL_SECONDS`. Alterações da política orientam
seleções futuras; não autorizam cancelar aquisições em andamento.
Essa recuperação automática atende torrents individuais. Um pacote de temporada
já admitido ainda requer intervenção se sua fonte ficar sem progresso.

## Idiomas de áudio e legenda

`AUDIO_LANGUAGES` aceita lista ordenada, por exemplo `original,pt-BR,en-US`.
Ela considera somente idiomas declarados nos metadados da release.
`original` exige contexto de idioma original do filme/série fornecido pelo Arr.
Sem contexto ou metadados, não há pontuação de idioma; a release continua
elegível pelos demais critérios. Nome do arquivo, título e ID numérico isolado
não comprovam idioma. `pt` genérico não comprova a região brasileira.

`SUBTITLE_LANGUAGES` aceita `pt-BR,en-US`. Com
`SUBTITLE_MATCH_MODES=release,compatible_edition`, a ordem padrão é:

1. pt-BR da mesma release.
2. pt-BR de edição compatível.
3. Inglês da mesma release.
4. Inglês de edição compatível.

O filtro de edição evita legendas de extended/director cut em versão base,
usando metadados e cobertura temporal, com
`SUBTITLE_CREDITS_MARGIN_SECONDS=600` por padrão. Não exige o mesmo nome de
release e não garante sincronismo perfeito. `SUBTITLE_ALLOW_GENERIC_ENGLISH=true`
aceita inglês genérico quando o provedor não declara região. pt-PT não é pt-BR.

`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita somente `pt-BR` nesta versão.
Dispensa exige evidência de áudio original brasileiro na mídia; dublagem ou
português genérico não bastam. Valor vazio desativa a dispensa.

## Indexadores, provedores e credenciais

`PROWLARR_INDEXERS` começa como `[]`. Cadastre UIndex e 1337x com suas
definições nativas conforme [o guia dos serviços](runbooks/service-setup.md).
`RELEASE_INDEXER_PRIORITY` limita tanto a escolha do worker quanto os
indexadores habilitados no Prowlarr/Arr; os demais ficam desativados, sem apagar
suas configurações. `MEDIA_RESOLUTIONS` é um alias legado de `MOVIE_RESOLUTIONS`;
novas instalações usam as duas preferências de resolução separadas.
`BYPARR_ENABLED` controla o proxy para desafios Cloudflare. Provedores de
legendas usam `SUBTITLE_PROVIDERS` e `BAZARR_PROVIDERS`, com suas credenciais:

- `SUBDL_API_KEY`.
- `OPENSUBTITLES_USERNAME` e `OPENSUBTITLES_PASSWORD`.
- API keys de Sonarr, Radarr, Prowlarr, Bazarr, Jellyfin e Seerr.
- `ARR_TOKEN`, `ADMIN_TOKEN` e `CSRF_TOKEN` do controlador.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `QBIT_USERNAME` e `QBIT_PASSWORD`.

Todos usam o prefixo `HOMESERVER_` e ficam diretamente no `.env`.
Jellyfin/Seerr podem ter key vazia no primeiro bootstrap. Senhas de contas
existentes não são redefinidas silenciosamente.

## Monitoramento e invariantes

`METRICS_INTERVAL_SECONDS` define a coleta; `METRICS_MAX_AGE_SECONDS` define
quando dados ficam antigos. `LOG_LEVEL`, `LOG_MAX_SIZE_MB` e `LOG_MAX_FILES`
controlam os logs próprios. Alertas são opcionais por `ALERTS_ENABLED`,
`ALERT_WEBHOOK_URL` e `ALERT_RETRY_SECONDS`.

Completed Download Handling permanece desabilitado nos Arr
(`enableCompletedDownloadHandling=false`) e hardlinks habilitados
(`copyUsingHardlinks=true`). Essas guardas são internas: o worker valida,
resolve legenda e importa antes de publicar no Jellyfin. O apply preserva IDs;
verify detecta alterações divergentes feitas nas UIs nativas.

Portas e paths atuais estão em [instalação](installation.md) e
[README](../README.md#acessar). Para mudar uma convenção estrutural, adapte o
Compose/código de forma explícita, mantendo as redes internas e o gateway.
