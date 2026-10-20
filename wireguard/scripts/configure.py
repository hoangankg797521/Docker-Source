#!/usr/bin/env python3
"""Generate site configuration and new credentials. Never overwrite an installation."""
import argparse
import ipaddress
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True, help='Public VPN DNS/IP, without scheme or port')
    parser.add_argument('--scope', required=True, help='Comma-separated approved IPv4 LAN CIDRs')
    parser.add_argument('--dns', default='1.1.1.1')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', args.endpoint):
        parser.error('Endpoint must be a hostname or IPv4 without scheme/port')
    try:
        networks = [ipaddress.ip_network(v.strip(), strict=True) for v in args.scope.split(',')]
        if any(n.version != 4 or n.prefixlen == 0 for n in networks):
            raise ValueError('Explicit IPv4 subnets required; /0 forbidden')
        dns = ','.join(str(ipaddress.ip_address(v.strip())) for v in args.dns.split(','))
    except ValueError as error:
        parser.error(str(error))
    if (ROOT/'.env').exists() or (ROOT/'secrets').exists():
        parser.error('.env or secrets already exists; refusing to overwrite')
    os.umask(0o077)
    directory = ROOT/'secrets'
    directory.mkdir(mode=0o700)
    for name in ('wg_token', 'noc_token'):
        path = directory/name
        path.write_text(secrets.token_hex(32)+'\n')
        # NOC runs as uid/gid 10001. Directory remains 0700; only the individual
        # token file is mounted read-only into its authorized container.
        path.chmod(0o444)
    scope = ','.join(str(n) for n in networks)
    (ROOT/'.env').write_text(f'''VPN_ENDPOINT={args.endpoint}
ACL_SCOPE={scope}
VPN_DNS={dns}
VPN_PORT=51820
VPN_BIND_IP=0.0.0.0
WEB_BIND_IP=127.0.0.1
WG_WEB_PORT=51821
NOC_WEB_PORT=51822
WG_EASY_UI_URL=http://localhost:51821
WG_IMAGE=ghcr.io/hoangankg797521/wg-easy:1.0.0
NOC_IMAGE=ghcr.io/hoangankg797521/wg-noc:1.0.0
ACL_IMAGE=ghcr.io/hoangankg797521/wg-acl:1.0.0
''')
    (directory/'bootstrap.env').write_text(f'''INIT_ENABLED=true
INIT_USERNAME=wgadmin
INIT_PASSWORD={secrets.token_urlsafe(32)}
INIT_HOST={args.endpoint}
INIT_PORT=51820
INIT_DNS={dns}
INIT_IPV4_CIDR=10.255.66.0/24
INIT_IPV6_CIDR=fd42:66::/64
INIT_ALLOWED_IPS=10.255.66.0/24,{scope}
''')
    print('Created .env and secrets/. Account: wgadmin. Read its password locally in secrets/bootstrap.env.')
    print('Web binds to localhost. Use SSH forwarding or configure a trusted management IP.')

if __name__ == '__main__':
    main()
