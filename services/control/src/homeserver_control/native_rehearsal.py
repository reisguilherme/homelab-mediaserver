"""Manual native checkpoint rehearsal, exclusively on fresh private /tmp fixtures.

No caller paths, endpoint, container IDs or external configuration are accepted.
An opaque local Linux copy exercises transport integrity; it is not a Windows or
USB backup test. The public report contains only aggregate non-secret evidence.
"""

import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .checkpoint import CheckpointError, capture_checkpoint, restore_checkpoint
from .native_containment import ContainmentError, Docker, OwnedContainer, OwnedSet
from .native_rehearsal_apps import NativeApps

IMAGES = {
    "jellyfin": "jellyfin/jellyfin@sha256:"
    "357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037",
    "sonarr": "linuxserver/sonarr:4.0.15",
    "radarr": "lscr.io/linuxserver/radarr@sha256:"
    "adb6c09d6b729ea5e642c99cea35af72702ef476bf4763f153299ac5db9f0b4f",
}


@dataclass
class NativeReport:
    scope: str = "isolated_native_fixture"
    containment_kind: str = "docker_stopped_native_applications"
    completed: bool = False
    native_versions: dict[str, str] = field(default_factory=dict)
    native_scan_baseline_verified: bool = False
    checkpoint_restore_verified: bool = False
    restored_application_state_verified: bool = False
    partial_restart_safe: bool = False
    production_ready: bool = False
    blocked_reasons: tuple[str, ...] = ()
    resources_retained: bool = False
    jellyfin_catalog_verified: bool = False
    arr_tag_objects_verified: bool = False
    arr_catalog_scan_exercised: bool = False
    opaque_local_transfer_verified: bool = False
    database_count: int = 0
    owned_containers_created: int = 0
    owned_containers_removed: int = 0
    unexpected_restart_rejected_before_capture: bool = False


def _resolve_images(docker):
    resolved = {}
    for role, reference in IMAGES.items():
        try:
            values = json.loads(docker.command("image", "inspect", reference))
            image = values[0]["Id"]
            # Reuse strict image-ID validation without creating a container.
            OwnedContainer("0" * 64, image, "validation", role, ())
            resolved[role] = image
        except (ValueError, IndexError, KeyError, TypeError):
            raise ContainmentError("local_image_required") from None
    return resolved


def _create(group, tree, images, nonce, registry):
    configs = {}
    identifiers = []
    for role, image in images.items():
        config = tree / "appdata" / role
        configs[role] = config
        binds = ((str(config), "/config", True), (str(tree / "media"), "/data/media", True))
        args = [
            "create",
            "--name",
            f"native-checkpoint-{nonce}-{role}-{len(group.owned)}",
            "--pull",
            "never",
            "--network",
            "none",
            "--restart",
            "no",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "384",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=2m",
            "--log-opt",
            "max-file=1",
            "--env",
            "HOME=/config",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,uid={os.getuid()},gid={os.getgid()},size=128m",
            "--tmpfs",
            f"/cache:rw,nosuid,nodev,uid={os.getuid()},gid={os.getgid()},size=3g",
            "--label",
            f"homeserver.native.nonce={nonce}",
            "--label",
            f"homeserver.native.role={role}",
        ]
        for source, destination, _ in binds:
            args += ["--mount", f"type=bind,src={source},dst={destination}"]
        if role != "jellyfin":
            args += ["--entrypoint", f"/app/{role}/bin/{role.capitalize()}"]
        args.append(image)
        if role != "jellyfin":
            args += ["-nobrowser", "-data=/config"]
        # A lost create response is ambiguous: caller retains fixture, never guesses IDs.
        marker = registry.parent / "create-in-progress"
        marker.touch(mode=0o600, exist_ok=False)
        identifier = group.docker.command(*args).strip()
        group.owned.append(OwnedContainer(identifier, image, nonce, role, binds))
        group.persist(registry)  # durable ownership BEFORE start
        marker.unlink()
        identifiers.append(identifier)
    group.start(identifiers)
    return configs


