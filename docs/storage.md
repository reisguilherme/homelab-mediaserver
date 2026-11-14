# SSD e HDs USB

## Acervo em um único HD

O layout independente `compose.storage.single.yaml` usa somente o pool `hdd3`
para mídia. O volume permanece em `/srv/external3/homeserver`, apresentado aos
aplicativos pelo mergerfs em `/srv/media-view` e pelos mesmos caminhos `/data`.
Appdata, bancos e transcode continuam nos seus binds separados de sistema.

O cadastro deste modo contém apenas `hdd3`. Prepare-o com `prepare-storage.py
--hdd3-uuid UUID_VERIFICADO --output /srv/appdata/control/storage.pending.json`;
a preparação bem-sucedida também produz `storage-native.identity` no mesmo
diretório. Esse arquivo não contém credenciais: os entrypoints nativos o usam,
com os UUIDs e mountinfo do host, para recusar disco ausente, estrangeiro, RO ou
um bind lógico antigo. O Compose precisa dos dois arquivos preparados antes da
recriação dos consumidores.

Combine `compose.yaml` com `compose.storage.single.yaml` para este modo. Os
overrides `storage`, `extra` e `fourth` descrevem os layouts de expansão abaixo;
seus binds não fazem parte do modo de disco único. A recuperação USB opt-in
também aceita o cadastro singleton e monitora somente seu dispositivo registrado.

Uma instalação existente exige migração verificada de biblioteca, payloads e
hardlinks, reconciliação de permits/claims e do anchor primário das reservas,
troca coordenada da união e recriação dos consumidores. Os caminhos lógicos,
IDs nativos, estados de pedidos e descriptors permanecem iguais. A remoção das
origens vem depois da prova de conteúdo e da leitura no novo layout. O plano de
consolidação fica em `docs/superpowers/plans/2026-11-08-single-media-disk.md`;
inventários, UUIDs reais, recibos e cópias de reversão ficam privados no servidor.

## Expansão com SSD e vários HDs

A expansão é opcional: o Compose básico continua usando `/srv/data`. Com
`compose.storage.yaml`, o gateway prefere o SSD e escolhe o HD USB quando o
torrent inteiro cabe nele. Espaços de discos diferentes nunca são somados para
admitir um torrent. A fila pausada e os bytes ainda pendentes entram na conta.
Um segundo HD USB é opcional: acrescente `compose.storage.extra.yaml` e instale
o cadastro com `--hdd2-uuid`. Para um terceiro HD USB, acrescente também
`compose.storage.fourth.yaml` e `--hdd3-uuid`. A preferência fica SSD, primeiro
HD, segundo HD e terceiro HD, sempre exigindo que o torrent inteiro caiba no
disco escolhido. Cadastros existentes de dois ou três pools continuam válidos
e não exigem os mounts opcionais ausentes do cadastro.

| Papel | Host | Containers |
|---|---|---|
| SSD físico ext4 | `/srv/data` | `/storage/ssd` |
| HD USB físico ext4 | `/srv/external/homeserver` | `/storage/hdd/homeserver` |
| Segundo HD USB físico ext4, opcional | `/srv/external2/homeserver` | `/storage/hdd2/homeserver` |
| Terceiro HD USB físico ext4, opcional | `/srv/external3/homeserver` | `/storage/hdd3/homeserver` |
| Visão mergerfs | `/srv/media-view` | `/data` |
| Cadastro privado | `/srv/appdata/control/storage.json` | `/run/homeserver/storage.json` |

Arr e qBit recebem uma única montagem `/data`. Torrents ficam em
`/data/torrents/.placements/<permit_id>`; o controlador cria cada destino apenas
no pool autorizado. Vídeos e SRT instalados pelo worker permanecem no filesystem
da fonte, com hardlinks verificados. Bazarr pode criar sua própria legenda no
SSD mesmo quando o vídeo está no HD, se a pasta lógica existe em ambos os
branches; os dois arquivos continuam juntos no caminho lógico. O controlador
captura o pool físico de cada arquivo na exclusão e as métricas contam seu disco
real. Uma série pode ter episódios em discos diferentes;
a ordem de importação continua sendo temporada/episódio.

## Margem para downloads

