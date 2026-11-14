"""Prepare native nftables persistence without activating services or rules.

Only dedicated policy/drop-in files are owned here. The caller separately reviews
the effective systemd configuration, enables the native service and activates it.
Stopping nftables deliberately retains the policy so dependent services cannot
continue running with their protection removed.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from homeserver_common.public_network import edge_policy, home_policy

POLICY = 'etc/nftables.d/homeserver-public.nft'
NATIVE_DROPIN = 'etc/systemd/system/nftables.service.d/30-homeserver-public.conf'
_STOCK_CONFIG = '''flush ruleset
table inet filter {
 chain input { type filter hook input priority filter; }
 chain forward { type filter hook forward priority filter; }
 chain output { type filter hook output priority filter; }
}'''


@dataclass(frozen=True)
class PersistencePlan:
    role: str
    files: Mapping[str, str]


def persistence_plan(role: str, **options) -> PersistencePlan:
    """Render only existing-service drop-ins and the validated generated policy."""
    if role not in {'home', 'edge'}:
        raise ValueError('Expected home or edge role')
    policy = home_policy(**options) if role == 'home' else edge_policy(**options)
    consumer = 'docker' if role == 'home' else 'caddy'
    native = f'''# Owned public policy; do not load the stock flush-ruleset file.
[Service]
ExecStart=
ExecStart=/usr/sbin/nft -f /{POLICY}
ExecReload=
ExecReload=/usr/sbin/nft -f /{POLICY}
# Keep protection in place on stop; never flush Docker/Tailscale rules.
ExecStop=
'''
    dependency = f'''[Unit]
Requires=nftables.service
After=nftables.service

[Service]
# Reapply atomically even if an active oneshot service has lost its tables.
# The + prefix executes this fixed root-owned file before dropping privileges.
ExecStartPre=+/usr/sbin/nft -f /{POLICY}
'''
    files = {
        POLICY: policy,
        NATIVE_DROPIN: native,
        f'etc/systemd/system/{consumer}.service.d/25-public-firewall.conf': dependency,
    }
    return PersistencePlan(role, MappingProxyType(files))


def _without_comments(value: str) -> str:
    return re.sub(r'\s+', '', '\n'.join(line.split('#', 1)[0] for line in value.splitlines()))


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Path must remain inside the installation root')
    for component in (root, *path.relative_to(root).parents, path.relative_to(root)):
        target = component if component.is_absolute() else root / component
        if target.is_symlink():
            raise ValueError('Refusing a symlink in the installation path')
    if path.exists() and not path.is_file():
        raise ValueError('Expected a regular installation file')
    return path


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.homeserver-public-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_native_overrides(root: Path) -> None:
    for directory in ('etc/systemd/system', 'run/systemd/system',
                      'usr/lib/systemd/system', 'lib/systemd/system'):
        base = root / directory
        if directory.startswith(('etc/', 'run/')):
            unit = base / 'nftables.service'
            if unit.exists() or unit.is_symlink():
                raise ValueError('Review custom native nftables service execution override')
        for fragment in (base / 'nftables.service.d').glob('*.conf'):
            if fragment == root / NATIVE_DROPIN:
                continue
            if re.search(r'(?m)^\s*Exec\w*\s*=', fragment.read_text()):
                raise ValueError('Review competing native nftables execution override')


def install_persistence(root: Path, plan: PersistencePlan) -> None:
    """Install files only; never call nft/systemctl or change service activation.

    A custom distribution nftables.conf requires explicit integration instead of
    being silently bypassed. Existing unrelated files and all running rules are
    untouched. Failed replacements restore previously owned files.
    """
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('An existing absolute installation root is required')
    stock = _safe_path(root, 'etc/nftables.conf')
    accepted = {'', 'flushruleset', _without_comments(_STOCK_CONFIG)}
    if stock.exists() and _without_comments(stock.read_text()) not in accepted:
        raise ValueError('Refusing to bypass custom persistent nftables configuration')
    _check_native_overrides(root)
    consumer = {'home': 'docker', 'edge': 'caddy'}.get(plan.role)
    expected = {POLICY, NATIVE_DROPIN,
                f'etc/systemd/system/{consumer}.service.d/25-public-firewall.conf'}
    if consumer is None or set(plan.files) != expected:
        raise ValueError('Unexpected persistence file ownership')
    paths = {name: _safe_path(root, name) for name in plan.files}
    previous = {
        name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        for name, path in paths.items()
    }
    changed = []
    try:
        # The policy is written before service drop-ins that reference it.
        for name, content in plan.files.items():
            path = paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content.encode(), 0o600 if name == POLICY else 0o644)
            changed.append(name)
    except BaseException:
        for name in reversed(changed):
            before = previous[name]
            if before is None:
                paths[name].unlink(missing_ok=True)
            else:
                _atomic_write(paths[name], *before)
        raise
