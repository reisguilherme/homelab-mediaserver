# Jellyfin com mídia NFS somente leitura

Este procedimento restaura o consumidor Jellyfin na VM playback. O
[plano](../plans/2026-11-15-playback-nfs.md) mantém automação, armazenamento e
entrada pública separados. Use exclusivamente
[`compose.playback.yaml`](../../compose.playback.yaml); o Compose da raiz depende
do monólito e não participa deste corte.

O [contrato de playback](../../platform.playback.contract.json) usa o schema
fixado pelo [lock da plataforma](../../platform.lock.json). A porta privada
8096 atende aplicação e administração nativa. NFS é uma dependência do guest;
o container recebe o bind já montado. Os 16/8 GiB de estado/cache são orçamentos,
sem quotas individuais aplicadas; os 16 TiB de mídia representam a visão
compartilhada solicitada, sem reservar novamente o espaço físico. `/transcode`
usa agora um disco virtual dedicado de 32 GiB em SSD; o tmpfs de 512 MiB foi
somente o perfil inicial. Credenciais permanecem no estado nativo, com a
redefinição posterior registrada abaixo; nenhum segredo é injetado por este
manifest. Métricas ainda não
estão habilitadas. Validar o contrato não concede acesso nem comprova deploy.

## Contrato de execução

| Origem na playback | Caminho no container | Uso |
| --- | --- | --- |
| `/srv/appdata/jellyfin` | `/config` | Configuração e catálogo locais, escrita |
| `/srv/appdata/jellyfin-cache` | `/cache` | Cache restaurado explicitamente, escrita |
| `/srv/media-view/media` | `/data/media` | NFSv4.2 `hard`, TCP, somente leitura |
| `/proc/1/mountinfo` | `/run/host-mountinfo` | Observação das montagens do guest, leitura |
| `/srv/transcode/data` | `/transcode` | Temporário RW em disco ext4 dedicado de 32 GiB |
| `/srv/transcode/.homelab-volume-id` | `/run/transcode-volume-id` | Marcador de identidade, somente leitura |
| `/var/lib/homelab-storage/admission` | `/run/storage-admission` | Lease de storage publicada pela plataforma, leitura |
| `/proc/sys/kernel/random/boot_id` | `/run/guest-boot-id` | Identidade do boot do guest, leitura |

O processo usa UID/GID 1000, filesystem da imagem somente leitura, sem
capabilities, com limite de 3 GiB de memória, 4 vCPUs e 256 processos. Esse
limite de RAM não mudou com o disco de transcode. O espaço útil do ext4 é
menor que os 32 GiB nominais; a alocação não comprova dez reproduções simultâneas.
GPU e transcodificação acelerada continuam sem aceite. Na cópia de trabalho,
`HardwareAccelerationType=none` e `EnableHardwareEncoding=false` substituem o
QSV antigo; `TranscodingTempPath=/transcode`, throttling e descarte de segmentos
mantêm a saída temporária no SSD dedicado. A configuração original é preservada.

Os caminhos `/data/media/movies` e `/data/media/tv` preservam os caminhos do
catálogo. O export do core é somente leitura também no servidor NFS; backup,
downloads e staging ficam fora dele. O contrato de montagem pertence à
[plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/runbooks/storage-core-handoff.md).

## Restauração offline

1. Confirme que a configuração de destino não tem consumidores ativos. Identifique
   no recibo privado o checkpoint, o SHA-256 confiável do manifesto e o prefixo
   exato do antigo volume anônimo de `/cache`. Não selecione volumes órfãos.
2. Execute o restaurador como root no Linux, para um diretório **novo** sob pai
   privado, com espaço disponível. As variáveis abaixo vêm do handoff privado:

   ```bash
   python3 scripts/restore-playback-checkpoint.py \
     --archive "$CHECKPOINT/state.tar" \
     --manifest "$CHECKPOINT/payload-manifest.json" \
     --manifest-sha256 "$MANIFEST_SHA256" \
     --cache-prefix "$CACHE_PREFIX" \
     --destination "$RESTORE_STAGE"
   ```

3. Exija `restore-receipt.json` com `complete=true`. O staging contém `config/`
   e `cache/`; o restaurador verifica hashes e metadados dos arquivos selecionados,
   sem iniciar aplicações. Ele rejeita links e tipos não suportados e não prova
   o hash integral do tar nem a integridade global da mídia. Sem recibo, preserve
   o staging incompleto para inspeção e não o publique.
