#!/usr/bin/env python3
"""Disposable clean-install, restart, backup and restore checks."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--image', required=True)
args = parser.parse_args()
docker = os.environ.get('DOCKER') or shutil.which('docker')
if not docker:
    raise SystemExit('Docker CLI is required')
project = 'ipam-package-' + secrets.token_hex(4)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
output = root / 'tests/output'
output.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='ipam-package-') as temp:
    temp = Path(temp)
    values = {'IPAM_IMAGE': args.image, 'APP_PORT': str(port), 'POSTGRES_DB': 'ipam',
              'POSTGRES_USER': 'ipam', 'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
              'IPAM_ADMIN_PASSWORD': secrets.token_urlsafe(24), 'DHCP_SYNC_TOKEN': secrets.token_urlsafe(32),
              'AUTH_SECURE_COOKIE': '0'}
    envfile = temp / 'installation.env'
    envfile.write_text(''.join(k + '=' + v + '\n' for k, v in values.items()))
    envfile.chmod(0o600)
    override = temp / 'isolated.yaml'
    override.write_text('services:\n  app:\n    environment:\n      IPAM_STUDIO_STAGING: "1"\n')
    compose = [docker, 'compose', '--project-name', project, '--env-file', str(envfile),
               '-f', str(root / 'compose.bridge.yaml'), '-f', str(override)]
    environment = {k: v for k, v in os.environ.items() if k not in values}
    environment.update(IPAM_TEST_URL=f'http://127.0.0.1:{port}',
                       IPAM_TEST_PASSWORD=values['IPAM_ADMIN_PASSWORD'], IPAM_TEST_OUTPUT=str(output),
                       IPAM_PROJECT_NAME=project, IPAM_COMPOSE_FILE=str(root / 'compose.bridge.yaml'),
                       IPAM_ENV_FILE=str(envfile))

    def run(command, **kwargs):
        return subprocess.run(command, check=True, env=environment, **kwargs)

    def wait_health():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(environment['IPAM_TEST_URL'] + '/healthz', timeout=3) as res:
                    if res.status == 200:
                        return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError('Test installation did not become healthy')

    try:
        run(compose + ['up', '-d', '--wait', '--wait-timeout', '120'])
        wait_health()
        run([sys.executable, str(root / 'tests/smoke.py')])
        run(compose + ['restart', 'app'])
        wait_health()
        run([sys.executable, str(root / 'tests/smoke.py'), '--existing'])
        backup = temp / 'backup'
        run(['sh', str(root / 'scripts/backup.sh'), str(backup)])
        run(['sh', str(root / 'scripts/restore.sh'), str(backup), '--replace-database'])
        wait_health()
        run([sys.executable, str(root / 'tests/smoke.py'), '--existing'])
        report = {'result': 'PASS', 'clean_install': True, 'restart': True, 'backup_restore': True,
                  'custom_admin_preserved': True, 'default_admin_rejected': True,
                  'production_data_included': False}
        (output / 'integration.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)
    except Exception:
        subprocess.run(compose + ['logs', '--no-color', '--tail', '80'], env=environment)
        raise
    finally:
        # These volumes belong only to the randomly named test project above.
        subprocess.run(compose + ['down', '--volumes', '--remove-orphans'], env=environment)
