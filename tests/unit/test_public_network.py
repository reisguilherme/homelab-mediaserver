import pytest

from homeserver_common.public_network import edge_policy, home_policy


@pytest.mark.parametrize('address', ['::1', '1.2.3.4; flush ruleset', 'not-an-ip'])
def test_policy_rejects_unvalidated_address(address):
    with pytest.raises(ValueError):
        home_policy(wan_ips=(address,))


@pytest.mark.parametrize('uid', [0, -1, True, '1000', 65535])
def test_edge_requires_unprivileged_integer_identity(uid):
    with pytest.raises(ValueError):
        edge_policy(caddy_uid=uid, allowed_cidrs=())


def test_allowlist_is_explicit_until_empty_public_mode():
    restricted = edge_policy(caddy_uid=998, allowed_cidrs=('177.200.32.70/32',))
    public = edge_policy(caddy_uid=998, allowed_cidrs=())
    assert '177.200.32.70/32' in restricted
    assert 'meta nfproto ipv4 tcp dport 443 accept' not in restricted
    assert 'meta nfproto ipv4 tcp dport 443 accept' in public
    assert 'meta nfproto ipv6 tcp dport 443 counter drop' in public


def test_tables_never_flush_distribution_or_docker_rules():
    for value in (home_policy(), edge_policy(caddy_uid=998, allowed_cidrs=())):
        assert 'flush ruleset' not in value
        assert 'flush table inet homeserver_public' in value
        assert 'DOCKER' not in value


def test_invalid_cidr_rejected_instead_of_widened():
    with pytest.raises(ValueError):
        edge_policy(caddy_uid=998, allowed_cidrs=('192.168.1.12/24',))


@pytest.mark.parametrize('ports', [(True,), (0,), (-1,), (65536,), ('8989',), ()])
def test_admin_port_configuration_rejects_unusable_values(ports):
    with pytest.raises(ValueError):
        home_policy(admin_ports=ports)


def test_configured_admin_port_is_used_by_host_and_dnat_filters():
    rendered = home_policy(admin_ports=(22, 18989))
    assert 'tcp dport { 22, 18989 }' in rendered
    assert 'ct original proto-dst { 22, 18989 }' in rendered
