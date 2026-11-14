# Planejar a migração por lotes

O requisito é servir o conteúdo já validado no HD de 18 TB e acrescentar o
restante ao longo dos dias, preservando todos os discos de origem. Esta etapa
prepara o cálculo dos lotes no projeto de mídia. Não é um comando de migração
nem um executor de cópia/publicação.

## Estado e fronteira operacional

Em 14/11/2026, uma nova inspeção SSH observou o verificador histórico ativo,
avançando de 1.701 para 1.705 dos 6.029 grupos da classe saudável. O relatório
ainda indicava uma pendência de reparo e prova global incompleta. Contar grupos
não mede percentual de bytes, integridade global ou tempo restante.

Jellyfin e telemetria responderam HTTP 200; o controlador permaneceu com 503
no endpoint de readiness. Nenhum processo foi parado, nenhuma manutenção foi
removida e nenhum segundo pipeline foi iniciado. Esses checks não demonstram
reprodução real ou capacidade de migração em paralelo.

O corte continua aguardando aviso explícito do proprietário. O pipeline antigo
exige corte integral e uma etapa de limpeza de origens; não deve ser reaproveitado
para este requisito. A manutenção do controlador não impede, sozinha, scans
internos de Jellyfin/Arr que possam reagir à ausência de arquivos.

## API de planejamento

Módulo: `homeserver_control.domain.migration_batches`. A API é pura: não lê
filesystem, não acessa APIs e não altera banco, claims, fila, mounts ou dados.
Seu uso é programático, por testes ou por um futuro adaptador revisado.

| Tipo/função | Papel |
|---|---|
| `MigrationGeneration(generation_id, source_id, destination_id)` | Contexto imutável do snapshot; IDs lógicos, sem nomes instáveis de device |
| `MigrationGroup(group_id, paths, logical_bytes, content_sha256, dependency_ids=())` | Um inode físico com todos os paths e dependências de outros grupos |
| `DeclaredReceipt(generation_id, source_id, destination_id, group_id, manifest_digest, phase)` | Referência declarada a uma evidência de fase; não é uma prova autenticada pelo módulo |
| `manifest_digest(group)` | Digest canônico do manifesto para impedir reaproveitar referências após mudança de grupo |
| `plan_batch(generation, groups, selected_paths, receipts=())` | Expansão completa dos grupos/dependências e avaliação das referências declaradas |

Todos os caminhos são relativos à raiz lógica da migração. O adaptador precisa
comprovar a identidade real dos dispositivos, mapear todas as origens e destinos,
estabilizar a geração e descobrir os links físicos. Dois inodes com conteúdo
igual continuam sendo dois grupos: o hash não autoriza deduplicação física.

O digest liga as referências aos campos fornecidos. Não prova que o inventário
está completo ou atual, que a hash foi calculada, que o recibo tem origem confiável
ou que o filesystem corresponde aos paths. O futuro adaptador deve verificar
essas propriedades antes de usar o plano, inclusive links não informados.

## Exemplo sintético

```python
from homeserver_control.domain.migration_batches import (
    MigrationGeneration,
    MigrationGroup,
    plan_batch,
)

generation = MigrationGeneration("generation-1", "source-a", "target-a")
group = MigrationGroup(
    group_id="group-1",
    paths=("library/example.mkv", "payload/example.mkv"),
    logical_bytes=1024,
    content_sha256="a" * 64,
)
plan = plan_batch(generation, (group,), ("library/example.mkv",))
assert plan.selected_paths == ("library/example.mkv", "payload/example.mkv")
assert plan.total_logical_bytes == 1024
assert plan.execution_allowed is False
```

O hash e os bytes acima são fictícios. Selecionar um membro expande todo o grupo
e suas dependências. `total_logical_bytes` conta cada grupo uma vez; não reserva
espaço nem contabiliza sparse allocation, metadados, staging já ocupado, margem,
espaço efetivo ou compromissos de outros consumidores.

## Interpretar o resultado

`blocked_reasons` informa inconsistências que impedem um lote positivo. Campos
malformados são rejeitados. Colisões entre grupos bloqueiam mesmo com hashes
iguais, assim como conflitos entre um arquivo e um descendente desse path.
Dependências ausentes ou cíclicas não devem gerar publicação parcial.

`evidence_states` separa referências às fases `staging_verified`,
`publication_verified` e `native_readback_verified`. Só uma cadeia compatível
com a geração, identidades e manifesto promove os indicadores declarados. Uma
fase final isolada ou recibo de outra geração não equivale a disponibilidade.
Esses nomes descrevem o tipo de evidência que o adaptador deverá produzir.

O módulo confere a presença das fases compatíveis; não autentica uma cadeia
temporal, não liga recibos ao predecessor e não verifica tentativas, leases ou
fencing. O adaptador deverá rejeitar evidências de tentativas incompatíveis e
renovar a geração quando houver mutação ou incerteza sobre o snapshot. Reunir
três declarações não comprova que as operações ocorreram nessa ordem.

Os quatro flags operacionais permanecem falsos em todo resultado:
`execution_allowed`, `source_cleanup_allowed`, `global_scan_allowed` e
`deletion_allowed`. Nem uma lista vazia de bloqueios nem todos os indicadores
de evidência verdadeiros liberam execução no servidor.

## Dependências para implementar o executor

1. Reconciliar o verificador ativo, pendências de reparo, provas e publicação
   anterior. Não iniciar outro escritor enquanto o pipeline mantém seus locks.
2. Produzir inventário fresco e privado por inode, links, conteúdo, dependências
   e colisões, diferenciando biblioteca nativa e staging. Medir capacidade real.
3. Demonstrar contenção dos scans e efeitos de ausência no Jellyfin/Arr, além dos
   guards do controlador. Ausência durante migração nunca autoriza exclusão.
4. Implementar journal, lock/fencing, quiescência por lote, cópia sem sobrescrita,
   prova de conteúdo/topologia e leitura nativa antes da publicação/retomada.
5. Ensaiar interrupção, retorno do USB, geração alterada e rollback em fixtures;
   revisar o conjunto de comandos e o estado real antes da janela autorizada.

A [plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/README.md)
mantém a autoridade sobre recursos físicos, mounts e exports. Este consumidor
mantém o planejamento lógico e os efeitos sobre a aplicação. Consulte o
[plano operacional](../superpowers/plans/2026-11-14-homelab-refactor-migration.md)
e o [registro de preparação](../architecture/homelab-2026/implementation-status.md).

## Verificação desta etapa

Python 3.12.3/WSL: 213 testes passaram, incluindo 69 casos do planejador e
regressões de singleton, consumidores, recuperação USB, admissão e exclusão
coordenada. Um caso foi pulado por exigir root real para fixture UID1000; nenhum
teste usou o acervo. Ruff e a revisão independente passaram. Essa rodada não
representa execução da suíte completa nem validação física do servidor.
