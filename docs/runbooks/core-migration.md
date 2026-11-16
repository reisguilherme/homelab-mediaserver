# Preparação e readmissão do core

**Estado desta revisão: preparação com inicialização negada. O core ainda não
está finalizado nem autorizado a executar aplicações.** O layout aprovado separa
o core de playback e edge: estado no SSD da VM de 4 GiB e dados locais ext4 em
`/srv/external3/homeserver`. Não iniciar o monólito nas novas VMs.

`compose.core.yaml` é independente. Cada serviço troca o entrypoint por
`scripts/core-entrypoint.sh`, que termina com código 78, inclusive com seleção
explícita do serviço, `--check`, argumentos, variáveis de ambiente ou recibos.
Não existe chave de ativação nesta revisão. Os nomes em `command` são identificadores
da preparação, não comandos nativos. O profile organiza a preparação; a recusa
do entrypoint é a barreira. Não substituir o entrypoint para testar a restauração.

Todos os binds estão RO e usam `create_host_path: false`; pastas ausentes causam
falha. Configuração em `/srv/appdata/core/{aplicação}` e dados não recebem escrita.
A rede é interna, sem egress, Docker socket, host network, privilégio ou init da
stack antiga. Healthchecks das imagens estão desativados para não executar código
nativo por esse caminho. A preparação não monta `.env` nem chama config apply.

## Serviços e orçamento inicial

| Serviço | Estado no SSD / destino | Memória máxima | Porta planejada na VM |
| --- | --- | ---: | --- |
| Sonarr | `core/sonarr` → `/config` | 512 MiB | 8989 |
| Radarr | `core/radarr` → `/config` | 512 MiB | 7878 |
| Prowlarr | `core/prowlarr` → `/config` | 256 MiB | 9696 |
| Bazarr | `core/bazarr` → `/config` | 256 MiB | 6767 |
| qBittorrent | `core/qbittorrent` → `/config` | 384 MiB | nenhuma porta nativa publicada |
| Seerr | `core/seerr` → `/app/config` | 384 MiB | 5055 |
| Control API | `core/control` → `/var/lib/homeserver` | 128 MiB | 8080 |
| Control worker | mesmo estado do controlador | 384 MiB | nenhuma |
| Download gateway | mesmo estado do controlador | 192 MiB | somente rede interna |
| Proxy administrativo qBit | configuração ainda pendente | 64 MiB | 18080 |

A soma de limites é 3072 MiB; resta 1 GiB para sistema, Docker e margem. Swap por
container não aumenta esses limites. Isso é orçamento, não medição de consumo ou
prova de capacidade das aplicações restauradas. Validar RSS/OOM e concorrência
durante readmissão gradual antes de afirmar estabilidade.

As portas publicadas exigem `CORE_BIND_IP` explícito. A validação de ativação
deverá aceitar somente o endereço privado aprovado da VM e recusar wildcard,
endereço público e endereço que não pertença ao guest. A interpolação do Compose
sozinha não valida essa política. Não publicar a administração nativa qBit nem
abrir portas no roteador. O proxy autenticado ainda precisa da configuração
revisada e da verificação de autenticação/CSRF antes de operar.

## Imagens e entrypoints: evidência ainda necessária

Copiar `.env.core.example` para configuração privada fora do Git e preencher o
endereço e os dez IDs de imagem verificados, sem o prefixo `sha256:`. O Compose usa
IDs imutáveis e `pull_policy: never`; não há build ou fallback para tags.
Preencher variáveis não libera inicialização.

Para cada papel, conferir o manifesto autenticado do checkpoint, hash do arquivo
de imagem, ID/config digest, arquitetura, usuário, versão do aplicativo e
`Entrypoint`/`Cmd` da imagem recuperada. Comparar também a captura do container e
sua camada gravável: uma imagem base antiga não comprova que o conteúdo executado
foi recuperado. Export de rootfs sem a reconstrução da configuração de imagem não
substitui essa prova. Não usar uma tag pública atual para mascarar imagem ausente.

O repositório histórico usa imagens LinuxServer para Arr/Bazarr/qBit e imagem
Seerr. O ensaio nativo existente demonstra binários diretos de Sonarr e Radarr
em suas fixtures; não prova os entrypoints das imagens privadas restauradas.
Prowlarr, Bazarr, qBit, Seerr e proxy precisam de verificação própria. Os três
papéis Python também precisam da imagem/camada correta, módulos e configuração
privada por papel. A preparação não executa `/init` nem tenta adivinhar lançadores.

