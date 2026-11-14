# Recuperação USB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Execute os itens sequencialmente dentro de cada tarefa; módulos independentes podem avançar em paralelo.

**Goal:** Recuperar os dois volumes USB cadastrados e a fila existente quando retornarem.

**Architecture:** Cron existente chama controlador root-owned e idempotente. A máquina de estados persiste intenção antes de quiescer, restaura mounts/binds, valida fontes/capacidade e retoma consumidores/agendador existentes.

**Tech Stack:** Python 3.12 stdlib, SQLite somente leitura, Docker CLI, mounts existentes, cron.

**Spec:** `docs/superpowers/specs/2026-10-11-usb-recovery-design.md`

## Global Constraints

- Não criar units, formatar, particionar, reescrever fstab ou alterar o serviço Lenovo.
- Não adicionar, excluir, mover, repriorizar ou force-start torrents.
- Configuração instalada e estado privados; UUIDs/serials reais não entram no Git.
- Fixtures temporárias Linux; provas físicas separadas e coordenadas.
- O agendador existente conserva o limite de 50 e os guards reais.

## Review Focus

- Geração USB mudou com major/minor reutilizado: renovar montagens e namespaces.
- Crash entre efeitos: retomar a fase com readback e intenção persistida.
- Disco errado/RO ou mount ocupado: manter hold sem desmontagem forçada.
- Snapshot anterior ao startup: capacidade conservadora antes de retomada nativa.
- Serviços/manutenção parados manualmente: preservar intenção e marcador externo.

## Task 1: Gates de fontes e capacidade

**Files:** criar `services/common/src/homeserver_common/usb_recovery_sources.py` e `tests/unit/test_usb_recovery_sources.py`.

**Interfaces:** `preflight_sources(rows, database, registry, minimum)` retorna as fontes vinculadas; `validate_sources(rows, files, database, registry, minimum)` adiciona prova de arquivos/prioridades; `check_preferences(prefs)` exige fila 50.

- [ ] Escrever fixtures SQLite/descritores reais e observar RED em Linux.
- [ ] Reutilizar parser de torrent e guarda de reserva existentes; não inicializar banco.
- [ ] Cobrir híbrido, padding/zero-length, cancelamento, mudança de destino/seleção/prioridade e capacidade.
- [ ] Observar GREEN e Ruff; revisar o limite entre captura indisponível e retomada.

## Task 2: Controlador e backend host

**Files:** criar `services/common/src/homeserver_common/usb_recovery.py` e `tests/unit/test_usb_recovery.py`.

**Interfaces:** `RecoveryController(backend, state_path).tick(force_pools=())`; backend host com observação, captura, quiesce, mount/union, prova de capacidade, startup/binds, recuperação qBit e refresh Jellyfin.

- [ ] Escrever testes de eventos/efeitos com filesystem e backend fixture e observar RED.
- [ ] Implementar estado durável, lock e controller; não colocar hooks de teste no produto.
- [ ] Implementar backend com argv fixos, timeouts, identidades dinâmicas e fingerprints Docker.
- [ ] Cobrir idempotência, crash, offline/clone/RO, manual disable e falhas de readback.
- [ ] GREEN/Ruff e revisão independente.

## Task 3: Instalação e operação

**Files:** criar `scripts/install-usb-recovery.py`, `scripts/usb-recovery.py`; atualizar `docs/storage.md` e escopo atual no `AGENTS.md`.

- [ ] Testar instalação em root fixture, cron idempotente e ownership/ausência de fstab/units.
- [ ] Instalar cópias root-owned dos módulos stdlib necessários e configuração extraída das identidades atuais.
- [ ] Criar apenas arquivo cron opt-in, lock/estado privado e CLI de status/disable/enable/recover.
- [ ] Rodar checks Linux apropriados e revisão do conjunto.
- [ ] Implantar no host autorizado, verificar tick saudável sem efeitos e executar recuperação coordenada real para ambos os volumes.
- [ ] Confirmar cron, montagens/binds/capacidade, 50 downloads, fontes/prioridades preservadas e catálogo; registrar limites de teste físico.
