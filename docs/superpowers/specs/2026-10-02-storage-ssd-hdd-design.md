# Armazenamento com SSD prioritário e HD USB

Status: implementação concluída e validada com 1.359 testes em Linux, lint,
ShellCheck e Compose. Após o bloqueio de escrita NTFS,
o usuário autorizou formatar o HD e fazer a preparação necessária. Somente a
partição de mídia foi formatada em ext4; SSD e demais partições foram preservados.
A montagem passou nos testes físicos descritos ao final. O usuário autorizou
explicitamente as duas entradas persistentes no fstab; a montagem gerada pelo
systemd foi iniciada com sucesso e sua ordem antes do Docker foi conferida.
Essas verificações não comprovam um reboot real.

## Resultado solicitado

Para cada torrent, calcular o tamanho real dos arquivos e os bytes ainda a baixar
da fila de cada disco. Preferir o SSD. Se não couber nele, verificar se o HD USB
está montado, identificado, gravável e com espaço suficiente; nesse caso baixar
nele e concluir validação, legendas, importação, seeding e exclusão coordenada.
Se nenhum disco comportar o torrent, manter o pedido aguardando capacidade.

Uma série pode ter episódios antigos no SSD e novos no HD. A ordem de importação
continua sendo por temporada/episódio; torrents podem baixar simultaneamente.
Um pacote de temporada inteiro ocupa um único disco. O painel mostra os dois
discos separadamente, inclusive quando o HD está indisponível.

## Evidência obtida em 2 de outubro de 2026

- O SSD secundário de 512 GB está montado em `/srv/data`, com ext4 e cerca de
  254 GB utilizáveis livres no momento da inspeção.
- O HD USB de 1 TB contém uma partição NTFS chamada MEDIA, de aproximadamente
  999 GB. O barramento negociou 5000M. Na inspeção inicial ela estava desmontada.
- A leitura dos metadados NTFS indicou que essa partição está praticamente vazia.
  Uma montagem temporária somente leitura confirmou cerca de 998,7 GB livres
  e ausência da pasta `homeserver`. Existem outras partições no dispositivo;
  não fazem parte desta proposta.
- A instalação possui `ntfs-3g` e o módulo `ntfs3`. Escrita, hardlinks e
  desconexão/reconexão ainda precisam de testes físicos antes da ativação.
- A tentativa posterior de montagem `rw,permissions,norecover` foi recusada pelo
  NTFS-3G: volume em estado inseguro, com fallback somente leitura. A criação da
  fixture falhou com `EROFS`, antes de criar arquivos. A montagem temporária foi
  desmontada. É necessário corrigir o estado do NTFS no Windows antes de provar
  as capacidades e habilitar o HD; não foi executado reparo forçado.
- O código atual assume `/data/torrents`, `/data/media` e uma única evidência de
  capacidade. A admissão real acontece na emissão da permissão do torrent.
- Sonarr importa para o caminho da série. Somente cadastrar outra pasta raiz
  não permite distribuir os episódios da mesma série entre discos.

UUIDs reais e inventário detalhado ficam na instalação, fora do Git.

## Alternativas avaliadas

1. **Pastas raiz separadas:** suficiente para atribuir filmes e séries inteiras
   a um disco. Não atende ao fallback por episódio de séries existentes.
2. **Visão única com mergerfs e admissão por disco:** mantém os caminhos que
   Arr/Jellyfin conhecem e permite episódios nos dois discos. É a recomendação.
3. **Importador próprio com links simbólicos ou movimentação de séries:** amplia
   muito o contrato de leitura, renomeação e exclusão e não é recomendado aqui.

O mergerfs apresenta uma árvore de diretórios; não decide se o torrent cabe.
A documentação explica que a política de criação não conhece o tamanho final
do arquivo. Essa decisão permanece no controlador, com transação e capacidade
por disco, sem usar o espaço agregado da visão.

## Layout proposto

```text
/srv/data/                       SSD físico existente
  torrents/                     downloads existentes
  torrents/.placements/ID/      novos torrents atribuídos ao SSD
  media/movies/
  media/tv/

/srv/external/                   montagem NTFS por UUID
  homeserver/                   pasta exclusiva do projeto
    torrents/.placements/ID/    novos torrents atribuídos ao HD
    media/movies/
    media/tv/

/srv/media-view/                 visão de /srv/data + /srv/external/homeserver
```

Os serviços recebem `/srv/media-view` como uma única montagem `/data`, com as
permissões apropriadas. Origem e destino de hardlinks devem atravessar a mesma
montagem lógica. Não introduzir submontagens físicas dentro de `/data/torrents`
nos containers do Arr, pois isso causaria `EXDEV`.

