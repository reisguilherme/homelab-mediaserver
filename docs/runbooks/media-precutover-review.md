# Inspeção e preparação do corte de mídia

Inspeção SSH em **14/11/2026, 21:39–21:49, America/Sao_Paulo**
(15/11, 00:39–00:49 UTC). Foram lidos metadados, estados existentes, labels/binds
dos containers e respostas de saúde. Não foram executados novos hashes de mídia,
cópias, publicação, mudanças de configuração, mounts ou comandos de parada.

Este documento prepara a operação; **o corte ainda não está pronto para
execução**. O proprietário mantém a disponibilidade até avisar expressamente.
A autorização da janela também não escolhe um SSD para sobrescrita ou autoriza
alterar qualquer montagem: dispositivo e escopo precisam estar identificados.

## Resultado observado

| Item | Observação | Consequência |
|---|---|---|
| Cache de verificação saudável | Processo ativo; 1.833 → 1.848 de 6.029 grupos, uma pendência de reparo, zero erros declarados, `complete_healthy=false`, `content_verified=false` | Progresso parcial, sem prova global e sem estimativa de tempo por contagem |
| Pipeline histórico | Progresso antigo em `hold`; cache saudável roda separadamente | Não reiniciar o pipeline ou lançar outro verificador/publicador |
| Serviços | Jellyfin e telemetria HTTP 200; readiness do controlador 503 | Saúde HTTP não comprova reprodução ou admissão funcional |
| Manutenção | Marcador presente; token e inode correspondem ao dono do incidente histórico | Não remover ou substituir como se pertencesse à nova preparação |
| Recuperação USB | Habilitada, estado `ready`, objeto de evento ainda presente | Não presumir que `ready` significa ausência de histórico ou compatibilidade com novos containers |
| Compose | 21 serviços canônicos em execução; overrides diferentes entre serviços | Preservar IDs, imagens e listas de arquivos por serviço; não fazer `compose up` genérico |
| Jellyfin | Bind de mídia atualmente RW | O requisito de playback RO ainda não foi implantado |

No diretório do incidente consultado não existiam `final-verification-inventory.json`,
`final-delta.json`, `delta-owner.json`, `delta-journal.json`,
`verification-proof.json`, `verification-manifest.json`,
`verification-progress.json`, `verification-metadata.json`,
`publication-receipt.json`, `publication-journal.json` ou
`publication-events.jsonl`. Isso não prova ausência de mutações por outros
processos ou incidentes; a árvore real ainda precisa ser reconciliada.

Existe um artefato histórico de cópia degradada, declarando
`source_content_verified=false`, `target_degraded=true` e `preserve_source=true`.
Ele não resolve a pendência de integridade nem autoriza aceitar perda nesta
migração. Não foi executado reparo nesta inspeção.

### Capacidade e separação entre nativo e staging

| Volume observado | Disponível para usuário, aproximadamente |
|---|---:|
| SSD de mídia | 10,93 GB |
| Primeiro HD de origem | 10,87 GB |
| Segundo HD de origem | 11,41 GB |
| HD de 18 TB | 6,84 TB |

Unidades decimais, medidas naquele instante. O HD de 18 TB já contém a árvore
nativa e o staging oculto, ambos no mesmo filesystem. O mergerfs ainda reúne
quatro branches. Nenhuma dessas medidas reserva espaço ou contabiliza demandas
futuras, margem, sparse, metadados e compromissos ativos de downloads.

O inventário inicial consultado foi produzido em outra geração e declara:

| Conjunto histórico | Caminhos de arquivos | Grupos device/inode | Bytes lógicos únicos |
|---|---:|---:|---:|
| Três origens | 8.216 | 6.029 | 5.454.162.679.322 |
| Nativo do HD de 18 TB | 9.709 | 6.563 | 5.890.064.899.152 |

Somar bytes por caminho duplicaria hardlinks. Mesmo estes totais por inode
descrevem somente os registros históricos: não são uma medição atual de cobertura
ou prova de que todos os aliases continuam presentes. Não houve inventário novo
de todo o acervo, nem auditoria atual de todos os conflitos arquivo/diretório.

Uma auditoria antiga do staging registra 8.214 caminhos regulares encontrados e
dois ausentes, sem mudanças de metadados nos registros comparados, com
`metadata_only=true` e `content_verified=false`. Ela não substitui releitura
atual ou verificação de conteúdo.

Foram lidos 1.833 recibos já existentes do cache: declaravam hashes de origem e
destino iguais e `destination_fsynced=true`. Não recalculamos essas hashes nem
reautenticamos bindings, assinaturas físicas e geração. O cache mantém
`content_verified=false` até mesmo ao terminar sua classe saudável; a prova
global pertence a outra etapa. Não importar esses recibos como autorização.

### Compatibilidade do inventário

`inventory.json` e `inventory-before-launch-refresh.json` têm o formato inicial:
`created_at`, `roots`, `files`, `special`, `collisions`. Ambos declaravam 17.925
arquivos, quatro raízes e listas vazias de especiais/colisões, sem versão,
geração, completude ou diretórios. Igualdade de tamanho dos JSONs não prova
igualdade de conteúdo.

