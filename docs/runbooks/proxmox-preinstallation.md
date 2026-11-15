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

O recibo final foi copiado para o Windows e conferido contra sua identidade
obtida por SSH. Depois, o proprietário decidiu **manter o backup final completo
somente no HD de 18 TB e seguir com a instalação**. A transferência do conteúdo
para o Windows foi interrompida; os arquivos parciais foram preservados e não
formam uma cópia verificada para restauração. O checkpoint anterior do Windows
continua intacto, com estado mais antigo.

A restauração completa do conjunto final não foi executada. Os ensaios anteriores
mantêm o alcance descrito abaixo; a escolha atual aceita prosseguir com a única
cópia final completa no HD. O proprietário autorizou apagar somente o SSD do
sistema identificado na ficha privada para instalar Proxmox, preservando o
segundo SSD e todos os HDs USB. Conferir modelo e serial no instalador.
Não houve retomada automática dos serviços.

### Desligamento e passagem física

Às 21:24 UTC, a auditoria confirmou o HD esperado montado, a identidade do
recibo, os tamanhos dos arquivos selados e os 36 containers parados, sem mudança
de configuração. Às 21:32 UTC, todos os HDs USB estavam ausentes do host e o
recibo já não estava acessível. A primeira tentativa de desligamento parou na
pré-checagem, antes de executar o comando. O proprietário confirmou depois
que havia desconectado todos os HDs USB. A captura e a auditoria final ocorreram
antes dessa retirada; não há evidência de perda do backup.

Às 21:33 UTC, depois de reconfirmar os containers parados, a sincronização e o
comando normal de desligamento concluíram com sucesso. SSH ficou inacessível;
naquele momento, a confirmação física ainda estava pendente. A instalação e a
reconexão posteriores estão registradas a seguir. A decisão de manter somente
a cópia final no HD continua válida.

### Reconexão após instalar Proxmox

O proprietário concluiu a instalação no SSD autorizado. A identidade SSH do
novo host foi comparada ao fingerprint informado pelo console; a chave pública
dedicada foi cadastrada e uma nova sessão autenticada por chave foi verificada.
A senha foi digitada pelo proprietário em terminal local, sem gravação.

O inventário de 15/11 às 21:56 UTC confirmou Proxmox VE 9.2.2 no SSD esperado,
o segundo SSD com o mesmo UUID ext4 e sem montagem, e os HDs USB ausentes.
Os nomes dos dispositivos NVMe trocaram de posição; continuar usando serial e
UUID para identidade. HTTPS foi validado com CA obtida pelo SSH autenticado,
nome/IP do certificado e resposta HTTP `200`.

A entrega do host acessível não restaura o consumidor: não há VMs nem serviços
de mídia executando. Nenhum acervo foi montado ou reparado nesta reconexão.
O firewall do Proxmox ainda estava desabilitado. A criação dos convidados,
restauração seletiva, posse do storage e isolamento seguem como próximas etapas.

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
| Segunda cópia final no Windows | Dispensada pelo proprietário; transferência interrompida e parcial preservado |
| Restauração completa da captura final | Não executada; limite registrado na decisão de seguir |
| Autorização do SSD e escopo de instalação | Recebida para somente o SSD do sistema; instalação concluída pelo proprietário e identidade do disco conferida depois por SSH |
| Rede de reconexão | Proprietário confirmou manter o adaptador e reservar o IP; configuração do roteador não conferida |
| Mídia de instalação | ISO x86 local conferida contra SHA-256 oficial; proprietário concluiu a instalação e o host foi conferido por SSH |

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

A captura final contém estado posterior ao usado nesse ensaio. Seus arquivos
foram conferidos por hash no HD; sua restauração completa não foi testada.
Os resultados acima não certificam o conjunto novo nem a retomada com os discos reais.

O checkpoint não é uma imagem bootável de todo o Ubuntu. Configurações antigas
do host servem à reconstrução seletiva e não devem ser restauradas por cima do
Proxmox. A documentação da plataforma distingue a entrega do host acessível da
restauração das VMs e serviços.

O checksum inicialmente informado correspondia à edição ARM64. O host Intel
precisa da edição **x86**; a ficha privada e o roteiro da plataforma apontam
o arquivo e o checksum oficiais. O download local da ISO x86 foi conferido:
1.706.178.560 bytes, SHA-256 igual ao oficial e arquivo estável durante a leitura.
Isso validou o download; posteriormente o proprietário concluiu a instalação,
e a versão e o disco do novo host foram conferidos por SSH.

Este preparo não publica lotes de mídia, altera mounts ou conclui a migração ao
hub. O reparo histórico e a prova de conteúdo continuam no escopo de
[migração incremental](incremental-media-planning.md); a manutenção existente
não deve ser removida para simular prontidão.
