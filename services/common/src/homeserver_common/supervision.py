"""Bounded recovery of the declared stack, respecting storage and maintenance guards."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .install import preflight, run_checked
from .render import atomic_write


def container_rows(text):
    try:
        payload = json.loads(text)
    except ValueError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    rows = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(row, dict) or not isinstance(row.get("Service"), str) for row in rows):
        raise ValueError("invalid container status")
    return rows


class StackSupervisor:
    def __init__(self, settings, *, runner=run_checked, guard=preflight, clock=time.time):
        self.settings, self.runner, self.guard, self.clock = settings, runner, guard, clock
        self.compose = Path(settings.install_root) / "shared/compose.json"
        self.state_path = Path(settings.run_root) / "supervisor.json"
        self.prefix = ["docker", "compose", "-p", settings.instance_name, "-f", str(self.compose)]

    def _state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        return state if isinstance(state, dict) else {}

    def _write(self, state):
        state["measured_at"] = self.clock()
        atomic_write(self.state_path, json.dumps(state, sort_keys=True) + "\n")
        return state

    def run_once(self):
        import fcntl

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with (self.state_path.parent / "operation.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state = self._state()
                state.update(status="maintenance", services={})
                return self._write(state)
            try:
                return self._run_once_locked()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _stop_for_storage(self, state):
        state.update(status="blocked", reason="storage_guard", services={}, writers_stopped=False)
        self._write(state)
        self.runner(self.prefix + ["stop"], timeout=120)
        state["writers_stopped"] = True
        return self._write(state)

    def _run_once_locked(self):
        state = self._state()
        marker = Path(self.settings.run_root) / "maintenance"
        recovery = Path(self.settings.appdata_root) / "control/RECOVERY_MODE"
        if recovery.exists() and "reason=storage_guard" in recovery.read_text(encoding="utf-8"):
            if not state.get("writers_stopped"):
                return self._stop_for_storage(state)
            return self._write(state)
        if marker.exists() or recovery.exists():
            state.update(status="maintenance", services={})
            return self._write(state)
        try:
            self.guard(self.settings, host_tools=False)
        except (OSError, RuntimeError, ValueError):
            recovery.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(recovery, "admission_enabled=false\nreason=storage_guard\n")
            return self._stop_for_storage(state)
        declared = json.loads(self.compose.read_text(encoding="utf-8"))["services"]
        expected = {name for name, spec in declared.items() if not spec.get("profiles")}
        result = self.runner(self.prefix + ["ps", "--all", "--format", "json"], timeout=30)
        actual = {row["Service"]: row for row in container_rows(result.stdout)}
        history = state.get("restarts", {})
        outcomes = {}
        now = self.clock()
        for name in sorted(expected):
            row = actual.get(name, {})
            if row.get("State") == "running" and row.get("Health") != "unhealthy":
                outcomes[name] = "running"
                continue
            attempts = [
                when
                for when in history.get(name, [])
                if 0 <= now - when < self.settings.supervisor_restart_window_seconds
            ]
            if len(attempts) >= self.settings.supervisor_max_restarts:
                outcomes[name] = "restart_limit"
                history[name] = attempts
                continue
            # Compose up preserves other containers, data, secrets, and native databases.
            operation = (
                ["restart", name]
                if row.get("Health") == "unhealthy"
                else ["up", "-d", "--no-deps", name]
            )
            self.runner(self.prefix + operation, timeout=120)
            history[name] = attempts + [now]
            outcomes[name] = "restart_requested"
        state.update(
            status="ok" if all(value == "running" for value in outcomes.values()) else "degraded",
            services=outcomes,
            restarts=history,
        )
        return self._write(state)