O [adaptador público](incremental-media-planning.md#adaptador-de-inventário)
exige o contrato normalizado v1 produzido pela etapa de delta final. Não podemos
completar campos por suposição, eliminar arquivos sem hash ou tratar o digest
de metadados como SHA de conteúdo. A preparação offline foi validada em fixtures;
não houve importação operacional do inventário observado.

## Contenção necessária

O marcador de manutenção bloqueia ciclos/admissões do controlador, mas não
congela seus bancos: heartbeat e snapshots continuam escrevendo. Também não
desliga os scans internos ou ações nativas de Jellyfin, Sonarr, Radarr e Bazarr.
Guards de UUID/mount protegem contra um disco errado/ausente; não impedem os
aplicativos de interpretar uma biblioteca parcial como exclusão.

Durante a futura mudança de paths/binds, a contenção comprovável é manter os
consumidores e escritores parados. Voltar a servir somente a parte nativa do HD
exige demonstrar comportamento seguro de scans/ausências antes da retomada. Um
bind RO impede escrita na mídia, mas não impede alterações destrutivas no banco
do aplicativo. Essa demonstração ainda não existe fora da simulação.

## Sequência preparada para a janela futura

Os blocos abaixo são comandos manuais de referência, **não executados**. As
listas `HS_*_IDS` devem conter somente os IDs completos do manifesto privado
renovado na janela; não são preenchidas automaticamente por esta documentação.
Qualquer divergência de container, imagem, bind, disco, manutenção ou processo
interrompe o procedimento para revisão.

1. Renovar o before-state privado: checkout, imagens/digests, IDs/estado inicial,
   sinal de parada configurado (`StopSignal`),
   listas Compose por serviço, binds, identidades dos discos, configuração e
   estado da recuperação, manutenção com dono, processos e locks do incidente.
   Capturar somente o projeto `homeserver`; há outro projeto no host. Guardar
   evidências com acesso restrito, sem imprimir ambiente ou credenciais.

   ```bash
   sudo docker ps --no-trunc --filter label=com.docker.compose.project=homeserver \
     --format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Status}}'
   ```

   Excluir one-offs e containers já parados da lista de retomada. Conferir que
   o verificador encerrou ou foi reconciliado com procedimento próprio antes
   de qualquer mudança que afete suas origens, destino ou evidências. PID
   isolado, lockfile existente ou contador parado não provam exclusividade.

2. Na janela autorizada, suspender a recuperação automática pelo entrypoint
   instalado e conferir o resultado antes de parar qualquer consumidor:

   ```bash
   sudo /usr/local/sbin/homeserver-usb-recovery --disable
   sudo /usr/local/sbin/homeserver-usb-recovery --status
   ```

   Ambos devem confirmar `enabled:false`. `phase:busy` pode retornar código 0
   e **não confirma** desativação; não seguir nesse caso. A inspeção online
   leu o JSON diretamente, pois até `--status` abre/cria lock e pode gerar log.
   Não usar `--once`/`--recover`: podem montar, iniciar e atualizar bibliotecas.
   Preservar todo o evento existente; não apagar estado para contornar um hold.

3. Fechar entradas pelos containers de proxy capturados, bloquear acessos
   diretos de clientes administrativos e impedir outros jobs de iniciar trabalho.
   No estado observado, entradas incluem `public-proxy`, `service-proxy`,
   `jellyfin-proxy`, `jellyfin-relay`, `seerr-proxy` e `integration-proxy`.
   Manter o marcador de manutenção com sua identidade/dono atuais.
   O mecanismo de bloqueio de acessos diretos e jobs ainda precisa ser definido
   e conferido; parar proxies, sozinho, não comprova essa contenção.

   ```bash
   sudo docker stop --timeout -1 "${HS_ENTRY_IDS[@]}"
   ```

4. Confirmar o fim do ciclo em andamento e entradas já admitidas. O heartbeat
   deve mostrar manutenção, `initialized=1`, idade até 30 s e
   `cycle_deadline_at=NULL`; isso é um reconhecimento, não um checkpoint.
   Depois parar worker, API, gateway e qbit-monitor pelos IDs originais.
   Parar também Jellyfin, Sonarr, Radarr, Bazarr, qBittorrent, Seerr e Prowlarr,
   além dos serviços restantes do projeto que escrevem ou mantêm recursos abertos.

   ```bash
   sudo docker stop --timeout -1 "${HS_CONTROL_IDS[@]}"
   sudo docker stop --timeout -1 "${HS_NATIVE_IDS[@]}"
   sudo docker stop --timeout -1 "${HS_REMAINING_IDS[@]}"
   sudo docker inspect --format '{{.Id}} {{.State.Running}} {{.State.Status}}' \
     "${HS_ALL_IDS[@]}"
   ```

   As quatro listas devem particionar exatamente os containers inicialmente
   ativos. Os 21 serviços observados incluem ainda `host-metrics`, `telemetry`,
   `public-security` e `byparr`; a lista deve ser recapturada, não presumida.
   Conferir ausência de escritores externos aos containers, incluindo jobs
   do host e clientes com acesso direto ao filesystem. Não existe prova de
   quiescência apenas porque o Docker está parado.

   `--timeout -1` evita SIGKILL por expiração, conforme a
   [documentação do Docker](https://docs.docker.com/reference/cli/docker/container/stop/).
   Se algum serviço não sair, manter o procedimento bloqueado e investigar,
   sem forçar a parada. Conferimos a opção `--timeout` no CLI instalado; não
   testamos uma parada real. Não usar `down`, `up`, build ou recreate aqui.

5. Só após quiescência confirmada, capturar o conjunto consistente completo:
   `/srv/appdata` (incluindo bancos e sidecars), claims/filas/estado de controle,
   configurações efetivas, checkout, overrides privados, estado/instalação da
   recuperação e mapa de montagens/identidades. Registrar metadados e erros;
   preservar também cópias recuperáveis das imagens locais necessárias, pois
   guardar somente seus digests não garante que poderão ser obtidas novamente.
   Validar integridade dos bancos **nas cópias**, sem abrir os originais para
   reparo. Uma cópia de arquivos ativos ou apenas do `.db` não atende ao aceite.
   O checkpoint anterior contém somente código/configuração e não o substitui.

   Guardar no HD de 18 TB e neste Windows, com permissões privadas e SHA-256
   conferido depois da transferência. Usar um arquivo/container de backup que
   preserve UID/GID, modos, hardlinks/symlinks, ACLs e xattrs; uma árvore de
   arquivos soltos no NTFS não garante esses metadados. Conferir capacidade dos
   dois destinos e ensaiar a restauração em Linux. Hashes iguais comprovam a
   transferência, não a capacidade de restauração ou consistência das aplicações.
   O manifesto fechado de arquivos e o ensaio de restauração ainda precisam
   ser preparados; não há comando de backup genérico autorizado a excluir
   subdiretórios ou ignorar bancos desconhecidos.

6. Reconciliar delta, prova de conteúdo/topologia e estado por lote antes de
   publicar. Preparar o mapa exato de mounts/binds e alterações de estado para
   revisão. **Os comandos de publicação e mudança de montagem não estão
   liberados por este roteiro**: dependem do executor incremental, contenção
   nativa, prova atual, retorno ensaiado e autorização dos dispositivos/escopo.
   Não executar o pipeline histórico de corte integral/cleanup para preencher
   essa lacuna. Todos os discos de origem permanecem intactos.

## Retomada e rollback

Antes de mudar layout ou publicar, uma parada controlada permite voltar aos
mesmos containers, mounts e configurações. Usar `docker start` somente nos IDs
originalmente ativos, em ordem revisada de dependências; não iniciar one-offs ou
serviços já parados. Isso retoma a configuração original e deve preservar seus
holds; não restaura bancos/estado byte a byte. Aplicativos e qBittorrent podem
escrever ou retomar tarefas ao iniciar, portanto a retomada também depende da
conferência do estado nativo e das entradas. Não autoriza remover manutenção ou
ativar admissão.

Após mudanças de layout/estado, não iniciar consumidores até validar o conjunto
coerente: dados, binds, banco, claims, configurações e imagens. Se falhar antes
de liberar escritas, restaurar esse conjunto a partir do checkpoint ensaiado,
com serviços parados. Se já houve novas escritas, primeiro reconciliar o delta;
restaurar somente um banco antigo sobre mídia nova pode perder estado.

A ordem prevista é infraestrutura/telemetria, consumidores nativos sob contenção
demonstrada, API/gateway, entradas e worker por último. Nenhuma ordem de start
substitui contenção. Testar leitura/reprodução de itens do lote, fila, hardlinks,
capacidade, RO do playback e guard de disco ausente antes da liberação.

Só o dono correto pode remover seu marcador. A manutenção histórica deve ser
resolvida no respectivo incidente; não usar `rm` genérico. Reativar a recuperação
USB somente se estava habilitada antes e configuração/evento forem compatíveis
com containers e layout efetivos. Eventos guardam IDs/fingerprints; recriação
pode gerar `container-replaced`. A reativação não é automática no rollback.

## Trabalho que segue sem indisponibilidade

Implementar e ensaiar contenção nativa em ambiente isolado, preparar o manifesto
de checkpoint/restauração e integrar evidências autênticas ao futuro executor.
Reconciliar a conclusão do verificador e a pendência de reparo quando houver
evidência nova. Só então fechar comandos específicos de corte para a janela.
Nobreak, isolamento em VMs e recuperação após mudança de arquitetura continuam
pendências da [plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/README.md).

Evidências com IDs, paths de incidentes, mapas de overrides e relatórios privados
ficam fora do Git. A presente inspeção não comprova WAN, reprodução, throughput,
retorno físico do USB, restauração ou integridade global do acervo.