4. Confira os bancos SQLite e registre uma referência privada de usuários,
   identidade do servidor, IDs/caminhos de mídia, coleções, playlists e histórico.
   Preserve a restauração verificada como referência. Copie suas árvores para
   `/srv/appdata/jellyfin` e `/srv/appdata/jellyfin-cache`, inicialmente vazios,
   mantendo UID/GID e permissões. Não restaure `/etc`, runtime Docker ou identidades
   do Ubuntu anterior.
5. Na **cópia de trabalho inativa**, execute uma única vez:

   ```bash
   python3 scripts/prepare-playback-config.py --config-root /srv/appdata/jellyfin
   ```

O preparador valida XML, JSON, caminhos e wizard concluído antes de alterar.
Desativa realtime em todas as bibliotecas e mantém as 20 tarefas nativas da versão
12.2, além das agendas existentes, sem triggers. Move `plugins/` integralmente
para um backup irmão privado, fora do bind `/config`, e recria o diretório vazio.
Configuração e plugins originais ficam em `.playback-config-backup-*/originals/`.
O marcador `.playback-preparation.json` impede repetição automática, inclusive
após falha parcial; não remova esse marcador para contornar uma falha.

Essa contenção precede o boot: um `StartupTrigger` pode executar após três segundos.
Agendas vazias são persistidas como `[]`; apagar o arquivo restaura os defaults.
Alterar `IsStartupWizardCompleted` pode enfileirar scan fora da agenda. Por isso o
preparador exige `true` e preserva `system.xml`.
[Scheduler](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/ScheduledTasks/ScheduledTaskWorker.cs),
[startup](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/ScheduledTasks/Triggers/StartupTrigger.cs),
[LibraryManager](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/Library/LibraryManager.cs).

