# Preparação da implementação — 14/11/2026

## Atualização operacional — 16/11/2026

O core mantém **oito processos nativos vivos**: Sonarr, Radarr, Prowlarr,
Bazarr, Seerr, integration-proxy, qBittorrent e monitor. API, download gateway
e **worker estão iniciados em manutenção**, sem readmissão da automação. A fila
conserva 734 torrents parados, um em `missingFiles`. Não se declara retomada de downloads.
Os gatilhos Jellyfin continuam contidos e a mídia na playback permanece RO.

O recibo do worker registra `RUNNING_IN_MAINTENANCE`. O guard real foi validado,
com worker/collector no mesmo PGID/SID e collector UID 1000. Capacidade e
heartbeat avançaram; o último sucesso de ingestão ficou inalterado. A amostra
de memória foi 87,3 MB, sem demonstrar consumo final sob carga.

O backend edge → playback:8096 respondeu HTTP 200. Firewall PVE está
`enabled/running`, com DROP IN/OUT e chains das NICs conferidos; conexões da
edge para core:8080, controle:443 e PVE:8006/22 deram timeout. Proxy sem sessão:
Items 401, configuração 403, Users/Public `[]`; Nginx direto 403 e guard
indisponível 500. Hostname incorreto no Caddy deu HTTP 200 com corpo vazio,
sem conteúdo Jellyfin.

As duas regras DNAT TCP 80/443 foram aplicadas no PVE sem reload. Pela LAN,
HTTP retornou 308 para o HTTPS correto. O aceite do proxy passou em **33 checks**:
login comum, identidade, catálogo de 20 itens, Range 206 de 64 KiB, WebSocket
101, login admin 403, token admin válido 403, GETs administrativos e spoof
negados e três logouts. Caddy definitivo, Nginx e guard estão Running, com
rootfs RO, UID 1000, capabilities ALL removidas e limites somados de 448 MiB.
DNS A, WAN e vínculo dos mapeamentos conferidos, com NAT loopback HTTP funcional,
**ainda sem certificado TLS**. ACME e 4G nas portas externas 80/443 deram timeout.
A regra **temporária WAN TCP 10443 → LAN TCP 443 do PVE** está ativa: o teste
4G retornou `ERR_SSL_PROTOCOL_ERROR` e a captura validada em `nic1` registrou
oito SYN de origem externa distinta para PVE:443, além dos controles LAN.
Isso comprova chegada pela porta alternativa ao mesmo destino interno, sem
TLS. A falha externa de 80/443 ainda não distingue roteador de operadora.
O proprietário escolheu **resolver primeiro 80/443 para URL sem porta**;
10443 é só diagnóstico temporário, sem preparação de DNS-01 ou uso em produção.
A conferência somente leitura de acesso WAN e filtros IP do roteador segue pendente;
o diagnóstico detalhado permanece no estado canônico da plataforma.

Limpeza dos dispositivos de teste Jellyfin v2/v3: seis registros conferidos,
quatro já ausentes e dois removidos por IDs de recibos, metadados e janela
temporal. Logout próprio verificado. Registros v1 não identificados com
segurança permaneceram intactos; nenhuma identidade ou token foi publicado.
O túnel temporário de teste foi encerrado; health privado na porta 18096 deu 200.
A publicação está em ativação, sem aceite de disponibilidade/reprodução externa.
Firewall preservado, sem ampliar portas no PVE/edge. O log temporário de diagnóstico
TLS do Caddy é privado, 0600 e limitado a 1 MiB; logs HTTP continuam desativados.

A publicação no hub integrou **8.209 caminhos de 6.023 arquivos únicos**
(5.449.345.898.283 bytes) às pastas finais `media` e `torrents`. O
[publicador](../../runbooks/consolidation-publication.md) utilizou hardlinks,
recibos históricos autenticados, metadados atuais e a lease core-writer v2.
Não houve sobrescrita, exclusão ou repetição da varredura integral de conteúdo.
Seis auxiliares de recuperação permaneceram fora da biblioteca. Os aliases
temporários foram preservados; compartilham o inode e não são outro backup.

Uma cópia publicada de cada uma das três origens passou por HTTP do Jellyfin e
decodificação de início, meio e fim, sem alterar o histórico dos usuários.
O resultado cobre três amostras, não o acervo inteiro. O delta somente de
metadados encontrou 8.209 correspondências publicadas e dois aliases do arquivo
degradado, sem outros casos no escopo inspecionado. Esses aliases ficam fora da
publicação, com origens preservadas; journals pendentes das origens limitam a
conclusão. O caso degradado e a readmissão da automação continuam pendentes.

