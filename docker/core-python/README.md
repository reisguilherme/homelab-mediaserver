# Imagem Python e Compose de ativação do core

`compose.core.active.yaml` é um manifesto separado, usado sozinho após preparar
as cópias operacionais e os recibos auditados. O manifesto preparado
`compose.core.yaml` continua negando inicialização. Este código não comprova
readmissão das aplicações nem retira manutenção, pausas ou bloqueios de fila.

A receita deriva da imagem Python worker local autenticada. Não instala pacotes,
resolve dependências, inicia serviços ou acessa dados restaurados. O contexto
privado mínimo contém este Dockerfile, `services/common/src`,
`services/control/src` e estes quatro scripts públicos:
`core-storage-guard.py`, `core-python-entrypoint.py`, `core-worker-runtime.py`,
`capacity-snapshot.py`. Excluir caches e arquivos locais; não incluir estado,
arquivos `.env`, backups ou o diretório privado de operação.

A operação confere o ID completo da base local, arquitetura e ausência de
`Config.OnBuild`, e associa uma tag local exclusiva ao ID comprovado. Construir
com `--pull=false --no-cache --build-arg BASE_IMAGE=TAG_LOCAL_VERIFICADA` e este
Dockerfile. Guardar privadamente o ID final, os hashes dos arquivos públicos,
a identidade efetiva da base e a versão do interpretador. As três funções Python
guardadas podem usar o mesmo ID final; cada função exige seu próprio recibo de
ativação. O integration-proxy usa essa imagem com um entrypoint próprio, sem estado.
As dependências permanecem as da base autenticada; não se afirma build bit a bit
reproduzível. As imagens capturadas podem conter ENV privado em suas camadas:
continuam privadas e todos os entrypoints usam `env -i`.

O launcher fica em `/app/scripts/core-python-entrypoint.py` e o interpretador é
`/app/.venv/bin/python`. Seu import de common exige o PYTHONPATH técnico explícito;
por isso o launcher usa `-B`, enquanto os filhos recebem o ambiente filtrado pelo
próprio launcher. Sem `--run`, UUID e pin de recibo, a imagem não inicia a aplicação.
Worker usa root e somente CHOWN,DAC_OVERRIDE,SETUID,SETGID,KILL; API e gateway
usam 1000:1000 sem capabilities. Nenhum script legado de configuração é chamado.

## Entradas do manifesto

Usar arquivo de interpolação privado explícito, sem credenciais de aplicação:
`CORE_BIND_IP`, `CORE_JELLYFIN_IP`, `CORE_FILESYSTEM_UUID`, `CORE_<ROLE>_IMAGE_ID` e
`CORE_<ROLE>_ACTIVATION_SHA256`. `<ROLE>` é o nome do serviço em maiúsculas,
substituindo hífens por underscores. qbit-monitor e integration-proxy exigem
somente IMAGE_ID; os consumidores guardados exigem também o pin do recibo.
Os IDs são 64 hex minúsculos
sem prefixo; o Compose acrescenta `sha256:`. O deploy valida o formato, confere
o ID efetivo contra o recibo e exige o endereço privado literal autorizado da VM;
Compose sozinho não valida essa identidade nem limita o valor ao endereço correto.

Todos os parâmetros são obrigatórios, inclusive os do perfil Byparr, pois Compose
interpola o arquivo antes de selecionar perfis. Não usar placeholders para passar
a renderização. Não publicar qBit8080 ou gateway8081: o único acesso qBit exposto
é o proxy privado em 18080, com autenticação/CSRF nativos preservados. API publica
8080 e as demais interfaces mantêm suas portas privadas conhecidas.

O orçamento padrão soma 3200 MiB, incluindo processos e tmpfs nos limites dos
containers; não representa uma medição de RSS. Byparr acrescenta 768 MiB no perfil
`byparr` e permanece adiado até revisar a RAM disponível e serviços concorrentes
na VM de 4 GiB. Rede core é interna; egress existe para provedores/downloads e
API/worker e integration-proxy que precisam alcançar playback em outra VM.
Gateway e qbit-monitor ficam somente na rede interna. Não há restart automático
nesta ativação inicial.

