"""Dedicated nftables policy for the public application boundary.

This table does not own Docker, Tailscale, UFW or the default host policy.
It must be loaded before Docker/Caddy by the distribution firewall service.
"""

from __future__ import annotations

import ipaddress

TABLE = 'homeserver_public'
_BLOCKED_V4 = (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
    '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
    '224.0.0.0/4', '240.0.0.0/4',
)
_PUBLIC_ROLES = (2, 3, 4, 5, 11, 18, 19, 20, 34, 35, 50, 51, 66, 67, 82, 98)
_ADMIN_PORTS = (22, 80, 5055, 6767, 7878, 8080, 8081, 8096, 8989, 9696, 18080)
_PEER_RULES = '''
        ip saddr 172.30.240.11 ip daddr 172.30.240.2 tcp dport 8096 return
        ip saddr 172.30.240.66 ip daddr 172.30.240.67 tcp dport 8787 return
    ip saddr {172.30.240.3,172.30.240.4,172.30.240.5} ip daddr 172.30.240.2 tcp dport 8096 return
        ip saddr { 172.30.240.19, 172.30.240.20 } ip daddr 172.30.240.18 tcp dport 5055 return
        ip saddr 172.30.240.35 ip daddr 172.30.240.34 tcp dport 8083 return
        ip saddr 172.30.240.51 ip daddr 172.30.240.50 tcp dport 8080 return
        ip saddr 172.30.240.98 ip daddr 172.30.240.99 tcp dport 7878 return
        ip saddr 172.30.240.98 ip daddr 172.30.240.100 tcp dport 8989 return'''
_SYN_RATE_LIMIT = (
    'tcp flags & (fin | syn | rst | ack) == syn tcp dport 443 '
    'limit rate over 100/second burst 200 packets counter drop'
)


