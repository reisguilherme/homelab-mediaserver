.PHONY: lint test-unit test-contract test-integration test-config test-install test-restic test-fresh-stack compose-check smoke

PYTHON ?= python3

lint:
	$(PYTHON) -m ruff check services tests scripts
	$(PYTHON) -m compileall -q services scripts
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/*.sh scripts/lib/*.sh; fi
	@set -e; for file in scripts/*.sh scripts/lib/*.sh; do bash -n "$$file"; done

test-unit:
	$(PYTHON) -m pytest tests/unit -q

test-contract:
	$(PYTHON) -m pytest tests/contract -q

test-integration:
	$(PYTHON) -m pytest tests/integration -q

test-config:
	$(PYTHON) -m pytest tests/unit/test_settings.py tests/unit/test_native_config.py tests/unit/test_stack_render.py tests/contract/test_configuration_cli.py tests/contract/test_native_configuration.py tests/contract/test_qbit_configuration.py -q

test-install:
	$(PYTHON) -m pytest tests/integration/test_installation.py tests/integration/test_fresh_stack_validator.py tests/integration/test_release_lifecycle.py -q

test-restic:
	$(PYTHON) -m pytest tests/integration/test_restic_lifecycle.py tests/integration/test_backup_restore.py -q

test-fresh-stack:
	$(PYTHON) scripts/validate-fresh-stack.py

compose-check:
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml config --quiet; else echo 'docker compose unavailable' >&2; exit 2; fi

smoke:
	$(PYTHON) -m pytest tests/system -q
	@set -e; for file in tests/system/*.sh; do bash "$$file"; done
