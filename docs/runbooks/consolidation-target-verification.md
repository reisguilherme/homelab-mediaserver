# Verificação das cópias já no destino

`scripts/verify-consolidation-target.py` lê apenas os grupos íntegros descritos
por recibos históricos autenticados. Produz provas atuais de conteúdo no SSD,
sem abrir os discos de origem, copiar/publicar mídia ou iniciar aplicações.
O caso degradado e arquivos parciais permanecem fora desse escopo.

O operador fornece os hashes fixados do recibo de extração e do manifesto do
checkpoint, os diretórios privados de evidência/saída, o mount exato do destino,
o caminho relativo do staging, boot ID, UUID, mount ID, major:minor e as contagens
esperadas de grupos, caminhos e bytes. Os valores reais são inventário privado;
não usar identidades do Ubuntu anterior como prova do guest atual.

O primeiro lote exige um diretório de saída novo. Lotes seguintes usam os mesmos
argumentos e `--resume`. Limites padrão: 32 grupos, 64 GiB, 64 MiB/s e 1.800 s
por lote. A ordem é tamanho crescente, seguida do digest do grupo; ela fica
vinculada à sessão. Um arquivo maior que o orçamento não é dividido em provas
parciais: é necessário revisar o orçamento. Interrupção durante um arquivo
descarta o hash incompleto; o próximo lote começa esse arquivo novamente.

Cada grupo recebe uma leitura SHA-256 por inode, conferência de todos os aliases,
metadados antes/depois e comparação com o hash autenticado. `O_NOATIME` é
obrigatório, sem fallback; não há fsync ou alteração deliberada de mídia.
Mudanças de boot, mount, namespace, roots, conteúdo, metadados ou evidência
impedem aceitar o grupo. O estado novo fica em filesystem distinto do destino,
com diretórios privados e lock para impedir verificadores concorrentes.

| Resultado JSON / saída | Ação |
| --- | --- |
| `batch_limited`, saída 0 | Lote encerrado; conferir progresso antes de executar outro com `--resume`. |
| `healthy_scope_complete`, saída 0 | Todos os grupos desse escopo têm leitura atual e aliases revalidados; não publica mídia. |
| `hold`, saída 2 | Parar e diagnosticar. Não repetir automaticamente nem mudar pins para contornar a causa. |

Um executor de vários lotes deve exigir avanço de `verified_groups`, parar ao
completar o escopo ou diante de HOLD e manter limite de recursos. O script não
instala serviços nem agenda reinícios. Sinais são atendidos entre leituras;
I/O preso no kernel não tem garantia de parada imediata. Progresso não substitui
a inspeção do processo executor e do frescor da observação.

Em 16/11/2026, 39 testes Linux passaram e a revisão independente acrescentou
uma reprodução de relógio lento. O piloto real conferiu 32 grupos/2.078 bytes;
a coordenação iniciou depois um executor transitório no guest, sem restart,
com limite de memória de 384 MiB, CPU de 75% de um núcleo e I/O ocioso. O
proprietário pediu sua interrupção no mesmo dia e substituiu a releitura integral
por triagem de reprodução. A parada foi confirmada: unit inativa, PID zero e
nenhum processo verificador restante. O último progresso registrava 3.824 grupos
e 84.489.546.300 bytes; a ordem por tamanho explica a diferença entre proporção
de grupos e de bytes. Os recibos parciais foram preservados. Não retomar esse
job sem nova orientação, nem declarar aceite integral. Evidências e comandos
operacionais ficam no handoff privado. A operação não é uma unit persistente
no host Proxmox nem volta automaticamente depois de reboot.

## Critério atual: reprodução

A conferência SHA-256 integral deixou de ser uma condição escolhida pelo
proprietário para a migração. Preservar a procedência das cópias e verificar
colisões/caminhos continua necessário; não inventar hashes atuais para o escopo
que não foi relido. A triagem trabalha com arquivos efetivamente disponíveis
no caminho da biblioteca, separando os que ainda aguardam publicação.

O piloto somente leitura usou três inodes distintos já presentes: resposta
autenticada HTTP 206 do Jellyfin com os primeiros 64 KiB iguais ao arquivo NFS,
seguida de decodificação pelo FFmpeg do Jellyfin de trechos solicitados de três
segundos no início, meio e fim. Vídeo e primeiro áudio, quando existente, foram
selecionados; houve frames e saída sem erro fatal nos nove trechos. Guards
passaram antes/depois e o histórico de usuários permaneceu igual. Um quarto
item considerado não estava no caminho atual; foi separado, sem exclusão.

Isso comprova entrega HTTP e decodificação amostral pelo servidor, não reprodução
em cada cliente, áudio percebido, todos os frames ou integridade do arquivo
inteiro. Nem todo erro implica defeito de conteúdo: ausência por migração,
autorização, rede, storage, codec indisponível, timeout e seek inconclusivo devem
ser diagnosticados. Parar o lote se o controle/guard falhar; não multiplicar
processos quando I/O estiver bloqueado.

A orientação do proprietário permite excluir cópias com defeito de conteúdo que
impeça reprodução, preservando as origens. Registrar identidade, caminhos e
aliases, erro fatal reproduzível e controle compatível funcionando antes de
classificar uma exclusão. Não excluir automaticamente por HTTP diferente de 200,
ausência no catálogo, retorno não zero do decoder ou aviso recuperável. O piloto
não identificou candidatos e **nenhum arquivo foi apagado**. A triagem completa
e a resolução dos caminhos ainda estão pendentes.

O resultado não comprova a saúde física de toda a superfície do disco, dados
novos depois do inventário histórico, resolução de journals das origens,
publicação, rebind do ledger ou prontidão das aplicações. `media_ready` e
`published` permanecem falsos mesmo quando o escopo íntegro termina. A leitura
inclui eventual cache do filesystem. O candidato degradado continua assim
classificado mesmo quando seu hash coincide com a prova histórica.

Para publicar, revisar primeiro colisões, cobertura/delta final, permissões e
um journal próprio da operação. A preparação do core permanece descrita em
[core-migration.md](core-migration.md).
