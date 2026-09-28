# Instalação em Ubuntu

O alvo é Ubuntu Server 24.04 LTS amd64 com systemd, Python 3.12, uv 0.8.0 e
Docker Engine com Compose v2. O instalador prepara arquivos e diretórios do
projeto em um host existente; pacotes, identidade do operador e disco montado
são pré-requisitos. Fresh e adopt preservam dados e serviços alheios ao projeto.

## Preparar o host

Instale Docker seguindo o [repositório oficial para Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Use o [instalador oficial do uv](https://docs.astral.sh/uv/getting-started/installation/)
na versão **0.8.0**, e deixe `uv` acessível também ao processo root/systemd.
São necessários `git`, `make`, `findmnt`, Python 3.12 e `python3.12-venv`.
Restic é necessário quando backup estiver habilitado; ShellCheck participa do
lint quando instalado.

```bash
python3.12 --version
uv --version
docker compose version
systemctl is-active docker
```

Escolha uma identidade existente para `SERVICE_UID/GID`. Descubra os números com
`id`; o configurador não muda usuários ou permissões de serviços externos.
Monte o filesystem de mídia previamente. Obtenha seu UUID e confirme o mountpoint:

```bash
findmnt --mountpoint /srv/data --output TARGET,UUID,FSTYPE,OPTIONS
bash scripts/check-mount.sh /srv/data UUID_REAL_DO_FILESYSTEM
```

O UUID é do filesystem montado, não um número escolhido para o `.env`. Diretório
existente no disco do sistema não substitui mountpoint. Este fluxo não particiona,
formata, modifica `fstab`, ativa mergerfs/RTX ou altera energia/serviço Lenovo.

## Criar a configuração privada

No checkout da release pretendida:

```bash
uv sync --frozen
sudo install -d -m 0700 /etc/homeserver
sudo .venv/bin/python scripts/homeserver env init --env-file /etc/homeserver/.env --mode prod
sudoedit /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config validate --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config show --redacted --env-file /etc/homeserver/.env
```

Init gera credenciais iniciais em arquivo `0600` e recusa sobrescrever um arquivo
existente. Configure pelo menos `MEDIA_UUID`, roots, `SERVICE_UID/GID`, timezone,
binds e URLs públicas conforme o host. Os nomes completos usam `HOMESERVER_`.
Não execute `.env` como shell: `$`, `#`, espaços e aspas são dados literais.
Segredos podem usar `_FILE`; consulte [a gramática](configuration.md).

Roots padrão do host: `/opt/homeserver`, `/srv/appdata`, `/srv/data`,
`/srv/transcode`, `/srv/backup-staging` e `/run/homeserver`. Nos containers a mídia
é `/data`, estado do controlador `/var/lib/homeserver`. Mantenha torrents e
biblioteca no mesmo filesystem para permitir hardlinks.

`TRANSCODE_MODE=cpu` funciona sem render node. Para Intel, configure
`TRANSCODE_MODE=intel`, `INTEL_RENDER_DEVICE` e os GIDs reais de `render`/`video`.
O preflight exige acesso ao device; confirme QSV/VA-API e reprodução no host
seguindo [o guia de hardware](runbooks/jellyfin-hardware.md).

Para acesso Tailscale, configure `ACCESS_MODE=tailscale`, `TAILSCALE_BIND_IP`,
hostname e URLs públicas; autenticação da tailnet é uma operação do administrador.
Modo LAN exige `ACCESS_MODE=lan` e bind explícito. O monitor qBit continua restrito
ao Tailscale ou loopback e mantém a autenticação nativa. A porta de peers TCP/UDP
é separada da WebUI. Nenhum comando abre portas no roteador.

## Planejar e preparar

```bash
sudo .venv/bin/python scripts/homeserver doctor --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver install plan --env-file /etc/homeserver/.env --mode fresh
sudo .venv/bin/python scripts/homeserver install apply --env-file /etc/homeserver/.env --mode fresh
```

Plan valida dependências, UUID e Intel quando selecionado. Apply cria apenas
diretórios e arquivos próprios, journal de checksums, Compose, operator.env e
units; prepara um runtime bootstrap quando necessário. Arquivos gerados editados
fora do fluxo são recusados. Esta preparação requer deploy para ativar a release.

Para inspeção isolada do render:

```bash
sudo .venv/bin/python scripts/homeserver render --env-file /etc/homeserver/.env --output /etc/homeserver/stack-preview.json
```

O render contém segredos e permanece `0600`; não publicá-lo. Os arquivos ativos
em `INSTALL_ROOT/shared` são derivados e não devem ser editados manualmente.

## Adotar uma instalação existente

Faça backup antes da migração. Preencha um `.env` privado com roots, portas,
usuário, chaves reais de API e credenciais existentes. Preserve os bancos e os
IDs em uso. Não copie inventário bruto para o Git. Importe os limites qBit exatos
em bytes/s; `UPLOAD_LIMIT_BYTES=-1` deriva de `UPLOAD_LIMIT_MBIT` e pode alterar
um arredondamento anterior.

```bash
sudo .venv/bin/python scripts/homeserver install plan --env-file /etc/homeserver/.env --mode adopt
sudo .venv/bin/python scripts/homeserver install apply --env-file /etc/homeserver/.env --mode adopt
```

Adopt não descobre automaticamente todo estado do servidor. Ele aplica o plano
aos arquivos próprios e recusa substituir configurações nativas não gerenciadas.
Credenciais reais devem ser importadas; contas existentes não recebem reset de
senha silencioso. Antes de ativar failover, registre no `.env` privado os hashes
das aquisições preexistentes protegidas pela migração em `SOURCE_PROTECTED_HASHES`.
Uma aquisição já concluída não deve ser reaberta.

## Ativar uma release

Produza o artefato em checkout limpo com credenciais do registry disponíveis:

```bash
bash scripts/build-release.sh --image-prefix ghcr.io/SEU_NAMESPACE/homeserver --output /tmp/homeserver-release
```

Build publica as imagens próprias e resolve upstreams por digest. Transfira
artefato e manifesto ao servidor. Substitua os placeholders pelo SHA completo
e pelos arquivos realmente produzidos:

```bash
sudo bash scripts/deploy.sh --env-file /etc/homeserver/.env \
  --release SHA_DE_40_HEXADECIMAIS \
  --artifact /caminho/homeserver-SHA.tar --manifest /caminho/homeserver-SHA.json
sudo .venv/bin/python scripts/homeserver config plan --env-file /etc/homeserver/.env
sudo .venv/bin/python scripts/homeserver config verify --env-file /etc/homeserver/.env
sudo bash scripts/smoke.sh --env-file /etc/homeserver/.env
systemctl status homeserver-stack.service homeserver-metrics.service
```

Deploy valida checksums/imagens/schema, captura backup quando exigido, aplica
migrações, configura serviços nativos no operator isolado e verifica prontidão.
Jellyfin/Seerr podem descobrir seus tokens reais na primeira configuração;
o host os salva no `.env` privado e refaz os derivados. Falha mantém admissão
bloqueada e exige revisão, sem restaurar bancos silenciosamente.

Ainda é necessário registrar um ensaio completo em host/VM novo: segundo apply
sem recursos duplicados, reboot com disco presente/ausente, backup e restore,
acesso de rede e reprodução. Fixtures locais não cumprem esse aceite físico.

## Deploy manual pelo GitHub Actions

Após um push em `main` com HomeServer CI aprovado, execute manualmente
**Build HomeServer release** com `commit` igual ao SHA completo validado.
Depois execute **Deploy HomeServer** com o mesmo `commit` e `release_run`
igual ao ID da execução de build concluída com sucesso. Deploy baixa o artefato
`homeserver-release-{SHA}` dessa execução e valida sua identidade; não recebe
um caminho arbitrário de artefato.

Configure o environment `production` do repositório com os secrets
`TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`, `HOMESERVER_SSH_KEY`,
`HOMESERVER_SSH_KNOWN_HOSTS`, `HOMESERVER_SSH_HOST` e `HOMESERVER_SSH_USER`.
As variáveis `HOMESERVER_INSTALL_ROOT` e `HOMESERVER_ENV_FILE` usam
`/opt/homeserver` e `/etc/homeserver/.env` quando omitidas. Cadastre o cliente
OAuth e autorize `tag:homeserver-ci` na tailnet administrada; isso exige ação
externa e não é criado pelo instalador. A host key SSH deve ser confirmada
fora do workflow e salva em known_hosts; acesso e confiança não são inferidos.

O host deve ter sido preparado pelo fluxo local, incluindo o runtime bootstrap
em `INSTALL_ROOT/current`. O deploy remoto chama `sudo bash` sobre
`current/scripts/deploy.sh`: ele precisa de execução root não interativa.
Configure uma autorização sudo limitada ao caminho/fluxo de deploy controlado;
uma política que exige senha falha na sessão SSH sem prompt interativo. O `.env`
privado permanece no host e não deve ser cadastrado como artefato de Actions.
