# Importação de fontes auditadas

`HOMESERVER_AUDITED_IMPORT_MANIFEST` fica vazio por padrão. Um operador pode
ativar uma fila de fontes e arquivos já aprovados apontando para
`/var/lib/homeserver/audited-imports.json`, ao lado de `control.sqlite` no
container. No host esse arquivo privado fica em `/srv/appdata/control`.
O arquivo deve ter modo `0600`, ser regular, não conter links simbólicos em
seu caminho e não conter credenciais, hashes de torrent ou URLs de aquisição.

O manifesto usa `schema_version: 1`, `mode: audited_manifest`, `manifest_id`,
`manifest_sha256` e uma lista `jobs`. Cada job contém um permit real de payload
inteiro, a reserva, o digest do descritor, IDs nativos atuais, o vetor completo
`payload_files` e sua divisão exata entre `files` e `excluded_files`.
`padding_bytes` é contabilizado separadamente: conteúdo mais padding deve ser
igual ao orçamento real da reserva. Arquivos extras continuam no payload e
no orçamento; não são importados. Um vídeo que cobre dois episódios contém
os dois IDs e escopos no mesmo item de `files`.

Uma versão posterior usa novo `manifest_id` e acrescenta jobs. Jobs anteriores
devem continuar presentes e imutáveis. A troca do arquivo é atômica; histórico,
vínculo do manifesto original e estados de importação permanecem no SQLite.
O descritor deve estar em `TorrentArtifactStore` e vinculado ao permit antes
de publicar o job ou despachar o download pelo gateway.

Com a fila ativada, o worker executa somente essa fase por padrão, com até
quatro arquivos por ciclo. Para processar novos pedidos do Seerr ao mesmo
tempo, configure `HOMESERVER_AUDITED_IMPORT_ALLOW_SEERR="true"` e reinicie o
worker. O manifesto continua ativo e precisa ser validado e selado em cada
ciclo; uma falha nessa validação também bloqueia novas aquisições do Seerr.

No modo de coexistência, os dois fluxos usam o mesmo registro de permits e
a capacidade física disponível. Filmes do manifesto e todas as temporadas
alcançadas por seus arquivos, inclusive packs multitemporada e alvos já
importados, ficam fora da admissão, busca e finalização normal do Seerr.
Temporadas novas de uma série auditada preservam seu perfil nativo; um pedido
com perfil divergente fica adiado, com motivo registrado. Reservas do operador
continuam separadas dos cancelamentos do Seerr. Reconciliações globais de
troca, descarte, prioridade e exclusão permanecem desativadas nesse modo para
não alcançarem as fontes auditadas.

O mecanismo nativo de Completed Download Handling do Sonarr/Radarr deve
permanecer desativado. Para voltar exclusivamente ao fluxo normal, o operador
esvazia o caminho do manifesto e reinicia o worker após revisar as pendências.

O import exige download completo, todos os arquivos de conteúdo com tamanhos
exatos, permit confirmado e ativo, placement físico válido, vídeo com áudio
detectado e capacidade de hardlink como UID/GID 1000. Os IDs e escopos nativos
são conferidos antes de um `ManualImport` com caminhos exatos em modo `Copy`
e hardlinks habilitados. Não há novo filtro de resolução, formato ou tamanho.
O retorno nativo deve apontar para o mesmo inode no pool admitido antes de
registrar a mídia importada ou pedir legendas.

As tabelas `audited_import_*` registram `planned`, `import_unknown`,
`import_requested`, `imported`, `subtitle_pending` e `complete`, com motivo e
próxima tentativa. A intenção é gravada antes do POST. Uma resposta perdida
nunca é repetida automaticamente: a confirmação depende do arquivo nativo e
de seu inode. Se nenhum arquivo aparecer, a pendência requer reconciliação
do operador. A reserva real termina quando todos os arquivos do job terminam;
nenhum recibo de episódio fictício é criado.

Séries buscam legendas somente no SubDL, com o ID e os escopos do arquivo.
Filmes preservam os provedores e o handoff existentes. Ausência de legenda
mantém `subtitle_pending` explícito sem repetir o import já confirmado. SRTs
validadas são criadas no branch físico admitido, com o nome do vídeo e o
sufixo de idioma. A verificação física ainda depende dos arquivos reais;
fixtures não comprovam reprodução nem disponibilidade do provedor.
