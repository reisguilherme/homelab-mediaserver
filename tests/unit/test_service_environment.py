"""Security boundaries for runtime configuration and generated Compose."""

import pytest

from homeserver_common.env import load_settings
from homeserver_common.render import render_stack
from homeserver_common.runtime import apply_runtime_configuration


def test_telemetry_runtime_never_inherits_unrelated_or_unknown_credentials(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text(
        'HOMESERVER_QBIT_PASSWORD=must-not-propagate\n'
        'HOMESERVER_ADMIN_PASSWORD=status-login\n'
        'HOMESERVER_SONARR_API_KEY=must-not-propagate\n'
        'HOMESERVER_JELLYFIN_ADMIN_PASSWORD=separate-private-admin\n'
    )
    environment = apply_runtime_configuration(preferences, 'dev', {
        'HOMESERVER_SERVICE_ROLE': 'telemetry',
        'HOMESERVER_ADMIN_PASSWORD': 'status-login',
        'HOMESERVER_NEW_SECRET': 'must-not-propagate',
        'AWS_SECRET_ACCESS_KEY': 'must-not-propagate',
        'PATH': '/usr/bin',
    })
    assert 'HOMESERVER_QBIT_PASSWORD' not in environment
    assert 'HOMESERVER_SONARR_API_KEY' not in environment
    assert 'HOMESERVER_JELLYFIN_ADMIN_PASSWORD' not in environment
    assert 'HOMESERVER_NEW_SECRET' not in environment
    assert 'AWS_SECRET_ACCESS_KEY' not in environment
    assert environment['HOMESERVER_ADMIN_PASSWORD'] == 'status-login'
    assert environment['PATH'] == '/usr/bin'


@pytest.mark.parametrize('role', [
    'jellyfin-relay', 'public-security', 'host-metrics', 'integration-proxy',
])
def test_unprivileged_runtime_does_not_open_env_file(tmp_path, monkeypatch, role):
    def forbidden(*args, **kwargs):
        pytest.fail('unprivileged process must not read private .env')

    monkeypatch.setattr('homeserver_common.runtime.load_settings', forbidden)
    result = apply_runtime_configuration(tmp_path / 'not-mounted.env', 'prod', {
        'HOMESERVER_SERVICE_ROLE': role,
        'HOMESERVER_JELLYFIN_URL': 'http://jellyfin:8096/jellyfin',
        'HOMESERVER_QBIT_PASSWORD': 'must-not-propagate',
    })
    assert 'HOMESERVER_QBIT_PASSWORD' not in result


def test_unknown_runtime_role_fails_closed(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('')
    with pytest.raises(ValueError, match='service role'):
        apply_runtime_configuration(preferences, 'dev', {'HOMESERVER_SERVICE_ROLE': 'new-role'})


def test_private_worker_retains_explicit_one_cycle_mode(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('')
    result = apply_runtime_configuration(preferences, 'dev', {
        'HOMESERVER_SERVICE_ROLE': 'control-worker',
        'HOMESERVER_WORKER_ONCE': 'true',
        'HOMESERVER_UNKNOWN_CREDENTIAL': 'must-not-propagate',
    })
    assert result['HOMESERVER_WORKER_ONCE'] == 'true'
    assert 'HOMESERVER_UNKNOWN_CREDENTIAL' not in result


def test_public_components_have_no_privileged_mounts_or_secrets(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('HOMESERVER_ADMIN_PASSWORD=private-admin\n')
    services = render_stack(load_settings(preferences, mode='dev'))['services']
    for name in ('jellyfin-relay', 'public-security', 'public-proxy', 'seerr-proxy',
                 'integration-proxy'):
        service = services[name]
        assert 'env_file' not in service
        assert not any(v['target'] in {'/project', '/var/lib/homeserver', '/data', '/data/media'}
                       for v in service['volumes'])
        assert not any(key.endswith(('_API_KEY', '_PASSWORD', '_TOKEN'))
                       for key in service.get('environment', {}))
        assert service['cap_drop'] == ['ALL']
        assert 'no-new-privileges:true' in service['security_opt']
        assert service['read_only'] is True
    assert services['public-security']['environment'][
        'HOMESERVER_PUBLIC_ACCESS_STATE_PATH'] == '/state/limits.sqlite3'
    assert services['jellyfin-relay']['volumes'] == []


def test_public_backends_cannot_join_administration_networks(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('')
    stack = render_stack(load_settings(preferences, mode='dev'))
    services = stack['services']
    for name in ('jellyfin', 'seerr', 'jellyfin-relay', 'public-security', 'public-proxy'):
        assert not {'apps', 'transfer', 'edge_control', 'egress', 'telemetry'} & set(
            services[name]['networks'])
    assert 'ports' not in services['jellyfin']
    assert 'ports' not in services['seerr']
    assert {int(p['published']) for p in services['public-proxy']['ports']} == {8097, 5056}
    assert {int(p['published']) for p in services['seerr-proxy']['ports']} == {5055}
    assert services['prowlarr']['dns'] == ['1.1.1.1', '1.0.0.1']
    assert services['jellyfin']['dns'] == services['seerr']['dns'] == ['1.1.1.1', '1.0.0.1']
    integration = services['integration-proxy']
    assert set(integration['networks']) == {
        'seerr_integration', 'automation_backends', 'public_jellyfin'}
    assert 'public_jellyfin' not in services['seerr']['networks']
    assert integration['networks']['automation_backends']['ipv4_address'] == '172.30.240.98'
    assert integration['volumes'] == []
    assert integration['command'][integration['command'].index('--port') + 1] == '8787'
    assert services['jellyfin-proxy']['networks']['public_jellyfin']['gw_priority'] == 1
    assert services['seerr-proxy']['networks']['public_seerr']['gw_priority'] == 1


def test_public_proxy_uses_the_exact_guard_and_relay_network_peers(tmp_path):
    from pathlib import Path

    from homeserver_common.public_network import home_policy

    preferences = tmp_path / '.env'
    preferences.write_text('')
    services = render_stack(load_settings(preferences, mode='dev'))['services']
    config = Path('deploy/public-proxy/nginx.conf').read_text()
    for service, network, port, source, target in (
        ('public-security', 'public_guard', 8080, '172.30.240.51', '172.30.240.50'),
        ('jellyfin-relay', 'public_relay', 8083, '172.30.240.35', '172.30.240.34'),
    ):
        assert services[service]['networks'][network]['ipv4_address'] == target
        assert services['public-proxy']['networks'][network]['ipv4_address'] == source
        assert f'ip saddr {source} ip daddr {target} tcp dport {port} return' in home_policy()
        # Docker DNS can select another shared network: pin the permitted peer.
        assert f'{service}:{port}' not in config
        assert config.count(f'{target}:{port};') == 2


def test_guard_trust_is_pinned_to_proxy_address_on_guard_network(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('')
    services = render_stack(load_settings(preferences, mode='dev'))['services']
    environment = services['public-security']['environment']
    key = 'HOMESERVER_PUBLIC_ACCESS_TRUSTED_PROXY_IPS'
    expected = services['public-proxy']['networks']['public_guard']['ipv4_address']
    assert environment[key] == expected == '172.30.240.51'
    runtime = apply_runtime_configuration(tmp_path / 'never-mounted.env', 'prod', environment)
    assert runtime[key] == expected
    assert expected != services['public-proxy']['networks']['public_jellyfin']['ipv4_address']


def test_telemetry_mounts_only_private_env_file_and_metrics_has_no_env(tmp_path):
    preferences = tmp_path / '.env'
    preferences.write_text('')
    services = render_stack(load_settings(preferences, mode='dev'))['services']
    for name in ('telemetry', 'host-metrics'):
        assert not any(v['target'] == '/project' for v in services[name]['volumes'])
    assert 'HOMESERVER_ENV_PATH' not in services['host-metrics']['environment']
    assert any(v['target'] == '/project/.env' and v['read_only']
               for v in services['telemetry']['volumes'])


def test_jellyfin_admin_password_is_optional_private_native_credential(tmp_path):
    from homeserver_common.settings import FIELDS

    preferences = tmp_path / '.env'
    preferences.write_text('')
    settings = load_settings(preferences, mode='dev')
    assert settings.jellyfin_admin_password == ''
    assert FIELDS['jellyfin_admin_password'].secret
    assert FIELDS['jellyfin_admin_password'].editable


def test_checked_in_compose_keeps_the_same_security_boundaries(tmp_path):
    from pathlib import Path

    import yaml

    preferences = tmp_path / '.env'
    preferences.write_text('')
    generated = render_stack(load_settings(preferences, mode='dev'))
    checked_in = yaml.safe_load(Path('compose.yaml').read_text(encoding='utf-8'))
    for name in ('jellyfin', 'seerr', 'jellyfin-relay', 'public-security', 'public-proxy',
                 'seerr-proxy', 'integration-proxy'):
        compiled = checked_in['services'][name]
        assert compiled['networks'] == generated['services'][name]['networks']
        assert 'env_file' not in compiled
        assert not any(v['target'].startswith('/project') for v in compiled['volumes'])
    assert 'ports' not in checked_in['services']['seerr']
    assert checked_in['services']['prowlarr']['dns'] == ['1.1.1.1', '1.0.0.1']
    assert checked_in['services']['public-security']['environment'][
        'HOMESERVER_PUBLIC_ACCESS_TRUSTED_PROXY_IPS'] == '172.30.240.51'
    for name, config in generated['networks'].items():
        assert checked_in['networks'][name] == config
