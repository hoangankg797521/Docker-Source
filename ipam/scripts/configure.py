#!/usr/bin/env python3
"""Create per-site credentials without overwriting an existing installation."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / '.env'
template = (root / '.env.example').read_text()
values = {
    'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
    'IPAM_ADMIN_PASSWORD': secrets.token_urlsafe(24),
    'DHCP_SYNC_TOKEN': secrets.token_urlsafe(32),
}
lines = []
for line in template.splitlines():
    key = line.split('=', 1)[0]
    lines.append(key + '=' + values[key] if key in values else line)
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('.env already exists; kept unchanged')
with os.fdopen(fd, 'w') as out:
    out.write('\n'.join(lines) + '\n')
print('Created .env (mode 600). Initial login: admin; password is IPAM_ADMIN_PASSWORD in .env.')
