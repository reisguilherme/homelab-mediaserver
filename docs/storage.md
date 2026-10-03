# SSD e HD USB

A expansão é opcional: o Compose básico continua usando `/srv/data`. Com
`compose.storage.yaml`, o gateway prefere o SSD e escolhe o HD USB quando o
torrent inteiro cabe nele. Espaços de discos diferentes nunca são somados para
admitir um torrent. A fila pausada e os bytes ainda pendentes entram na conta.

| Papel | Host | Containers |
|---|---|---|
| SSD físico ext4 | `/srv/data` | `/storage/ssd` |
| HD USB físico ext4 | `/srv/external/homeserver` | `/storage/hdd/homeserver` |
| Visão mergerfs | `/srv/media-view` | `/data` |
| Cadastro privado | `/srv/appdata/control/storage.json` | `/run/homeserver/storage.json` |

Arr e qBit recebem uma única montagem `/data`. Torrents ficam em
`/data/torrents/.placements/<permit_id>`; o controlador cria cada destino apenas
no pool autorizado. Vídeos e SRT instalados pelo worker permanecem no filesystem
da fonte, com hardlinks verificados. Bazarr pode criar sua própria legenda no
SSD mesmo quando o vídeo está no HD, se a pasta lógica existe em ambos os
branches; os dois arquivos continuam juntos no caminho lógico. O controlador
captura o pool físico de cada arquivo na exclusão e as métricas contam seu disco
real. Uma série pode ter episódios em ambos os discos;
a ordem de importação continua sendo temporada/episódio.

## Preparar as montagens

Este procedimento exige autorização explícita para alterar montagens no host.
Formatação ou particionamento exige autorização própria, identificação do disco
e escopo exato; os scripts do projeto nunca executam essas operações. Preserve
as outras partições, o SSD, o SO e as entradas existentes em `/etc/fstab`.

Use ext4 para ambos os pools. NTFS não faz parte deste fluxo: permissões,
hardlinks e recuperação de volume sujo não são presumidos. Antes de qualquer
escrita, confira `lsblk -f`, `findmnt /srv/data` e `findmnt /srv/external`. Ambos
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

Monte os volumes previamente identificados. Crie no HD montado apenas a pasta
do projeto e, em ambos os pools, `torrents`, `media/movies` e `media/tv` para
UID/GID 1000. Prepare `torrents/.placements` como root, modo 0755. O instalador
também cria essas pastas depois de verificar os UUIDs; os branches precisam
existir antes de montar a união. Preserve a propriedade de diretórios existentes.

Para mergerfs 2.33.5, fornecido pelo Ubuntu 24.04:

```bash
sudo mergerfs /srv/data:/srv/external/homeserver /srv/media-view \
  -o category.create=epff,ignorepponrename=true,inodecalc=hybrid-hash,moveonenospc=false,link_cow=false,symlinkify=false,minfreespace=0,allow_other,use_ino,nodev,nosuid
findmnt /srv/media-view
```

O nome `link_cow` usa underscore nessa versão. Ao trocar versão/driver/opções,
confira as opções suportadas e refaça a fixture; não copie uma capacidade antiga.
`epff` mantém a criação no branch que já contém o destino exclusivo;
`moveonenospc=false` impede transferência silenciosa de um torrent para outro
disco. A instalação verifica os branches pelo xattr `user.mergerfs.branches`
em `/srv/media-view/.mergerfs`, pois o campo source de mountinfo pode abreviar
os nomes. Nunca monte os pools físicos sobre subpaths de `/data` no Arr.

## Verificar e ativar

Execute o script no host Linux, como root, com UUIDs que você acabou de conferir.
Ele não monta discos, não formata, não instala pacotes e não edita `fstab`.

```bash
sudo python3 scripts/prepare-storage.py \
  --ssd-uuid UUID_DO_SSD --hdd-uuid UUID_DO_HD \
  --output /srv/appdata/control/storage.pending.json
```

