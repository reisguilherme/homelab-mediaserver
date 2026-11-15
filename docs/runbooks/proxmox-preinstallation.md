# Preparação da mídia antes de instalar Proxmox

Trabalho iniciado em **15/11/2026**, com indisponibilidade autorizada. A passagem
física e o acesso ao hipervisor pertencem à
[plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/runbooks/proxmox-installation-handoff.md).
Este registro reúne a preservação e os testes do consumidor de mídia.

## Estado de partida

O [checkpoint operacional](operational-checkpoint-execution.md) teve cópias
verificadas no HD e no Windows, restauração integral dos arquivos e 11 bancos
SQLite íntegros. Os serviços voltaram a executar depois dele. Esse checkpoint
permanece preservado; a passagem final precisa registrar o estado posterior.

O levantamento ampliado identificou dois SSDs ocupados, um pelo sistema e outro
pelo acervo. A identidade exata fica na ficha privada; o SSD de mídia permanece
fora do escopo de sobrescrita. A rede atual usa um adaptador Ethernet USB que
precisa continuar conectado quando os HDs USB forem retirados com o host desligado.

Foram encontrados sete volumes Docker, incluindo seis sem associação a containers
atuais. O checkpoint anterior incluía apenas o volume montado pelo Jellyfin.
Um dos adicionais tem cerca de 58 GB; ele foi preservado junto dos demais,
sem limpeza automática. O projeto adicional e as configurações do host também
entraram na captura final.

### Captura final no HD

A captura final foi fechada em **15/11/2026, 21:08 UTC**. Os 22 containers que
estavam ativos foram parados de forma coordenada; os 14 já parados permaneceram
assim. O host continuou acessível por SSH na LAN. Cron, timers e Tailscale foram
pausados para preservar o estado do host; recuperação USB ficou desabilitada e
o marcador de manutenção foi mantido.

Foram preservadas **313.827 entradas**, com 189.935 arquivos regulares únicos
e 129.461.539.241 bytes de conteúdo lógico, abrangendo os sete volumes Docker.
O estado das origens permaneceu estável nas conferências antes e depois da
captura. O conjunto contém 49 imagens disponíveis e três rootfs históricos sem
reconstrução comprovada. Os 26 arquivos do recibo somam **124.604.820.080 bytes**;
os arquivos tar foram sincronizados e relidos no HD antes do fechamento.

O recibo final já foi copiado para o Windows e conferido contra sua identidade
obtida por SSH. A transferência do conteúdo está em andamento. A segunda cópia
ainda precisa de verificação integral e restauração de teste; esta seção não
libera sobrescrita do SSD. Não houve retomada automática dos serviços.

## Ensaios e aceite

Os ensaios usam cópias descartáveis da restauração, imagens já verificadas e
daemon isolado no Linux. Nenhum bind de produção, socket Docker do host, porta
publicada ou acesso à rede externa é concedido aos clones. Os arquivos originais
da restauração e os checkpoints não são montados para escrita.

O resultado deve distinguir proteção de disco ausente, inicialização da aplicação
em clone e funcionamento com mídia real. Remover um guard somente no clone para
testar a aplicação não comprova prontidão da stack original ou segurança de uma
biblioteca parcial. Comparar contagens de catálogo e estado de tarefas antes e
depois, sem disparar workers automáticos.

| Condição | Estado neste registro |
|---|---|
| Checkpoint operacional anterior | Verificado, preservado |
| Inventário ampliado de host, projeto adicional e volumes | Concluído em arquivo privado |
| Inicialização isolada dos serviços de mídia | Concluída com os limites descritos abaixo; não certifica integração ou produção |
| Captura consistente final no HD | Concluída, origens estáveis e recibo fechado |
| Segunda cópia e restauração da captura final | Transferência para Windows em andamento; verificações pendentes |
| Autorização do SSD e escopo de instalação | Pendente; identidades já levantadas |
| Rede de reconexão | Proprietário confirmou manter o adaptador e reservar o IP; configuração do roteador não conferida |
| Mídia de instalação | ISO x86 local conferida contra SHA-256 oficial; gravação e boot do pendrive pendentes |

### Resultado da inicialização isolada

Foram examinados **13 serviços** a partir das cópias restauradas: onze tiveram
ao menos uma resposta HTTP `200`, e os dois componentes de segurança negaram
o cliente não confiável com `403`. Não houve mudança nas contagens de catálogo
monitoradas; os bancos SQLite examinados nas cópias passaram na conferência
após a parada. Os daemons usados pelo ensaio foram encerrados.

| Serviço ou grupo | Evidência e limite |
|---|---|
| Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr e Seerr | Inicialização e UI responderam; as consultas autenticadas de catálogo passaram em Sonarr/Radarr/Prowlarr/Seerr. Os endpoints protegidos testados em Jellyfin/Bazarr retornaram `401`, portanto sua API autenticada não foi aceita |
| qBittorrent | Fila reconhecida e arquivos de torrent/retomada preservados; todas as entradas observadas ficaram `missingFiles` na ausência de mídia real |
| API de controle e telemetria | Liveness `200`; readiness `503` com storage/worker indisponíveis no ensaio |
| Relé Jellyfin | Liveness `200`; reprodução e encaminhamento autenticado não testados |
| Public-security e integration-proxy | Negação `403` do cliente não confiável; fluxo autenticado completo não testado |
| Byparr | UI `200`; health `502` com DNS/rede externa bloqueados |

Cinco guardas originais de storage recusaram o ambiente sem discos. O desvio
necessário para observar a aplicação ocorreu somente nos clones. A observação
foi curta e não executou scans prolongados, reprodução, tarefas automáticas ou
aceite de GPU. Worker, gateway de download, host-metrics, proxies de entrada e
qbit-monitor ficaram fora desta rodada. O arquivo base permaneceu intacto;
`production_ready` e o aceite da integração completa continuam falsos.

A captura final contém estado posterior ao usado nesse ensaio. Sua integridade
e restauração precisam de evidências próprias; os resultados acima não
substituem essa verificação nem a retomada com os discos reais.

O checkpoint não é uma imagem bootável de todo o Ubuntu. Configurações antigas
do host servem à reconstrução seletiva e não devem ser restauradas por cima do
Proxmox. A documentação da plataforma distingue a entrega do host acessível da
restauração das VMs e serviços.

O checksum inicialmente informado correspondia à edição ARM64. O host Intel
precisa da edição **x86**; a ficha privada e o roteiro da plataforma apontam
o arquivo e o checksum oficiais. O download local da ISO x86 foi conferido:
1.706.178.560 bytes, SHA-256 igual ao oficial e arquivo estável durante a leitura.
Isso valida o download; a gravação e o boot do pendrive ainda não foram verificados.

Este preparo não publica lotes de mídia, altera mounts ou conclui a migração ao
hub. O reparo histórico e a prova de conteúdo continuam no escopo de
[migração incremental](incremental-media-planning.md); a manutenção existente
não deve ser removida para simular prontidão.