## Restauração offline e whitelist

Restaurar somente cópias paradas autenticadas, em destino privado novo, usando o
restaurador aprovado e seu manifesto de hashes/metadados. Os diretórios permitidos
do appdata são `sonarr`, `radarr`, `prowlarr`, `bazarr`, `qbittorrent`, `seerr` e
`control`. Mapear volumes nomeados explicitamente. Preservar bancos e sidecars,
donos, modos, xattrs e timestamps; guardar originais e evidência fora do Git.
Links de logs precisam da revisão do restaurador. Não seguir aliases de estado
ou banco. Perfis históricos de reparo qBit permanecem arquivados e inativos.
Não executar o instalador, bootstrap, config apply ou consolidação legada.

`scripts/audit-core-state.py` é um leitor de evidências das cópias, não um
restaurador nem um modificador de configuração. Ele abre apenas esta whitelist:

| Aplicação | Banco | Configurações fingerprintadas |
| --- | --- | --- |
| Sonarr | `sonarr.db` | `config.xml` |
| Radarr | `radarr.db` | `config.xml` |
| Prowlarr | `prowlarr.db` | `config.xml` |
| Bazarr | `db/bazarr.db` | `config/config.yaml` |
| Seerr | `db/db.sqlite3` | `settings.json` |
| Control | `control.sqlite` | nenhuma; `.env` não é lido |
| qBit | nenhum | `qBittorrent/qBittorrent.conf`, `qBittorrent/qBittorrent-data.conf` |

Exemplo com caminhos genéricos privados, em Linux/Python 3.12; o diretório do
relatório deve existir, pertencer ao usuário executor e ter modo 0700:

```sh
python3 scripts/audit-core-state.py \
  --state-root /private/restored/appdata \
  --application sonarr --application radarr --application prowlarr \
  --application bazarr --application seerr --application control \
  --application qbittorrent --output /private/reports/core-state.json
```

Não apontar esse comando para estado em execução ou mídia. O operador deve
comprovar que são cópias offline; o script não consulta Docker ou processos.
Ele não percorre logs, torrents, payloads, backups ou perfis de reparo. Symlinks
e hardlinks dos arquivos selecionados, arquivos especiais, bancos ausentes,
erros SQLite e limites excedidos impedem a produção de um relatório de sucesso.
Arquivos de configuração ausentes são declarados como ausentes; nenhuma ausência
é tratada como configuração segura.

Cada banco é copiado com `-wal`, `-shm` e `-journal`, quando presentes, para um
diretório temporário privado; somente a cópia é aberta por SQLite. A fonte recebe
leituras e verificações de identidade, sem checkpoint ou escrita pela aplicação.
Leituras podem atualizar atime conforme o filesystem. SQLite usa `quick_check`,
schema real e consultas limitadas. Não é um `integrity_check` completo, não valida
referências funcionais nem autentica a captura. Limites: 4 GiB por arquivo de banco
ou sidecar, 8 GiB totais, configuração 10 MiB, 512 tabelas e 10 mil linhas por
tabela de observações; consultas de cada banco têm prazo de 120 segundos. Há
espaço temporário necessário para o maior banco e seus sidecars.

O relatório novo, modo 0600, contém hashes/tamanhos, nomes de tabelas/colunas,
contagens e flags numéricas observadas em tarefas, clientes, notificações,
indexadores e root folders. Tipos de tarefas só são exibidos quando correspondem
exatamente à lista pública permitida; outros são contados. Caminhos de mídia são
classificados, sem divulgar nomes. Versões numéricas de migração não são a versão
do aplicativo. Configurações são fingerprintadas, sem imprimir valores, tokens,
URLs, nomes de clientes, títulos ou blobs de settings. Mantê-lo privado mesmo assim.

`startup_allowed`, `native_containment_verified`, `restore_provenance_verified`
e `image_versions_verified` permanecem `false`. Sucesso do auditor não congela
scans, importações, RSS, torrents ou callbacks. Ainda é necessário revisar, pelas
versões e schemas recuperados, quais campos e tarefas devem ser contidos. Não
inventar mutações de banco/configuração antes dessa revisão. O auditor não emite
o recibo de contenção que o guard futuro exigirá.