`HOMESERVER_STORAGE_MIN_FREE_GIB=10` mantém permanentemente 10 GiB livres
utilizáveis em cada disco. Para admitir um torrent, o controlador desconta do
livre físico todos os bytes ainda pendentes e essa margem; o torrent inteiro
precisa caber em um único disco. Fontes paradas continuam consumindo capacidade
até o descarte comprovado. O valor é inteiro, não negativo, e pode ser alterado
no `.env`; recrie worker e gateway para aplicar a mesma preferência aos dois.

A margem não modifica o espaço físico medido, as reservas ext4 ou as montagens.
**Reserva do filesystem** no gráfico é `total − usado − livre utilizável`;
**Disponível para novos downloads** já desconta fila e margem de downloads.
Reduzir a disponibilidade impede novas admissões e retomadas que não cabem;
não apaga payload nem pausa automaticamente downloads já em andamento.

## Preparar as montagens

Este procedimento exige autorização explícita para alterar montagens no host.
Formatação ou particionamento exige autorização própria, identificação do disco
e escopo exato; os scripts do projeto nunca executam essas operações. Preserve
as outras partições, o SSD, o SO e as entradas existentes em `/etc/fstab`.

Use ext4 para todos os pools. NTFS não faz parte deste fluxo: permissões,
hardlinks e recuperação de volume sujo não são presumidos. Antes de qualquer
escrita, confira `lsblk -f`, `findmnt /srv/data` e `findmnt /srv/external`; com
o segundo HD, confira também `findmnt /srv/external2`; com o terceiro, confira
`findmnt /srv/external3`. Todos
devem ser raízes de filesystems físicos, UUIDs distintos, montados em leitura
e escrita. UUIDs reais ficam só na instalação privada.

No Ubuntu 24.04, instale mergerfs e confira sua versão:

```bash
sudo apt update
sudo apt install mergerfs
mergerfs --version
```

Antes da primeira montagem do USB, com a stack parada e o mountpoint realmente
desmontado, prepare um fallback vazio que UID 1000 não possa usar. Não aplique
estes comandos sobre dados montados:

```bash
sudo install -d -o root -g root -m 0555 /srv/external
sudo install -d -o root -g root -m 0555 /srv/external/homeserver
sudo install -d -o root -g root -m 0555 /srv/external/homeserver/torrents
sudo install -d -o root -g root -m 0555 /srv/external/homeserver/torrents/.placements
sudo install -d -o root -g root -m 0555 /srv/media-view
```

Para instalar o segundo HD, prepare também os mesmos diretórios protegidos sob
`/srv/external2` **enquanto esse mountpoint está desmontado**, antes de montar o
volume. Os fallbacks dos dois USBs são independentes; UID 1000 não pode criar
destinos em nenhum deles quando o respectivo disco estiver ausente.

Monte os volumes previamente identificados. Crie no HD montado apenas a pasta
do projeto e, em todos os pools, `torrents`, `media/movies` e `media/tv` para
UID/GID 1000. Prepare `torrents/.placements` como root, modo 0755. O instalador
também cria essas pastas depois de verificar os UUIDs; os branches precisam
existir antes de montar a união. Preserve a propriedade de diretórios existentes.

Para mergerfs 2.33.5, fornecido pelo Ubuntu 24.04:

```bash
sudo mergerfs /srv/data:/srv/external/homeserver /srv/media-view \
  -o category.create=epff,ignorepponrename=true,inodecalc=hybrid-hash,moveonenospc=false,link_cow=false,symlinkify=false,cache.files=auto-full,dropcacheonclose=true,minfreespace=0,allow_other,use_ino,nodev,nosuid
findmnt /srv/media-view
```

Com dois HDs, use os mesmos parâmetros e estes três branches, nesta ordem:
`/srv/data:/srv/external/homeserver:/srv/external2/homeserver`. A união deve ser
recriada com a stack parada antes da fixture de três pools. Não acrescente o
terceiro branch a uma união em uso.

O nome `link_cow` usa underscore nessa versão. Ao trocar versão/driver/opções,
confira as opções suportadas e refaça a fixture; não copie uma capacidade antiga.
`epff` mantém a criação no branch que já contém o destino exclusivo;
`moveonenospc=false` impede transferência silenciosa de um torrent para outro
disco. A instalação verifica os branches pelo xattr `user.mergerfs.branches`
em `/srv/media-view/.mergerfs`, pois o campo source de mountinfo pode abreviar
os nomes. Nunca monte os pools físicos sobre subpaths de `/data` no Arr.

