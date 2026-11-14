import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from homeserver_common.public_persistence import install_persistence, persistence_plan

STOCK = '''#!/usr/sbin/nft -f
flush ruleset
table inet filter {
 chain input { type filter hook input priority filter; }
 chain forward { type filter hook forward priority filter; }
 chain output { type filter hook output priority filter; }
}
'''


def fixture_root(tmp_path):
    (tmp_path / 'etc').mkdir()
    (tmp_path / 'etc/nftables.conf').write_text(STOCK)
    return tmp_path


@pytest.mark.parametrize('role', ['home', 'edge'])
def test_native_service_owns_only_dedicated_policy_and_keeps_stop_rules(role):
    options = {} if role == 'home' else {'caddy_uid': 999, 'allowed_cidrs': ()}
    plan = persistence_plan(role, **options)
    policy = plan.files['etc/nftables.d/homeserver-public.nft']
    native = plan.files['etc/systemd/system/nftables.service.d/30-homeserver-public.conf']
    assert 'flush ruleset' not in policy
    assert 'flush table inet homeserver_public' in policy
    assert ('flush table bridge homeserver_public' in policy) == (role == 'home')
    assert 'ExecStop=\n' in native
    assert not any(line.startswith('ExecStop=') and line != 'ExecStop='
                   for line in native.splitlines())
    assert 'ExecStart=\nExecStart=/usr/sbin/nft -f /etc/nftables.d/homeserver-public.nft' in native
    assert ('ExecReload=\nExecReload=/usr/sbin/nft -f '
            '/etc/nftables.d/homeserver-public.nft') in native
    consumer = 'docker' if role == 'home' else 'caddy'
    dependency = plan.files[f'etc/systemd/system/{consumer}.service.d/25-public-firewall.conf']
    assert 'Requires=nftables.service' in dependency
    assert 'After=nftables.service' in dependency
    assert 'ExecStartPre=+/usr/sbin/nft -f /etc/nftables.d/homeserver-public.nft' in dependency
    assert not any(Path(name).suffix == '.service' for name in plan.files)


def test_file_install_preserves_stock_config_and_other_unit_dropins(tmp_path):
    root = fixture_root(tmp_path)
    other = root / 'etc/systemd/system/docker.service.d/10-existing.conf'
    other.parent.mkdir(parents=True)
    other.write_text('[Service]\nEnvironment=EXISTING=1\n')
    plan = persistence_plan('home', admin_ports=(22, 18989))
    install_persistence(root, plan)
    assert (root / 'etc/nftables.conf').read_text() == STOCK
    assert other.read_text() == '[Service]\nEnvironment=EXISTING=1\n'
    for name, content in plan.files.items():
        assert (root / name).read_text() == content
    install_persistence(root, plan)
    assert (root / 'etc/nftables.conf').read_text() == STOCK


@pytest.mark.parametrize('content', [
    'flush ruleset\ninclude "/etc/other.nft"\n',
    'table inet filter { chain input { type filter hook input priority filter; policy drop; } }',
    'table ip custom { chain forward { ip saddr 1.2.3.4 drop; } }',
])
def test_custom_persistent_firewall_is_rejected_before_any_write(tmp_path, content):
    root = fixture_root(tmp_path)
    (root / 'etc/nftables.conf').write_text(content)
    with pytest.raises(ValueError, match='custom'):
        install_persistence(root, persistence_plan('home'))
    assert not (root / 'etc/nftables.d').exists()
    assert (root / 'etc/nftables.conf').read_text() == content


@pytest.mark.skipif(os.name != 'posix', reason='Linux symlink containment')
def test_symlinked_target_is_rejected_before_any_write(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    fixture_root(root)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'etc/nftables.d').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        install_persistence(root, persistence_plan('home'))
    assert list(outside.iterdir()) == []


def test_partial_file_failure_restores_previous_owned_files(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    original = persistence_plan('home', admin_ports=(22,))
    install_persistence(root, original)
    replacement = persistence_plan('home', admin_ports=(22, 18989))
    real_replace = os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if str(destination).endswith('/25-public-firewall.conf') and not failed:
            failed = True
            raise OSError('fixture replacement failure')
        return real_replace(source, destination)

    monkeypatch.setattr(os, 'replace', fail_once)
    with pytest.raises(OSError, match='fixture replacement'):
        install_persistence(root, replacement)
    for name, content in original.files.items():
        assert (root / name).read_text() == content


@pytest.mark.parametrize('role', ['docker', '', '../edge'])
def test_unknown_role_is_rejected(role):
    with pytest.raises(ValueError):
        persistence_plan(role)


@pytest.mark.parametrize('relative', [
    'etc/systemd/system/nftables.service.d/99-unsafe.conf',
    'run/systemd/system/nftables.service.d/99-runtime.conf',
    'usr/lib/systemd/system/nftables.service.d/99-vendor.conf',
])
def test_other_native_execution_overrides_are_not_silently_ignored(tmp_path, relative):
    root = fixture_root(tmp_path)
    override = root / relative
    override.parent.mkdir(parents=True)
    override.write_text('[Service]\nExecStop=/usr/sbin/nft flush ruleset\n')
    with pytest.raises(ValueError, match='execution'):
        install_persistence(root, persistence_plan('home'))
    assert not (root / 'etc/nftables.d').exists()


@pytest.mark.parametrize('role', ['home', 'edge'])
def test_cli_writes_reviewable_files_without_activating_services(tmp_path, role):
    root = fixture_root(tmp_path)
    options = tmp_path / 'policy-options.json'
    options.write_text(json.dumps({} if role == 'home' else {
        'caddy_uid': 999, 'allowed_cidrs': ['177.200.32.70/32']}))
    command = [sys.executable, 'scripts/security/install_firewall_persistence.py',
               '--root', str(root), '--role', role, '--options-json', str(options)]
    result = subprocess.run(command, check=True, capture_output=True, text=True,
                            env=os.environ | {'PYTHONPATH': os.pathsep.join(sys.path)})
    assert json.loads(result.stdout) == {
        'files_installed': 3, 'services_activated': False, 'rules_applied': False}
    assert (root / 'etc/nftables.conf').read_text() == STOCK