A mídia atual permanece no SSD. A visão é montada em um novo diretório, nunca
sobre a origem `/srv/data`. O conteúdo preexistente do HD não é incorporado;
somente a pasta `homeserver` participa.

## Identidade e admissão

- Cadastrar os pools `ssd` e `hdd` com identidade física estável, raízes
  permitidas e prioridade. O UUID esperado é adotado explicitamente na
  preparação da instalação; não aceitar automaticamente um disco substituto.
- Snapshots por pool contêm identidade, instante da medição, total, usado,
  livre utilizável, presença e condição de leitura/escrita.
- Comprovar a montagem física. Um diretório existente, um bind Docker ou o
  tamanho total do filesystem não identificam o disco esperado.
- Persistir `pool_id`, identidade física e destino em cada permissão de torrent.
  A reserva de temporada é uma identidade lógica e pode conter permissões dos
  dois pools. Permissões antigas são associadas ao SSD após conferência.
- Dentro da transação de admissão, descontar somente compromissos do pool
  avaliado. Incluir os bytes restantes de torrents pausados da fila, sem
  descontar novamente bytes já alocados. Um torrent confirmado conta uma vez.
- Selecionar SSD primeiro; se insuficiente, avaliar HDD. Nunca somar os livres
  para aceitar um torrent que não cabe inteiro em nenhum disco.
- Criar um diretório exclusivo da permissão apenas no pool escolhido. Verificar
  que não existe contraparte no outro pool. O gateway resolve a permissão por
  identidade verificada e aplica seu destino ao add; o path vindo do Arr não
  pode autorizar outro disco.
- O qBit usa esse destino também para incompletos, sem AutoTMM que o mova.
  Não pré-marcar arquivos como completos e não alocar uma cota fixa por filme.

## Permanência no disco escolhido

A combinação proposta para teste é `category.create=epff`,
`ignorepponrename=true` e `inodecalc=hybrid-hash`. Criação usa a ramificação do
diretório exclusivo; importação por hardlink cria o destino na ramificação da
origem. Desabilitar explicitamente `moveonenospc`, `link-cow`, `symlinkify` e
qualquer conversão de hardlink para symlink.

A política sozinha não protege contra reconexão ou desaparecimento de uma
ramificação. Os pais de `.placements/ID` precisam impedir que o UID do qBit
recrie o diretório em outro pool. Somente o controlador cria os diretórios
autorizados; o qBit escreve dentro deles. Validar essa proteção com o HD ausente,
inclusive antes de criar um subdiretório novo do torrent. Se ela não puder ser
comprovada, não habilitar downloads no HD.

Usar o pool físico para contabilização e conferência da fonte. O `st_dev` da
visão mergerfs identifica a visão, não o disco. Revalidar identidade e destino
na adição, retomada, importação, substituição e exclusão.

## Importação, legendas e exclusão

Antes de habilitar o HD, testar criação, reabertura, hardlink, renomeação e
exclusão de uma fixture com o UID/GID efetivos dos serviços. Comprovar que o
hardlink da biblioteca usa o mesmo arquivo físico da fonte. Se o NTFS/driver
não passar, manter o pool indisponível para novas admissões; não cair numa
cópia que dobre o consumo sem contabilização.

Gerar legendas externas no pool do vídeo e instalar o arquivo final pela visão
unificada quando a escrita for do controlador. O Bazarr mantém sua escrita
nativa: em uma pasta de temporada presente nos dois discos, uma legenda pequena
pode ficar no SSD enquanto o vídeo fica no HD. Ambos permanecem na mesma pasta
lógica. Contabilizar o uso real de cada disco e capturar a identidade física de
cada arquivo auxiliar separadamente para a exclusão, inclusive quando seu pool
difere do vídeo. Não introduzir um movimentador automático só para essas legendas.

Preservar a importação sequencial e as verificações de qualidade existentes.
A exclusão explícita pelo Jellyfin identifica o pool real e remove somente
os arquivos e vínculos capturados. Pacotes compartilhados seguem a proteção
atual; HD ausente não equivale a arquivo apagado e não confirma exclusão.

## Ausência, reinício e reconexão

- HD ausente deixa seus pedidos aguardando armazenamento; o SSD continua útil.
- Nunca escrever na pasta vazia do ponto de montagem no disco do sistema.
- Não recriar torrents do HD no SSD nem transferir downloads em andamento.
- Após reconexão, conferir UUID, montagem, permissões e arquivos antes de retomar.
- A continuidade do SSD cobre HD ausente ou desmontado com fallback protegido.
  Desconexão abrupta que deixe um bind com erro de I/O, ou uma montagem substituta
  desconhecida, exige recuperação explícita: remontar o UUID correto e recriar
  containers. Enquanto não for possível provar os guards, recusar novas escritas,
  mesmo no SSD. Não ignorar o erro nem mudar branches automaticamente.