O monitor realtime usa `EnableRealtimeMonitor` de `options.xml` por biblioteca.
Mesmo com triggers vazios, APIs administrativas e plugins podem solicitar scans;
não execute refresh global enquanto o acervo estiver parcial. A tarefa histórica
de limpeza de coleções/playlists foi removida da árvore 12.2; não confunda a
documentação genérica de versões anteriores com a lista efetiva da imagem.
[Monitor](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/IO/LibraryMonitor.cs),
[persistência das opções](https://github.com/jellyfin/jellyfin/blob/v12.2/MediaBrowser.Controller/Entities/CollectionFolder.cs),
[alteração da limpeza](https://github.com/jellyfin/jellyfin/pull/16062).

## Montagem e inicialização privada

A entrada NFS da playback permanece `noauto`: iniciar o Compose não monta o
export. A plataforma deve estabelecer a montagem e a ordem de inicialização no
guest antes do Docker. O drop-in da unit Docker existente usa `RequiresMountsFor`,
`BindsTo` e `After` para solicitar a montagem mesmo com `noauto`. O retorno
automático foi conferido no perfil inicial, antes da lease obrigatória.
Não há nova unit no host Proxmox. Com o guard atual, montar storage e iniciar
Docker não dispensa readmissão explícita; o ensaio posterior com SSD e lease passou.

Configure `.env.playback`, privado e ignorado pelo Git. Os endereços seguintes
são exclusivamente exemplos de documentação; substitua-os pelo handoff privado:

```dotenv
PLAYBACK_NFS_SOURCE=192.0.2.10:/
PLAYBACK_BIND_IP=192.0.2.20
PLAYBACK_TRANSCODE_ID=UUID_DO_FILESYSTEM_TRANSCODE
HOMESERVER_TIMEZONE=America/Sao_Paulo
```

Depois de validar montagem e configuração, confira e inicie somente esta stack:

```bash
docker compose --env-file .env.playback -f compose.playback.yaml config --quiet
docker compose --env-file .env.playback -f compose.playback.yaml up -d
docker compose --env-file .env.playback -f compose.playback.yaml ps
docker compose --env-file .env.playback -f compose.playback.yaml logs --tail=100 jellyfin
```

O guard compara mountinfo do guest e do container: origem, raiz do export,
NFSv4.2, TCP, `hard`, modo RO e dispositivo devem coincidir. Recusa mounts ausentes,
divergentes ou montagens adicionais dentro da mídia. Depois do início, verifica
a identidade a cada cinco segundos e encerra Jellyfin se ela mudar. O healthcheck
também verifica o guard e `/jellyfin/health`, exigindo o corpo `Healthy`; uma
resposta HTTP não prova login ou vídeo. O base URL anterior `/jellyfin` é mantido.

O guard de transcode também exige o mount ext4 RW esperado, a subárvore de
dados, o dispositivo e o UUID indicado no marcador RO. A presença da pasta
ou do marcador isoladamente não autoriza fallback para o disco do banco.
`PLAYBACK_TRANSCODE_ID` deve conter o UUID real entregue pela plataforma.

A porta 8096 está vinculada ao endereço privado da VM, sujeita ao firewall da
plataforma. Administração é privada; a edge continua fechada. Não abra a porta
no roteador nem publique o acesso administrativo nativo para cumprir este aceite.

## Aceite inicial — 15/11/2026

Este marco usou transcode provisório em tmpfs. A entrega posterior do SSD e
a redefinição de senhas estão abaixo; os hashes de todas as colunas de `Users`
descritos aqui antecedem a alteração deliberada das credenciais.

Já verificado nesta etapa: imagem Jellyfin 12.2 amd64 no digest fixado baixada na
VM; 75 testes focados passaram e `make lint` passou. Fixtures Docker reais
confirmaram mount NFS válido, recusa sem mount ou com origem divergente,
preservação do exit code 7 do processo filho e parada do filho quando a visão
simulada do mountinfo do guest perde a montagem. Esses testes não desligaram o HD.

A restauração seletiva terminou no core e na playback: 32.777 arquivos,
25.497 diretórios e 12.944.183.365 bytes, com hashes e metadados conferidos.
A transferência intermediária teve SHA-256 integral conferido; isso não é uma
nova leitura integral do tar original de todos os serviços. A referência e o
checkpoint original foram preservados; a configuração preparada é outra cópia.

- SQLite passou no `quick_check`. Após início e reboot, coincidiram os hashes
  das colunas de identidade/caminho/tipo/nome/parentesco de 32.373 `BaseItems` e
  de todas as colunas de 18 `Users`, 807 `UserData`, 432 `Permissions` e
  234 `Preferences`. Isso cobre a referência comparada, sem certificar que
  todo arquivo do catálogo já está presente no HD de destino.
- Jellyfin 12.2.0 saudável, mídia RO, root filesystem RO, UID/GID 1000, recursos
  e binds conferidos no container. Plugins restaurados continuam em quarentena.
- Autenticação com chave de API existente passou; identidade do servidor
  preservada, três bibliotecas com realtime desativado e 20 tarefas em `Idle`,
  `Triggers=[]`. Nenhum erro ou execução de scan apareceu nos logs inspecionados.
- Uma mídia existente respondeu HTTP 206 com 64 KiB idênticos ao arquivo NFS;
  ffmpeg decodificou três segundos/72 frames em CPU, sem saída na mídia e sem
  alterar `UserData`. Isso não é teste de transcode completo, cliente externo
  ou dez streams simultâneos.
- Reboot gracioso da playback confirmado por mudança de boot ID; NFS RO,
  Docker e Jellyfin retornaram automaticamente. Autenticação, agendas e hashes
  do catálogo foram novamente conferidos. O core alcança o healthcheck; a edge
  permanece sem acesso às portas de playback.
- A interface web respondeu pelo túnel SSH em loopback do computador do
  proprietário. Login interativo com senha e reprodução em navegador/TV não
  foram testados. O túnel depende da sessão SSH e não publica o serviço.

Os scripts privados usam `Authorization: MediaBrowser Token="..."` para a
API 12.2; o cabeçalho legado retornou 401. O arquivo original `device.txt`
contém BOM UTF-8 e foi comparado sem alterar seus bytes. Recibos, hashes,
logs, endereços e dados de autenticação permanecem no handoff privado.

O deploy aceito é exclusivamente o consumidor privado de playback. Arr,
workers, GPU, entrada pública, consolidação restante e recuperação física
do HD continuam pendentes. Não execute scan global durante essa transição.

## Transcode em SSD e redefinição de acesso

O proprietário autorizou um **novo disco virtual de 32 GiB em `local-lvm`**,
conectado como `scsi1` somente à playback. Ele recebeu ext4 e foi montado por
UUID em `/srv/transcode`; a raiz e o marcador de identidade pertencem a root,
e `/srv/transcode/data` é gravável pelo UID 1000. O container recebe essa
subpasta em `/transcode` RW e o marcador RO. A mídia permanece RO e o limite
do container continua em **3 GiB de RAM**. Nenhum disco do acervo foi formatado.

Os testes reais do guard aceitaram a identidade correta e recusaram mount
ausente ou UUID divergente. A escrita como UID 1000 na área temporária e a
proteção RO do marcador também passaram. Um transcode por CPU com `libx264`
produziu **10,01 segundos em 720p**, em **1,149 segundo**, com **281.888 bytes**.
Somente a saída de teste própria foi removida; `UserData` não mudou. Esse
trecho curto não mede concorrência, reprodução em TV ou capacidade contínua.
Um novo reboot confirmou o retorno do SSD por UUID e do NFS RO, com Docker ativo.
O guard recusou a lease do boot anterior; a aplicação voltou saudável depois
de prova nova e readmissão explícita. SQLite e os 807 registros originais de
histórico permaneceram íntegros, preservando também as seis adições observadas.

As **18 senhas** foram redefinidas pela API nativa para valores aleatórios,
distintos e com dez caracteres. As 18 autenticações com senha nova e seus
respectivos logouts passaram. As políticas permaneceram iguais, exceto três
contadores `InvalidLoginAttemptCount` que voltaram naturalmente a zero após
o login correto. Os hashes de **807 `UserData`, 432 `Permissions` e 234
`Preferences`** permaneceram estáveis. Isso não afirma encerramento de todas
as sessões históricas nem teste interativo de navegador/TV.

Uma inspeção posterior encontrou **813 `UserData`**: os 807 registros originais
continuavam idênticos, com seis novos registros observados enquanto a aplicação
estava disponível. As adições foram preservadas, sem rollback. O teste ffmpeg
acima tem sua própria comparação sem mudança; não atribuir a ele essas adições
nem afirmar que o hash do conjunto atual ainda corresponde ao conjunto de 807.

A relação em texto simples fica somente no Windows privado; nomes, senhas,
tokens e seu caminho não entram na documentação pública. Abyss foi adiado
nesse marco; em 16/11, o proprietário retomou a integração. Os testes passaram
em login, banner, detalhes e vídeo em desktop/celular,
além dos checks internos de assets e bloqueios na edge, conforme o
[runbook Abyss](abyss.md). A inspeção somente leitura encontrou
a iGPU Intel em grupo IOMMU isolado, mas não houve passthrough ou aceite de GPU.

## Admissão obrigatória de storage

A plataforma publica uma autorização temporária de storage no SSD da playback;
o consumidor implantado a recusa quando ausente, inválida ou expirada. O prazo máximo
é **45 segundos**, vinculado ao boot do guest. Mount presente não substitui
essa autorização nem comprova saúde física do disco.

Falhas entram em `HOLD`. A readmissão exige ação explícita de `resume` e prova
nova do storage; a simples volta do HD não libera o consumidor. O coordenador
não executa start, mas uma autorização nova pode permitir que a política
Docker `unless-stopped` reinicie o serviço. Uma parada manual pelo Docker
permanece respeitada.

O consumidor iniciou saudável com lease obrigatória. Na expiração sintética,
o filho recebeu TERM em **11,502 segundos**; esse tempo pertence ao ensaio,
não é um RTO universal. O `HOLD` real deixou HTTP indisponível e nenhum processo
Jellyfin ativo. No teste de pausa manual, `docker stop` permaneceu respeitado
após a lease voltar a READY; somente um start explícito retomou a aplicação
saudável. Nenhum desses testes desligou o HD.

O supervisor usa o cron existente com janelas de **58 segundos**, deadline de
probe de **15 segundos** e lease de até **45 segundos**. Uma janela inicial
mais curta provocou HOLD indevido na passagem ao cron; a causa foi identificada
e a cadência corrigida. Disputa de lock retorna código **75**: comando ocupado
não é confirmação de `resume`; conferir o estado antes de repetir.

**Reboot não significa retorno integral desassistido.** O guard recusa lease
de outro boot; a retomada exige prova nova e readmissão explícita. O ensaio de
reboot com SSD e lease passou, incluindo a recusa anterior à readmissão. Após reboot do host Proxmox, o
operador deve auditar o estado do coordenador antes de readmitir, por desenho.

Esse contrato não implica reintegração física do USB, reparo automático ou
resolução de I/O preso em NFS `hard`. Preservar os guards de mount e a contenção
de scans, sem anunciar recuperação automática.

## Falhas e reversão

Uma queda do HD ou do servidor NFS pode deixar o mount presente e o acesso preso
no kernel. A comparação de mountinfo não detecta essa condição; o probe inicial
tem timeout, mas NFS `hard` pode manter I/O bloqueado mesmo após sinais. Portanto
esta etapa **não implementa nem comprova reintegração automática do USB**. Não
troque para NFS `soft` nem force/lazifique unmount para aparentar recuperação.

Em falha, interrompa o consumidor de forma coordenada, preserve configuração,
logs e recibos privados e confira identidade/saúde do storage antes de retomá-lo.
Para reverter a preparação, mantenha Jellyfin parado e recupere uma cópia nova
da referência preservada; as agendas e plugins originais também estão no backup
irmão. Não sobreponha árvores parciais nem reative scans com a biblioteca ainda
incompleta. O checkpoint original e o acervo não são destinos de escrita dessas
ferramentas.
