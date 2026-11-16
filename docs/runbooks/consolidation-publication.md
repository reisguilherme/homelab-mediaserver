# Publicar aliases do staging consolidado

**Execução aceita em 16/11/2026:** publicação por hardlinks de 8.209 caminhos,
6.023 arquivos únicos e 5.449.345.898.283 bytes (5,449 TB), sem sobrescrita ou
exclusão. Dois aliases degradados ficaram fora da publicação, com origens
preservadas; seis auxiliares permaneceram fora da biblioteca. O delta posterior
de metadados encontrou os 8.209 publicados e os dois aliases degradados, sem
outros casos no escopo inspecionado. Isso não prova todos os bytes atuais nem
remove a limitação de journals pendentes nas origens. Gatilhos Jellyfin seguem
contidos; não repetir a publicação ou a varredura integral automaticamente.

`scripts/publish-consolidation-target.py` prepara um plano privado e publica
hardlinks no mesmo ext4. A implementação foi usada na operação assistida acima;
os procedimentos abaixo documentam seu contrato. O operador coordena a
quiescência, as provas físicas atuais e a readmissão das aplicações separadamente.

O critério autorizado nesta etapa é **hash histórico autenticado e metadados
atuais**. O publicador não lê o conteúdo da mídia nem exige a revalidação integral
por SHA-256. Isso não comprova que todos os bytes atuais ainda correspondem ao
hash histórico; triagem por reprodução e análise de problemas específicos
continuam possíveis. O recibo registra `current_content_hash_verified=false`.

## Escopo

Os recibos healthy são autenticados pelo SHA-256 do recibo privado de extração,
pelo pin do manifesto de checkpoint e pelos hashes individuais. Os pins são
parâmetros operacionais. O programa importa somente primitivas do verificador
`verify-consolidation-target.py`, que deve estar no mesmo diretório; ele não chama
a leitura de conteúdo desse verificador nem importa o publicador legado.

Somente caminhos com primeiro componente `media` ou `torrents` são publicados.
O plano remove o prefixo histórico do pool ao mapear cada alias para
`homeserver/<caminho>`. As três procedências aceitas são `ssd`, `hdd` e `hdd2`;
não é necessário montar suas fontes antigas. Arquivos auxiliares de recuperação,
o grupo degraded, seus aliases ausentes e `.rsync-partial` ficam fora da publicação.
Nenhum inventário por varredura amplia essa lista. Grupos com aliases mistos
mantêm os aliases fora do escopo no staging e publicam somente os permitidos.

A cobertura histórica autenticada desta etapa tem 6.029 grupos healthy,
8.215 caminhos e 5.449.345.903.837 bytes por inode. Seis caminhos auxiliares
identificados pelo operador ficam fora do escopo público. O resumo do plano
informa grupos, caminhos e bytes selecionados e excluídos; não declarar o acervo
inteiro pronto a partir dessas contagens.

## Preparação e comandos

Executar como root no guest core Linux, com `linkat(AT_EMPTY_PATH)` disponível.
Não há fallback para copiar ou vincular por um caminho de origem substituível.
O destino `homeserver` precisa existir. O plano e seu journal devem ficar em um
diretório **novo** sob um pai privado 0700 no SSD, em device diferente da mídia;
arquivos operacionais usam 0600. Não colocar esse diretório dentro da evidência
restaurada. Os exemplos usam placeholders privados, não valores de inventário.

Antes e durante a operação, manter um único operador e nenhum escritor de mídia:
apps do core paradas, playback RO, scans suspensos e cópias/verificação antiga
paradas. O programa não executa Docker, não altera mounts e não readmite apps.
Seu lock exclui outra execução sobre o mesmo plano; a exclusão entre planos e
os demais escritores é responsabilidade do operador.

```bash
python3 scripts/publish-consolidation-target.py plan \
  --mountpoint "$MOUNT" --stage-relative "$STAGE_RELATIVE" \
  --publication-relative homeserver \
  --boot-id "$BOOT_ID" --filesystem-uuid "$FILESYSTEM_UUID" \
  --mount-id "$MOUNT_ID" --device "$MAJOR_MINOR" \
  --evidence-directory "$EVIDENCE_ON_SSD" \
  --evidence-receipt-sha256 "$EXTRACTION_RECEIPT_SHA256" \
  --checkpoint-manifest-sha256 "$CHECKPOINT_MANIFEST_SHA256" \
  --expected-groups 6029 --expected-paths 8215 --expected-bytes 5449345903837 \
  --directory-uid 1000 --directory-gid 1000 --directory-mode 0755 \
  --plan-directory "$NEW_PRIVATE_PLAN_DIRECTORY"

python3 scripts/publish-consolidation-target.py apply --execute \
  --plan-directory "$NEW_PRIVATE_PLAN_DIRECTORY" --plan-sha256 "$PLAN_SHA256" \
  --lease-path /var/lib/homelab-storage/core-writer-admission/lease
```

