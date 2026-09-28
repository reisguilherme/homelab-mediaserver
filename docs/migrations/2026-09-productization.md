# Migração para a configuração canônica

Este guia transfere a intenção dos arquivos antigos para um `.env` privado
validado. A [auditoria](../audits/2026-09-28-homeserver-productization-audit.md)
descreve o baseline anterior; o [registro de aceite](../evidence/productization-acceptance.md)
descreve as verificações posteriores. O primeiro boot CPU isolado dos sete
serviços e sua reaplicação foram exercitados; isso não significa que a produção
antiga foi adotada ou que reboot, GPU, rede e reprodução foram aprovados.

## Mapear a intenção existente

Os nomes abaixo omitem `HOMESERVER_` para caber na tabela. Use o prefixo em
todas as chaves do `.env`; famílias como `*_PORT` representam as chaves concretas
do [catálogo](../configuration.md), não uma chave literal. O arquivo passado
por `--env-file` é explícito: `server.env` e `deploy.env` não são mesclados
automaticamente, e o ambiente do shell não substitui seus valores.

| Origem antiga | Destino canônico | Consumidor | Verificação |
|---|---|---|---|
| `server.env` / `deploy.env`: identidade, paths, `HOMESERVER_ROOT` e `COMPOSE_PROJECT_NAME` | `INSTANCE_NAME`, `INSTALL_ROOT`, `APPDATA_ROOT`, `MEDIA_ROOT`, `TRANSCODE_ROOT`, `RUN_ROOT`, `BACKUP_STAGING_ROOT`, `TIMEZONE` | Loader, install, Compose, units | `config validate`, `doctor`, plano de paths e projeto |
| `ARR_UID/GID`; `HOMESERVER_RUNTIME` usado por templates antigos | `SERVICE_UID/GID`; importar cada root explicitamente, sem alias automático de runtime | Bind mounts e processos | `id`, permissões no host, `install plan` |
| Override: portas, endereços, redes de egress e URLs do painel | `ACCESS_MODE`, `LAN_BIND_IP`, `TAILSCALE_BIND_IP`, `TAILSCALE_HOSTNAME`, `*_PORT`, `*_PUBLIC_URL`, `QBIT_PEER_PORT/BIND_IP`; redes nos templates versionados | Renderer e dashboard | Plano/render privado, Compose, teste real de acesso; monitor qBit somente leitura |
| Override: binds de Seerr/Prowlarr e demais aplicativos | `APPDATA_ROOT` e layout versionado por serviço; mídia compartilhada em `/data` | Compose e serviços nativos | Conferir origem dos binds e IDs/bancos; `config verify` após ativação |
| Override: device Intel e grupos do host | `TRANSCODE_MODE`, `INTEL_RENDER_DEVICE`, `RENDER_GID`, `VIDEO_GID`, `TRANSCODE_THREADS` | Preflight e Jellyfin | CPU por padrão; device/GIDs e playback Intel no host quando habilitado |
| Envs/arquivos de segredo e credenciais nativas existentes | `*_API_KEY`, `QBIT_USERNAME/PASSWORD`, `ARR_TOKEN`, `ADMIN_TOKEN`, demais segredos ou suas formas `_FILE` | Operator e clientes internos | Importar valores reais privadamente; plan/read-back sem reset de contas |
| Preferências qBit persistidas, fora do env antigo | `DOWNLOAD_MAX_ACTIVE`, `SEED_MAX_ACTIVE`, `TORRENT_MAX_ACTIVE`, `QUEUE_IGNORE_SLOW_TORRENTS`, `UPLOAD_LIMIT_BYTES`, `DOWNLOAD_LIMIT_BYTES`, conexões/slots e limites de seed | Reconciliador qBit | Leitura nativa antes/depois, incluindo arredondamento KiB/s |
| `WORKER_INTERVAL`, `SOURCE_SLOW_SECONDS` e constantes de busca/failover | `WORKER_INTERVAL_SECONDS`, `SOURCE_SLOW_WINDOW_SECONDS`, demais `SOURCE_*`, `HTTP_TIMEOUT_SECONDS`, `SEARCH_TIMEOUT_SECONDS`, `WORKER_CYCLE_TIMEOUT_SECONDS` | Worker e clientes HTTP | Plan, heartbeat/prontidão e testes de timeout/fonte |
| Política antiga em YAML e preferências manuais de qualidade/legenda | `MEDIA_RESOLUTIONS`, `MEDIA_SOURCES`, `QUALITY_MIN_MIB_PER_MIN_*`, preferências Dolby, `AUDIO_LANGUAGES`, `SUBTITLE_*`, `BAZARR_PROVIDERS` | Política, Arr, Bazarr e worker | Perfil/diff/read-back; não importar tetos antigos de filme/episódio ou reserva fixa |
| Importação automática/cópia configuradas na UI Arr | Invariantes `enableCompletedDownloadHandling=false` e `copyUsingHardlinks=true`; sem chave editável | Operator e validação/importação pelo worker | Config plan/apply/verify, preservando IDs e chaves não gerenciadas |
| Aquisições que devem permanecer intactas na transição | `SOURCE_PROTECTED_HASHES`, lista de infohashes SHA1 de 40 hexadecimais | Worker, gateway e proteção SQLite | Persistência antes da admissão; ausência de probe, troca, pausa/retomada ou reordenação da fonte protegida |
| Métricas do override/drop-in e paths internos de DB/snapshots | `NETWORK_INTERFACE`, `METRICS_*`, `CAPACITY_SNAPSHOT_MAX_AGE_SECONDS`, `WORKER_HEARTBEAT_MAX_AGE_SECONDS`; paths internos derivados dos roots | Métricas, saúde e admissão | Snapshots recentes do UUID correto e readiness; não reaproveitar medições antigas |
| `HOMESERVER_RESTIC_REPOSITORY/PASSWORD_FILE`, agenda/transportes em units ou `deploy.env` | `BACKUP_ENABLED`, `BACKUP_REPOSITORY/PASSWORD_FILE`, `BACKUP_TARGET_REPOSITORY/PASSWORD_FILE`, `BACKUP_SSH_*`, `BACKUP_SCHEDULE`, `BACKUP_KEEP_*`, `BACKUP_STALE_HOURS`, `BACKUP_STAGING_MAX_GIB` | Restic e timers | Backup/check/restore isolado; destino externo validado separadamente |
| Drop-in de restart e logging do Compose | `SUPERVISOR_*`, `LOG_LEVEL`, `LOG_MAX_SIZE_MB`, `LOG_MAX_FILES`; units geradas | Supervisor e logging | Units efetivas, recuperação limitada e respeito ao UUID/manutenção |
| `config/versions.env` e tags/overrides de imagens | Manifesto de release com SHA, checksums e digests | Build, deploy e rollback | Identidade das imagens e compatibilidade de schema; sem override editável concorrente |
| CYD/MQTT exclusivo, ACLs e mounts associados | Retirados do runtime atual; métricas/painel HTTP permanecem | Renderer e telemetria | Stack sem broker/firmware; preservar dados/backups antigos até revisão |

