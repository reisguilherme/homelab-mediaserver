# Guia do operador

Edite apenas o `.env` privado, por exemplo `/etc/homeserver/.env`. O catálogo
em [configuration.md](configuration.md) mostra tipos, defaults, unidade e
consumidor. Compose, operator.env, units e configurações nativas são derivados.

## Aplicar uma mudança

No checkout com `.venv` sincronizado:

```bash
sudoedit /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config validate --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config plan --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config apply --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config verify --env-file /etc/homeserver/.env
```

Em produção, o host executa configuração nativa no container operator conectado
às redes internas. O diff limita-se às chaves próprias e oculta segredos. Apply
faz read-back, preserva IDs existentes e permite retomar uma execução parcial.
Atualizações de runtime devem conservar os digests da release ativa. Mudanças
de configuração não são autorização para limpar mídia ou contornar o gateway.

Exit codes: `0` sucesso; `2` configuração inválida; `3` dependência indisponível;
`4` aplicação parcial, capability não suportada ou drift. Plan pode listar
mudanças com exit 0; verify exige ausência de divergência. Um serviço `unsupported`
precisa de versão/shape/ID compatível, conforme [service-setup](runbooks/service-setup.md).

Contas nativas existentes preservam nomes e senhas; mudar `ADMIN_PASSWORD` não
faz reset silencioso. Conta ausente recebe as credenciais explícitas no primeiro
bootstrap. `JELLYFIN_ENABLE_MEDIA_DELETION` vazio preserva a permissão existente;
`true`/`false` altera somente a permissão de exclusão do usuário configurado,
mantendo ID e demais políticas. Exclusão solicitada no Jellyfin passa pelo proxy
e pela coordenação entre serviços; essa permissão não habilita limpeza automática.

## Downloads, qualidade e seeding

O painel nativo do qBit em `QBIT_MONITOR_PUBLIC_URL` é somente de consulta e
usa o login nativo. O proxy permite os arquivos da interface, APIs de leitura
explícitas e login/logout; adicionar, excluir, iniciar, pausar ou alterar
preferências retorna HTTP 403, mesmo que a interface mostre esses controles.
Downloads passam pelo gateway com reserva; preferências gerenciadas são
aplicadas pelo comando de configuração. O painel não habilita UPnP nem abre
portas no roteador.

Uma instalação nova permite quatro downloads, oito seedings e doze torrents
ativos. Upload padrão é 20 Mbit/s, ou 2.500.000 bytes/s; valores adotados em
`UPLOAD_LIMIT_BYTES` preservam precisão. Mbit/s usa 1.000.000 bits/s, MB usa
1.000.000 bytes, MiB usa 1.048.576 bytes e KiB usa 1.024 bytes. O limiar default
de fonte lenta é 1.024 KiB/s, ou 1 MiB/s. Limite zero de bandwidth significa
ilimitado; `-1` nos limites de seed desabilita o limite correspondente.

O qBit 5.1.2 armazena bandwidth em KiB inteiros: 20 Mbit/s converte para
2.500.000 bytes/s solicitados e 2.499.584 bytes/s efetivos. O plano e o read-back
mostram o valor representável; adoção importa esse valor exato. Valores positivos
abaixo de 1 KiB/s recebem o mínimo nativo de 1.024 bytes/s.

Capacidade é calculada com bytes pendentes reais, reservas concorrentes e
medição recente do filesystem correto. Não há reserva fixa de 80 GB ou limite
de tamanho por filme/episódio. Download bloqueado por capacidade ou recovery
nunca é retomado por uma alteração das preferências nativas do qBit.

