# Configuração

O `.env` privado é a interface do operador. O schema em
`services/common/src/homeserver_common/settings.py` define tipos, defaults, unidades,
consumidores e aplicação. Gere o catálogo com `scripts/homeserver config catalog`
e o exemplo com `scripts/homeserver config catalog --example`. As saídas não incluem
credenciais. O exemplo representa produção e precisa ser preenchido antes da validação.
No checkout com `.venv` sincronizado, prepare o arquivo privado conforme o
[guia de instalação](installation.md#criar-a-configuração-privada):

```bash
sudo .venv/bin/python scripts/homeserver env init --env-file /etc/homeserver/.env --mode prod
sudo .venv/bin/python scripts/homeserver config validate --env-file /etc/homeserver/.env --mode prod
sudo .venv/bin/python scripts/homeserver config show --redacted --env-file /etc/homeserver/.env --mode prod
```

O init gera tokens e senhas iniciais aleatórios, usa permissões `0600` e recusa
sobrescrever um arquivo existente. Informe UUID de mídia, endereço Tailscale e
chaves das integrações obtidas no onboarding. Validação do UUID no `.env` não
comprova montagem física; a guarda no host é obrigatória antes de escrever mídia.
O modo dev exige binds loopback e usa `.runtime/dev` ao lado do `.env` por default.

Cada linha contém `KEY=value`. Linhas vazias e linhas iniciadas por `#` são
ignoradas. Não existem expansão de variáveis, comandos, `export` ou comentários
ao final de uma linha: `$HOME`, `#`, apóstrofos e espaços são dados literais.
Valores sem aspas perdem espaços externos; aspas simples delimitam texto literal.
Aspas duplas usam escapes JSON, por exemplo `"senha $HOME # com ' e \\"aspas\\""`.
A serialização oficial usa strings JSON e preserva espaços e caracteres especiais.
Uma chave duplicada é erro. Chaves `HOMESERVER_` desconhecidas são rejeitadas;
chaves de terceiros são ignoradas. O arquivo explícito prevalece sobre o ambiente
do processo. Nunca execute o arquivo com `source` ou `eval`.

Segredos aceitam `HOMESERVER_<CHAVE>_FILE` em lugar do valor direto. As duas
formas simultâneas são erro, mesmo quando o valor direto está vazio. Caminhos
relativos são resolvidos ao lado do `.env`; apenas a quebra de linha final do
arquivo de segredo é removida. Diagnósticos mostram `[redacted]` ou `[absent]`.

Bandwidth usa Mbit/s decimais: 20 Mbit/s corresponde a 2.500.000 bytes/s; zero
significa ilimitado. `UPLOAD_LIMIT_BYTES` e `DOWNLOAD_LIMIT_BYTES` preservam
arredondamentos de instalações adotadas: `-1` usa a conversão de Mbit/s e valores
não negativos têm precedência. Limites de seed `-1` desabilitam aquele limite.
O total de torrents deve comportar cada limite individual de downloads e seed.

Resoluções e fontes são listas ordenadas separadas por vírgulas. Idiomas de
legenda atualmente suportados são `pt-BR` e `en-US`; fontes são `remux`, `bluray`
e `webdl`. A busca de fonte lenta começa após 300 segundos por default e mantém
a fonte atual durante a avaliação. Caminhos dos containers permanecem `/data`
e `/var/lib/homeserver`; `MEDIA_ROOT` e demais raízes são caminhos do host.

Migração reconhece explicitamente `ARR_UID/GID` como `SERVICE_UID/GID`,
`WORKER_INTERVAL` como `WORKER_INTERVAL_SECONDS`, `SOURCE_SLOW_SECONDS` como
`SOURCE_SLOW_WINDOW_SECONDS`, `RESTIC_REPOSITORY` como `BACKUP_REPOSITORY` e
`RESTIC_PASSWORD_FILE` como `BACKUP_PASSWORD_FILE`. Alias conflitante é erro.
DB, snapshots e marcador `RECOVERY_MODE` são estado interno; seus nomes antigos
são aceitos na importação, mas seus caminhos efetivos são derivados do runtime.


## Parâmetros de operação

| Parâmetro `.env` (prefixo `HOMESERVER_`) | Default | Efeito |
|---|---|---|
| `DOWNLOAD_MAX_ACTIVE`, `SEED_MAX_ACTIVE`, `TORRENT_MAX_ACTIVE` | 4, 8, 12 | Concorrência nativa qBit |
| `MEDIA_RESOLUTIONS` | 2160,1080 | Resoluções permitidas, por preferência |
| `MEDIA_SOURCES` | remux,bluray,webdl | Fontes permitidas; WEBRip/TV ficam excluídas |
| `QUALITY_MIN_MIB_PER_MIN_720` | 10 | Piso de tamanho do vídeo por minuto |
| `QUALITY_MIN_MIB_PER_MIN_1080` | 20 | Piso de tamanho do vídeo por minuto |
| `QUALITY_MIN_MIB_PER_MIN_2160` | 50 | Piso de tamanho do vídeo por minuto |
| `PREFER_DOLBY_VISION`, `PREFER_ATMOS` | true, true | Preferências dentro da qualidade permitida |
| `AUDIO_LANGUAGES` | original | Ordem de preferência dos idiomas declarados pela release |
| `SUBTITLE_LANGUAGES` | pt-BR,en-US | Ordem de idiomas da política |
| `SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` | pt-BR | Dispensa com evidência de áudio original brasileiro; vazio desativa |
| `SOURCE_SLOW_WINDOW_SECONDS`, `SOURCE_MIN_RATE_KIB` | 300, 1024 | Quando procurar alternativa à fonte lenta |
| `SOURCE_PROBE_SECONDS`, `SOURCE_MIN_TIME_GAIN_PERCENT` | 60, 20 | Janela de medida e vantagem de ETA exigida |
| `HTTP_TIMEOUT_SECONDS`, `SEARCH_TIMEOUT_SECONDS` | 15, 90 | Requisições rotineiras e busca Arr |
| `WORKER_CYCLE_TIMEOUT_SECONDS` | 900 | Prazo máximo fixo de cada ciclo; heartbeat não o renova |
| `BACKUP_EXCLUDE_DIRS` | .venv,__pycache__,logs,cache | Diretórios gerados omitidos da captura de backup |

O piso usa MiB (1.048.576 bytes) por minuto da duração informada pelo Arr:
episódio primeiro, duração da série como fallback, e duração do filme no Radarr.
O worker confere o tamanho do vídeo principal nos metadados do torrent;
amostras, legendas e outros arquivos não aumentam esse tamanho. O Arr recebe
o mesmo mínimo em suas definições de qualidade. Um episódio 1080p de 45 minutos
precisa de pelo menos 900 MiB (aproximadamente 944 MB): uma release de 434 MB
é rejeitada, e uma de 1,8 GB passa pelo critério de tamanho. Resolução, fonte,
edição, legenda e capacidade continuam sendo verificadas separadamente.

Tamanho não garante qualidade visual, pois codecs e conteúdo têm eficiências
diferentes. Os pisos são uma proteção contra encodes muito pequenos; altere-os
no `.env` para ajustar a política. Zero desabilita o piso daquela resolução.
Duração ausente não comprova um piso habilitado e impede nova aquisição daquela
candidata. A validação cobre aquisição inicial e alternativas de failover;
não remove arquivos existentes. Não há teto de tamanho: `maxSize` das qualidades
gerenciadas é ilimitado, e capacidade usa os bytes reais pendentes.

## Idiomas de áudio e legenda

`AUDIO_LANGUAGES` aceita uma lista ordenada, por exemplo `original,pt-BR,en-US`.
A preferência considera somente o campo de idiomas declarado nos metadados
da release. `original` exige o idioma original do filme/série informado pelo
Arr. Sem esse contexto, o valor não pontua; sem metadados de idioma, a release
continua elegível pelos demais critérios. Título, nome do arquivo e ID numérico
isolado não estabelecem idioma. Uma preferência regional como `pt-BR` exige
região declarada; `pt` genérico não comprova áudio brasileiro.

Na escolha da release, fonte, resolução, Dolby Vision e Atmos têm precedência
sobre idioma; idioma precede seeds. Isso é preferência, não um requisito que
elimine toda release com idioma desconhecido. A ordem das séries continua
sequencial. Mais seeds anunciados não comprovam maior velocidade real.

Idiomas de legenda suportados são `pt-BR` e `en-US`. A ordem padrão tenta
pt-BR da mesma release, pt-BR de edição compatível, inglês da mesma release e
inglês de edição compatível. `SUBTITLE_ALLOW_GENERIC_ENGLISH=true` permite
inglês genérico quando a fonte não informa região. Identificação da edição e
cobertura temporal evitam versões extended/director cut incompatíveis; a
margem para créditos não garante sincronismo perfeito.

`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita somente `pt-BR` nesta versão.
A dispensa depende de evidência de áudio original brasileiro na mídia;
mera dublagem ou tag portuguesa genérica não a autorizam. Use valor vazio
(`HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=""`) para exigir legenda
também nesse caso. Outros idiomas nessa chave retornam erro de validação.

## Guardas internas de importação

O operator mantém `enableCompletedDownloadHandling=false` e
`copyUsingHardlinks=true` no Sonarr/Radarr. O worker coordena validação de mídia,
legenda e importação; hardlinks preservam o torrent para seeding sem alocar
uma segunda cópia do vídeo. Esses valores são invariantes do fluxo e não
recebem chaves editáveis no `.env`. Mudanças pela UI nativa aparecem como drift
no `config verify` e são reconciliadas pelo `config apply`, com IDs e demais
preferências preservados.

## Diretórios excluídos do backup

`BACKUP_EXCLUDE_DIRS` é uma lista de nomes de diretórios separados por vírgulas.
O padrão `.venv,__pycache__,logs,cache` omite esses diretórios recursivamente em
appdata e releases. Logs e caches são estado gerado; os arquivos originais não
são apagados ou alterados. Arquivos comuns com esses nomes continuam incluídos.
Isso permite capturar o estado do Seerr sem seguir seus links de logs rotativos.

Use `HOMESERVER_BACKUP_EXCLUDE_DIRS=""` para incluir todos os diretórios comuns.
A lista aceita apenas nomes com letras ASCII, números, ponto, hífen e underscore;
caminhos absolutos, barras, `.` e `..` são rejeitados. Symlinks em qualquer estado
incluído continuam impedindo a captura e exigem revisão do operador. A exclusão
não altera a rejeição de symlinks durante a restauração.