O loader reconhece os aliases prefixados `ARR_UID/GID`, `WORKER_INTERVAL`,
`SOURCE_SLOW_SECONDS`, `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE` e
`TELEMETRY_PORT` (para `STATUS_PORT`). Alias conflitante é erro. Variáveis
`RESTIC_*` sem prefixo e campos de terceiros não são importação automática;
traduza-os explicitamente. Chaves `HOMESERVER_` desconhecidas são recusadas.
Nomes internos antigos de DB/snapshots/recovery podem ser aceitos, mas não
controlam os paths efetivos. `HOMESERVER_ROOT` exige transcrição para
`HOMESERVER_INSTALL_ROOT`; `HOMESERVER_RELEASE_MANIFEST` vira argumento
`--manifest`, não configuração do loader. Nomes sem prefixo usados no override,
como `QBITTORRENT_PEER_PORT`, devem ser traduzidos para as chaves do catálogo
(`HOMESERVER_QBIT_PEER_PORT` nesse caso). Nunca concatene ou execute os envs
antigos com `source`/`eval`.

## Preparar a adoção

1. Registre privadamente a release atual, comando/projeto Compose, arquivos
   override, units/drop-ins, roots, UUID, UID/GID, IDs nativos, preferências e
   localização dos backups. Não publique inventário, tokens, hashes ou mídia.
   Confira a SHA pretendida e a branch remota antes da integração; trocar o
   nome de uma branch não comprova que o código foi consolidado.
2. Faça e verifique um backup consistente com o fluxo atualmente ativo antes
   de trocar sua gestão. Preserve envs, overrides, units e release de retorno
   em armazenamento privado. O backup novo só controla o projeto declarado
   em `INSTALL_ROOT/shared/compose.json`; ele não descobre escritores estrangeiros.
