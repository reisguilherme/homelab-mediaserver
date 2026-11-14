# Plano: planejador incremental de mídia

> Implementar com superpowers:subagent-driven-development; testes RED/GREEN e
> revisão independente. A publicação na branch refactor já está autorizada.

**Objetivo:** preparar lotes preservando fontes, sem executar operações no acervo.
**Arquitetura:** função de domínio pura no consumidor, independente dos helpers
privados e de qualquer adaptador de filesystem ou banco.
**Stack:** Python 3.12, dataclasses/stdlib e pytest existentes.
**Spec:** [contrato e limites](../specs/2026-11-14-incremental-media-planning.md).

## Restrições e revisão

Nenhum SSH mutável, deploy, montagem, cópia, cleanup, liberação de manutenção,
mudança de Compose ou novas dependências. Fixtures sintéticas somente. Nunca
copiar IDs, paths físicos, títulos ou inventários privados para código/testes.
Revisar particularmente traversal, ambiguidades de path/inode, dependências
cíclicas, recibos reaproveitados e alegações de capacidade/readiness física.

## Task 1 — Domínio de lotes e provas declaradas

Arquivos: criar `services/control/src/homeserver_control/domain/migration_batches.py`
e `tests/unit/test_migration_batches.py`. O implementador documenta a assinatura
exata escolhida no runbook, com tipos imutáveis e nomes que indiquem declaração.

Interface: geração (ID, origem, destino); grupo (ID, paths, bytes, SHA-256 do
conteúdo, IDs de dependências); recibo (geração/origem/destino, grupo, digest de
manifesto, fase); `manifest_digest(group)` e `plan_batch(...)`. Resultado contém
grupos/paths selecionados, motivos de bloqueio, estados de evidência por grupo,
total lógico de bytes e os quatro flags operacionais sempre falsos.

- [x] Escrever testes para seleção de um membro expandir hardlinks e dependências,
  deduplicar bytes, ordenar saída e preservar input; executar RED.
- [x] Implementar contratos e planejamento mínimo, validando tipos/IDs/paths/hash.
- [x] Testar colisões (inclusive hash igual), grupo/dependência/seleção ausente,
  ciclo, bool no lugar de inteiro, recibos antigos e mudança de manifesto.
- [x] Testar que staging/publicação/readback são fases encadeadas; nenhuma
  combinação libera execução, limpeza, exclusão ou scan global nesta etapa.
- [x] Executar GREEN e Ruff. Não criar backend nem hook de runtime.

## Task 2 — Integração documental e verificação

Arquivos: `docs/runbooks/incremental-media-planning.md`, ponte em `docs/storage.md`
e atualização sanitizada do registro de preparação operacional. Não versionar
inventário nem diagnóstico bruto da sessão SSH.

- [x] Documentar exemplo sintético, API entregue, confiança dos recibos, limites
  de recursos, fronteira de repositórios e bloqueios do próximo adaptador.
- [x] Executar teste novo e regressões pertinentes: 213 passed, 1 skipped (fixture
  root/UID1000) em Python 3.12.3/WSL; Ruff passou. Nenhuma mudança de observabilidade.
- [x] Revisão independente, correções, diff/check de dados privados.
- [x] Preparar a publicação revisada em `refactor`; confirmação do hash remoto
  acompanha a entrega. A migração do acervo permanece pendente.
