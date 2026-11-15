# Execução do checkpoint operacional

Registro de **15/11/2026**. O proprietário autorizou prosseguir
com indisponibilidade. A captura dos dados de aplicação terminou com os
21 serviços de mídia parados, fontes estáveis e cinco arquivos sincronizados
e relidos por SHA-256 no HD de 18 TB. O retorno dos mesmos 21 serviços, do cron
e da recuperação USB foi confirmado às **14:42 em São Paulo**. A segunda cópia
Windows passou na conferência integral às **15:19**; a restauração de arquivos
no Linux e a integridade dos 11 bancos SQLite foram verificadas às **15:50**.
Os limites de reconstrução e funcionamento das aplicações permanecem abaixo.

Este registro sucede a
[preparação do checkpoint](operational-checkpoint-preparation.md). Inventários,
identidades de dispositivos e containers, caminhos privados, credenciais,
conteúdo de configuração e recibos completos permanecem fora do Git.

## Parada e preservação do estado

- Os 21 serviços de mídia foram parados sem encerramento forçado. Os nove
  containers da mídia que já estavam parados permaneceram assim; o outro
  projeto no host não foi alterado.
- Recuperação USB e execução do cron foram temporariamente suspensas para
  impedir retomadas durante a captura. A instalação e a política persistente
  de habilitação do cron não foram substituídas.
- IDs, configurações efetivas e montagens foram preservados. O marcador de
  manutenção existente manteve identidade e conteúdo; locks e holds não foram
  removidos. Não houve recriação por `compose up`.
- A enumeração e o cálculo dos hashes ocorreram com os serviços parados e
  verificações de escritores do host. O término dos hashes sem mudança
  observada não dispensa a reenumeração posterior ao arquivo e às imagens.

A biblioteca de mídia e os discos de origem não são o payload desta captura.
Arquivos de reparo já presentes no appdata pertencem ao conjunto de aplicação
preservado. Não houve publicação incremental, limpeza de mídia, alteração de
mounts, formatação ou instalação do Proxmox.

## Conjunto capturado e tratamento de arquivos

| Medição sob quiescência | Resultado |
|---|---:|
| Membros enumerados | 259.318 |
| Inodes únicos de arquivos regulares | 147.380 |
| Bytes lógicos únicos de arquivos regulares | 63.228.971.417 |

O conjunto inclui appdata, bancos e sidecars, configuração privada e overrides,
estado operacional, instalação declarada do host e o volume anônimo Jellyfin.
O arquivo usa lista explícita de membros e não segue symlinks nem amplia o
escopo por recursão. O censo anterior tinha 259.322 candidatos; a lista estável
da captura foi obtida novamente depois da parada.

Os 1.152 symlinks foram classificados: 163 relativos dentro do conjunto,
965 absolutos com destino dentro do conjunto e 24 externos destinados ao
Python 3.12 ou Python 3 do sistema. Seus destinos textuais são preservados.
No ensaio, links absolutos permanecem inertes; ambientes virtuais e fixtures
históricas não devem ser executados para tentar resolver essas dependências.
A classificação não comprova portabilidade para outro sistema operacional.

Há 1.063 grupos de hardlinks com aliases externos ao escopo. O conteúdo único
e a topologia entre os nomes selecionados são preservados no mesmo arquivo;
não se exige que a contagem restaurada de links reproduza aliases externos.
Essa exceção deve constar no manifesto e ser conferida no ensaio.

Dois FIFOs históricos permanecem no conjunto como tipos de arquivo, sem abrir
ou ler seus fluxos. Dois caminhos conhecidos de sockets possuem exclusão
nominal permitida; o recibo final registra a quantidade efetivamente encontrada.
Os sockets já não existiam após a parada: **zero exclusões efetivas**.
Qualquer tipo especial inesperado bloqueia a captura.

