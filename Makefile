.PHONY: lint test-unit test-contract test-integration compose-check smoke

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

compose-check:
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml config --quiet; else echo 'docker compose unavailable' >&2; exit 2; fi

smoke:
	$(PYTHON) -m pytest tests/system -q
	@set -e; for file in tests/system/*.sh; do bash "$$file"; done
