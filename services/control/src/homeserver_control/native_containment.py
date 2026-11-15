"""Local Docker transport and fail-closed ownership boundary for synthetic clones.

This is an internal manual rehearsal helper, not an operational fence. Only IDs
returned by this process's create calls may enter OwnedSet. Visibility or stop
failure retains resources; cleanup never force-removes a container.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


class ContainmentError(RuntimeError):
    """Only fixed public codes, never Docker output, paths or credentials."""


class Docker:
    def __init__(self, *, runner=subprocess.run, executable=None, config=None):
        self.runner = runner
        self.executable = executable or shutil.which("docker", path=os.defpath)
        if not self.executable:
            raise ContainmentError("docker_unavailable")
        self.config = config

    def command(self, *args, stdin=None, timeout=180):
        command = [self.executable, "--host", "unix:///var/run/docker.sock"]
        if self.config:
            command += ["--config", str(self.config)]
        try:
            result = self.runner(
                [*command, *args],
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                env={
                    "PATH": os.defpath,
                    "LC_ALL": "C",
                    "HOME": "/nonexistent",
                    "DOCKER_CONFIG": "/nonexistent",
                },
            )
        except subprocess.TimeoutExpired:
            self._record_error(args[0], "timeout")
            raise ContainmentError("docker_timeout") from None
        except (OSError, subprocess.SubprocessError):
            self._record_error(args[0], "transport_error")
            raise ContainmentError("docker_unavailable") from None
        if result.returncode:
            self._record_error(args[0], "command_failed", result.stderr)
            raise ContainmentError("docker_failed")
        return result.stdout

    def _record_error(self, operation, kind, detail=""):
        if self.config:
            try:
                path = Path(self.config).parent / "docker-errors.private.jsonl"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps({"operation": operation, "kind": kind, "detail": detail}) + "\n"
                    )
            except OSError:
                pass

    def inspect(self, identifiers):
        try:
            result = json.loads(self.command("inspect", *identifiers))
            if not isinstance(result, list) or len(result) != len(identifiers):
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise ContainmentError("docker_ambiguous") from None


@dataclass(frozen=True)
class OwnedContainer:
    identifier: str
    image: str
    nonce: str
    role: str
    binds: tuple[tuple[str, str, bool], ...]

    def __post_init__(self):
        if not re.fullmatch("[0-9a-f]{64}", self.identifier) or not re.fullmatch(
            "sha256:[0-9a-f]{64}", self.image
        ):
            raise ContainmentError("invalid_owned_identity")


def _state(snapshot):
    state = snapshot["State"]
    values = tuple(
        state[key] for key in ("Running", "Restarting", "Paused", "Pid", "StartedAt", "FinishedAt")
    )
    if any(type(value) is not bool for value in values[:3]):
        raise ContainmentError("docker_ambiguous")
    if type(values[3]) is not int or not all(isinstance(v, str) and v for v in values[4:]):
        raise ContainmentError("docker_ambiguous")
    return values


def _stopped(snapshot):
    return _state(snapshot)[:4] == (False, False, False, 0)


class OwnedSet:
    def __init__(self, docker, owned=None):
        self.docker = docker
        self.owned = list(owned or ())
        self.hold = False

    def snapshot(self):
        if not self.owned:
            return []
        try:
            snapshots = self.docker.inspect([item.identifier for item in self.owned])
            if len(snapshots) != len(self.owned):
                raise ContainmentError("docker_ambiguous")
            for item, snapshot in zip(self.owned, snapshots, strict=True):
                labels = snapshot["Config"]["Labels"]
                if not isinstance(labels, dict):
                    raise ContainmentError("docker_ambiguous")
                mounts = snapshot["Mounts"]
                binds = tuple(
                    sorted(
                        (m["Source"], m["Destination"], m["RW"])
                        for m in mounts
                        if m["Type"] == "bind"
                    )
                )
                if (
                    snapshot["Id"] != item.identifier
                    or snapshot["Image"] != item.image
                    or labels.get("homeserver.native.nonce") != item.nonce
                    or labels.get("homeserver.native.role") != item.role
                    or binds != tuple(sorted(item.binds))
                    or any(m["Type"] not in ("bind", "tmpfs") for m in mounts)
                ):
                    raise ContainmentError("owned_identity_changed")
                _state(snapshot)
            return snapshots
        except ContainmentError:
            self.hold = True
            raise
        except (KeyError, TypeError, ValueError):
            self.hold = True
            raise ContainmentError("docker_ambiguous") from None

    def _command(self, *args):
        try:
            return self.docker.command(*args)
        except ContainmentError:
            self.hold = True
            raise

    def persist(self, path: Path):
        """Durable private registration must precede starting any created clone."""
        temporary = path.with_suffix(".partial")
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump([asdict(item) for item in self.owned], stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def start(self, identifiers=None):
        snapshots = self.snapshot()  # validate the ENTIRE set before first mutation
        chosen = set(identifiers or (item.identifier for item in self.owned))
        if not chosen <= {item.identifier for item in self.owned}:
            raise ContainmentError("invalid_owned_identity")
        if any(
            not _stopped(s)
            for item, s in zip(self.owned, snapshots, strict=True)
            if item.identifier in chosen
        ):
            raise ContainmentError("not_stopped")
        for item in self.owned:
            if item.identifier in chosen:
                self._command("start", item.identifier)

    def stop(self):
        snapshots = self.snapshot()
        if any(s["State"]["Restarting"] or s["State"]["Paused"] for s in snapshots):
            self.hold = True
            raise ContainmentError("unstable_container")
        for item, snapshot in zip(self.owned, snapshots, strict=True):
            if snapshot["State"]["Running"]:
                self._command("stop", "--timeout", "-1", item.identifier)
        baseline = self.snapshot()
        if not all(_stopped(s) for s in baseline):
            self.hold = True
            raise ContainmentError("not_stopped")
        timestamps = tuple(_state(s) for s in baseline)

        def guard():
            current = self.snapshot()
            if tuple(_state(s) for s in current) != timestamps:
                raise ContainmentError("containment_changed")

        return guard

    def cleanup(self):
        if self.hold:
            return False
        try:
            guard = self.stop()
            guard()
            # Every container must be confirmed stopped before the first removal.
            while self.owned:
                if not all(_stopped(s) for s in self.snapshot()):
                    raise ContainmentError("not_stopped")
                self._command("rm", self.owned[0].identifier)
                self.owned.pop(0)
            return True
        except ContainmentError:
            return False
