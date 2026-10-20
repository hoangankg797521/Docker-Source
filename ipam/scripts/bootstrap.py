"""Initialize an empty installation; never replace an existing site's schema/data."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

import psycopg

HERE = Path(__file__).resolve().parent


def connect():
    params = dict(
        host=os.getenv('POSTGRES_HOST', 'postgres'),
        port=int(os.getenv('POSTGRES_PORT', '5432')),
        dbname=os.getenv('POSTGRES_DB', 'ipam'),
        user=os.getenv('POSTGRES_USER', 'ipam'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
        connect_timeout=4,
    )
    if not params['password']:
        raise RuntimeError('POSTGRES_PASSWORD is required')
    deadline = time.monotonic() + 90
    while True:
        try:
            return psycopg.connect(**params)
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError('PostgreSQL is unavailable; check host, port, database and credentials') from None
            time.sleep(1)


def initialize():
    release = json.loads((HERE / 'release.json').read_text())
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (1729020201,))
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        password = os.environ.get('IPAM_ADMIN_PASSWORD', '')
        if not tables:
            if len(password) < 12:
                raise RuntimeError('Fresh installation requires IPAM_ADMIN_PASSWORD with at least 12 characters')
            schema = (HERE / 'schema.sql').read_bytes()
            if hashlib.sha256(schema).hexdigest() != release['schema_sha256']:
                raise RuntimeError('Bundled database schema checksum mismatch')
            conn.execute(schema.decode(), prepare=False)
            conn.execute('SET search_path TO public')
            print('IPAM: initialized empty PostgreSQL schema', flush=True)
        else:
            missing = set(release['schema_tables']) - tables
            if missing:
                raise RuntimeError('Existing database requires a reviewed migration; missing tables: ' + ', '.join(sorted(missing)))
        admin = conn.execute("SELECT id FROM public.admin_users WHERE username='admin'").fetchone()
        if not admin:
            if len(password) < 12:
                raise RuntimeError('Initial admin requires IPAM_ADMIN_PASSWORD with at least 12 characters')
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 180000).hex()
            row = conn.execute(
                "INSERT INTO public.admin_roles(name,description,permissions,builtin) VALUES(%s,%s,%s,1) "
                "ON CONFLICT(name) DO UPDATE SET name=excluded.name RETURNING id",
                ('Super Admin', 'Toàn quyền quản trị IPAM', '["*"]'),
            ).fetchone()
            conn.execute(
                "INSERT INTO public.admin_users(username,full_name,password_hash,role_id,status) VALUES(%s,%s,%s,%s,%s)",
                ('admin', 'Administrator', f'pbkdf2$180000${salt}${digest}', row[0], 'active'),
            )
            print('IPAM: initial admin created using the installation password', flush=True)
        print('IPAM: database ready; existing admin passwords are preserved', flush=True)


if __name__ == '__main__':
    try:
        initialize()
    except Exception as exc:
        # Do not print connection parameters or a DSN containing credentials.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('IPAM startup blocked: ' + message, file=sys.stderr)
        sys.exit(1)
    command = sys.argv[1:] or ['python', '-m', 'app.main']
    os.execvp(command[0], command)