O `integration-proxy` preserva o endpoint interno 8787 usado pelo Seerr restaurado
e as políticas existentes para Arr/Jellyfin; não apontar Seerr diretamente aos
Arr para contorná-lo. A factory é
`homeserver_control.api.integration_proxy:create_app`, com proxy headers e access
log desativados. Não lê settings, SQLite, mídia ou credenciais próprias; não recebe
volumes nem lease de storage. Seu limite inicial é 128 MiB, sujeito a medição na VM.
Os upstreams Arr permanecem `radarr:7878/radarr/api/v3` e
`sonarr:8989/sonarr/api/v3`. Jellyfin permanece
`http://jellyfin:8096/jellyfin`; um `extra_hosts` exclusivamente nesse serviço
resolve jellyfin para `CORE_JELLYFIN_IP`, o endereço literal privado autorizado da
VM de playback. O deploy confere esse IP. Nenhuma porta do integration-proxy é
publicada; ele mantém a autorização de peers pelo nome `seerr` da rede core.

## Layout obrigatório antes de criar containers

Os binds não criam diretórios ausentes. Native config usa cópias SSD
`/srv/appdata/core/<app>` RW, com Seerr em `/app/config`; Byparr usa tmpfs novo.
`/srv/appdata/core/control` é SQLite/WAL compartilhado RW dos três Python.
Provar acesso root/UID1000, consistência e ausência de referência ao backup antes
de iniciar concorrência. A imagem Caddy deve ser conferida com o binário
`/usr/bin/caddy` e volumes declarados limitados aos tmpfs `/config` e `/data`.

| Fonte SSD | Destino | Permissão |
| --- | --- | --- |
| `/srv/appdata/core-activation/<role>` | `/run/core-activation` | RO; dir root0755, receipt root0444 |
| `/srv/appdata/core-runtime/<role>.env` | `/run/core-runtime.env` | RO; arquivo root:1000 0440, somente o próprio papel |
| `/srv/appdata/core-runtime/status` | `/run/homeserver` | RO; root0755, manutenção/status sem credenciais |
| `/srv/appdata/core-runtime/storage.json` | `/run/homeserver/storage.json` | bind RO explícito; root0444 |
| `/srv/appdata/core-placements` | `/var/lib/homeserver/storage-placements` | root0755; worker RW, API/gateway RO |
| `/srv/appdata/core-capacity` | `/run/core-capacity` | 1000:1000 0750; worker RW, API/gateway RO |

O diretório status precisa conter o mountpoint regular `storage.json` para o
bind sobreposto. O mount do status não expõe o diretório pai com arquivos .env.
Preparar `maintenance` no status antes do primeiro worker; ele não substitui a
auditoria do ledger/fila e não equivale a RECOVERY_MODE. O worker contém um
collector UID1000 no grupo supervisionado e exige a primeira amostra antes do
ciclo; remover manutenção depende da reconciliação operacional já documentada.

Todos os dez consumidores guardados montam RO a lease distinta core-writer,
boot do guest, mountinfo do PID1 e marker ext4. `/data` é RW apenas nos quatro
nativos escritores e worker, RO para API/gateway, ausente nos demais. O guard
contém o grupo ao perder a admissão; a presença dos mounts não prova saúde física.
`/run` tmpfs é root0755 para preservar a confiança dos ancestrais dos inputs.
Os três nativos com Python original montam o script público RO; os quatro
derivados usam o guard da imagem. No deploy, conferir o hash correspondente.

Renderizar com `docker compose --env-file ARQUIVO_PRIVADO -f compose.core.active.yaml
config` antes de qualquer start. Renderização e testes de contrato não substituem
ensaios de processo, configurações nativas reais, disponibilidade de memória,
SQLite, autenticação, pausa manual ou os testes de lease na VM.
