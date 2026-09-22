# HomeServer

Projeto reproduzível de mídia, automação, telemetria e operação para um Legion Ubuntu já preparado. O código próprio é deliberadamente conservador: SQLite local, Compose com redes separadas, guarda do mount por UUID, gateway de admissão e snapshots de telemetria compactos.

## Desenvolvimento

```bash
uv sync --frozen
uv run make lint
uv run make test-unit
uv run make test-contract
uv run make compose-check
```

O arquivo `config/policy.yaml` contém somente limites técnicos. Copie `config/dev.env.example` para um arquivo local quando necessário; não coloque credenciais no Git.

## Implantação

1. Execute `scripts/audit-server.sh --check` e revise o relatório sanitizado.
2. Confirme o UUID real de `/srv/data` e use `scripts/check-mount.sh` antes da stack.
3. Rode `scripts/bootstrap-server.sh --check`/`--plan`; em modo `adopt`, aplique apenas dependências faltantes.
4. Faça deploy de um artefato identificado com `scripts/deploy.sh`; nunca use `latest` em produção.
5. Execute `scripts/smoke.sh` e registre a evidência antes de ativar aquisições.

O checkout contém a implementação e fixtures locais. A ausência de servidor real, hardware, rede externa e testes físicos não é mascarada por fixtures; aquisições continuam bloqueadas até os critérios de integração e aceite serem demonstrados.
