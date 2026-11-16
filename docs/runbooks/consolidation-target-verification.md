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
com limite de memória de 384 MiB, CPU de 75% de um núcleo e I/O ocioso. Isso é
execução em andamento, não aceite de 5,45 TB ou consolidação encerrada. Evidências
e comandos operacionais ficam no handoff privado. A operação não é uma unit
persistente no host Proxmox nem volta automaticamente depois de reboot.

O resultado não comprova a saúde física de toda a superfície do disco, dados
novos depois do inventário histórico, resolução de journals das origens,
publicação, rebind do ledger ou prontidão das aplicações. `media_ready` e
`published` permanecem falsos mesmo quando o escopo íntegro termina. A leitura
inclui eventual cache do filesystem. O candidato degradado continua assim
classificado mesmo quando seu hash coincide com a prova histórica.

Para publicar, revisar primeiro colisões, cobertura/delta final, permissões e
um journal próprio da operação. A preparação do core permanece descrita em
[core-migration.md](core-migration.md).