3. Gere um `.env` novo e importe os valores reais, conforme
   [instalação](../installation.md#criar-a-configuração-privada). Preserve os
   paths e IDs atuais. Init gera credenciais para instâncias novas; elas não
   correspondem automaticamente às contas existentes.
4. Registre `SOURCE_PROTECTED_HASHES` antes de iniciar o novo worker. A proteção
   é persistida em SQLite e também consultada pelo gateway; retire uma exceção
   somente após reconciliação explícita. Remover um hash do env não apaga sua
   proteção persistida. Não emita comandos individuais de pausa, retomada,
   reordenação ou troca da fonte protegida durante a migração e não estenda sua
   exceção aos próximos episódios.

Importe `AUDIO_LANGUAGES` como preferência, sem transformar título ou idioma
desconhecido em prova. `original` usa o contexto original do Arr. A dispensa
de legenda aceita apenas `SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=pt-BR`, ou
vazio para desativar, e exige evidência no arquivo. Mudanças na política de
qualidade/idioma não cancelam aquisições existentes. Uma ação pontual autorizada
sobre um hash protegido exige reconciliação explícita; não retire sua proteção
persistida apenas para fazer a troca de gestão.

No checkout sincronizado com `uv sync --frozen`:

```bash
sudo .venv/bin/python scripts/homeserver config validate --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config show --redacted --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver doctor --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver install plan --env-file /etc/homeserver/.env --mode adopt
```

Adopt preserva arquivos nativos existentes, mas não importa todos os ajustes do
servidor nem substitui Compose/units antigos sem prova de propriedade no journal.
Se o plano recusar um arquivo gerado desconhecido, compare-o e arquive-o
privadamente após revisar sua intenção. Não fabrique checksums nem apague o
journal para forçar overwrite. Pare a stack/supervisão antiga e os timers
concorrentes antes da troca; parar um unit antigo sozinho pode deixar
containers gravando. Confirme que os escritores antigos pararam pelo projeto
Compose efetivo. Resolva drop-ins antigos para que não substituam os units novos.
Não há conversão automática de layout nem cópia de bancos enquanto ativos.

```bash
sudo .venv/bin/python scripts/homeserver install apply --env-file /etc/homeserver/.env --mode adopt
```

Apply prepara arquivos, diretórios e units, sem ativar uma release. A ativação
usa [deploy com artefato/manifesto](../runbooks/releases.md). Antes de liberar
admissão, exija backup verificado, guarda UUID, config verify, smoke, heartbeat
e snapshots atuais. Repita plan/apply/verify e confirme IDs sem duplicação.

Adopt preserva os limites efetivos: importe bandwidth em bytes/s. Em qBit 5.1.2,
20 Mbit/s solicitados representam 2.499.584 B/s efetivos; `UPLOAD_LIMIT_BYTES=-1`
deriva de Mbit/s. Os defaults 4 downloads/8 seedings/12 ativos são para instalação
nova, não uma instrução para substituir valores existentes sem diff. Torrents
lentos excluídos da contagem fazem o total ativo diferir do número carregado.

## Backup e retorno

Após a definição canônica estar preparada e os escritores antigos parados:

```bash
sudo .venv/bin/python scripts/homeserver backup create --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup verify --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup copy --env-file /etc/homeserver/.env
```

A mídia não está no snapshot. A captura inclui appdata, o env indicado em
`config/operator.env` e releases; não inclui automaticamente todo `/etc/homeserver`
nem arquivos externos referenciados por `_FILE` ou chaves SSH. Preserve esses
arquivos privados e as senhas dos repositórios separadamente.

Copy usa um repositório Restic independente, mas copiar/check não converte
snapshots legados. O restore novo exige `backup-manifest.json` e `appdata/control`;
verify/latest selecionam a tag `homeserver`. Backups antigos com tag/layout
diferentes precisam do procedimento de restore correspondente à versão que os
produziu. Preserve os repositórios antigos até validar recuperação dos snapshots
necessários. Uma chave restrita ao rsync antigo não comprova acesso SFTP ao novo
transporte. Consulte [backup/restore](../runbooks/backup-restore.md).

```bash
sudo bash scripts/rollback.sh --env-file /etc/homeserver/.env --release SHA_ANTERIOR_DE_40_HEXADECIMAIS
sudo .venv/bin/python scripts/homeserver backup restore --env-file /etc/homeserver/.env \
  --snapshot ID_DO_SNAPSHOT --target /srv/restore-isolado
```

Rollback requer uma release verificada compatível com o banco atual, preserva
segredos atuais e não faz downgrade SQLite. Uma release legada sem manifesto
compatível não se torna alvo válido por existir no diretório de releases.
Falha durante ativação mantém manutenção/recovery e admissão bloqueada; SHA,
manifesto ou schema incompatíveis podem ser recusados antes dessa etapa.
Restore só publica uma
árvore isolada vazia; reconcilie UUID atual, mídia, tombstones, reservas e efeitos
externos antes de qualquer promoção. Voltar ao runtime legado exige revisão de
compatibilidade e dos arquivos privados preservados, sem copiar bancos antigos
por cima dos ativos. Siga [recuperação](../runbooks/recovery.md).