Guardar o `plan_sha256` devolvido pelo primeiro comando, conferir o resumo e
fixá-lo no segundo. O plano prende também os hashes dos dois scripts, boot,
namespace, UUID, mount ID, device e identidades das raízes. Alterar o código depois
de planejar invalida o plano. Não mudar os pins para contornar uma recusa.

A lease é lida diretamente no host do guest: protocolo v2 de nove campos,
`role=core-writer`, `state=ready`, boot/UUID correspondentes, geração física
SHA-256 presente e validade de até 45 segundos por `CLOCK_BOOTTIME`. Diretórios
ancestrais devem pertencer a root sem escrita de grupo/outros; arquivo regular,
root 0644, um link, sem symlinks. A geração e a geração física permanecem estáveis
e a sequência não pode regredir, inclusive entre processos pelo journal. A
mesma sequência exige conteúdo idêntico, incluindo o prazo. A renovação normal
com sequência crescente é aceita. Cada mutação exige nova
leitura; expiry, HOLD ou mudança de geração interrompem novas mutações.

## Preflight, journal e retomada

O planner autentica todos os recibos healthy, compara a topologia e metadados
atuais de cada grupo selecionado e verifica globalmente todos os destinos e
ancestrais. Inode, tamanho, modo, uid/gid, mtime, nlink e hashes de xattrs/ACLs
são acompanhados. Device/inode históricos não substituem identidade atual.
Qualquer destino já existente causa HOLD, mesmo sendo bytes iguais ou um
hardlink conhecido fora deste journal. Diretórios regulares existentes são
preservados. Novos diretórios usam os parâmetros explícitos do plano; não se
executa chmod/chown/utime em arquivos ou diretórios existentes.

Uma intenção pequena e encadeada por hash é persistida com fsync antes de cada
mkdir/link. O arquivo de origem permanece aberto com O_NOFOLLOW/O_NOATIME, e
`linkat` usa seu FD. O novo link é criado sem sobrescrita. O inode e o diretório
de destino recebem fsync, os aliases e os efeitos são conferidos, e a conclusão
é persistida antes da próxima mutação. O efeito esperado é nlink +1 e ctime não
anterior ao registrado; os outros metadados permanecem iguais. Metadados de
tempo dos diretórios mudam naturalmente com a criação de filhos.

`apply --max-links N` permite um piloto ou lotes. O retorno
`publication_partial` é progresso, sem recibo final. Repetir **o mesmo plano e
pin** continua a partir do journal. Não criar outro plano para ignorar links
já existentes. Para reconciliar somente a intenção pendente:

```bash
python3 scripts/publish-consolidation-target.py reconcile --execute \
  --plan-directory "$NEW_PRIVATE_PLAN_DIRECTORY" --plan-sha256 "$PLAN_SHA256"
```

Destino ausente e assinatura original intacta permitem concluir a intenção
pendente; um destino no inode esperado com nlink exatamente +1 permite fsync e
registro da conclusão sem outro link. Divergência de inode, ctime inexplicado,
metadados ou nlink causa HOLD. Não há rehash ou reparo automático para aceitar a
divergência. Um mkdir interrompido antes de registrar o inode tem identidade
ambígua: a retomada dá HOLD se o diretório já existir. Journal truncado, corrompido
ou com registro incompleto também dá HOLD; não há truncamento/limpeza automática.

Código de saída 2 e JSON `phase=HOLD` exigem investigação operacional. Não repetir
em loop nem iniciar outro worker enquanto o anterior estiver preso. SIGINT e
SIGTERM cooperam entre syscalls. I/O em kernel D-state não tem prazo garantido.
Uma interrupção pode deixar links visíveis; o journal preserva essa situação,
sem falso recibo de sucesso e sem remoção de mídia.

## Aceite e limites

`publication_complete` exige nova conferência de todos os aliases originais,
destinos, contagens finais e admissibilidade atual. O novo
`publication-receipt.json` vincula plano e journal, registra o critério histórico
e preserva explicitamente `degraded_published=false` e `all_media_ready=false`.
Ele é uma prova pontual da publicação, não monitoramento contínuo. Após escrita
legítima pelas apps, executar novamente o plano antigo pode dar HOLD por mudanças
de metadados, e os recibos históricos continuam representando o estado anterior.

**Hardlinks compartilham o inode.** Escrita futura por uma aplicação RW em um
nome publicado também altera os aliases correspondentes do staging. O staging
passa a ser um conjunto de aliases, não um backup imutável. Esta operação não
remove fontes RO ou checkpoint. Não há comandos de rollback, delete, cleanup,
cópia, publicação de degradados ou início de apps neste publicador.

Inventário, títulos, pins privados, planos e journals ficam fora do Git. Stdout
contém somente contagens, hashes e motivos fixos de HOLD, sem caminhos de mídia.
