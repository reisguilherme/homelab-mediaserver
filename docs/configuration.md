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
HOMESERVER_MEDIA_RESOLUTIONS="2160,1080"
HOMESERVER_ADMIN_PASSWORD="preencha sua senha"
HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=""
```

Não execute `source .env` nem `eval`. Guarde o arquivo com permissão `0600`;
ele está ignorado pelo Git. Os consumidores rodam UID 1000 e precisam ler o
arquivo; preserve esse owner ou adapte a identidade estrutural no Compose.
O operator escreve API keys adotadas no mesmo
arquivo, sem criar arquivos de segredo separados. Veja
[o procedimento de alteração](operator-guide.md#aplicar-uma-mudança).

## Downloads e seeding

| Parâmetro, com prefixo `HOMESERVER_` | Default | Unidade e efeito |
|---|---|---|
| `DOWNLOAD_MAX_ACTIVE` | 4 | Downloads simultâneos globais |
| `SERIES_DOWNLOAD_WINDOW` | 4 | Janela de episódios ainda não importados por série |
| `SEED_MAX_ACTIVE` | 8 | Torrents em seeding simultâneos |
| `TORRENT_MAX_ACTIVE` | 12 | Total ativo nativo qBit |
| `UPLOAD_LIMIT_MBIT` | 20 | Mbit/s; zero é ilimitado |
| `DOWNLOAD_LIMIT_MBIT` | 0 | Mbit/s; zero é ilimitado |
| `TORRENT_MAX_CONNECTIONS` | 500 | Conexões globais |
| `TORRENT_MAX_CONNECTIONS_PER_TORRENT` | 100 | Conexões por torrent |
| `UPLOAD_SLOTS`, `UPLOAD_SLOTS_PER_TORRENT` | 20, 4 | Slots de upload |
| `SEED_RATIO_LIMIT` | -1 | Ratio; -1 desativa o limite |
| `SEED_TIME_LIMIT_MINUTES`, `SEED_INACTIVE_LIMIT_MINUTES` | -1, -1 | Minutos; -1 desativa |

Mbit/s é decimal: 20 Mbit/s equivale a 2.500.000 bytes/s. qBit pode arredondar
para KiB inteiros; o read-back mostra o valor efetivo. O total ativo deve
comportar os limites individuais. Contribuir com seeding depende de upload,
peers e disco disponíveis, não somente do limite configurado.

Capacidade considera tamanho solicitado, bytes já baixados, bytes ainda
pendentes e reservas concorrentes no filesystem de mídia. Não existe reserva
fixa de 80 GB por filme nem teto estático de tamanho.

## Qualidade e fontes lentas

| Parâmetro | Default | Efeito |
|---|---|---|
| `MEDIA_RESOLUTIONS` | 2160,1080 | Resoluções aceitas em ordem de preferência |
| `MEDIA_SOURCES` | remux,bluray,webdl | Fontes aceitas; WEBRip/TV ficam excluídas |
| `PREFER_DOLBY_VISION`, `PREFER_ATMOS` | true, true | Preferências de vídeo/áudio |
| `QUALITY_MIN_MIB_PER_MIN_720` | 10 | Piso MiB/min do vídeo principal |
| `QUALITY_MIN_MIB_PER_MIN_1080` | 20 | Piso MiB/min do vídeo principal |
| `QUALITY_MIN_MIB_PER_MIN_2160` | 50 | Piso MiB/min do vídeo principal |
| `AUTOMATIC_UPGRADES` | false | Atualizações automáticas após aquisição |
| `SOURCE_SLOW_WINDOW_SECONDS`, `SOURCE_STALL_SECONDS` | 300, 300 | Janela de lentidão ou falta de progresso |
| `SOURCE_MIN_RATE_KIB` | 1024 | KiB/s; limiar de lentidão |
| `SOURCE_PROBE_SECONDS` | 60 | Medição da candidata |
| `SOURCE_MIN_TIME_GAIN_PERCENT` | 20 | Melhoria mínima do ETA medido |

Fonte, resolução, Dolby Vision e Atmos precedem idioma de áudio e seeds na
escolha da release. Filmes elegíveis podem passar à frente por seeds.
Séries podem baixar em paralelo dentro de `SERIES_DOWNLOAD_WINDOW`, respeitando
o limite global. A janela contém os primeiros episódios ainda não importados:
um episódio sem fonte ou lento não impede buscar os seguintes dessa janela.
Importação/Jellyfin mantém temporada/episódio em ordem: E7 pronto espera E5/E6;
S2 pode baixar quando couber na janela, mas aguarda S1 para ser importada.
Episódios desmonitorados ou futuros conhecidos não bloqueiam a cadeia.
Um episódio completo aguardando importação não conta como download ativo.
Seeds anunciados não garantem velocidade real.

Os pisos recusam encodes muito pequenos, usando bytes do vídeo principal e
duração declarada, sem somar samples/sidecars. Tamanho não garante qualidade
visual. Zero desativa o piso daquela resolução; duração desconhecida não
comprova um piso habilitado. Essa regra vale também para candidatas de failover,
sem apagar arquivos existentes.

Após cinco minutos lento ou sem progresso, o worker busca e mede uma candidata
mantendo o download atual. Promoção exige qualidade/edição compatíveis,
capacidade conjunta e previsão de término melhor. Pausas por operador/capacidade
e torrents completos não disparam troca. Alterações da política orientam
seleções futuras; não autorizam cancelar aquisições em andamento.

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

`PROWLARR_INDEXERS` começa como `[]`. Configure fontes públicas e suas
definições nativas conforme [o guia dos serviços](runbooks/service-setup.md).
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