O script recusa mounts ausentes, RO, UUID errado, symlinks e uma visão que não
seja mergerfs. Uma pequena fixture roda como UID/GID 1000 em cada disco, escreve
pelo mesmo path lógico dos serviços, cria hardlink, renomeia e remove os próprios
arquivos. Compara dispositivo/inode físicos, propriedade e ausência de contraparte;
também tenta criar um destino não autorizado sob os pais protegidos. Somente
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
Para manter o comando habitual `docker compose`, quando não houver outro
`compose.override.yaml` no checkout, ative o override automático com
`ln -s compose.storage.yaml compose.override.yaml`. O link local não vai para
o Git; os comandos sem `-f` passam a usar o arquivo acompanhado pelo projeto.
O init verifica SSD, pais protegidos, montagem/bind real da união, branches e
opções antes de preparar configurações. Seus binds da mídia são somente leitura:
as pastas já devem estar prontas. Worker ganha apenas as capacidades necessárias
para criar destinos e verificar hardlinks com UID 1000; gateway, API e coletores
têm binds físicos somente leitura. Claims persistentes ficam em
`/srv/appdata/control/storage-placements`; a fila usa `storage-queue.json` no mesmo
diretório. Os snapshots são substituídos atomicamente dentro do bind de diretório.
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

## Boot e persistência

Revise suas entradas existentes de `fstab`; não substitua o arquivo inteiro.
Este é um exemplo para adaptar, não um comando do instalador:

```fstab
# Preserve a entrada existente do SSD, que é obrigatório antes da união.
UUID=UUID_DO_HD /srv/external ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
/srv/data:/srv/external/homeserver /srv/media-view fuse.mergerfs defaults,category.create=epff,ignorepponrename=true,inodecalc=hybrid-hash,moveonenospc=false,link_cow=false,symlinkify=false,minfreespace=0,allow_other,use_ino,nodev,nosuid,x-systemd.requires-mounts-for=/srv/data,x-systemd.after=srv-external.mount,x-systemd.before=docker.service 0 0
```

A união depende do SSD real e espera a tentativa de montagem do USB, com timeout
limitado. A ausência do USB usa os diretórios protegidos preparados no fallback;
o filesystem do SO nunca pode virar um pool. Não configure o SSD obrigatório
como uma montagem opcional que deixe `/srv/data` vazio. Confira a ordem das
montagens e do Docker no próprio host. O projeto não instala units do host.
As opções de ordem, dependência e timeout estão descritas no
[manual systemd.mount do Ubuntu 24.04](https://manpages.ubuntu.com/manpages/noble/man5/systemd.mount.5.html).

`depends_on` protege a criação da stack pelo Compose. O restart automático do
Docker após boot não reexecuta esse fluxo: a ordem correta do host continua
necessária. Os guards impedem o início dos leitores e do worker com um bind
comum, mas não substituem a ordem de boot. Não inicie os containers antes das
montagens. Faça um teste de boot
com e sem USB antes de considerar essa recuperação comprovada. A fixture e os
testes locais não demonstram reboot, Tailscale, hardware ou reprodução.

## Ausência e reconexão do HD

O painel apresenta SSD e HD USB separadamente, com usado/livre, fila/disponível
e estado. Ausência ou evidência vencida produz números indisponíveis. CPU e rede
continuam sendo coletadas. Downloads existentes no USB não são transferidos para
o SSD; exclusões explícitas ficam pendentes enquanto sua identidade física não
puder ser confirmada. Marcar como assistido não remove mídia.

HD ausente desde o boot ou desmontado de forma limpa expõe o fallback
protegido e permite continuar no SSD. Uma remoção abrupta pode deixar mounts
ou binds com EIO; um disco estrangeiro pode expor pais sem proteção. Esses
casos recusam operações de armazenamento, inclusive novas operações no SSD,
até uma recuperação verificada. Não há remount automático ou alteração
automática de branches para contornar essa condição.

Para manutenção planejada, pare os serviços que acessam a mídia antes de
desmontar. Ao reconectar, confirme o mesmo UUID e mount RW em `/srv/external`,
restaure a união e confira branches/opções. Binds Docker podem continuar presos
ao mount anterior: recrie os containers que recebem mídia, pools ou mountinfo.

```bash
sudo python3 scripts/prepare-storage.py \
  --ssd-uuid UUID_DO_SSD --hdd-uuid UUID_DO_HD --check
docker compose -f compose.yaml -f compose.storage.yaml up -d --force-recreate
```

Se as opções mudaram, execute a fixture completa, revise/promova o novo cadastro
e recrie os containers. Um disco diferente não pode receber o UUID antigo.
Não remova o cadastro ou volte ao Compose básico para contornar uma falha;
isso exige primeiro reconciliar os destinos existentes de ambos os pools.