O conjunto de cinco arquivos soma **58.114.509.824 bytes**: estado, imagens e
três arquivos de rootfs. O arquivo de estado ocupa 52.919.449.600 bytes e o de
imagens, 3.213.752.320 bytes. O tamanho do arquivo de estado inclui cabeçalhos
e representa regiões esparsas sem armazenar todos os seus bytes lógicos.
A soma lógica acima já conta cada inode regular uma única vez.

Foram exportadas **42 imagens**, incluindo as 20 originais disponíveis e as
camadas graváveis preservadas por commit. Três workers históricos já parados
referenciavam uma imagem indisponível: seus arquivos internos foram exportados
separadamente, junto do inventário privado de configuração. Essa preservação
não comprova a reconstrução dos três containers em outro host.

A primeira exportação das imagens foi recusada porque o Docker acrescentou
um aviso de depreciação à saída do commit. O coletor tratou inicialmente o aviso
como parte do identificador. A correção aceitou somente o aviso conhecido e um
SHA-256, confirmou cada ID por inspect e repetiu a exportação após revalidar
as fontes. Nenhum container foi recriado para corrigir essa falha.

## Evidência do procedimento de arquivo

Uma fixture em ext4 no WSL, com GNU tar, verificou conteúdo, UID/GID, modos,
mtime com precisão de nanossegundos, xattrs, ACL POSIX, symlinks e hardlinks
internos com alias externo declarado. Incluiu FIFO, exclusão nominal de socket
e arquivo esparso com mais de 4 GiB. Criação, listagem e extração terminaram
sem avisos; a verificação SQLite da fixture passou.

Esse ensaio verifica o procedimento com dados sintéticos. A restauração do
conjunto real também passou: membros, hashes, metadados, links e topologia
corresponderam ao manifesto, e os 11 bancos SQLite passaram em
`PRAGMA integrity_check`, em cópias descartáveis com seus sidecars.
Imagens originais sozinhas não
preservam as mudanças das camadas graváveis dos containers; o recibo deve
identificar a preservação dessas mudanças e eventuais limitações.