def _transfer(source, destination, receipt, guard):
    guard()
    destination.mkdir(mode=0o700)
    for name, expected in (
        ("archive.tar", receipt.archive_sha256),
        ("manifest.json", receipt.manifest_sha256),
    ):
        with (source / name).open("rb") as reader, (destination / name).open("xb") as writer:
            os.chmod(destination / name, 0o600)
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        with (destination / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ContainmentError("opaque_transfer_failed")
    guard()


def run_native_rehearsal(scenario="success"):
    if scenario not in ("success", "unexpected_restart"):
        raise ValueError("unsupported_scenario")
    report = NativeReport()
    if sys.platform != "linux" or os.getuid() == 0:
        report.blocked_reasons = ("ordinary_linux_user_required",)
        return report
    try:
        root = Path(tempfile.mkdtemp(prefix="homeserver-native-", dir="/tmp"))
    except OSError:
        report.blocked_reasons = ("private_fixture_unavailable",)
        return report
    group = None
    try:
        os.chmod(root, 0o700)
        config = root / "docker-config"
        config.mkdir(mode=0o700)
        group = OwnedSet(Docker(config=config))
        images = _resolve_images(group.docker)  # all local image IDs before any create
        tree = root / "source"
        tree.mkdir(mode=0o700)
        for role in IMAGES:
            (tree / "appdata" / role).mkdir(mode=0o700, parents=True)
        movie = tree / "media/movies/Probe (2026)"
        movie.mkdir(mode=0o700, parents=True)
        (tree / "media/tv").mkdir(mode=0o700)
        (movie / "movie.nfo").write_text(
            "<movie><title>Probe</title><year>2026</year>"
            "<plot>Fresh synthetic native checkpoint fixture.</plot></movie>",
            encoding="utf-8",
        )
        nonce = secrets.token_hex(16)
        registry = root / "owned.json"
        configs = _create(group, tree, images, nonce, registry)
        report.owned_containers_created = len(group.owned)
        versions, state = NativeApps(group, configs, evidence=root).baseline()
        report.native_versions = versions
        report.native_scan_baseline_verified = True
        report.jellyfin_catalog_verified = True
        report.arr_tag_objects_verified = True
        guard = group.stop()
        bundle = root / "bundle"
        if scenario == "unexpected_restart":
            group.start([group.owned[0].identifier])
            NativeApps(group, configs, evidence=root).verify_jellyfin(versions, state)
            group.stop()
            # The actual checkpoint entry point must reject its stale callback
            # before creating even an incomplete bundle, not just before publish.
            try:
                capture_checkpoint(tree, bundle, guard=guard)
            except CheckpointError as error:
                if bundle.exists():
                    raise ContainmentError("unexpected_capture_effect") from None
                if str(error) != "guard_failed":
                    raise
                report.unexpected_restart_rejected_before_capture = True
                guard()  # recover the fixed containment_changed reason
            raise ContainmentError("unexpected_restart_undetected")
        receipt = capture_checkpoint(tree, bundle, guard=guard)
        transferred = root / "local-transfer"
        _transfer(bundle, transferred, receipt, guard)
        report.opaque_local_transfer_verified = True
        restored = root / "reference-restore"
        result = restore_checkpoint(transferred, restored, receipt=receipt, guard=guard)
        report.checkpoint_restore_verified = (
            result.bytes_verified and result.metadata_verified and result.sqlite_verified
        )
        report.database_count = result.database_count
        disposable = root / "native-startup-copy"
        guard()
        shutil.copytree(restored, disposable, symlinks=True)
        guard()
        configs = _create(group, disposable, images, nonce, registry)
        report.owned_containers_created = len(group.owned)
        NativeApps(group, configs, evidence=root).verify_restored(versions, state)
        report.restored_application_state_verified = True
        report.completed = report.checkpoint_restore_verified
    except (ContainmentError, CheckpointError) as error:
        report.blocked_reasons = (str(error),)
    except Exception:
        report.blocked_reasons = ("native_rehearsal_failed",)
        try:
            (root / "private-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            os.chmod(root / "private-error.txt", 0o600)
        except Exception:
            # Diagnostics are optional; cleanup and the sanitized report are not.
            pass
    # Never delete a root on uncertain create/visibility/stop/removal outcomes.
    count = len(group.owned) if group else 0
    cleaned = group.cleanup() if group else True
    if group:
        report.owned_containers_created = max(report.owned_containers_created, count)
        report.owned_containers_removed = count - len(group.owned)
    if (root / "create-in-progress").exists():
        cleaned = False
    preserve_failure = bool(report.blocked_reasons) and not (
        scenario == "unexpected_restart" and report.blocked_reasons == ("containment_changed",)
    )
    if cleaned and not preserve_failure:
        try:
            shutil.rmtree(root)
        except OSError:
            cleaned = False
    report.resources_retained = not cleaned or preserve_failure
    if not cleaned:
        report.completed = False
        report.blocked_reasons += ("cleanup_hold",)
    return report