def _ipv4(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4:
        raise ValueError('IPv4 required')
    return str(address)


def home_policy(*, edge_lan: str = '192.168.1.24', edge_tail: str = '100.66.60.55',
                wan_ips: tuple[str, ...] = (),
                admin_ports: tuple[int, ...] = _ADMIN_PORTS) -> str:
    """Render dedicated inet and bridge tables; never flush other owners' tables.

    admin_ports must contain the effective published administrative TCP ports,
    including any local configuration overrides. Bridge filtering is native nft
    and does not depend on br_netfilter or the bridge-nf-call-iptables sysctl.
    """
    if not admin_ports or any(type(port) is not int or not 1 <= port <= 65535
                              for port in admin_ports):
        raise ValueError('Valid administrative TCP ports required')
    ports = ', '.join(str(port) for port in sorted(set(admin_ports)))
    edge = ', '.join(_ipv4(x) for x in (edge_lan, edge_tail))
    blocked = ', '.join((*_BLOCKED_V4, *(_ipv4(x) for x in wan_ips)))
    roles = ', '.join(f'172.30.240.{x}' for x in _PUBLIC_ROLES)
    return f'''add table inet {TABLE}
flush table inet {TABLE}
table inet {TABLE} {{
    set public_roles {{ type ipv4_addr; elements = {{ {roles} }}; }}
    set blocked4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {blocked} }}; }}
    set edge4 {{ type ipv4_addr; elements = {{ {edge} }}; }}
    chain app_egress {{
{_PEER_RULES}
        ip daddr @blocked4 counter drop
        fib daddr type local counter drop
        ip saddr {{ 172.30.240.2, 172.30.240.18 }} ip daddr {{1.1.1.1,1.0.0.1}} udp dport 53 return
        ip saddr {{ 172.30.240.2, 172.30.240.18 }} ip daddr {{1.1.1.1,1.0.0.1}} tcp dport 53 return
        ip saddr {{ 172.30.240.2, 172.30.240.18 }} tcp dport {{ 80, 443 }} return
        counter drop
    }}
    chain input {{
        type filter hook input priority -20; policy accept;
        iifname "hs-public-*" meta nfproto ipv6 counter drop
        iifname "lo" accept
        ct direction reply ct state established,related accept
        ip saddr @public_roles counter drop
        ip saddr @edge4 counter drop
        tcp dport {{ {ports} }} ip saddr 192.168.1.0/24 accept
        tcp dport {{ {ports} }} iifname "tailscale0" accept
        tcp dport {{ {ports} }} counter drop
    }}
    chain forward {{
        type filter hook forward priority -20; policy accept;
        iifname "hs-public-*" meta nfproto ipv6 counter drop
        oifname "hs-public-*" meta nfproto ipv6 counter drop
        ct direction reply ct state established,related accept
        ip saddr @edge4 ip daddr 172.30.240.82 tcp dport {{ 8097, 5056 }} accept
        ip saddr @edge4 counter drop
        ip daddr 172.30.240.82 tcp dport {{ 8097, 5056 }} counter drop
        ip saddr @public_roles jump app_egress
        ct status dnat ct original proto-dst {{ {ports} }} ip saddr 192.168.1.0/24 accept
        ct status dnat ct original proto-dst {{ {ports} }} iifname "tailscale0" accept
        ct status dnat ct original proto-dst {{ {ports} }} counter drop
    }}
}}
add table bridge {TABLE}
flush table bridge {TABLE}
table bridge {TABLE} {{
    set public_roles {{ type ipv4_addr; elements = {{ {roles} }}; }}
    chain app_peers {{
{_PEER_RULES}
        counter drop
    }}
    chain forward {{
        type filter hook forward priority -20; policy accept;
        meta ibrname "hs-public-*" ether type ip6 counter drop
        meta obrname "hs-public-*" ether type ip6 counter drop
        ct direction reply ct state established,related accept
        ip saddr @public_roles jump app_peers
    }}
}}
'''


def edge_policy(*, caddy_uid: int, allowed_cidrs: tuple[str, ...],
                home_lan: str = '192.168.1.20', wan_ips: tuple[str, ...] = ()) -> str:
    """Keep the allowlist until acceptance; an empty list explicitly means public."""
    if type(caddy_uid) is not int or not 1 <= caddy_uid <= 65534:
        raise ValueError('Non-root Caddy UID required')
    home = _ipv4(home_lan)
    allowed = []
    for value in allowed_cidrs:
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4:
            raise ValueError('Edge publication is IPv4 only')
        allowed.append(str(network))
    blocked = ', '.join((*_BLOCKED_V4, *(_ipv4(x) for x in wan_ips)))
    allow = ('ip saddr { ' + ', '.join(allowed) + ' } tcp dport 443 accept') if allowed else (
        'meta nfproto ipv4 tcp dport 443 accept'
    )
    return f'''add table inet {TABLE}
flush table inet {TABLE}
table inet {TABLE} {{
    set blocked4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {blocked} }}; }}
    chain input {{
        type filter hook input priority -20; policy accept;
        iifname "lo" accept
        ct direction reply ct state established,related accept
        iifname "tailscale0" tcp dport 22 accept
        ip saddr 192.168.1.0/24 tcp dport 22 accept
        tcp dport 22 counter drop
        meta nfproto ipv6 tcp dport 443 counter drop
        {_SYN_RATE_LIMIT}
        {allow}
        tcp dport 443 counter drop
        tcp dport 2019 counter drop
    }}
    chain output {{
        type filter hook output priority -20; policy accept;
        meta skuid {caddy_uid} jump caddy_egress
    }}
    chain caddy_egress {{
        ct direction reply ct state established,related accept
        ip daddr {home} tcp dport {{ 8097, 5056 }} accept
        ip daddr {{ 127.0.0.53, 127.0.0.54, 1.1.1.1, 1.0.0.1 }} udp dport 53 accept
        ip daddr {{ 127.0.0.53, 127.0.0.54, 1.1.1.1, 1.0.0.1 }} tcp dport 53 accept
        meta nfproto ipv6 counter drop
        ip daddr @blocked4 counter drop
        fib daddr type local counter drop
        tcp dport {{ 80, 443 }} accept
        counter drop
    }}
}}
'''
