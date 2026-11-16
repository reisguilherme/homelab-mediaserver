# Jellyfin com mídia NFS somente leitura

Este procedimento restaura o consumidor Jellyfin na VM playback. O
[plano](../plans/2026-11-15-playback-nfs.md) mantém automação, armazenamento e
entrada pública separados. Use exclusivamente
[`compose.playback.yaml`](../../compose.playback.yaml); o Compose da raiz depende
do monólito e não participa deste corte.

## Contrato de execução

| Origem na playback | Caminho no container | Uso |
| --- | --- | --- |
| `/srv/appdata/jellyfin` | `/config` | Configuração e catálogo locais, escrita |
| `/srv/appdata/jellyfin-cache` | `/cache` | Cache restaurado explicitamente, escrita |
| `/srv/media-view/media` | `/data/media` | NFSv4.2 `hard`, TCP, somente leitura |
| `/proc/1/mountinfo` | `/run/host-mountinfo` | Observação das montagens do guest, leitura |
| tmpfs | `/transcode` | Temporário limitado a 512 MiB |

O processo usa UID/GID 1000, filesystem da imagem somente leitura, sem
capabilities, com limite de 3 GiB de memória, 4 vCPUs e 256 processos. O tmpfs
consome memória dentro desse orçamento. Os 512 MiB de transcode são um limite
inicial para aceite; não comprovam capacidade para dez reproduções simultâneas.
GPU e transcodificação acelerada continuam sem aceite.

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
guest antes do Docker. O drop-in da unit Docker existente ainda precisa de aceite;
não há nova unit no host Proxmox nesta etapa.

Configure `.env.playback`, privado e ignorado pelo Git. Os endereços seguintes
são exclusivamente exemplos de documentação; substitua-os pelo handoff privado:

```dotenv
PLAYBACK_NFS_SOURCE=192.0.2.10:/
PLAYBACK_BIND_IP=192.0.2.20
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
também verifica o guard e `/health`; uma resposta HTTP não prova login ou vídeo.

A porta 8096 está vinculada ao endereço privado da VM, sujeita ao firewall da
plataforma. Administração é privada; a edge continua fechada. Não abra a porta
no roteador nem publique o acesso administrativo nativo para cumprir este aceite.

## Aceite runtime pendente

Já verificado nesta etapa: imagem Jellyfin 12.2 amd64 no digest fixado baixada na
VM; 68 testes focados passaram e `make lint` passou. Fixtures Docker reais
confirmaram mount NFS válido, recusa sem mount ou com origem divergente,
preservação do exit code 7 do processo filho e parada do filho quando a visão
simulada do mountinfo do guest perde a montagem. Esses testes não desligaram o HD.

A restauração seletiva do checkpoint final está em execução. Ainda não registrar
esta etapa como deploy concluído. O aceite do serviço restaurado deve registrar:

- Recibo de restauração, SQLite e contenção offline conferidos.
- Versão/digest, mounts RO e diretórios locais confirmados no container efetivo.
- Login autenticado e leitura de bibliotecas, com realtime desativado e tarefas
  em `Idle`, `Triggers=[]`, sem scan automático nos logs.
- Catálogo, usuários, coleções, playlists e histórico comparados com a referência.
- Reinício e ordem mount/Docker verificados; leitura de mídia separada de um
  teste efetivo de reprodução.

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