- Proteger scanners e operações de limpeza contra a ausência temporária do HD.
- Documentar montagem por UUID e inicialização ordenada antes do Compose. Não
  reintroduzir units próprias, CI/CD, releases ou gerenciadores de backup.
- Qualquer entrada de montagem persistente deverá ser uma adição específica
  revisada; preservar todas as entradas existentes do host.

## Painel e configuração

Mostrar cards SSD e HD USB com total, usado físico, livre utilizável, restante
da fila, disponível para novos downloads, estado e idade da amostra. Separar
filmes, séries, torrents e outros usos por disco, deduplicando hardlinks.
HD ausente mostra indisponível, sem números apresentados como medição atual.

Atualizar métricas do host mesmo se um pool falhar. Manter temporariamente os
campos legados de capacidade como aliases do SSD, sem torná-los uma soma.
Preferências editáveis continuam no `.env`; layout técnico e montagem ficam
na preparação documentada da máquina. Não criar arquivos de secrets.

## Áreas da implementação e validação

1. Registro/guard de pools e snapshots: `services/common`, `scripts/host-metrics.py`,
   `scripts/capacity-snapshot.py`, Compose e renderizador.
2. Admissão e roteamento: `persistence/db.py`, `gateway/permits.py`,
   `gateway/app.py`, `worker/capacity_evidence.py` e aquisição de filmes/séries.
3. Importação e exclusão: finalizadores, legendas, captura Jellyfin, coordenador
   de exclusão, recuperação e permissões de packs/probes.
4. Painel e guias: telemetria/status, template, contratos, README, instalação,
   configuração e diagnóstico.

Testes obrigatórios: cabe no SSD; cabe somente no HD; soma cabe mas nenhum
disco individual cabe; HD ausente/RO/UUID errado; mountpoint vazio no SO;
admissões concorrentes; fila pausada; atualização legada; pacote em um pool;
episódios da mesma série em pools distintos e importação sequencial; legendas;
hardlinks sem cópia duplicada; exclusão de episódio/temporada/filme; desconexão
durante escrita; reconexão e reboot; dois cards independentes e deduplicação.

Executar fixtures em Linux/WSL e depois fixtures pequenas na montagem real.
Não encher o SSD real para testar fallback. Usar evidência controlada nos testes
automatizados e uma admissão de fixture explicitamente direcionada no teste
físico, removendo somente os artefatos identificados da própria fixture.

## Autorizações e evolução da instalação

O `AGENTS.md` atual determina: “Não formatar/particionar discos, ativar
mergerfs/RTX, substituir `fstab`”. Esta proposta solicita exceção somente para
mergerfs e preparação persistente das montagens necessárias, preservando o
NTFS, as partições, os arquivos existentes e as demais configurações do host.
Essa era a proposta inicial. Diante da recusa de escrita do NTFS, o usuário
autorizou posteriormente a formatação. A implementação da instalação passou
a usar ext4 no HD, sem migração ou exclusão automática de mídia do SSD.

Testes físicos posteriores comprovaram escrita como UID/GID 1000, hardlinks no
mesmo inode físico em ambos os discos, afinidade do destino com a origem,
rename/unlink, xattrs de identificação da ramificação e recusa de recriação de
destino quando o HD foi desmontado. A remontagem preservou a fixture; todos os
arquivos temporários desses testes foram removidos. Não foi feito reboot.

## Fontes técnicas verificadas

- [Sonarr: construção do destino da série](https://raw.githubusercontent.com/Sonarr/Sonarr/main/src/NzbDrone.Core/Organizer/FileNameBuilder.cs)
- [mergerfs: tamanho do arquivo e políticas](https://trapexit.github.io/mergerfs/latest/faq/configuration_and_policies/)
- [mergerfs: rename e hardlink](https://trapexit.github.io/mergerfs/latest/config/rename_and_link/)
- [mergerfs: identidade de inodes](https://trapexit.github.io/mergerfs/latest/config/inodecalc/)
- [mergerfs: compatibilidade, incluindo NTFS](https://trapexit.github.io/mergerfs/latest/faq/compatibility_and_integration/)
- [Kernel 6.8: NTFS3](https://www.kernel.org/doc/html/v6.8/filesystems/ntfs3.html)