A revisão dos gerenciadores públicos de tarefas de
[Sonarr](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/Jobs/TaskManager.cs),
[Radarr](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/Jobs/TaskManager.cs) e
[Prowlarr](https://github.com/Prowlarr/Prowlarr/blob/develop/src/NzbDrone.Core/Jobs/TaskManager.cs)
mostra que `ApplicationStartedEvent` recria os intervalos padrão. Assim, zerar
`ScheduledTasks.Interval` offline, sozinho, não é contenção comprovada. Conferir
o código da versão restaurada e testar o início antes de montar mídia parcial.

## Menor sequência para um core funcional

1. Finalizar a restauração autenticada e a prova das imagens/camadas. Inspecionar
   schemas e configurações offline, revisar o delta de endpoints entre VMs,
   caminhos raiz, clientes de download e notificações. DNS Docker não atravessa
   VMs. Suspender os gatilhos de scan/import/RSS/renomeação, tarefas Seerr/Bazarr,
   estado de torrents e ações do worker usando contratos comprovados de cada
   versão. Testar em cópias descartáveis sem mídia/egress antes de aplicar às
   cópias destinadas ao core. Produzir recibo auditado ligado aos hashes finais.
2. A plataforma deve publicar uma **nova** lease para escritores do core. O
   contrato coordenado usa `/var/lib/homelab-storage/core-writer-admission`,
   diretório root 0755 montado RO, e arquivo `lease` root 0644 substituído
   atomicamente. São exatamente nove campos ASCII únicos e ordenados:
   `version=2`, `role=core-writer`, `state=ready|hold`, `boot_id`, `filesystem_uuid`,
   `generation`, `physical_generation`, `sequence`, `expires_uptime_seconds`.
   Boot e UUID devem coincidir com os valores esperados; geração usa 32 hex
   minúsculos, prova física 64 hex minúsculos e sequência é inteiro positivo.
   READY exige prazo monotônico futuro de no máximo 45 segundos; HOLD sempre
   recusa, com `physical_generation=none` e prazo zero. Não reutilizar
   `/var/lib/homelab-storage/admission` nem aceitar o protocolo exclusivo de
   playback. A implementação/deploy da plataforma e o consumidor são trabalho
   separado; este manifesto não publica nem consome a nova lease.
3. Implementar e testar o novo guard consumidor: campos exatos, arquivos/ancestrais
   confiáveis, boot atual, TTL, geração e papel; prova local do mount ext4 correto
   em `/srv/external3`, UUID e bind de `/srv/external3/homeserver` sem fallback para
   SSD, NFS ou diretório comum. A prova física pertence à plataforma. O guard
   combina lease fresca, identidade local e recibo de restauração/contenção;
   ausência, hold, expiração, remount RO e perda de mount negam execução.
4. Adicionar acompanhamento contínuo que pare escritores quando a prova falhar,
   preserve parada manual e exija resume explícito com prova nova. Testar ausência
   de lease, oferta da lease de playback, boot anterior, corrupção, expiração,
   alteração de mount e falha física em fixtures. O guard histórico mergerfs do
   worker não aceita esse layout; criar contrato ext4 explícito sem enfraquecê-lo.
5. Somente após revisão independente, substituir a recusa fixa por esse guard,
   ligar comandos nativos comprovados, configurar rede/segredos por papel, permitir
   escrita apenas nos binds necessários e validar o proxy administrativo. Liberar
   aplicações gradualmente, com agendamentos ainda contidos enquanto o acervo
   estiver parcial. Validar memória e operação real antes da readmissão ampla.

O manifesto preparado não consome a lease atual e não concede permissão a nenhum
escritor. Dashboard, controle por VM e otimização de RAM permanecem no radar;
não são pré-requisitos inventados para a cópia de mídia nem justificam iniciar
aplicações antes da contenção.

## Verificação local

```sh
uv run --frozen pytest -q tests/unit/test_core_state_audit.py \
  tests/unit/test_core_entrypoint.py tests/contract/test_core_compose.py
sh -n scripts/core-entrypoint.sh
docker compose --env-file /private/core.env -f compose.core.yaml \
  --profile core-prepared config --quiet
```

`config` valida sintaxe e interpolação sem iniciar containers. Não usar `up`,
`run`, overrides ou o monólito como verificação. Testes locais usam somente
fixtures; não comprovam filesystem, rede, versões, capacidade ou restauração da VM.

Referências de contrato: [serviços e binds do Compose](https://docs.docker.com/reference/compose-file/services/),
[SQLite WAL e sidecars](https://sqlite.org/wal.html) e
[`quick_check`](https://www.sqlite.org/pragma.html#pragma_quick_check).
