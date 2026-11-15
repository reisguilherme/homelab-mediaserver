# Preparação da implementação — 14/11/2026

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
essa ação. Eles continuam desconectados, aguardando a etapa de storage nas VMs.

O inventário posterior confirmou o Proxmox no SSD correto e o segundo SSD de
mídia com o mesmo UUID ext4, sem montagem. Nenhuma VM ou aplicação foi iniciada;
restauração, isolamento e hub permanecem pendentes. O estado do host está na
[plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/implementation-status.md).

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