O backend explícito `ext4-single` foi implementado e testado. No core, fixtures
novas como UID 1000 comprovaram hardlinks, rename, unlink, ownership e proteção
do diretório de placements. Nenhum arquivo real foi usado nessas operações.
O registry e o marcador foram preparados antes da ativação parcial registrada
acima. O restante deste documento preserva o histórico das etapas datadas.

A arquitetura geral passou para
[homelab-platform](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/README.md).
Este registro preserva os fatos da preparação da aplicação. Na separação de
repositórios, Prometheus/Grafana e seu provisioning passaram para a plataforma;
o exporter, a publicação sanitizada e o dashboard de mídia permaneceram aqui.
Os números de testes abaixo são evidências da etapa anterior à separação.
Consulte a [integração atual](../../platform-integration.md).

O proprietário autorizou implementar e publicar na branch `refactor`, incluindo
as alterações locais anteriores. **Em 15/11/2026, autorizou também a
indisponibilidade.** A [execução do checkpoint](../../runbooks/operational-checkpoint-execution.md)
registra a captura no HD, a cópia Windows e a restauração dos arquivos em Linux
verificadas, além do retorno dos 21 serviços originais. A autorização não escolhe
SSD para sobrescrita, não altera mounts nem equivale à instalação do Proxmox.

O fechamento posterior está na
[preparação para instalar Proxmox](../../runbooks/proxmox-preinstallation.md):
ensaio funcional em clones isolados, inventário ampliado dos dois projetos,
preservação de todos os volumes Docker e captura consistente final. Esse
registro mantém explícitas as pendências antes de substituir o sistema.
O checkpoint final no HD foi fechado às 21:08 UTC de 15/11. O proprietário
dispensou a segunda cópia final no Windows e decidiu seguir com a cópia completa
no HD de 18 TB; a transferência foi interrompida e o parcial preservado.
A restauração completa do conjunto final não foi executada. O desligamento
normal foi aceito às 21:33 UTC, com os containers parados e as gravações
sincronizadas; SSH ficou inacessível. Depois, o proprietário instalou Proxmox
no SSD autorizado e o acesso por chave foi retomado, com identidade conferida
pelo console. HTTPS também foi validado pela CA obtida no SSH autenticado.
Antes do desligamento, o proprietário retirou todos os HDs USB e confirmou
essa ação. Posteriormente, reconectou e autorizou o HD de 18 TB para posse
exclusiva do core; os demais HDs permanecem fora dessa etapa.