`cache.files=auto-full` permite o `mmap` compartilhado usado pelo libtorrent2
do qBit. No mergerfs 2.33.5, `cache.files=off` impede esse acesso e pode produzir
ENODEV (`No such device`), mesmo quando uma escrita comum e hardlinks funcionam.
`dropcacheonclose=true` reduz cache duplicado ao fechar os arquivos. Ambas as
opções existem no [manual oficial da versão 2.33.5](https://github.com/trapexit/mergerfs/blob/2.33.5/README.md#page-caching).
O instalador e o startup exigem essas opções; a fixture testa escrita, flush e
leitura via `mmap` pelo mesmo caminho lógico, como UID 1000.

## Verificar e ativar

Execute o script no host Linux, como root, com UUIDs que você acabou de conferir.
Ele não monta discos, não formata, não instala pacotes e não edita `fstab`.

```bash
sudo python3 scripts/prepare-storage.py \
  --ssd-uuid UUID_DO_SSD --hdd-uuid UUID_DO_HD \
  --output /srv/appdata/control/storage.pending.json
```

Para dois HDs, acrescente `--hdd2-uuid UUID_DO_SEGUNDO_HD`. O instalador exige
três UUIDs distintos e os três branches exatos; omitir a opção continua
verificando apenas o layout original de dois pools. As capacidades são
verificadas novamente em todos os discos, inclusive os já cadastrados.

O script recusa mounts ausentes, RO, UUID errado, symlinks e uma visão que não
seja mergerfs. Uma pequena fixture roda como UID/GID 1000 em cada disco, escreve
pelo mesmo path lógico dos serviços, testa `mmap` compartilhado com flush/readback,
confere os bytes no pool físico, cria hardlink, renomeia e remove os próprios
arquivos. Compara dispositivo/inode físicos, propriedade e ausência de contraparte
em **todos** os outros pools; também tenta criar um destino não autorizado sob
os pais protegidos. Somente
um resultado completo grava capacidades positivas. Os diretórios temporários
da fixture têm nomes exclusivos e são removidos; a mídia existente é preservada.

O cadastro fica em modo 0600, proprietário UID 1000. O arquivo de staging evita
que containers do Compose básico enxerguem a expansão antes de receber os binds
corretos. Após o resultado positivo e a conferência das montagens:

```bash
sudo mv /srv/appdata/control/storage.pending.json /srv/appdata/control/storage.json
docker compose -f compose.yaml -f compose.storage.yaml config --quiet
docker compose -f compose.yaml -f compose.storage.yaml up -d --build
docker compose -f compose.yaml -f compose.storage.yaml ps
```

Use os mesmos dois arquivos também em `run`, `logs`, `restart` e `down`.
Com dois HDs, use os três arquivos em todos esses comandos:

```bash
docker compose -f compose.yaml -f compose.storage.yaml -f compose.storage.extra.yaml config --quiet
docker compose -f compose.yaml -f compose.storage.yaml -f compose.storage.extra.yaml up -d --build
```

O override extra acrescenta somente os binds físicos do segundo HD. Arr e qBit
continuam recebendo a mesma visão `/data`; apenas o worker escreve pelos binds
físicos. Gateway, API, init e coletores recebem o novo bind somente leitura,
com `create_host_path: false`. Não use apenas o override extra sem o principal.

Para a instalação com um único HD, é possível manter o comando habitual
`docker compose`: quando não houver outro
`compose.override.yaml` no checkout, ative o override automático com
`ln -s compose.storage.yaml compose.override.yaml`. O link local não vai para
o Git; os comandos sem `-f` passam a usar o arquivo acompanhado pelo projeto.
Para dois HDs, continue usando explicitamente os três arquivos; esse link
sozinho não inclui o override extra.
O init verifica SSD, pais protegidos, montagem/bind real da união, branches e
opções antes de preparar configurações. Seus binds da mídia são somente leitura:
as pastas já devem estar prontas. Worker ganha apenas as capacidades necessárias
para criar destinos e verificar hardlinks com UID 1000; gateway, API e coletores
têm binds físicos somente leitura. Claims persistentes ficam em
`/srv/appdata/control/storage-placements`; a fila usa `storage-queue.json` no mesmo
diretório. Os snapshots são substituídos atomicamente dentro do bind de diretório.
Jellyfin recebe a biblioteca com escrita para permitir administração de metadados
e legendas. Exclusões de filmes, episódios e temporadas continuam passando pelo
proxy e pelo controlador para coordenar os serviços e liberar os hardlinks.
Os entrypoints de Arr, qBit, Bazarr e Jellyfin também verificam que seu bind é
mergerfs antes de iniciar; essa verificação nativa confere o tipo de montagem.
O worker repete a validação completa em cada restart. Mudar branches/opções
exige parar a stack, verificar identidade/guards, refazer a fixture e recriar
containers; não reconfigure uma união em uso.

Antes de liberar o worker root e as APIs UID 1000, o init da expansão cria o
banco novo `control/control.sqlite` como UID/GID do serviço, modo 0600. Bancos
e journals existentes mantêm seus bytes e permissões; owner inesperado ou
symlink recusa a inicialização, sem correção automática ou chown recursivo.

`--check` faz apenas verificações de mount/UUID/guard, sem criar pastas, executar
fixtures ou publicar cadastro. Não é uma nova prova de capacidades.

## Terceiro HD USB: quatro pools

O terceiro USB usa `hdd3`, com UUID próprio em `/srv/external3`. Antes da
montagem, prepare sob esse mountpoint desmontado os mesmos fallbacks protegidos
do primeiro USB: raiz, `homeserver`, `homeserver/torrents` e
`homeserver/torrents/.placements`, todos root, modo 0555. Não aplique essas
permissões sobre um filesystem montado. Depois da montagem ext4 RW, crie apenas
os diretórios do projeto, com a mesma propriedade e os guards dos outros pools.

Com os consumidores de mídia parados, recrie a união com os mesmos parâmetros
seguros e os quatro branches nesta ordem:

```text
/srv/data:/srv/external/homeserver:/srv/external2/homeserver:/srv/external3/homeserver
```

Para persistência, preserve as entradas existentes de SSD e USBs, acrescente
somente a entrada ext4 do novo UUID em `/srv/external3`, com timeout limitado,
e acrescente esse branch à entrada existente da união. Sua espera deve incluir
`srv-external3.mount`, além dos dois USBs anteriores, usando uma opção
`x-systemd.after=` por unidade, separadas por vírgulas. Não junte nomes de
unidades com dois-pontos. A única dependência
obrigatória continua sendo o SSD em `/srv/data`; não substitua o arquivo
`fstab` inteiro. Formatar, particionar e alterar essas montagens exige a
autorização explícita dos dispositivos e do escopo, como nos demais discos.

Confira o novo volume, todos os UUIDs e a união antes de executar a fixture:

```bash
sudo python3 scripts/prepare-storage.py \
  --ssd-uuid UUID_DO_SSD --hdd-uuid UUID_DO_HD \
  --hdd2-uuid UUID_DO_SEGUNDO_HD --hdd3-uuid UUID_DO_TERCEIRO_HD \
  --output /srv/appdata/control/storage.pending.json
```

O probe testa cada pool contra todos os outros três. Após conferir o resultado,
promova o cadastro pendente e recrie os consumidores com os quatro arquivos:

```bash
sudo mv /srv/appdata/control/storage.pending.json /srv/appdata/control/storage.json
docker compose -f compose.yaml -f compose.storage.yaml -f compose.storage.extra.yaml -f compose.storage.fourth.yaml config --quiet
docker compose -f compose.yaml -f compose.storage.yaml -f compose.storage.extra.yaml -f compose.storage.fourth.yaml up -d --build --force-recreate
```

O cadastro é um bind de arquivo: a troca atômica no host não atualiza o inode
visto por containers antigos. Recriá-los aplica tanto o cadastro de quatro
pools quanto o bind físico novo. O override `fourth` adiciona apenas
`/srv/external3` → `/storage/hdd3` aos mesmos consumidores do override `extra`,
com escrita apenas para o worker; Arr e qBit mantêm a união em `/data`.
Use os mesmos quatro arquivos nos comandos Compose seguintes.

Para conferência sem fixture nem escrita, repita o comando de preparação com
`--check`. O novo pool aparece separadamente no painel. Ele recebe novas
admissões quando os pools anteriores não acomodam o torrent inteiro, já
descontados seus próprios compromissos e margem. A adição não move mídia nem
reescreve reservas, permits, claims ou destinos de downloads existentes.

## Boot e persistência

Revise suas entradas existentes de `fstab`; não substitua o arquivo inteiro.
Este é um exemplo para adaptar, não um comando do instalador:

```fstab
# Preserve a entrada existente do SSD, que é obrigatório antes da união.
UUID=UUID_DO_HD /srv/external ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
/srv/data:/srv/external/homeserver /srv/media-view fuse.mergerfs defaults,category.create=epff,ignorepponrename=true,inodecalc=hybrid-hash,moveonenospc=false,link_cow=false,symlinkify=false,cache.files=auto-full,dropcacheonclose=true,minfreespace=0,allow_other,use_ino,nodev,nosuid,x-systemd.requires-mounts-for=/srv/data,x-systemd.after=srv-external.mount,x-systemd.before=docker.service 0 0
```

A união depende do SSD real e espera a tentativa de montagem do USB, com timeout
limitado. A ausência do USB usa os diretórios protegidos preparados no fallback;
o filesystem do SO nunca pode virar um pool. Não configure o SSD obrigatório
como uma montagem opcional que deixe `/srv/data` vazio. Confira a ordem das
montagens e do Docker no próprio host. O projeto não instala units do host.
As opções de ordem, dependência e timeout estão descritas no
[manual systemd.mount do Ubuntu 24.04](https://manpages.ubuntu.com/manpages/noble/man5/systemd.mount.5.html).

Para dois HDs, preserve essas entradas e acrescente a entrada ext4 do segundo
UUID em `/srv/external2`, também com timeout limitado. A entrada da união deve
conter os três branches acima e esperar também `srv-external2.mount`
(`x-systemd.after=srv-external.mount,x-systemd.after=srv-external2.mount`). A dependência
obrigatória continua sendo `/srv/data`; cada HD ausente usa seu próprio fallback
protegido. Isso exige conferir a ordem no host e testar a ausência de cada USB.

`depends_on` protege a criação da stack pelo Compose. O restart automático do
Docker após boot não reexecuta esse fluxo: a ordem correta do host continua
necessária. Os guards impedem o início dos leitores e do worker com um bind
comum, mas não substituem a ordem de boot. Não inicie os containers antes das
montagens. Faça um teste de boot
com e sem USB antes de considerar essa recuperação comprovada. A fixture e os
testes locais não demonstram reboot, Tailscale, hardware ou reprodução.

## Ausência e reconexão dos HDs

O painel apresenta SSD e cada HD cadastrado separadamente, com usado/livre, fila/disponível
e estado. A fila é consultada em uma tarefa independente a cada cinco segundos,
mesmo durante buscas demoradas; falhas mantêm o timestamp anterior. Ausência ou
evidência vencida produz números indisponíveis. CPU e rede
continuam sendo coletadas. Downloads existentes no USB não são transferidos para
o SSD; exclusões explícitas ficam pendentes enquanto sua identidade física não
puder ser confirmada. Marcar como assistido não remove mídia.

HD ausente desde o boot ou desmontado de forma limpa expõe o fallback
protegido e permite continuar no SSD. Uma remoção abrupta pode deixar mounts
ou binds com EIO; um disco estrangeiro pode expor pais sem proteção. Esses
casos recusam operações de armazenamento, inclusive novas operações no SSD,
até uma recuperação verificada. Sem a rotina opcional descrita abaixo, a
recuperação das montagens exige intervenção. Os branches nunca são alterados
automaticamente para contornar essa condição.

Para manutenção planejada, pare os serviços que acessam a mídia antes de
desmontar. Ao reconectar, confirme o mesmo UUID e mount RW em `/srv/external`,
restaure a união e confira branches/opções. Binds Docker podem continuar presos
ao mount anterior: recrie os containers que recebem mídia, pools ou mountinfo.

```bash
sudo python3 scripts/prepare-storage.py \
  --ssd-uuid UUID_DO_SSD --hdd-uuid UUID_DO_HD --check
docker compose -f compose.yaml -f compose.storage.yaml up -d --force-recreate
```

Com dois HDs, inclua `--hdd2-uuid UUID_DO_SEGUNDO_HD` no check e
`-f compose.storage.extra.yaml` na recriação. A identidade e a capacidade do
outro HD continuam independentes: nenhuma reserva ou download existente muda
de disco para aproveitar o espaço do segundo HD.

Se as opções mudaram, execute a fixture completa, revise/promova o novo cadastro
e recrie os containers. Um disco diferente não pode receber o UUID antigo.
Não remova o cadastro ou volte ao Compose básico para contornar uma falha;
isso exige primeiro reconciliar os destinos existentes de todos os pools.

## Recuperação automática dos USBs (opt-in)

`scripts/install-usb-recovery.py --install --project /caminho/absoluto/do/projeto`
instala uma tarefa no **cron já existente**. Execute como root no namespace de
montagem do host, após autorização para recuperar `/srv/external` (partição 3)
e `/srv/external2` (partição 1). O SSD de mídia obrigatório usa o dispositivo
inteiro; sua montagem é conferida e não é refeita pela rotina. Os três pools
precisam estar disponíveis durante a instalação. O instalador não edita `fstab`,
não instala pacotes e não cria units do host.

Se `hdd3` estiver cadastrado, a configuração também inclui `/srv/external3`
(partição 1), e os quatro pools precisam estar disponíveis na instalação. Após
adicionar o disco e validar a stack, atualize a instalação dessa rotina para
capturar a identidade do novo USB. Configurações anteriores de três pools
continuam válidas.

A configuração privada `/etc/homeserver-usb-recovery.json` fixa UUID, serial,
capacidade e número de partição observados no cadastro existente. Nomes como
`sdb1` podem mudar. A tarefa roda a cada minuto, com lock exclusivo e estado
persistente em `/var/lib/homeserver-usb-recovery`. Uma reconexão é estabilizada
antes da recuperação; o processo normalmente leva alguns minutos.

Se um USB desaparecer, mudar de geração ou deixar binds antigos, a rotina salva
os containers anteriormente ativos e a fila, para o agendador e os consumidores
de mídia. Aguarda todos os USBs registrados, confere as entradas e units de
montagem existentes, refaz as montagens necessárias e a união, e inicia os
mesmos containers com os binds atualizados. Containers já parados pelo operador
permanecem parados. Após reboot, o cache anterior só é usado para serviços com
identidade preservada e evidência de falha na tentativa de partida desse boot.

Antes da retomada, confere permissões, guards, claims, capacidade física dos
pools cadastrados, autorizações existentes, descritores e prioridades dos arquivos.
Somente fontes confirmadas e preservadas podem ser recuperadas. Erros do qBit
são parados individualmente quando necessário; o agendador existente retoma os
downloads dentro do limite configurado em `HOMESERVER_DOWNLOAD_MAX_ACTIVE`,
mantendo sua distribuição entre discos. Fontes concluídas anteriormente ativas
também podem retomar seed.
O guarda também compara a regra de ignorar fontes lentas com
`HOMESERVER_QUEUE_IGNORE_SLOW_TORRENTS`, sem alterar seus limiares nativos.
Antes disso, os arquivos de conteúdo com tamanho positivo precisam existir no
placement e ter o tamanho do descritor; arquivo ausente ou truncado mantém hold,
sem iniciar um redownload após liberação da reserva. Jellyfin recebe uma
atualização da biblioteca após a recuperação.

Disco ausente, diferente, somente leitura, reserva revogada, capacidade
insuficiente ou evidência incompleta mantém a recuperação pendente. Não há
formatação, reparo automático com `fsck`, exclusão de mídia, troca de fontes,
criação de novos downloads ou alteração de prioridades. Uma falha física de
fonte/cabo/USB pode impedir a recuperação até que o dispositivo volte estável.

Comandos de operação, no host como root:

```bash
homeserver-usb-recovery --status
homeserver-usb-recovery --disable
homeserver-usb-recovery --enable
homeserver-usb-recovery --once
homeserver-usb-recovery --once --recover hdd
homeserver-usb-recovery --once --recover hdd2
journalctl -t homeserver-usb-recovery --since today
```

Use `--disable` antes de manutenção planejada. A rotina também respeita o
`RECOVERY_MODE` existente e nunca o remove. `--recover` inicia uma recuperação
coordenada do USB indicado; as próximas execuções periódicas continuam o evento.
Logs e status usam códigos e contagens, sem cookies ou credenciais. Estado e
snapshots são privados. Uma reinstalação interrompida permanece desativada;
depois de corrigir/reinstalar, habilite explicitamente com `--enable`. Uma
desativação manual é preservada mesmo após reinstalação bem-sucedida.

Fixtures locais verificam ausência/reconexão, retomada após interrupção e holds;
a validação real exige conferir montagens, binds, fontes e limite da fila no
servidor. Uma recuperação coordenada de manutenção não comprova uma queda
física de energia ou a correção da causa da desconexão USB.