Resoluções/fontes permitidas são configuráveis; Dolby Vision/Atmos e seeds
participam da ordenação. Na seleção da release, fonte/resolução/Dolby precedem
idioma de áudio e seeds. Áudio só recebe preferência por metadados declarados;
`original` exige contexto do Arr. A fila pode priorizar filmes elegíveis por
seeds, e séries mantêm temporada/episódio em sequência. Pisos de vídeo por
minuto recusam encodes pequenos nas aquisições e alternativas; consulte
[os parâmetros](configuration.md#parâmetros-de-operação). Mudanças de política
afetam seleções futuras; não cancelam arquivos em andamento.

Sonarr/Radarr mantêm Completed Download Handling desabilitado e hardlinks
habilitados. São guardas internas que preservam validação/importação pelo
worker e seeding sem uma segunda cópia física; `config verify` detecta drift.

Após 300 segundos sem progresso ou com média abaixo do limiar, o worker busca
uma alternativa mantendo a fonte atual. A avaliação mede a candidata por
60 segundos e considera qualidade, edição, capacidade conjunta e ETA. A troca
exige previsão de conclusão pelo menos 20% melhor, não apenas mais seeds ou
velocidade anunciada. Pausas por capacidade/operador e torrents completos não
disparam failover. Hashes em `SOURCE_PROTECTED_HASHES` preservam aquisições
preexistentes protegidas durante a migração.

Legendas seguem quatro prioridades: pt-BR da mesma release, pt-BR de edição
compatível, inglês da mesma release, inglês de edição compatível. Compatibilidade
exige metadados/cobertura verificáveis; legenda genérica não equivale a mesma
release. Áudio original pt-BR pode dispensar legenda com evidência na mídia.
`SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES` aceita somente `pt-BR`; valor vazio
desativa a dispensa. Não há detector de dispensa de áudio original en-US.

## Métricas e supervisão

O painel HTTP é publicado em `STATUS_PUBLIC_URL`; os cards usam as URLs públicas
de cada aplicativo. Host/capacidade são escritos em `RUN_ROOT` e recebidos pela
telemetria. `/health/live` verifica processo; `/health/ready` verifica condições
para operar. Worker usa heartbeat persistente. Snapshot antigo ou recovery
deve aparecer como indisponibilidade, não como medição atual.

O healthcheck Docker do download-gateway consulta `/health/live`: confirma
que o processo HTTP responde. Admissão depende também das verificações de
capacidade, reserva, autenticação e recovery feitas pelos fluxos gerenciados.

```bash
systemctl status homeserver-stack.service homeserver-metrics.service
journalctl -u homeserver-stack.service -u homeserver-metrics.service --since '30 minutes ago'
sudo .venv/bin/python scripts/homeserver doctor --env-file /etc/homeserver/.env
sudo bash scripts/smoke.sh --env-file /etc/homeserver/.env
```

Supervisor tem tentativas limitadas por janela e respeita manutenção/recovery.
`LOG_LEVEL` controla os serviços próprios; URLs HTTP e exceções externas não
são impressas porque podem conter credenciais. `BACKUP_STALE_HOURS` define
quando uma cópia verificada passa a antiga; zero desabilita esse limiar.
O recibo persiste em appdata e o painel mostra sua idade somente com métricas
recentes. Alertas são opt-in por `ALERTS_ENABLED/ALERT_WEBHOOK_URL`, usam outbox
persistente e retry exponencial iniciado em `ALERT_RETRY_SECONDS`.
Ele recupera container encerrado ou unhealthy sem recriar bancos. Antes de um
restart manual, confirme UUID, marcador de manutenção e a razão da falha.
Logs nativos podem conter dados privados: revise e sanitize antes de compartilhar.

## Backup e restore

Configure `BACKUP_ENABLED`, repository, password ou `_FILE`, agenda e retenção.
Repositório local vazio pode ser inicializado pelo projeto; remoto deve ser
preparado explicitamente. Use [o procedimento oficial do Restic](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
para criação/acesso ao destino. SFTP usa chave existente e validação estrita do
host; SSH/Tailscale precisam de verificação real.

```bash
sudo .venv/bin/python scripts/homeserver backup create --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup verify --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver backup copy --env-file /etc/homeserver/.env
systemctl list-timers homeserver-backup.timer homeserver-backup-send.timer
```

Captura para a stack sob manutenção, materializa SQLite sem WAL/SHM, guarda
configuração privada e releases, valida o novo snapshot e depois aplica retenção.
A mídia não está incluída. `backup copy` usa `BACKUP_TARGET_REPOSITORY/PASSWORD`
independentes, valida o destino e aplica retenção própria. Sucesso do backup local
não comprova a cópia externa; confira as duas operações.

```bash
sudo .venv/bin/python scripts/homeserver backup restore --env-file /etc/homeserver/.env \
  --snapshot ID_DO_SNAPSHOT --target /srv/restore-isolado
```

Restore aceita somente destino isolado vazio, fora dos roots ativos, verifica
checksums e bancos e cria `RECOVERY_MODE` com admissão desabilitada. Não inicia
o worker nem adota o UUID antigo. Reconcilie mídia atual, tombstones/cancelamentos,
reservas e efeitos externos antes de promover qualquer estado recuperado.
Consulte [recuperação](runbooks/recovery.md).

## Atualizar e voltar de release

Use `scripts/build-release.sh` em checkout limpo e
`scripts/deploy.sh --env-file ... --release ... --artifact ... --manifest ...`.
A ativação verifica imagens e schema antes de trocar `current`; cada release
mantém sua configuração e manifesto. O [guia de instalação](installation.md)
mostra os argumentos completos.

```bash
sudo bash scripts/rollback.sh --env-file /etc/homeserver/.env --release SHA_ANTERIOR_DE_40_HEXADECIMAIS
```

Rollback exige compatibilidade do banco atual com a release alvo. Ele não
restaura um banco antigo implicitamente. Incompatibilidade exige restore isolado
e reconciliação; uma reserva antiga nunca deve reativar conteúdo excluído.