Na entrega inicial, o inventário confirmou o Proxmox no SSD correto e o segundo
SSD de mídia com o mesmo UUID ext4, sem montagem; ainda não havia VMs ou
aplicações iniciadas. Depois, core, playback e edge foram criadas com Ubuntu,
e os testes de rede, autostart ordenado e reboot foram aceitos. O core passou
a exportar mídia NFS RO para playback, com leitura e negações de escrita
testadas. Depois, a restauração selecionada da configuração e do cache do
Jellyfin foi concluída no core e na playback, conforme a etapa abaixo. A
recuperação USB automática e a retomada da automação continuam pendentes. O
[estado canônico da plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/implementation-status.md#jellyfin-na-playback)
registra o alcance das verificações e os limites restantes.

## Jellyfin isolado na playback — 15/11/2026

O [Compose de playback](../../../compose.playback.yaml) separa Jellyfin 12.2
da stack de automação. A imagem foi instalada na VM playback com o digest
original conferido e iniciou saudável, com HTTP privado na porta 8096. A
mídia chega do core por NFS RO e permanece somente leitura no container;
configuração e cache usam o SSD virtual da playback.
Arr, qBittorrent, controlador e workers não foram iniciados. A edge continua
sem publicação, e o HD de 18 TB e o backup final foram preservados.

A restauração selecionada conferiu **32.777 arquivos regulares, 25.497
diretórios e 12.944.183.365 bytes** de configuração e cache, com hashes de
conteúdo e metadados verificados no core e na playback. A transferência entre
as VMs também foi conferida por hash. Isso não constitui restauração integral
do checkpoint nem prova global do acervo. O estado restaurado de referência
permanece separado da cópia de trabalho usada pela aplicação.

O preparo offline desabilitou as agendas nativas e o monitoramento em tempo
real e separou os plugins enquanto apenas parte do acervo está acessível.
A API autenticada confirmou 20 tarefas ociosas sem gatilhos e três bibliotecas
sem monitoramento em tempo real. Não houve erros nem execução de scans nos
logs examinados. A configuração de playback usa processamento por CPU, sem
passthrough de GPU, e transcode temporário limitado a 512 MiB. Esse limite
não dimensiona dez reproduções simultâneas.

O guard recusa iniciar com mount ausente, estranho ou gravável e encerra o
processo se a identidade do mount mudar. O drop-in da unit Docker existente
na VM exige o mount NFS antes de iniciar. Mount NFS `hard` ainda registrado
pode bloquear I/O após falha do HD; esses guards não demonstram detecção nem
reintegração automática do USB.

O SQLite passou na verificação de integridade, e os hashes do estado comparado
coincidiram com a referência: 32.373 itens de catálogo, 18 usuários, 807
registros de histórico, 432 permissões e 234 preferências. A identidade original
do servidor foi preservada. No catálogo, a comparação cobriu identificação,
tipo, nome, hierarquia e caminho dos itens; não se declara hash de toda a base.

Uma requisição autenticada de mídia retornou HTTP `206`; os 64 KiB recebidos
coincidiram por hash com a leitura NFS. A decodificação por CPU processou três
segundos e 72 frames, sem alteração do histórico. A autenticação usou uma
chave API existente; login interativo por senha e reprodução em cliente externo
não foram testados.

O reboot da playback mudou o boot ID e trouxe o mount NFS RO automaticamente
pela dependência do Docker, mantendo `noauto` no `fstab`. Docker voltou ativo e
Jellyfin saudável, com contador de reinícios do container em zero. Identidade,
catálogo, usuários, histórico e contenção de scans foram conferidos novamente,
com os mesmos hashes e sem erros nos logs examinados. A saúde HTTP respondeu
ao core; nesse aceite de 15/11, conexões da edge às portas 8096 e 18096
continuaram negadas. Em 16/11, a exceção de backend liberou somente 8096,
conforme o estado atual acima. Esse teste histórico
não simula queda de energia do HD nem valida recuperação física do USB.

O [runbook de playback](../../runbooks/playback-nfs.md) registra os procedimentos
e o alcance da evidência. A publicação WAN, GPU, consolidação do acervo e
retomada da automação continuam fora desta entrega.

## Estado observado por SSH em 14/11/2026

- Servidor Ubuntu 24.04.5 LTS, com virtualização disponível. O SSD do sistema e
  o SSD de mídia são dispositivos distintos; nenhum foi escolhido para sobrescrita.
- Quatro volumes de mídia ainda compõem o mergerfs. Os três menores estão quase
  cheios. O HD de 18 TB tinha aproximadamente 6,84 TB livres na consulta; isso
  não equivale à capacidade disponível para novas admissões.
- Há staging de uma consolidação anterior e um verificador de conteúdo ativo.
  O progresso aumentou entre consultas, mas a prova global ainda não terminou.
  Existe uma pendência de reparo, que não foi aceita como perda de conteúdo.
- Jellyfin e telemetria responderam aos checks locais. O controlador respondeu
  `503` com admissão desabilitada, e o marcador de manutenção estava presente.
  Não foi removida manutenção nem reativada aquisição durante a inspeção.
- A execução usa overrides privados além dos arquivos Compose do repositório.
  Uma implantação deve preservá-los até revisão individual. Substituir a lista
  de arquivos pela configuração padrão pode desfazer proteções existentes.

Identidades de discos, endereços, credenciais, inventários, paths de incidentes
e detalhes de mídia permanecem em evidências privadas, fora do Git. Esses checks
não comprovam reprodução real, exposição WAN, desempenho ou integridade total.

## Entrega de código preparada

| Área | Preparação | Ainda necessário |
|---|---|---|
| Storage singleton | Suporte existente para `hdd3`, consumidores e recuperação incluídos na branch | Reconciliação dos dados e estado real antes do corte |
| Observabilidade | Exporter privado, publicação sanitizada opt-in, Prometheus e Grafana com provisioning | Build, ativação, renderização, permissões e consumo medidos no servidor |
| Governança | Desenho separa leitura de métricas e controle administrativo | Proxmox real, usuários/ACL por VM, MFA e tarefas administrativas ensaiadas |
| Recuperação USB | Implementação local existente, confirmada habilitada em `ready` após o checkpoint; desenho de coordenação host/core/NFS | Ensaiar falha e retorno físicos; implementar e ensaiar coordenação virtualizada |
| Energia | Nobreak registrado como aquisição pendente | Equipamento, autonomia, comunicação e desligamento coordenado |

A observabilidade inicial cobre host, rede, capacidade por pool e validade/idade
dos snapshots. Não cobre ainda todas as VMs, aplicações, energia, auditoria ou
alertas. O [guia de ativação](../../observability.md) distingue preparação e runtime.

## Checkpoint privado anterior: código e configuração

Foi criado um checkpoint do código/configuração efetivamente presentes no
servidor, incluindo configuração privada e os overrides ativos. O arquivo foi
relido no HD de 18 TB e copiado para este computador Windows. Tamanho e SHA-256
coincidiram nos dois destinos; o arquivo Windows está em diretório com ACL
restrita. Nenhum desses conteúdos entra na branch.

**Esse checkpoint anterior não inclui bancos nem constitui um estado consistente
de todas as aplicações.** A captura posterior sob quiescência está registrada
abaixo, com segunda cópia e restauração dos arquivos verificadas. A validação
funcional das aplicações e a preservação do outro projeto no host ainda são
necessárias antes de qualquer alteração do SSD do sistema.

## Checkpoint operacional: arquivos restaurados e conferidos

Após a liberação da indisponibilidade em 15/11/2026, foram parados de forma limpa
os 21 serviços de mídia originalmente ativos. Os nove containers já parados
permaneceram assim, e o outro projeto no host foi preservado. Recuperação USB
e cron foram temporariamente suspensos. A captura terminou com fontes estáveis
e cinco arquivos sincronizados e relidos por SHA-256 no HD, totalizando
58.114.509.824 bytes. Os mesmos 21 IDs retornaram às **14:42 em São Paulo**,
com mounts e marcador de manutenção preservados. Cron e recuperação USB foram
restabelecidos, com USB habilitado em `ready`.

O censo sob quiescência enumerou 259.318 membros, 147.380 inodes regulares únicos
e 63.228.971.417 bytes lógicos únicos. As 42 imagens exportadas passaram pela
cópia Windows, retorno ao Linux e importação em daemon isolado; as 20 imagens
originais disponíveis foram conferidas. Três rootfs de workers históricos com
imagem indisponível têm hashes verificados, mas sua reconstrução não foi
demonstrada. Nenhum container de ensaio dessas aplicações foi iniciado.

Na consulta final às **15:52 em São Paulo**, os mesmos 21 IDs estavam ativos,
os nove já parados continuavam assim e todos mantinham contador de reinícios
em zero. Byparr, Jellyfin, telemetria e gateway estavam `healthy`; API e worker
do controlador mantinham o `unhealthy` anterior. Jellyfin, telemetria e liveness
do controlador responderam HTTP 200; readiness permaneceu em 503, com a
manutenção histórica preservada. Cron estava ativo, USB habilitado em `ready`
e o container ativo do outro projeto permanecia em execução. Isso não comprova
reprodução, acesso WAN ou recuperação após falha física do HD.

A segunda cópia no Windows foi aceita às **15:19 em São Paulo**: 13 arquivos,
58.209.849.661 bytes, sincronizados com `fsync` e relidos por SHA-256, além do
recibo de captura confirmado separadamente. O conjunto inclui os cinco arquivos
de estado/imagens/rootfs e os metadados necessários à conferência.

O retorno ao ext4 do WSL passou pelo preflight, extração sem avisos e verificação
às **15:50 em São Paulo** (18:50:54 UTC), sem falhas. Foram conferidas 259.318
entradas do manifesto e 16 diretórios ancestrais implícitos, totalizando 259.334
entradas restauradas. A comparação cobriu conteúdo e metadados dos 147.380
inodes regulares únicos, com 63.228.971.417 bytes relidos por hash, além de
links, ACLs, xattrs e topologia dos hardlinks selecionados.

Onze bancos SQLite passaram em `integrity_check`, com sidecars preservados em
cópias descartáveis. Uma persistência legada Mosquitto apresentou assinatura
válida, hash e metadados preservados; sua integridade semântica não foi
verificada. Relatórios e validadores foram guardados também no Windows privado.

Nenhuma aplicação restaurada foi iniciada. A restauração funcional, a
reconstrução dos três workers históricos e a preservação do outro projeto no
host continuam pendentes. `production_ready=false`, `partial_restart_safe=false`
e a prova global de conteúdo da migração continuam falsas. Resultados e limites
estão no [registro de execução](../../runbooks/operational-checkpoint-execution.md).

### Evidência anterior: preparação somente leitura

**Inspeção de preparação: 15/11/2026, 12:58–13:03 em São Paulo.** A
[preparação do checkpoint operacional](../../runbooks/operational-checkpoint-preparation.md)
registrou 30 containers da mídia, dos quais 21 ativos e nove parados, um volume
anônimo do Jellyfin e alterações em camadas graváveis. O censo das dez raízes
declaradas terminou sem erros, com cerca de 63,23 GB lógicos únicos, incluindo
20,27 GB de reparo dentro do appdata. Há capacidade para as cópias e o ensaio,
com orçamento conjunto para Windows e WSL; isso não substitui a medição na janela.

O relatório anterior terminou **6.029/6.029 grupos saudáveis**, sem erros e com
uma pendência de reparo. `complete_healthy=true` não significa prova global:
`content_verified=false` continua registrado, e nenhum hash de mídia foi relido.
O PID registrado já não existia. Manutenção, locks, serviços e mounts não foram
alterados. A recuperação reportava `ready` com evento; HTTP não foi retestado.

O manifesto privado daquela inspeção cobria as raízes inventariadas e explicitava
as pendências: camadas graváveis, links/tipos especiais, contenção real e lista
de captura validada. `capture_allowed`, `restore_allowed` e `production_ready`
estavam falsos. Naquela etapa não houve backup novo ou restauração real.
Outro projeto ativo foi identificado no host; sua preservação também é necessária antes de reinstalar
o SSD. A proposta manual não reinstala agenda, gerenciador de backups ou units.

### Evidência anterior: ensaio nativo isolado

Preparação adicional em 15/11/2026: o
[ensaio nativo isolado](../../runbooks/native-checkpoint-rehearsal.md) executou
Jellyfin 12.2.0, Sonarr 4.0.15.2941 e Radarr 6.4.4.10685 sobre configurações novas
e mídia sintética no WSL/Linux local. O catálogo Jellyfin e as tags Arr foram
preservados após parada, checkpoint, cópia opaca local e restauração; cinco
bancos SQLite foram verificados. A rodada de sucesso criou e removeu seis
clones próprios. Tags Arr não representam scan do catálogo Arr.
O teste opt-in final de sucesso passou após readiness autenticada e refresh
explícito; a CLI negativa final bloqueou a captura após restart/stop deliberado
do clone próprio, com `containment_changed`, e removeu seus três clones.
`partial_restart_safe=false` e `production_ready=false`: esta evidência não
cobre produção, transferência/restauração Windows ou backup físico USB, e não
altera as pendências operacionais registradas abaixo.

**Inspeção anterior: 14/11/2026, 21:39–21:49 em São Paulo** (15/11,
00:39–00:49 UTC). O cache de verificação saudável avançou de 1.833 para 1.848
dos 6.029 grupos, ainda incompleto e com uma pendência de reparo. Jellyfin e
telemetria responderam 200; readiness do controlador, 503. Há cerca de 6,84 TB
disponíveis no HD grande, ocupado por biblioteca nativa e staging distintos.

O [registro de inspeção e preparação do corte](../../runbooks/media-precutover-review.md)
documenta os agregados históricos, limites da evidência e comandos de contenção
para uma janela futura. Não foram encontrados inventário/delta finais, prova
global ou journal/recibo de publicação no incidente consultado. Isso não prova
ausência de alterações fora desses artefatos. O inventário inicial observado
não é compatível com o adaptador v1; não foi convertido nem importado.

A manutenção pertence ao incidente histórico e a recuperação USB continua
habilitada. Foram identificados 21 serviços ativos e diferenças de overrides
por serviço. O bind de mídia do Jellyfin ainda é RW. Nenhum deles foi alterado;
o requisito de playback RO e a contenção real de scans continuam pendentes.
Naquela inspeção, o roteiro ainda exigia executor operacional, checkpoint
conjunto restaurável, contenção nativa demonstrada e reconciliação da
verificação/reparo. O checkpoint de arquivos foi executado posteriormente,
conforme o registro acima; o ensaio funcional e os requisitos da migração
continuam pendentes.

### Etapa anterior: planejamento e ensaio offline

Atualização da inspeção em 14/11/2026, 23:20 UTC: o mesmo verificador histórico
continuava ativo e avançou de 1.701 para 1.705/6.029 grupos saudáveis durante a
consulta. A prova global continuava falsa, com uma pendência de reparo. Jellyfin
e telemetria responderam 200; readiness do controlador continuou em 503. Nenhum
serviço, disco ou montagem foi alterado, nem foi iniciado outro pipeline.

O [planejador de lotes](../../runbooks/incremental-media-planning.md) prepara um
contrato puro para a publicação incremental. Ele não interpreta os recibos
históricos automaticamente nem libera execução, cleanup, exclusão ou scans.
O adaptador offline de inventário já valida metadados, hardlinks, colisões e
cobertura de hashes declaradas antes de chamar o planejador. Um ensaio
executável percorre cópia, publicação e releitura em arquivos sintéticos Linux,
com nove cenários de sucesso e falha. Nesta versão, faltarem hashes de qualquer
grupo de origem mantém o plano bloqueado, inclusive para seleções menores.
O acervo e os recibos históricos não foram importados nessa validação.

Contenção de Jellyfin/Arr no ensaio é simulada e não libera nenhuma ação real.
Integração operacional com o servidor, contenção nativa e executor continuam
pendentes. A entrega do ensaio não fez conexão SSH ou implantação, não parou
serviços e não publicou arquivos no HD de 18 TB. A inspeção posterior está
registrada acima e não executou o ensaio no acervo.

1. Concluir/reconciliar a verificação anterior sem executar outro publicador ou
   modificá-la enquanto mantém locks do pipeline.
2. Medir conteúdo nativo versus staging, colisões e grupos de hardlinks. Espaço
   ocupado no HD de 18 TB não demonstra que tudo já esteja publicado na biblioteca.
3. Preparar publicação por lote com prova nova e estado por conteúdo. O helper
   histórico exige corte integral e cleanup; ele não atende ao requisito atual
   de disponibilizar o HD grande e acrescentar o restante ao longo dos dias.
4. Ensaiar funcionalmente as aplicações em cópias isoladas da restauração já
   verificada e resolver as dependências históricas não comprovadas. Preservar
   também o outro projeto antes de reinstalar o sistema. Validar comandos
   concretos e apresentar o SSD/montagens exatos
   antes de qualquer operação destrutiva ou alteração de montagem.
5. Conservar os discos de origem. Não executar cleanup nem usar exceções antigas
   de conteúdo degradado como autorização para perda nesta migração.

O [plano operacional](../../superpowers/plans/2026-11-14-homelab-refactor-migration.md)
registra as dependências. Nenhuma cópia nova de mídia, formatação, alteração de
mount, instalação de hipervisor ou reinicialização de serviço foi executada nesta
preparação.

## Verificação do código

- Incremento de inventário/ensaio: **126 testes passaram** em Python 3.12/WSL
  (41 do adaptador, 16 do ensaio e 69 do planejador existente), sem skips.
  Ruff passou nos cinco arquivos Python novos. CLI executada também diretamente:
  sucesso com três grupos/quatro caminhos; perda de contenção com um caminho
  parcial, lote incompleto e nenhum scan liberado. Somente fixtures locais.
- Python 3.12 em WSL; `uv sync --frozen` conferiu 27 pacotes sem alterar dependências.
- Suíte unitária, de contrato, integração e sistema: **4.362 passaram, 156 pulados**.
  Os skips dependem de root, runtimes/imagens Nginx/Caddy ou Compose no WSL.
- Após a última correção do `.env`, **98 testes de configuração e isolamento de
  ambiente passaram**. Essa rodada tem sobreposição com a suíte ampla; não somar
  os números como se fossem testes distintos.
- Ruff, compilação Python, sintaxe dos nove scripts Shell, ShellCheck e as três
  fixtures Shell de sistema passaram. `git diff --check` sem erros.
- Compose normalizado com ambiente fictício, inclusive merge do publicador e
  conflito de nome de projeto. Nenhum daemon de produção foi usado nesse check.
- Fixture Linux confirmou escrita pelo UID1000, herança do GID65534 e leitura sem
  escrita pelo UID65534. Isso não valida ainda os binds reais em containers.

A revisão independente cobriu as mudanças de observabilidade e a exclusão de
artefatos privados na publicação. As alterações anteriores também estão incluídas
por autorização do proprietário; não se declara auditoria funcional integral de
todo esse histórico, scan de CVEs, teste de carga ou aceite físico.