A classificação dos bancos distingue SQLite da persistência legada Mosquitto,
que usa [formato próprio](https://mosquitto.org/man/mosquitto-conf-5.html).
Somente o arquivo nominal conhecido pode receber essa classificação, mediante
assinatura binária correspondente ao [código oficial](https://github.com/eclipse-mosquitto/mosquitto/blob/master/src/persist_read.c).
Ele continua sujeito a hashes e metadados, mas não conta como SQLite íntegro
nem recebe aceite de integridade semântica. Sete fixtures adicionais cobriram
assinatura correta e incorreta, caminho diferente, alias externo à exceção,
SQLite inválido e válido e alteração de conteúdo.

O escopo de integridade SQLite considera os bancos operacionais em appdata.
O volume anônimo Jellyfin não possui candidatos com as extensões verificadas.
Os 2.350 candidatos fora de appdata pertencem a fixtures, snapshots, backups
ou incidentes históricos; nenhum fica sob mount de escrita dos containers
ativos capturados. Seus bytes e metadados são preservados e comparados, mas
não se exige integridade SQLite de fixtures deliberadamente inválidas.

## Resultados e limites

| Etapa | Estado neste registro | Evidência |
|---|---|---|
| Parada dos serviços previstos | Concluída | Estados dos mesmos IDs e preservação da manutenção |
| Censo e hashes do payload | Concluídos | Manifesto privado; nenhuma mudança observada durante os hashes |
| Arquivo de estado | Concluído | Sem avisos, reenumeração estável e SHA-256 final |
| Camadas graváveis e imagens | Preservadas com limitação histórica | 42 imagens e três rootfs; reconstrução dos três workers não verificada |
| Selo do checkpoint no HD | Concluído | Cinco arquivos sincronizados e relidos, hashes e recibo final |
| Retorno dos serviços | Concluído | 21 IDs originais ativos, nove parados preservados, manutenção inalterada |
| Recuperação USB e cron | Restaurados | Evento, bindings, boot e gerações compatíveis; USB habilitado em `ready` |
| Segunda cópia no Windows | Concluída | 13 arquivos sincronizados e relidos por tamanho e SHA-256, recibo confirmado separadamente e diretório privado |
| Retorno Windows → Linux e restauração | Concluído | Todos os hashes conferidos, preflight aprovado, extração sem avisos em raiz nova e comparação integral |
| Integridade dos bancos restaurados | Verificada para 11 SQLite | Cópias descartáveis com sidecars; persistência Mosquitto preservada e reconhecida, sem aceite semântico |
| Imagens em daemon isolado | Concluída para as imagens disponíveis | 42 IDs, camadas e plataforma das 20 originais conferidos; três rootfs com hashes válidos |

O retorno ao mesmo layout pode ocorrer após o selo da captura, antes da cópia
Windows e do ensaio, para limitar a indisponibilidade. Isso retoma as tarefas
existentes; não demonstra contenção suficiente para uma biblioteca parcial.
Não se inicia container anteriormente parado nem se remove manutenção para
aparentar prontidão. A recuperação USB só pode ser reabilitada após conferir
compatibilidade do evento com os mesmos mounts e containers.

Na consulta às **15:52**, Jellyfin, telemetria e liveness do controlador
responderam HTTP 200. Readiness do controlador permaneceu em 503, com a
manutenção anterior preservada; API e worker já estavam `unhealthy` antes
da parada. Não houve reinícios inesperados. Byparr já estava `healthy`,
após seu intervalo original de healthcheck de 15 minutos. Os 21 IDs ativos
continuavam iguais aos anteriores. O outro projeto manteve seu container ativo.
Não houve teste de reprodução, acesso WAN ou falha física do HD.

O ensaio de imagens leu a cópia Windows, conferiu sua identidade contra o
recibo obtido separadamente pelo canal de controle e devolveu um subconjunto
de 5.195.418.663 bytes ao ext4 do WSL. Os hashes foram relidos no destino.
As 42 imagens foram importadas em daemon com diretórios, socket e namespaces
próprios. Não foram criados nem iniciados containers. Os daemons do ensaio
foram encerrados ao final. Isso confirma o arquivo de imagens disponível;
não comprova restauração funcional das aplicações ou dos três workers antigos.

A segunda cópia completa contém os 13 arquivos do recibo, somando
58.209.849.661 bytes, além do próprio recibo de captura. Todos foram
sincronizados no Windows antes da releitura e comparados com os tamanhos
e hashes do HD. A identidade do recibo foi obtida separadamente pelo SSH.

O retorno completo dessa cópia ao ext4 do WSL também passou na conferência
dos hashes. A extração criou **259.334 entradas**, correspondentes aos
259.318 membros do manifesto e a 16 diretórios ancestrais implícitos.
A comparação releu **147.380 inodes regulares**, totalizando
**63.228.971.417 bytes lógicos**, e conferiu UID/GID, modos, mtime com
nanossegundos, atributos, destinos de symlinks e topologia dos hardlinks.
Não houve falha ou alteração observada na árvore durante a verificação.

Os 11 bancos SQLite passaram em cópias descartáveis com seus sidecars.
A persistência legada Mosquitto teve assinatura, bytes e metadados conferidos,
mas sua integridade semântica não foi verificada. Relatórios e versões dos
validadores foram preservados junto à cópia privada Windows. O recibo original
da captura permanece imutável; as provas posteriores possuem recibos próprios.
Nenhuma aplicação foi iniciada a partir da restauração, que não constitui
ensaio funcional completo nem prontidão para substituir o sistema do servidor.

`production_ready=false`, `partial_restart_safe=false` e a prova global de
conteúdo da migração continuam falsas. O relatório histórico de 6.029 grupos
saudáveis mantém uma pendência de reparo; o checkpoint de aplicações não
resolve essa pendência nem verifica o acervo completo. O outro projeto no
host também precisa ser preservado antes de qualquer reinstalação do SSD.
