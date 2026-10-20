import asyncio
import csv
import io
import ipaddress
import json
import os
import re
import sqlite3
import psycopg
from psycopg import sql as pg_sql
import subprocess
import time
import uuid
import hashlib
import secrets
import socket
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

DBIntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DB_PATH = os.getenv("DB_PATH", "/data/ipam.db")
PORT = int(os.getenv("APP_PORT", "8088"))
CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "512"))
TIMEOUT_MS = int(os.getenv("SCAN_TIMEOUT_MS", "250"))
MAX_SCAN_HOSTS = 65534
DHCP_SYNC_TOKEN = os.getenv("DHCP_SYNC_TOKEN", "change-me-ipam-dhcp-token")
DHCP_SYNC_STALE_MINUTES = int(os.getenv("DHCP_SYNC_STALE_MINUTES", "30"))
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "8"))
AUTH_COOKIE = os.getenv("AUTH_COOKIE", "ipam_session")
AUTH_SECURE_COOKIE = os.getenv("AUTH_SECURE_COOKIE", "0") == "1"

app = FastAPI(title="IP Manager Pro")

# Serve the SPA entry point. Keep this outside the /api namespace so routes
# such as /, /#ips and other hash-based frontend views load normally.
@app.get("/", include_in_schema=False)
def serve_index():
    return FileResponse(STATIC / "index.html")
def _local_time(value):
    """Convert SQLite CURRENT_TIMESTAMP (UTC) text to Vietnam local time."""
    if not value:
        return ""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        dt=datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(value)

jobs = {}
_scan_cancel_events = {}
_discovery_task = None
_discovery_locks = {}
_discovery_locks_guard = threading.Lock()
DISCOVERY_AUTO_DEFAULT_MINUTES = max(1, int(os.getenv("DISCOVERY_AUTO_INTERVAL_MINUTES", "5")))

# V75.2.18: lightweight per-IP switch/port correlation cache.  This is an
# in-memory read-through cache only; it does not alter the SQLite schema or
# stored Discovery data.  Detail views can therefore resolve a single IP
# without waiting for the full /api/ips enrichment pass.
_IP_LOCATION_CACHE_TTL = max(5, int(os.getenv("IP_LOCATION_CACHE_TTL", "30")))
_ip_location_cache = {}
_ip_location_cache_lock = threading.RLock()

def _invalidate_ip_location_cache():
    with _ip_location_cache_lock:
        _ip_location_cache.clear()

def _cached_ip_locations(ip_id, ip, mac="", vlan=None):
    """Use the same read-only resolver as the IP Address table."""
    now=time.monotonic()
    key=int(ip_id)
    fingerprint=(ip,mac,vlan)
    with _ip_location_cache_lock:
        hit=_ip_location_cache.get(key)
        if hit and now-hit["at"] < _IP_LOCATION_CACHE_TTL and hit.get("fingerprint")==fingerprint:
            return hit.get("locations",[])
    c=db()
    try:
        locations=_bulk_ip_network_locations(c,[{"id":key,"ip":ip,"mac":mac,"vlan":vlan}]).get(key,[])
    finally:
        c.close()
    with _ip_location_cache_lock:
        _ip_location_cache[key]={"at":now,"fingerprint":fingerprint,"locations":locations}
    return locations


class _PGRow:
    """SQLite-row compatible view over a PostgreSQL tuple.

    Supports both row[0] and row["column"], plus dict(row) / row.get().
    """
    __slots__ = ("_values", "_names", "_map")
    def __init__(self, names, values):
        self._values = tuple(values)
        self._names = tuple(names)
        self._map = {n: v for n, v in zip(self._names, self._values)}
    def __getitem__(self, key):
        if isinstance(key, int): return self._values[key]
        return self._map[key]
    def get(self, key, default=None): return self._map.get(key, default)
    def keys(self): return self._map.keys()
    def items(self): return self._map.items()
    def values(self): return self._map.values()
    def __iter__(self): return iter(self._map)
    def __len__(self): return len(self._values)


def _convert_pg_rows(cur, rows):
    names=[d.name for d in cur.description] if cur.description else []
    return [_PGRow(names, r) for r in rows]


def _translate_sql(statement):
    """Translate the small SQLite SQL dialect used by the stable IPAM code."""
    s=str(statement)
    # Keep the legacy SQLite timestamp representation (UTC text, second precision)
    # because the migrated schema intentionally preserves TEXT date columns.
    ts="TO_CHAR(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')"
    token='__IPAM_CURRENT_TS__'
    # Protect CURRENT_TIMESTAMP tokens already present in the application SQL.
    s=s.replace('CURRENT_TIMESTAMP', token)
    s=s.replace(
        "datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')",
        "TO_CHAR(CURRENT_TIMESTAMP - (GREATEST(15, COALESCE(d.discovery_interval_minutes, ?)*3) * INTERVAL '1 minute'), 'YYYY-MM-DD HH24:MI:SS')"
    )
    s=s.replace(
        "datetime('now', '-' || MAX(15, ?) || ' minutes')",
        "TO_CHAR(CURRENT_TIMESTAMP - (GREATEST(15, ?) * INTERVAL '1 minute'), 'YYYY-MM-DD HH24:MI:SS')"
    )
    s=s.replace("datetime('now', '-14 days')", "TO_CHAR(CURRENT_TIMESTAMP - INTERVAL '14 days', 'YYYY-MM-DD HH24:MI:SS')")
    s=s.replace("datetime('now', ?)", "TO_CHAR(CURRENT_TIMESTAMP + ?::interval, 'YYYY-MM-DD HH24:MI:SS')")
    s=s.replace("datetime('now')", token)
    # SQLite INSERT OR IGNORE is implemented as INSERT + ON CONFLICT DO NOTHING.
    s=re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', s, flags=re.I)
    # SQLite placeholder -> psycopg placeholder.
    s=s.replace('?', '%s')
    s=re.sub(r'\bMAX\(\s*15\s*,', 'GREATEST(15,', s, flags=re.I)
    # SQLite case-insensitive collation -> explicit LOWER() comparisons.
    for expr in ('m.mac','i.mac','h.mac','mac','name'):
        s=re.sub(r'(?<![A-Za-z0-9_])'+re.escape(expr)+r'\s+COLLATE\s+NOCASE\b', 'LOWER('+expr+')', s, flags=re.I)
    s=re.sub(r'%s\s+COLLATE\s+NOCASE\b', 'LOWER(%s)', s, flags=re.I)
    s=s.replace('COLLATE NOCASE','')
    # Restore the canonical text timestamp representation.
    s=s.replace(token, ts)
    return s


class _PGCursor:
    def __init__(self, conn, raw):
        self._conn=conn
        self._raw=raw
        self._lastrowid=None
        self._returning_id_pending=False
    @property
    def description(self): return self._raw.description
    @property
    def rowcount(self): return self._raw.rowcount
    @property
    def lastrowid(self):
        if self._lastrowid is not None: return self._lastrowid
        if self._returning_id_pending:
            row=self._raw.fetchone()
            self._returning_id_pending=False
            self._lastrowid=row[0] if row else None
        return self._lastrowid
    def execute(self, statement, params=None):
        original = str(statement)
        was_ignore = bool(
            re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', original, flags=re.I)
        )

        s = _translate_pg_sql(original)

        # psycopg treats % as a parameter marker when parameters are supplied.
        # Preserve %s placeholders while escaping literal % used by SQLite LIKE.
        if params:
            s = s.replace("%s", "__PG_PARAM__")
            s = s.replace("%", "%%")
            s = s.replace("__PG_PARAM__", "%s")

        self._lastrowid = None

        # Ask PostgreSQL to return generated IDs for INSERT statements
        # that previously relied on SQLite lastrowid.
        m = re.search(
            r'\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)',
            s,
            flags=re.I
        )

        id_tables = {
            'admin_roles',
            'network_groups',
            'admin_users',
            'snmp_profiles',
            'network_devices',
            'network_interfaces',
            'network_mac_entries',
            'network_mac_port_history',
            'network_neighbors',
            'network_arp_entries',
            'discovery_jobs',
            'ips',
            'subnets',
            'users',
            'user_groups',
            'dhcp_sync_requests',
            'audit',
            'conflict_events',
            'scan_observations',
            'topology_positions',
        }

        if m and m.group(1).lower() in id_tables and "RETURNING" not in s.upper():
            s = s.rstrip().rstrip(";") + " RETURNING id"

        if was_ignore:
            if re.search(r'\bON\s+CONFLICT\b', s, flags=re.I):
                pass
            else:
                s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

                # Keep RETURNING after ON CONFLICT.
                if " RETURNING id" in s:
                    s = s.replace(
                        " RETURNING id ON CONFLICT DO NOTHING",
                        " ON CONFLICT DO NOTHING RETURNING id"
                    )

        self._raw.execute(s, params)

        if m and m.group(1).lower() in id_tables and "RETURNING id" in s.upper():
            # Do not consume the RETURNING row here. Stable application code
            # often calls cur.fetchone() immediately after execute().
            self._returning_id_pending=True

        self._conn._changed = max(
            0,
            self._conn._changed + max(0, self._raw.rowcount)
        )

        return self

    def executemany(self, statement, seq):
        original=str(statement)
        s=_translate_pg_sql(original)
        was_ignore=bool(re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', original, flags=re.I))
        if was_ignore and not re.search(r'\bON\s+CONFLICT\b', s, flags=re.I):
            s=s.rstrip().rstrip(';')+' ON CONFLICT DO NOTHING'
        self._raw.executemany(s, seq)
        self._conn._changed=max(0, self._conn._changed + max(0, self._raw.rowcount))
        return self
    def fetchone(self):
        row=self._raw.fetchone()
        if self._returning_id_pending:
            self._returning_id_pending=False
            self._lastrowid=row[0] if row else None
        if row is None: return None
        names=[d.name for d in self._raw.description] if self._raw.description else []
        return _PGRow(names,row)
    def fetchall(self):
        rows=self._raw.fetchall()
        return _convert_pg_rows(self._raw, rows)
    def fetchmany(self, size=None):
        rows=self._raw.fetchmany(size) if size is not None else self._raw.fetchmany()
        return _convert_pg_rows(self._raw, rows)
    def close(self): return self._raw.close()
    def __iter__(self):
        for row in self._raw:
            names=[d.name for d in self._raw.description] if self._raw.description else []
            yield _PGRow(names,row)


def _translate_pg_sql(statement):
    original=str(statement)
    was_ignore=bool(re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', original, flags=re.I))
    s=_translate_sql(original)
    # _translate_sql intentionally leaves conflict placement to the cursor.
    return s


class _PGConnection:
    def __init__(self, raw):
        self._raw=raw
        self._changed=0
    def cursor(self): return _PGCursor(self, self._raw.cursor())
    def execute(self, statement, params=None):
        cur=self.cursor(); cur.execute(statement, params); return cur
    def executemany(self, statement, seq):
        cur=self.cursor(); cur.executemany(statement, seq); return cur
    def commit(self): return self._raw.commit()
    def rollback(self): return self._raw.rollback()
    def close(self): return self._raw.close()
    @property
    def total_changes(self): return self._changed


def db():
    conn=psycopg.connect(
        host=os.getenv('POSTGRES_HOST','127.0.0.1'),
        port=int(os.getenv('POSTGRES_PORT','5432')),
        dbname=os.getenv('POSTGRES_DB','ipam'),
        user=os.getenv('POSTGRES_USER','ipam'),
        password=os.getenv('POSTGRES_PASSWORD',''),
        connect_timeout=10,
    )
    return _PGConnection(conn)


def _ensure_pg_performance_indexes(c):
    """Small, additive PostgreSQL indexes for hot IPAM read paths.

    These are all IF NOT EXISTS and do not change tables, columns, or data.
    They specifically support IP search and Switch/Port correlation.
    """
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_ips_ip ON ips (ip)",
        "CREATE INDEX IF NOT EXISTS idx_ips_mac_lower ON ips (LOWER(mac))",
        "CREATE INDEX IF NOT EXISTS idx_ips_status_vlan ON ips (status, vlan)",
        "CREATE INDEX IF NOT EXISTS idx_nd_name_lower ON network_devices (LOWER(name))",
        "CREATE INDEX IF NOT EXISTS idx_nme_device_mac_lower ON network_mac_entries (device_id, LOWER(mac))",
        "CREATE INDEX IF NOT EXISTS idx_nme_device_if ON network_mac_entries (device_id, if_index)",
        "CREATE INDEX IF NOT EXISTS idx_nae_device_ip ON network_arp_entries (device_id, ip)",
        "CREATE INDEX IF NOT EXISTS idx_nae_ip_mac ON network_arp_entries (ip, mac)",
        "CREATE INDEX IF NOT EXISTS idx_dhcp_leases_ip_mac ON dhcp_leases (ip, mac)",
        "CREATE INDEX IF NOT EXISTS idx_dhcp_res_ip_mac ON dhcp_reservations (ip, mac)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_mac_last_seen ON network_mac_port_history (LOWER(mac), last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_last_seen ON network_mac_port_history (last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_vlan_last_seen ON network_mac_port_history (vlan_id, last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_device_last_seen ON network_mac_port_history (device_id, last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_port_last_seen ON network_mac_port_history (LOWER(if_name), last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_type_last_seen ON network_mac_port_history (LOWER(port_type), last_seen DESC)",
        # PostgreSQL substring filters used by Endpoint History. Trigram indexes
        # make MAC/Port/Switch/Hostname contains-search responsive without
        # changing application data or the existing query contract.
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_mac_trgm ON network_mac_port_history USING gin (LOWER(mac) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_mac_hist_ifname_trgm ON network_mac_port_history USING gin (LOWER(if_name) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_nd_name_trgm ON network_devices USING gin (LOWER(name) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_ips_hostname_trgm ON ips USING gin (LOWER(hostname) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_admin_users_username_lower ON admin_users (LOWER(username))",
        "CREATE INDEX IF NOT EXISTS idx_admin_users_email_lower ON admin_users (LOWER(email))",
        "CREATE INDEX IF NOT EXISTS idx_admin_users_role ON admin_users (role_id)",
        "CREATE INDEX IF NOT EXISTS idx_user_groups_user ON user_groups (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups (group_id)",
        "CREATE INDEX IF NOT EXISTS idx_network_groups_name_lower ON network_groups (LOWER(name))",
        "CREATE INDEX IF NOT EXISTS idx_admin_roles_name_lower ON admin_roles (LOWER(name))",
    ]
    for ddl in indexes:
        c.execute(ddl)

def _cleanup_endpoint_history(batch=5000):
    """Delete old endpoint history in short PostgreSQL transactions.

    Only network_mac_port_history is pruned.  Deletes are batched so cleanup
    never holds a long table lock and cannot recreate the old SQLite lock issue.
    """
    total=0
    while True:
        c=db()
        try:
            cur=c.execute("""DELETE FROM network_mac_port_history
                             WHERE id IN (SELECT id FROM network_mac_port_history
                                          WHERE last_seen < TO_CHAR(clock_timestamp() - INTERVAL '14 days', 'YYYY-MM-DD HH24:MI:SS')
                                          ORDER BY id LIMIT %s)""", (int(batch),))
            n=max(0, int(cur.rowcount or 0))
            c.commit()
        except Exception:
            c.rollback()
            n=0
        finally:
            c.close()
        total += n
        if n < batch:
            break
    return total


def init_db():
    """PostgreSQL production startup check/seed.

    The schema was migrated offline from the known-good SQLite database.
    Phase 2 deliberately does not run SQLite DDL or ALTER TABLE statements at
    application startup; this prevents accidental schema mutation on production.
    """
    required=('ips','audit','subnets','dhcp_sources','dhcp_leases','dhcp_reservations',
              'dhcp_managed_ips','dhcp_sync_requests','admin_roles','network_groups',
              'admin_users','user_groups','admin_sessions','snmp_profiles','network_devices',
              'network_interfaces','network_neighbors','topology_positions','topology_manual_links',
              'discovery_jobs','network_mac_entries','network_mac_port_history','network_arp_entries',
              'network_groups','users','groups','sessions','network_lag_members','studio_link_history',
              'network_port_modes')
    c=db()
    try:
        found={r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
        missing=[t for t in required if t not in found]
        if missing:
            raise RuntimeError('PostgreSQL schema thiếu bảng: '+', '.join(sorted(set(missing))))
        _ensure_pg_performance_indexes(c)
        # Ensure the same seed roles/admin behavior as the stable SQLite build.
        role_defs=[
            ('Super Admin','Toàn quyền quản trị IPAM',['*']),
            ('IPAM Admin','Quản lý IP, VLAN/Subnet, DHCP và Scanner',['ip.read','ip.write','subnet.read','subnet.write','scanner','dhcp.read','dhcp.write','audit.read','discovery.read','discovery.write']),
            ('Network Operator','Vận hành mạng và Scanner',['ip.read','ip.write','subnet.read','scanner','dhcp.read','dhcp.write','discovery.read','discovery.write']),
            ('Viewer','Chỉ xem dữ liệu IPAM',['ip.read','subnet.read','dhcp.read','audit.read']),
        ]
        for rn,rd,perms in role_defs:
            c.execute("INSERT INTO admin_roles(name,description,permissions,builtin) VALUES(%s,%s,%s,1) ON CONFLICT(name) DO NOTHING",(rn,rd,json.dumps(perms,ensure_ascii=False)))
        salt='ipam-admin-seed'; ph=hashlib.pbkdf2_hmac('sha256',b'admin',salt.encode(),180000).hex()
        rr=c.execute("SELECT id FROM admin_roles WHERE name='Super Admin'").fetchone()
        if rr:
            c.execute("INSERT INTO admin_users(username,full_name,password_hash,role_id,status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING",('admin','Administrator',f'pbkdf2$180000${salt}${ph}',rr[0],'active'))
        for rn in ('IPAM Admin','Network Operator'):
            rr=c.execute("SELECT id,permissions FROM admin_roles WHERE name=%s",(rn,)).fetchone()
            if rr:
                try: perms=json.loads(rr[1] or '[]')
                except Exception: perms=[]
                for perm in ('discovery.read','discovery.write'):
                    if perm not in perms: perms.append(perm)
                c.execute("UPDATE admin_roles SET permissions=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(json.dumps(perms,ensure_ascii=False),rr[0]))
        # Keep the same orphan cleanup safety from the stable build.
        for table in ('network_neighbors','network_interfaces','network_mac_entries','network_mac_port_history','network_arp_entries','topology_positions','discovery_jobs'):
            c.execute(f"DELETE FROM {table} WHERE device_id IS NOT NULL AND device_id NOT IN (SELECT id FROM network_devices)")
        c.execute("DELETE FROM topology_manual_links WHERE source_device_id NOT IN (SELECT id FROM network_devices) OR target_device_id NOT IN (SELECT id FROM network_devices)")
        c.commit()
    finally:
        c.close()


def audit(action, target, detail="", conn=None):
    # When called from an active SQLite write transaction, reuse that same
    # connection. Opening a second connection here would try to acquire a
    # competing write lock on the same database and can deadlock the
    # discovery transaction (database is locked).
    if conn is not None:
        conn.execute("INSERT INTO audit(action,target,detail) VALUES(?,?,?)",(action,target,detail))
        return
    c=db()
    try:
        c.execute("INSERT INTO audit(action,target,detail) VALUES(?,?,?)",(action,target,detail))
        c.commit()
    finally:
        c.close()


def merge_import_fields(existing, incoming, allowed_fields=None):
    """
    Merge import data without erasing existing values.
    Only non-empty incoming values are applied. Fields not present
    in the import are preserved.
    """
    allowed = set(allowed_fields or incoming.keys())
    result = dict(existing or {})
    for key, value in incoming.items():
        if key not in allowed:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        result[key] = value
    return result


async def _auto_discovery_loop():
    last_history_cleanup=0.0
    while True:
        try:
            # Prune endpoint history at most once per hour, using short batched
            # PostgreSQL transactions so normal Discovery/API traffic stays responsive.
            nonlocal_cleanup_due = time.monotonic() - last_history_cleanup >= 3600
            if nonlocal_cleanup_due:
                _cleanup_endpoint_history()
                last_history_cleanup=time.monotonic()
            c=db(); rows=c.execute("SELECT id,discovery_interval_minutes FROM network_devices WHERE COALESCE(auto_discovery_enabled,1)=1").fetchall(); c.close()
            for row in rows:
                # A manual network batch owns the scan window. Do not start
                # another scheduled device scan while that batch is active.
                with _discover_all_jobs_lock:
                    batch_active=any(j.get("state") in ("queued","running") for j in _discover_all_jobs.values())
                if batch_active: break
                interval=max(1,int(row["discovery_interval_minutes"] or DISCOVERY_AUTO_DEFAULT_MINUTES))
                # sqlite3.Row is mapping-like but does not implement dict.get().
                # Keep the auto-discovery scheduler compatible with all sqlite3.Row builds.
                row_id=int(row["id"])
                c2=db(); due=c2.execute("SELECT id FROM network_devices WHERE id=? AND COALESCE(auto_discovery_enabled,1)=1 AND (last_discovery IS NULL OR last_discovery='' OR last_discovery <= datetime('now', ?))",(row_id,f"-{interval} minutes")).fetchone(); c2.close()
                if not due: continue
                lock=_get_discovery_lock(row_id)
                if not lock.acquire(blocking=False): continue
                try:
                    await asyncio.to_thread(_discover_device_sync,row_id,False,True)
                    audit("DISCOVERY_AUTO",str(row_id),f"device_id={row_id}; interval={interval}m")
                except Exception as e:
                    # A device can be deleted while the 30-second scheduler is
                    # processing its previous snapshot. Do not keep generating
                    # scary errors for a device that no longer exists.
                    try:
                        cc=db(); exists=cc.execute("SELECT 1 FROM network_devices WHERE id=?",(row_id,)).fetchone(); cc.close()
                    except Exception:
                        exists=True
                    if exists:
                        audit("DISCOVERY_AUTO_ERROR",str(row_id),str(e)[:900])
                finally: lock.release()
        except Exception:
            pass
        await asyncio.sleep(30)

def _get_discovery_lock(device_id:int):
    with _discovery_locks_guard:
        return _discovery_locks.setdefault(int(device_id),threading.Lock())

@app.on_event("startup")
async def startup():
    global _discovery_task
    init_db()
    _discovery_task=None if os.getenv("IPAM_STUDIO_STAGING")=="1" else asyncio.create_task(_auto_discovery_loop())

@app.on_event("shutdown")
async def shutdown():
    global _discovery_task
    if _discovery_task:
        _discovery_task.cancel()
        try: await _discovery_task
        except asyncio.CancelledError: pass
        _discovery_task=None

class IPIn(BaseModel):
    ip: str
    vlan: str = ""
    hostname: str = ""
    device: str = ""
    user_dept: str = ""
    status: str = "free"
    notes: str = ""
    mac: str = ""
    vendor: str = ""

class BulkDelete(BaseModel):
    ids: list[int]

class BulkStatus(BaseModel):
    ids: list[int]
    status: str

class BulkMetadata(BaseModel):
    ids: list[int]
    device: str | None = None
    user_dept: str | None = None

class ScanIn(BaseModel):
    cidr: str
    nmap: bool = False
    tcp_scan: bool = False
    reverse_dns: bool = False
    ping_stats: bool = False

class ScanRegister(BaseModel):
    records: list[dict]
    status: str = "used"

class SubnetIn(BaseModel):
    vlan: str = ""
    name: str = ""
    cidr: str
    gateway: str = ""
    dns: str = ""
    dhcp: str = ""
    notes: str = ""

class DHCPLease(BaseModel):
    ip: str
    mac: str = ""
    hostname: str = ""

class DHCPReservation(BaseModel):
    ip: str
    mac: str = ""
    hostname: str = ""

class DHCPSyncIn(BaseModel):
    source_id: str
    name: str
    server_ip: str = ""
    leases: list[DHCPLease] = []
    reservations: list[DHCPReservation] = []

class AdminUserIn(BaseModel):
    username: str
    full_name: str = ""
    email: str = ""
    password: str = ""
    role_id: Optional[int] = None
    status: str = "active"
    group_ids: list[int] = []

class NetworkGroupIn(BaseModel):
    name: str
    description: str = ""
    vlan: str = ""
    subnet_id: Optional[int] = None

class AdminRoleIn(BaseModel):
    name: str
    description: str = ""
    permissions: list[str] = []

class LoginIn(BaseModel):
    username: str
    password: str

class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str

class AdminUserUpdateIn(BaseModel):
    full_name: str = ""
    email: str = ""
    password: str = ""
    role_id: Optional[int] = None
    status: str = "active"
    group_ids: list[int] = []

class AdminRoleUpdateIn(BaseModel):
    description: str = ""
    permissions: list[str] = []

class SNMPProfileIn(BaseModel):
    name: str
    version: str = "2c"
    community: str = ""
    username: str = ""
    security_level: str = "authPriv"
    auth_protocol: str = "SHA"
    auth_password: str = ""
    priv_protocol: str = "AES"
    priv_password: str = ""
    port: int = 161
    timeout: int = 3
    retries: int = 1

class NetworkDeviceIn(BaseModel):
    name: str
    management_ip: str
    vendor: str = "Cisco"
    device_type: str = "Cisco Nexus"
    snmp_profile_id: int
    auto_discovery_enabled: bool = True
    discovery_interval_minutes: int = DISCOVERY_AUTO_DEFAULT_MINUTES

PERMISSION_MAP = {
    ("GET", "/api/insights"): "ip.read",
    ("POST", "/api/insights/"): "ip.write",
    ("GET", "/api/scan-history"): "scanner",
    ("DELETE", "/api/scan/"): "scanner",
    ("GET", "/api/ips"): "ip.read",
    ("GET", "/api/ips/filters"): "ip.read",
    ("GET", "/api/stats"): "ip.read",
    ("GET", "/api/dashboard"): "ip.read",
    ("GET", "/api/check-ip"): "ip.read",
    ("POST", "/api/ips"): "ip.write",
    ("PUT", "/api/ips/"): "ip.write",
    ("DELETE", "/api/ips/"): "ip.write",
    ("POST", "/api/ips/bulk-delete"): "ip.write",
    ("POST", "/api/ips/bulk-status"): "ip.write",
    ("POST", "/api/ips/bulk-metadata"): "ip.write",
    ("POST", "/api/ips/bulk-register-scan"): "ip.write",
    ("POST", "/api/free/candidates"): "ip.read",
    ("POST", "/api/free/create"): "ip.write",
    ("GET", "/api/tools/ping"): "scanner",
    ("GET", "/api/tools/trace"): "scanner",
    ("POST", "/api/scan"): "scanner",
    ("GET", "/api/scan/"): "scanner",
    ("GET", "/api/subnets"): "subnet.read",
    ("GET", "/api/subnets/"): "subnet.read",
    ("POST", "/api/subnets"): "subnet.write",
    ("PUT", "/api/subnets/"): "subnet.write",
    ("DELETE", "/api/subnets/"): "subnet.write",
    ("GET", "/api/audit"): "audit.read",
    ("GET", "/api/history"): "discovery.read",
    ("GET", "/api/history/options"): "discovery.read",
    ("GET", "/api/discovery/profiles"): "discovery.read",
    ("POST", "/api/discovery/profiles"): "discovery.write",
    ("PUT", "/api/discovery/profiles/"): "discovery.write",
    ("DELETE", "/api/discovery/profiles/"): "discovery.write",
    ("GET", "/api/discovery/devices"): "discovery.read",
    ("POST", "/api/discovery/devices"): "discovery.write",
    ("POST", "/api/discovery/devices/replace"): "discovery.write",
    ("PUT", "/api/discovery/devices/"): "discovery.write",
    ("DELETE", "/api/discovery/devices/"): "discovery.write",
    ("POST", "/api/discovery/devices/"): "discovery.write",
    ("GET", "/api/discovery/devices/"): "discovery.read",
    ("GET", "/api/discovery/topology"): "discovery.read",
    ("POST", "/api/discovery/topology/positions"): "discovery.write",
    ("POST", "/api/discovery/topology/links"): "discovery.write",
    ("DELETE", "/api/discovery/topology/links/"): "discovery.write",
    ("GET", "/api/dhcp/status"): "dhcp.read",
    ("GET", "/api/dhcp/leases"): "dhcp.read",
    ("GET", "/api/dhcp/reservations"): "dhcp.read",
    ("POST", "/api/dhcp/sync-now"): "dhcp.write",
    ("POST", "/api/dhcp/sync-now/"): "dhcp.write",
    ("POST", "/api/import"): "ip.write",
    ("GET", "/api/export"): "ip.read",
    ("GET", "/api/export-xlsx"): "ip.read",
    ("GET", "/api/admin/users"): "user.read",
    ("GET", "/api/admin/users/"): "user.read",
    ("POST", "/api/admin/users"): "user.write",
    ("PUT", "/api/admin/users/"): "user.write",
    ("DELETE", "/api/admin/users/"): "user.write",
    ("GET", "/api/admin/groups"): "group.read",
    ("POST", "/api/admin/groups"): "group.write",
    ("PUT", "/api/admin/groups/"): "group.write",
    ("DELETE", "/api/admin/groups/"): "group.write",
    ("GET", "/api/admin/roles"): "role.read",
    ("POST", "/api/admin/roles"): "role.write",
    ("PUT", "/api/admin/roles/"): "role.write",
    ("DELETE", "/api/admin/roles/"): "role.write",
}

def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, it, salt, digest = stored.split("$", 3)
        if scheme != "pbkdf2": return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(it)).hex()
        return secrets.compare_digest(calc, digest)
    except Exception:
        return False

def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def _role_for_user(c, user_id: int):
    return c.execute("SELECT u.*, r.name role_name, r.permissions FROM admin_users u LEFT JOIN admin_roles r ON r.id=u.role_id WHERE u.id=?", (user_id,)).fetchone()

def _permissions(row):
    try: return json.loads(row["permissions"] or "[]") if row else []
    except Exception: return []

def _has_permission(row, perm: str) -> bool:
    return bool(row and ("*" in _permissions(row) or perm in _permissions(row)))

def _public_user(row):
    return {"id":row["id"],"username":row["username"],"full_name":row["full_name"],"email":row["email"],"role_id":row["role_id"],"role_name":row["role_name"],"status":row["status"],"permissions":_permissions(row)}

@app.post("/api/auth/login")
def auth_login(x: LoginIn):
    from fastapi import Response
    c=db(); row=c.execute("SELECT u.*, r.name role_name, r.permissions FROM admin_users u LEFT JOIN admin_roles r ON r.id=u.role_id WHERE lower(u.username)=lower(?)", (x.username.strip(),)).fetchone()
    if not row or row["status"] != "active" or not _verify_password(x.password, row["password_hash"]):
        c.close(); raise HTTPException(401,"Username hoặc Password không đúng")
    token=secrets.token_urlsafe(48); th=_session_hash(token)
    c.execute("DELETE FROM admin_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
    c.execute("INSERT INTO admin_sessions(token_hash,user_id,expires_at) VALUES(?,?,datetime('now', ?))", (th,row["id"],f"+{SESSION_HOURS} hours"))
    c.commit(); c.close()
    from fastapi.responses import JSONResponse
    out=JSONResponse({"ok":True,"user":_public_user(row)})
    out.set_cookie(AUTH_COOKIE,token,max_age=SESSION_HOURS*3600,httponly=True,samesite="lax",secure=AUTH_SECURE_COOKIE,path="/")
    return out

@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token=request.cookies.get(AUTH_COOKIE)
    c=db()
    if token: c.execute("DELETE FROM admin_sessions WHERE token_hash=?",(_session_hash(token),)); c.commit()
    c.close()
    from fastapi.responses import JSONResponse
    out=JSONResponse({"ok":True}); out.delete_cookie(AUTH_COOKIE,path="/"); return out

@app.get("/api/auth/me")
def auth_me(request: Request):
    token=request.cookies.get(AUTH_COOKIE)
    if not token: raise HTTPException(401,"Chưa đăng nhập")
    c=db(); row=c.execute("SELECT u.*, r.name role_name, r.permissions FROM admin_sessions s JOIN admin_users u ON u.id=s.user_id LEFT JOIN admin_roles r ON r.id=u.role_id WHERE s.token_hash=? AND u.status='active' AND s.expires_at > CURRENT_TIMESTAMP",(_session_hash(token),)).fetchone()
    if not row: c.close(); raise HTTPException(401,"Phiên đăng nhập đã hết hạn")
    c.execute("UPDATE admin_sessions SET last_seen=CURRENT_TIMESTAMP WHERE token_hash=?",(_session_hash(token),)); c.commit(); c.close(); return _public_user(row)

@app.post("/api/auth/change-password")
def auth_change_password(x: PasswordChangeIn, request: Request):
    token=request.cookies.get(AUTH_COOKIE)
    if not token: raise HTTPException(401,"Chưa đăng nhập")
    c=db(); row=c.execute("SELECT u.*, r.name role_name, r.permissions FROM admin_sessions s JOIN admin_users u ON u.id=s.user_id LEFT JOIN admin_roles r ON r.id=u.role_id WHERE s.token_hash=? AND u.status='active' AND s.expires_at > CURRENT_TIMESTAMP",(_session_hash(token),)).fetchone()
    if not row: c.close(); raise HTTPException(401,"Phiên đăng nhập đã hết hạn")
    if not _verify_password(x.current_password,row["password_hash"]): c.close(); raise HTTPException(400,"Password hiện tại không đúng")
    if len(x.new_password) < 8: c.close(); raise HTTPException(400,"Password mới phải có ít nhất 8 ký tự")
    c.execute("UPDATE admin_users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(_password_hash(x.new_password),row["id"])); c.commit(); c.close(); audit("PASSWORD_CHANGE",row["username"],"Đổi password"); return {"ok":True}

def _auth_context(request: Request):
    token=request.cookies.get(AUTH_COOKIE)
    if not token: raise HTTPException(401,"Chưa đăng nhập")
    c=db(); row=c.execute("SELECT u.*, r.name role_name, r.permissions FROM admin_sessions s JOIN admin_users u ON u.id=s.user_id LEFT JOIN admin_roles r ON r.id=u.role_id WHERE s.token_hash=? AND u.status='active' AND s.expires_at > CURRENT_TIMESTAMP",(_session_hash(token),)).fetchone(); c.close()
    if not row: raise HTTPException(401,"Phiên đăng nhập đã hết hạn")
    return row

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path=request.url.path; method=request.method
    if not path.startswith("/api/") or path in {"/api/auth/login","/api/auth/logout","/api/auth/me","/api/auth/change-password"} or path.startswith("/api/dhcp/sync") or path.startswith("/api/dhcp/sync-request"):
        return await call_next(request)
    key=(method,path)
    perm=PERMISSION_MAP.get(key)
    if not perm:
        for (m,pfx),v in PERMISSION_MAP.items():
            if m==method and pfx.endswith("/") and path.startswith(pfx): perm=v; break
    if not perm: return await call_next(request)
    try: row=_auth_context(request)
    except HTTPException as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":e.detail},status_code=e.status_code)
    if not _has_permission(row,perm):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":f"Không có quyền: {perm}"},status_code=403)
    request.state.user=row
    return await call_next(request)

def _password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 180000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2${iterations}${salt}${digest}"

@app.get("/api/admin/roles")
def admin_roles():
    # PostgreSQL-safe read path. Keep this endpoint deliberately simple so the
    # Administrator/Role page never depends on SQLite compatibility tricks.
    c=db()
    try:
        rows=c.execute("""
            SELECT id,name,description,permissions,builtin,created_at,updated_at
            FROM admin_roles
            ORDER BY builtin DESC, name ASC
        """).fetchall()
        out=[]
        for r in rows:
            d=row_dict(r)
            raw=d.get("permissions")
            if isinstance(raw, str):
                try: perms=json.loads(raw) if raw.strip() else []
                except Exception: perms=[]
            elif raw is None:
                perms=[]
            else:
                try: perms=list(raw)
                except Exception: perms=[]
            d["permissions"]=perms
            d["builtin"]=bool(d.get("builtin"))
            out.append(d)
        return out
    finally:
        c.close()

@app.post("/api/admin/roles")
def create_admin_role(x: AdminRoleIn):
    if not x.name.strip(): raise HTTPException(400,"Tên Role không được trống")
    c=db()
    try:
        cur=c.execute("INSERT INTO admin_roles(name,description,permissions,builtin) VALUES(?,?,?,0) RETURNING id",
                      (x.name.strip(),x.description.strip(),json.dumps(x.permissions,ensure_ascii=False)))
        row=cur.fetchone()
        if not row:
            c.rollback(); raise HTTPException(500,"PostgreSQL không trả về Role ID")
        rid=row[0]
        c.commit()
    except DBIntegrityError:
        c.rollback(); c.close(); raise HTTPException(409,"Role đã tồn tại")
    except HTTPException:
        c.close(); raise
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); audit("ROLE_CREATE",x.name.strip(),"Tạo Role"); return {"id":rid}

@app.put("/api/admin/roles/{rid}")
def edit_admin_role(rid:int, x: AdminRoleUpdateIn):
    c=db(); row=c.execute("SELECT name,builtin FROM admin_roles WHERE id=?",(rid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"Role không tồn tại")
    if row["builtin"]: c.close(); raise HTTPException(400,"Role BUILT-IN không thể chỉnh sửa")
    c.execute("UPDATE admin_roles SET description=?,permissions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(x.description.strip(),json.dumps(x.permissions,ensure_ascii=False),rid)); c.commit(); c.close(); audit("ROLE_EDIT",row["name"],"Sửa Role"); return {"ok":True}

@app.delete("/api/admin/roles/{rid}")
def delete_admin_role(rid:int):
    c=db(); row=c.execute("SELECT name,builtin FROM admin_roles WHERE id=?",(rid,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"Role không tồn tại")
    if row["builtin"]:
        c.close(); raise HTTPException(400,"Role BUILT-IN không thể xóa")
    used=c.execute("SELECT COUNT(*) FROM admin_users WHERE role_id=?",(rid,)).fetchone()[0]
    if used:
        c.close(); raise HTTPException(409,f"Role đang được gán cho {used} User, không thể xóa")
    c.execute("DELETE FROM admin_roles WHERE id=?",(rid,)); c.commit(); c.close(); audit("ROLE_DELETE",row["name"],"Xóa Role"); return {"ok":True}

@app.get("/api/admin/groups")
def admin_groups():
    # Explicit columns avoid driver-dependent g.* row layouts on PostgreSQL.
    c=db()
    try:
        rows=c.execute("""
            SELECT g.id,g.name,g.description,g.vlan,g.subnet_id,g.created_at,g.updated_at,
                   s.name AS subnet_name,s.cidr,
                   COALESCE((SELECT COUNT(*) FROM user_groups ug WHERE ug.group_id=g.id),0) AS user_count
            FROM network_groups g
            LEFT JOIN subnets s ON s.id=g.subnet_id
            ORDER BY g.name ASC
        """).fetchall()
        return [row_dict(r) for r in rows]
    finally:
        c.close()

@app.post("/api/admin/groups")
def create_admin_group(x: NetworkGroupIn):
    if not x.name.strip(): raise HTTPException(400,"Tên Group không được trống")
    c=db()
    try:
        cur=c.execute("INSERT INTO network_groups(name,description,vlan,subnet_id) VALUES(?,?,?,?) RETURNING id",
                      (x.name.strip(),x.description.strip(),x.vlan.strip(),x.subnet_id))
        row=cur.fetchone()
        if not row:
            c.rollback(); raise HTTPException(500,"PostgreSQL không trả về Group ID")
        gid=row[0]
        c.commit()
    except DBIntegrityError:
        c.rollback(); c.close(); raise HTTPException(409,"Group đã tồn tại")
    except HTTPException:
        c.close(); raise
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); audit("GROUP_CREATE",x.name.strip(),"Tạo Network Group"); return {"id":gid}

@app.put("/api/admin/groups/{gid}")
def edit_admin_group(gid:int, x: NetworkGroupIn):
    if not x.name.strip(): raise HTTPException(400,"Tên Group không được trống")
    c=db(); row=c.execute("SELECT name FROM network_groups WHERE id=?",(gid,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"Group không tồn tại")
    try:
        c.execute("UPDATE network_groups SET name=?,description=?,vlan=?,subnet_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (x.name.strip(),x.description.strip(),x.vlan.strip(),x.subnet_id,gid))
        c.commit()
    except DBIntegrityError:
        c.rollback(); c.close(); raise HTTPException(409,"Tên Group đã tồn tại hoặc Subnet không hợp lệ")
    c.close(); audit("GROUP_EDIT",x.name.strip(),"Sửa Network Group"); return {"ok":True}

@app.delete("/api/admin/groups/{gid}")
def delete_admin_group(gid:int):
    c=db(); row=c.execute("SELECT name FROM network_groups WHERE id=?",(gid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"Group không tồn tại")
    c.execute("DELETE FROM network_groups WHERE id=?",(gid,)); c.commit(); c.close(); audit("GROUP_DELETE",row["name"],"Xóa Network Group"); return {"ok":True}

@app.get("/api/admin/users")
def admin_users(q:str=""):
    """Fast PostgreSQL admin-user list. Keep this read-only and avoid a large GROUP BY.

    The previous join+GROUP BY path was unnecessarily expensive on PostgreSQL and
    could fail on migrated schemas with mixed legacy column types. A correlated
    aggregate keeps the result identical while allowing the indexed user lookup
    to stay small.
    """
    q=str(q or "").strip()
    c=db()
    try:
        like=f"%{q}%"
        rows=c.execute("""
            SELECT u.id,u.username,u.full_name,u.email,u.role_id,u.status,u.created_at,u.updated_at,
                   r.name role_name,
                   COALESCE((SELECT STRING_AGG(g2.name, ', ' ORDER BY g2.name)
                             FROM user_groups ug2
                             JOIN network_groups g2 ON g2.id=ug2.group_id
                             WHERE ug2.user_id=u.id),'') group_names
            FROM admin_users u
            LEFT JOIN admin_roles r ON r.id=u.role_id
            WHERE ?=''
               OR u.username ILIKE ? OR u.full_name ILIKE ? OR u.email ILIKE ?
               OR r.name ILIKE ?
               OR EXISTS (SELECT 1 FROM user_groups ug3 JOIN network_groups g3 ON g3.id=ug3.group_id
                          WHERE ug3.user_id=u.id AND g3.name ILIKE ?)
            ORDER BY u.username
        """,(q,like,like,like,like,like)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

@app.get("/api/admin/users/{uid}")
def get_admin_user(uid:int):
    c=db(); row=c.execute("SELECT u.id,u.username,u.full_name,u.email,u.role_id,u.status,r.name role_name FROM admin_users u LEFT JOIN admin_roles r ON r.id=u.role_id WHERE u.id=?",(uid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"User không tồn tại")
    d=dict(row); d["group_ids"]=[r["group_id"] for r in c.execute("SELECT group_id FROM user_groups WHERE user_id=? ORDER BY group_id",(uid,)).fetchall()]; c.close(); return d

@app.post("/api/admin/users")
def create_admin_user(x: AdminUserIn):
    username=x.username.strip()
    if not username: raise HTTPException(400,"Username không được trống")
    if not x.password: raise HTTPException(400,"Password không được trống")
    if x.status not in ("active","disabled"): raise HTTPException(400,"Status không hợp lệ")
    c=db()
    try:
        cur=c.execute("INSERT INTO admin_users(username,full_name,email,password_hash,role_id,status) VALUES(?,?,?,?,?,?) RETURNING id",(username,x.full_name.strip(),x.email.strip(),_password_hash(x.password),x.role_id,x.status)); uid=cur.fetchone()[0]
        for gid in sorted(set(x.group_ids)): c.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)",(uid,gid))
        c.commit()
    except DBIntegrityError: c.rollback(); c.close(); raise HTTPException(409,"Username đã tồn tại hoặc Role/Group không hợp lệ")
    c.close(); audit("USER_CREATE",username,"Tạo tài khoản quản trị"); return {"id":uid}

@app.put("/api/admin/users/{uid}")
def edit_admin_user(uid:int, x: AdminUserUpdateIn):
    c=db()
    row=c.execute("SELECT id,username FROM admin_users WHERE id=?",(uid,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"User không tồn tại")
    if x.status not in ("active","disabled"):
        c.close(); raise HTTPException(400,"Status không hợp lệ")
    if x.password and len(x.password) < 8:
        c.close(); raise HTTPException(400,"Password mới phải có ít nhất 8 ký tự")
    try:
        if x.password:
            c.execute("UPDATE admin_users SET full_name=?,email=?,password_hash=?,role_id=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (x.full_name.strip(),x.email.strip(),_password_hash(x.password),x.role_id,x.status,uid))
        else:
            c.execute("UPDATE admin_users SET full_name=?,email=?,role_id=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (x.full_name.strip(),x.email.strip(),x.role_id,x.status,uid))
        c.execute("DELETE FROM user_groups WHERE user_id=?",(uid,))
        for gid in sorted(set(x.group_ids)):
            c.execute("INSERT INTO user_groups(user_id,group_id) VALUES(?,?)",(uid,gid))
        c.commit()
    except DBIntegrityError:
        c.rollback(); c.close(); raise HTTPException(409,"Role/Group không hợp lệ")
    c.close(); audit("USER_EDIT",row["username"],"Sửa tài khoản quản trị"); return {"ok":True}

@app.delete("/api/admin/users/{uid}")
def delete_admin_user(uid:int):
    c=db(); row=c.execute("SELECT username FROM admin_users WHERE id=?",(uid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"User không tồn tại")
    if row["username"]=="admin": c.close(); raise HTTPException(400,"Không thể xóa tài khoản admin mặc định")
    c.execute("DELETE FROM admin_users WHERE id=?",(uid,)); c.commit(); c.close(); audit("USER_DELETE",row["username"],"Xóa tài khoản quản trị"); return {"ok":True}

def valid_ip(s):
    try:
        return str(ipaddress.ip_address(s))
    except: return None

DHCP_VLAN_DEPT_MAP = {
    "172": "Bệnh Viện",
    "20": "Phòng Khám",
    "200": "PC Office",
    "40": "PC IT",
    "11": "IP Phone",
    "30": "Management",
}

def _dhcp_user_dept_for_vlan(vlan: str) -> str:
    return DHCP_VLAN_DEPT_MAP.get(str(vlan).strip(), "")

def _dhcp_vlan_for_ip(c, ip: str) -> str:
    """Resolve VLAN from the configured subnet containing this DHCP IP.

    When multiple subnets overlap, prefer the most specific (longest prefix)
    match. DHCP-managed addresses should inherit VLAN from IPAM subnet config;
    if no matching subnet (or the subnet has no VLAN) exists, return empty and
    leave any existing VLAN unchanged.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return ""
    matches = []
    for row in c.execute("SELECT vlan,cidr FROM subnets WHERE vlan IS NOT NULL AND TRIM(vlan) <> ''"):
        try:
            net = ipaddress.ip_network(row["cidr"], strict=False)
            if addr.version == net.version and addr in net:
                matches.append((net.prefixlen, str(row["vlan"]).strip()))
        except Exception:
            continue
    if not matches:
        return ""
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[0][1]


def normalize_mac(s):
    raw=(s or "").strip().lower()
    compact=re.sub(r"[.\-:\s]", "", raw)
    if re.fullmatch(r"[0-9a-f]{12}", compact):
        return ":".join(compact[i:i+2] for i in range(0,12,2))
    if len(raw)%2==0 and re.fullmatch(r"[0-9a-f]+", raw):
        try:
            decoded=bytes.fromhex(raw).decode("ascii")
            compact2=re.sub(r"[.\-:\s]", "", decoded.lower())
            if re.fullmatch(r"[0-9a-f]{12}", compact2):
                return ":".join(compact2[i:i+2] for i in range(0,12,2))
        except Exception:
            pass
    return ""


def row_dict(r):
    d=dict(r)
    # Some PostgreSQL endpoints (Roles/Groups/History) do not select user_dept.
    # Never let a missing legacy column turn a valid 200 response into a blank UI.
    if "user_dept" in d:
        d["user"] = d.pop("user_dept") or ""
    return d

def _is_port_channel_name(name):
    p=str(name or "").strip().lower()
    return bool(p) and (p.startswith("po") or "port-channel" in p or "port channel" in p or p.startswith("lag"))

def _is_physical_interface_name(name):
    p=str(name or "").strip().lower()
    if not p or _is_port_channel_name(p):
        return False
    # Cisco Nexus / SG350X physical interface names.
    return bool(re.match(r"^(eth|ethernet|gi|gigabitethernet|te|tengigabitethernet|tw|twentyfivegige|fo|fortygigabitethernet|hu|hundredgige|fa|fastethernet|mgmt|management)\s*\d", p))

def _fanout_is_probable_uplink(macs, vlans):
    # A phone + PC or a small endpoint bridge can legitimately learn several MACs.
    return macs >= 128 or vlans >= 8 or (macs >= 32 and vlans >= 3)

def _network_location_is_direct(loc):
    """Expose only a real physical client-facing interface.

    Logical interfaces (Port-Channel/LAG/VLAN/SVI) are never client ports.
    Inter-switch ports are removed separately using discovered CDP/LLDP
    neighbors. This prevents an uplink such as Gi1/0/24 from being presented
    as a user/client attachment merely because a MAC/ARP entry was learned
    there.
    """
    if not _is_physical_interface_name(loc.get("port")) or loc.get("inter_switch"):
        return False
    if str(loc.get("port_type") or "").lower() in ("uplink", "lag", "trunk"):
        return False
    if bool(loc.get("probable_uplink")):
        return False
    if _fanout_is_probable_uplink(int(loc.get("fanout_macs") or 0), int(loc.get("fanout_vlans") or 0)):
        return False
    desc=str(loc.get("if_descr") or "").lower()
    return not any(k in desc for k in ("uplink","trunk","peer","vpc","port-channel","port channel","to sw","to switch","switch-to-switch"))

def _attachment_confidence(loc):
    """Score IP->MAC->port evidence without changing the stored source data."""
    score=40  # physical interface + MAC/ARP correlation is already useful evidence
    if loc.get("arp_if_index") and loc.get("if_index") and int(loc.get("arp_if_index") or 0)==int(loc.get("if_index") or 0):
        score += 30
    if loc.get("mac_if_index") and loc.get("if_index") and int(loc.get("mac_if_index") or 0)==int(loc.get("if_index") or 0):
        score += 15
    if str(loc.get("port_status") or "").lower() in ("up","up(1)","1"):
        score += 5
    if loc.get("inter_switch"):
        score -= 50
    return max(0,min(100,score))

def _collapse_network_locations(locations):
    """Return the cleanest direct client attachment for an IP/MAC."""
    direct=[x for x in locations if _network_location_is_direct(x)]
    if not direct:
        return []
    uniq=[]; seen=set()
    for x in direct:
        key=(str(x.get("switch_name") or ""),str(x.get("port") or ""),str(x.get("vlan") or ""))
        if key in seen: continue
        seen.add(key); uniq.append(x)
    def score(x):
        conf=_attachment_confidence(x)
        desc=str(x.get("if_descr") or "").lower()
        penalty=20 if any(k in desc for k in ("uplink","trunk","peer","vpc","port-channel","port channel")) else 0
        return (conf-penalty, 1 if str(x.get("port_status") or "").lower() in ("up","up(1)","1","learned") else 0, str(x.get("last_discovery") or ""))
    uniq.sort(key=score, reverse=True)
    return uniq[:1]

def _accurate_ip_network_locations(c, target_rows, switch_port=""):
    """Batch IP -> Switch/Port resolver.

    This keeps the V75.2.16 evidence/scoring rules but removes the N+1 query
    pattern from V75.2.19.  One batch uses a handful of indexed/aggregated SQL
    reads instead of one SQL query per MAC/interface.  DHCP lease and
    reservation MACs are first-class candidates, so DHCP/RESERVED rows behave
    exactly like STATIC rows.
    """
    if not target_rows:
        return {}

    # Current ARP evidence for all target IPs in this batch.
    ipmarks=','.join('?'*len(target_rows))
    ips=[str(r.get('ip') or '') for r in target_rows]
    arp_rows=c.execute(f"""
        SELECT a.ip,a.mac,a.device_id,a.if_index,a.last_seen
        FROM network_arp_entries a JOIN network_devices d ON d.id=a.device_id
        WHERE a.ip IN ({ipmarks})
          AND LOWER(COALESCE(d.status,''))='up'
          AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
          AND a.last_seen >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
        ORDER BY a.last_seen DESC
    """, (*ips,DISCOVERY_AUTO_DEFAULT_MINUTES)).fetchall()
    arps_by_ip={}
    for r in arp_rows: arps_by_ip.setdefault(str(r['ip']),[]).append(r)

    # DHCP sources for this batch. Reservations are not time-limited; leases
    # use the same freshness expectation as Discovery/ARP.
    dhcp_rows=c.execute(f"""
        SELECT ip,mac,last_seen FROM dhcp_leases
        WHERE ip IN ({ipmarks}) AND TRIM(COALESCE(mac,''))<>''
          AND last_seen >= datetime('now', '-' || MAX(15, ?) || ' minutes')
        UNION ALL
        SELECT ip,mac,last_seen FROM dhcp_reservations
        WHERE ip IN ({ipmarks}) AND TRIM(COALESCE(mac,''))<>''
    """, (*ips,DISCOVERY_AUTO_DEFAULT_MINUTES,*ips)).fetchall()
    dhcp_by_ip={}
    for r in dhcp_rows: dhcp_by_ip.setdefault(str(r['ip']),[]).append(r)

    # Candidate MACs per IP, preserving the evidence that made a MAC strong.
    candidates_by_ip={}
    all_macs=set()
    for target in target_rows:
        ip=str(target.get('ip') or '')
        arr=[]
        im=normalize_mac(target.get('mac') or '')
        if im:
            arr.append((im,0,0))
            all_macs.add(im.lower())
        for a in arps_by_ip.get(ip,[]):
            am=normalize_mac(a['mac'] or '')
            if am:
                arr.append((am,int(a['device_id'] or 0),int(a['if_index'] or 0)))
                all_macs.add(am.lower())
        for h in dhcp_by_ip.get(ip,[]):
            hm=normalize_mac(h['mac'] or '')
            if hm:
                arr.append((hm,0,0))
                all_macs.add(hm.lower())
        # de-duplicate while retaining ARP preference evidence
        seen=set(); clean=[]
        for item in arr:
            key=item[0].lower()
            if key not in seen:
                seen.add(key); clean.append(item)
        candidates_by_ip[ip]=clean

    if not all_macs:
        return {int(r.get('id') or 0):[] for r in target_rows}

    # Pull all FDB rows for the batch's MAC set in one query. Optional
    # switch filtering is pushed down to SQL, making the column filter fast.
    mm=','.join('?'*len(all_macs))
    sql=f"""
        SELECT m.*,d.name switch_name,d.management_ip switch_ip,d.status device_status,
               d.last_discovery,d.discovery_interval_minutes,d.sys_name,
               COALESCE(ni.if_name,m.if_name,'') resolved_if_name,
               COALESCE(ni.if_descr,'') if_descr,COALESCE(ni.oper_status,'') oper_status
        FROM network_mac_entries m
        JOIN network_devices d ON d.id=m.device_id
        LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
        WHERE m.mac COLLATE NOCASE IN ({mm})
          AND LOWER(COALESCE(d.status,''))='up'
          AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
          AND m.last_seen >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
    """
    args=[x.lower() for x in all_macs]+[DISCOVERY_AUTO_DEFAULT_MINUTES]
    if switch_port:
        sql += " AND LOWER(d.name)=LOWER(?)"
        args.append(str(switch_port))
    sql += " ORDER BY m.vlan_id,m.if_index"
    rows=c.execute(sql,args).fetchall()

    # Only compute fanout and LLDP/CDP context for interfaces that actually
    # appeared in this batch. This avoids regrouping the entire FDB/neighbors
    # tables for every 300-IP page.
    pairs=sorted({(int(r['device_id']),int(r['if_index'] or 0)) for r in rows if int(r['if_index'] or 0)})
    fan={}
    if pairs:
        cond=' OR '.join(['(device_id=? AND if_index=?)']*len(pairs))
        par=[]
        for did,idx in pairs: par.extend([did,idx])
        for fr in c.execute(f"SELECT device_id,if_index,COUNT(DISTINCT mac) macs,COUNT(DISTINCT vlan_id) vlans FROM network_mac_entries WHERE {cond} GROUP BY device_id,if_index",par).fetchall():
            fan[(int(fr['device_id']),int(fr['if_index']))]=(int(fr['macs'] or 0),int(fr['vlans'] or 0))
    neighbor_pairs=set()
    dids=sorted({did for did,_ in pairs})
    if dids:
        dm=','.join('?'*len(dids))
        for nr in c.execute(f"SELECT * FROM network_neighbors WHERE device_id IN ({dm})",dids).fetchall():
            if _neighbor_proves_transit(dict(nr)):
                neighbor_pairs.add((int(nr['device_id']),int(nr['local_if_index'])))

    by_mac={}
    for r in rows:
        rr=dict(r)
        name=str(rr.get('resolved_if_name') or rr.get('if_name') or '').strip()
        descr=str(rr.get('if_descr') or '')
        fan_m,fan_v=fan.get((int(rr['device_id']),int(rr['if_index'] or 0)),(0,0))
        rr['fanout_macs']=fan_m; rr['fanout_vlans']=fan_v
        rr['inter_switch']=1 if (int(rr['device_id']),int(rr['if_index'] or 0)) in neighbor_pairs else 0
        inter=bool(rr['inter_switch'])
        textual=any(k in descr.lower() for k in ('uplink','trunk','peer','vpc','port-channel','port channel','to sw','to switch'))
        probable=bool(inter or _is_port_channel_name(name) or textual or _fanout_is_probable_uplink(fan_m,fan_v))
        role='uplink' if probable else ('access' if _is_physical_interface_name(name) else 'unknown')
        evidence=[]
        if inter: evidence.append('LLDP/CDP')
        if textual: evidence.append('description')
        if fan_m>=32: evidence.append(f'fanout={fan_m} MAC')
        if fan_v>=3: evidence.append(f'VLANs={fan_v}')
        item=rr
        item['_name']=name; item['_descr']=descr; item['_fan_m']=fan_m; item['_fan_v']=fan_v
        item['_inter']=inter; item['_probable']=probable; item['_role']=role; item['_evidence']=', '.join(evidence)
        by_mac.setdefault(normalize_mac(rr.get('mac') or '') or str(rr.get('mac') or '').lower(),[]).append(item)

    out_by_id={}
    for target in target_rows:
        ip_id=int(target.get('id') or 0); ip=str(target.get('ip') or ''); vlan=target.get('vlan')
        locs=[]
        for mac, pref_dev, pref_if in candidates_by_ip.get(ip,[]):
            for z in by_mac.get(mac.lower(),[]):
                vlan_match=bool(vlan not in (None,'','—') and str(z.get('vlan_id'))==str(vlan))
                arp_match=bool(pref_if and int(z.get('if_index') or 0)==pref_if)
                preferred=bool(pref_dev and int(z.get('device_id') or 0)==pref_dev)
                score=20
                if _is_physical_interface_name(z['_name']): score+=30
                if z['_role']=='access': score+=25
                if vlan_match: score+=15
                if arp_match: score+=12
                if str(z.get('oper_status') or '').lower() in ('up','up(1)','1'): score+=5
                if preferred: score+=2
                if z['_probable']: score-=70
                if _is_port_channel_name(z['_name']): score-=40
                locs.append({'switch_name':z.get('switch_name'),'switch_ip':z.get('switch_ip'),
                    'port':z['_name'] or '—','port_status':z.get('oper_status') or 'learned',
                    'vlan':z.get('vlan_id'),'last_discovery':z.get('last_seen') or z.get('last_discovery'),
                    'if_descr':z['_descr'],'if_index':z.get('if_index') or 0,
                    'mac_if_index':z.get('if_index') or 0,'arp_if_index':pref_if or 0,
                    'inter_switch':z['_inter'],'port_type':'uplink' if z['_probable'] else ('access' if z['_role']=='access' else 'unknown'),
                    'probable_uplink':z['_probable'],
                    'role_evidence':z['_evidence'],'confidence':max(0,min(100,score)),
                    'fanout_macs':z['_fan_m'],'fanout_vlans':z['_fan_v']})
        # Same strict final gate as V75.2.18/19: never show a transit port as
        # the endpoint. If several direct observations exist, choose one.
        selected=_collapse_network_locations(locs)
        out_by_id[ip_id]=selected
    missing=[r for r in target_rows if not out_by_id.get(int(r.get('id') or 0))]
    if missing:
        history=_batch_historical_ip_locations(c,missing,candidates_by_ip,switch_port)
        for key, value in history.items():
            out_by_id[key]=value
    return out_by_id


def _batch_historical_ip_locations(c, targets, candidates_by_ip, switch_port=""):
    """Bounded history fallback; label transit/history rather than invent access ports."""
    macs=sorted({m.lower() for r in targets for m,_,_ in candidates_by_ip.get(str(r.get('ip') or ''),[])})
    if not macs: return {}
    marks=','.join('?'*len(macs))
    args=list(macs)
    swsql=''
    if switch_port:
        swsql=' AND LOWER(d.name)=LOWER(?)'; args.append(str(switch_port))
    rows=c.execute(f"""WITH recent AS (
        SELECT h.*,ROW_NUMBER() OVER (PARTITION BY LOWER(h.mac),h.vlan_id,h.device_id ORDER BY h.last_seen DESC,h.id DESC) rn
        FROM network_mac_port_history h
        WHERE LOWER(h.mac) IN ({marks}) AND h.last_seen >= datetime('now', '-14 days')
    ) SELECT h.*,d.name switch_name,d.management_ip switch_ip,
        COALESCE(NULLIF(ni.if_name,''),h.if_name,'') resolved_if_name,
        COALESCE(ni.if_descr,'') if_descr
      FROM recent h JOIN network_devices d ON d.id=h.device_id
      LEFT JOIN network_interfaces ni ON ni.device_id=h.device_id AND ni.if_index=h.if_index
      WHERE h.rn<=4{swsql} ORDER BY h.last_seen DESC,h.id DESC""",args).fetchall()
    # Apply the same LLDP endpoint gate as live and selected-switch Search.
    # Keep this batched: history may include several switches for each MAC.
    neighbor_pairs=set();dids=sorted({int(r['device_id']) for r in rows})
    if dids:
        dm=','.join('?'*len(dids))
        for nr in c.execute(f'SELECT * FROM network_neighbors WHERE device_id IN ({dm})',dids).fetchall():
            if _neighbor_proves_transit(dict(nr)):
                neighbor_pairs.add((int(nr['device_id']),int(nr['local_if_index'])))
    by_mac={}
    for row in rows:
        r=dict(row);r['inter_switch']=(int(r['device_id']),int(r['if_index'] or 0)) in neighbor_pairs
        name=str(r.get('resolved_if_name') or r.get('if_name') or '')
        textual=any(k in str(r.get('if_descr') or '').lower() for k in ('uplink','trunk','peer','vpc','port-channel','to switch','to sw'))
        transit=bool(r.get('inter_switch') or _is_port_channel_name(name) or textual or str(r.get('port_type') or '').lower()=='uplink')
        loc={'switch_name':r['switch_name'],'switch_ip':r['switch_ip'],'port':name or '—',
             'vlan':r.get('vlan_id'),'last_discovery':r.get('last_seen') or '',
             'if_index':r.get('if_index') or 0,'port_status':'historical','historical':True,
             'port_type':'uplink' if transit else ('access' if _is_physical_interface_name(name) else 'unknown'),
             'probable_uplink':transit,'inter_switch':bool(r.get('inter_switch')),
             'role_evidence':'Last known từ lịch sử Discovery',
             'confidence':min(35 if transit else 90,max(0,int(r.get('confidence') or 40)))}
        by_mac.setdefault(normalize_mac(r.get('mac') or ''),[]).append(loc)
    out={}
    for target in targets:
        arr=[]; seen=set(); vlan=str(target.get('vlan') or '')
        for mac,_,_ in candidates_by_ip.get(str(target.get('ip') or ''),[]):
            for loc in by_mac.get(mac,[]):
                if vlan and vlan!='—' and str(loc['vlan'])!=vlan: continue
                key=(loc['switch_ip'],loc['port'],loc['vlan'])
                if key not in seen: seen.add(key);arr.append(loc)
        direct=[z for z in arr if z['port_type']=='access' and not z['probable_uplink']]
        out[int(target.get('id') or 0)]=(direct or arr)[:4]
    return out


def _bulk_ip_network_locations(c, target_rows, switch_port=""):
    if not target_rows:
        return {}
    result=_accurate_ip_network_locations(c,target_rows,switch_port=switch_port)
    for locs in result.values():
        for z in locs: z["evidence"]=_location_evidence(z)
    return result


def _discovered_switch_trunk_ports(c, device_id):
    """Return only ports whose operational trunk state came from Cisco VTP MIB."""
    rows=c.execute('''SELECT pm.if_index,ni.if_name,ni.oper_status,ni.speed_bps,
                             pm.configured_mode,pm.operational_mode,pm.source,pm.last_seen
                      FROM network_port_modes pm
                      JOIN network_interfaces ni ON ni.device_id=pm.device_id AND ni.if_index=pm.if_index
                      WHERE pm.device_id=? AND pm.operational_mode='TRUNKING'
                      ORDER BY LOWER(ni.if_name)''',(int(device_id),)).fetchall()
    if not rows: return []
    indexes=[int(r['if_index']) for r in rows]
    marks=','.join('?'*len(indexes))
    peers={}
    for r in c.execute(f'''SELECT local_if_index,protocol,remote_device,remote_port FROM network_neighbors
                           WHERE device_id=? AND local_if_index IN ({marks})
                           ORDER BY CASE WHEN UPPER(protocol)='LLDP' THEN 0 ELSE 1 END,confidence DESC''',
                      [int(device_id),*indexes]).fetchall():
        peers.setdefault(int(r['local_if_index']),{'peer':'','peer_port':'','protocol':''})
        current=peers[int(r['local_if_index'])]
        if not current['peer'] and r['remote_device'] and str(r['remote_device']).strip('"'):
            current.update({'peer':str(r['remote_device']).strip('"'),'peer_port':str(r['remote_port'] or ''),'protocol':str(r['protocol'] or '')})
    return [{**dict(r),**peers.get(int(r['if_index']),{})} for r in rows]

@app.get("/api/ips/network-locations")
def get_ip_network_locations(q:str="", status:str="", vlan:str="", ip:str="", mac:str="", hostname:str="", device:str="", user:str="", notes:str="", switch_port:str="", offset:int=0, limit:int=300, fast:int=0):
    """Progressive, accurate IP->Switch/Port hydration.

    The endpoint is intentionally paged.  Each response is small enough to
    return quickly, and the resolver is the same V75.2.16 implementation used
    by the accurate /api/ips enrichment path.  No SNMP request is performed.
    """
    offset=max(0,int(offset or 0)); limit=max(1,min(1000,int(limit or 300)))
    c=db()
    try:
        if switch_port and int(fast or 0):
            sw=c.execute("SELECT id,name,management_ip FROM network_devices WHERE LOWER(name)=LOWER(?) LIMIT 1",(str(switch_port),)).fetchone()
            if not sw:
                return {'locations':{},'switch_ports':[],'matched_ips':0,'matched_ids':[],'rows':[],'offset':offset,'limit':limit,'returned_ips':0,'next_offset':None,'located_ips':0}
            swid=int(sw['id'])
            switch_ports=_discovered_switch_trunk_ports(c,swid)
            sql="SELECT i.id,i.ip,i.mac,i.vlan,i.hostname,i.device,i.user_dept,i.notes,i.status FROM ips i WHERE 1=1"; args=[]
            if q:
                nq=re.sub(r"[.\-:\s]", "", q.lower())
                sql += " AND (i.ip ILIKE ? OR i.mac ILIKE ? OR i.hostname ILIKE ? OR i.device ILIKE ? OR i.user_dept ILIKE ? OR i.notes ILIKE ? OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(i.mac,'')),':',''),'-',''),'.','') LIKE ?)"; args += [f"%{q}%"]*6 + [f"%{nq}%"]
            if status: sql += " AND i.status=?"; args.append(status)
            if vlan: sql += " AND i.vlan=?"; args.append(vlan)
            if ip: sql += " AND i.ip ILIKE ?"; args.append(f"%{ip}%")
            if mac: sql += " AND i.mac ILIKE ?"; args.append(f"%{mac}%")
            if hostname: sql += " AND i.hostname ILIKE ?"; args.append(f"%{hostname}%")
            if device: sql += " AND i.device ILIKE ?"; args.append(f"%{device}%")
            if user: sql += " AND i.user_dept ILIKE ?"; args.append(f"%{user}%")
            if notes: sql += " AND i.notes ILIKE ?"; args.append(f"%{notes}%")
            # Switch/Port filter must match a real client-facing physical
            # interface on the selected switch.  A MAC learned only on Po/LAG
            # is a transit observation and must not make every downstream IP
            # appear as "Po1" on this switch.
            physical_exists = """
              EXISTS (
                SELECT 1 FROM network_mac_entries m
                LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
                WHERE m.device_id=? AND m.mac COLLATE NOCASE=i.mac COLLATE NOCASE
                  AND TRIM(COALESCE(ni.if_name,m.if_name,''))<>''
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'po%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE '%port-channel%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'lag%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'vlan%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'loopback%'
              )
              OR EXISTS (
                SELECT 1 FROM network_arp_entries a
                LEFT JOIN network_interfaces ni ON ni.device_id=a.device_id AND ni.if_index=a.if_index
                WHERE a.device_id=? AND a.ip=i.ip
                  AND TRIM(COALESCE(ni.if_name,a.if_name,''))<>''
                  AND LOWER(COALESCE(ni.if_name,a.if_name,'')) NOT LIKE 'po%'
                  AND LOWER(COALESCE(ni.if_name,a.if_name,'')) NOT LIKE '%port-channel%'
                  AND LOWER(COALESCE(ni.if_name,a.if_name,'')) NOT LIKE 'lag%'
                  AND LOWER(COALESCE(ni.if_name,a.if_name,'')) NOT LIKE 'vlan%'
                  AND LOWER(COALESCE(ni.if_name,a.if_name,'')) NOT LIKE 'loopback%'
              )
              OR EXISTS (
                SELECT 1 FROM dhcp_leases h
                JOIN network_mac_entries m ON m.device_id=? AND m.mac COLLATE NOCASE=h.mac COLLATE NOCASE
                LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
                WHERE h.ip=i.ip
                  AND TRIM(COALESCE(ni.if_name,m.if_name,''))<>''
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'po%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE '%port-channel%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'lag%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'vlan%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'loopback%'
              )
              OR EXISTS (
                SELECT 1 FROM dhcp_reservations h
                JOIN network_mac_entries m ON m.device_id=? AND m.mac COLLATE NOCASE=h.mac COLLATE NOCASE
                LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
                WHERE h.ip=i.ip
                  AND TRIM(COALESCE(ni.if_name,m.if_name,''))<>''
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'po%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE '%port-channel%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'lag%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'vlan%'
                  AND LOWER(COALESCE(ni.if_name,m.if_name,'')) NOT LIKE 'loopback%'
              )
            """
            sql += " AND (" + physical_exists + ")"
            args += [swid,swid,swid,swid]
            total=int(c.execute("SELECT COUNT(*) FROM ("+sql+")",args).fetchone()[0])
            targets=[dict(r) for r in c.execute(sql+" ORDER BY i.id LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()]
            if not targets:
                return {'locations':{},'switch_ports':switch_ports,'matched_ips':total,'matched_ids':[],'rows':[],'offset':offset,'limit':limit,'returned_ips':0,'next_offset':None,'located_ips':0}
            ips_q=[str(r['ip']) for r in targets]; im=','.join('?'*len(ips_q))
            arp_rows=[]
            for chunk_start in range(0,len(ips_q),400):
                chunk=ips_q[chunk_start:chunk_start+400]; imc=','.join('?'*len(chunk))
                arp_rows.extend(c.execute(f"SELECT ip,mac,if_index,if_name FROM network_arp_entries WHERE device_id=? AND ip IN ({imc}) ORDER BY last_seen DESC",[swid,*chunk]).fetchall())
            arp_by_ip={}
            for r in arp_rows: arp_by_ip.setdefault(str(r['ip']),[]).append(dict(r))
            macs=[]
            for r in targets:
                m=normalize_mac(r.get('mac') or '')
                if m: macs.append(m.lower())
            for rr in arp_rows:
                m=normalize_mac(rr['mac'] or '')
                if m: macs.append(m.lower())
            dhcp_rows=[]
            for chunk_start in range(0,len(ips_q),400):
                chunk=ips_q[chunk_start:chunk_start+400]; imc=','.join('?'*len(chunk))
                dhcp_rows.extend(c.execute(f"SELECT ip,mac FROM dhcp_leases WHERE ip IN ({imc}) UNION ALL SELECT ip,mac FROM dhcp_reservations WHERE ip IN ({imc})",[*chunk,*chunk]).fetchall())
            dhcp_by_ip={}
            for r in dhcp_rows:
                m=normalize_mac(r['mac'] or '')
                if m:
                    macs.append(m.lower()); dhcp_by_ip.setdefault(str(r['ip']),set()).add(m.lower())
            macs=sorted(set(macs)); fdb_by_mac={}
            if macs:
                for chunk_start in range(0,len(macs),400):
                    chunk=macs[chunk_start:chunk_start+400]; mm=','.join('?'*len(chunk))
                    fr=c.execute(f"SELECT m.*,COALESCE(ni.if_name,m.if_name,'') resolved_if_name FROM network_mac_entries m LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index WHERE m.device_id=? AND m.mac COLLATE NOCASE IN ({mm}) ORDER BY m.vlan_id,m.if_index",[swid,*chunk]).fetchall()
                    for r in fr: fdb_by_mac.setdefault(normalize_mac(r['mac'] or '') or str(r['mac']).lower(),[]).append(dict(r))
            # Build the interface context once for this selected switch.
            # V75.2.50.6 only checked that a name was physical, which allowed
            # a real physical trunk/uplink (for example Gi1/0/48) to leak into
            # the Switch/Port filter.  Keep this fast path SQL-only, but apply
            # the same authoritative evidence used by the normal resolver:
            # LLDP/CDP neighbour, interface description, MAC fanout and VLAN
            # fanout.  This avoids an N+1 query per IP/MAC.
            candidate_ifidx=set()
            for vals in fdb_by_mac.values():
                for r in vals:
                    try: candidate_ifidx.add(int(r.get('if_index') or 0))
                    except Exception: pass
            for vals in arp_by_ip.values():
                for r in vals:
                    try: candidate_ifidx.add(int(r.get('if_index') or 0))
                    except Exception: pass
            candidate_ifidx.discard(0)
            iface_ctx={}
            if candidate_ifidx:
                imarks=','.join('?'*len(candidate_ifidx)); ipar=[swid,*sorted(candidate_ifidx)]
                for ir in c.execute(f"SELECT if_index,if_name,if_descr,oper_status FROM network_interfaces WHERE device_id=? AND if_index IN ({imarks})",ipar).fetchall():
                    iface_ctx[int(ir['if_index'])]={'if_name':str(ir['if_name'] or ''),'if_descr':str(ir['if_descr'] or ''),'oper_status':str(ir['oper_status'] or '')}
                for fr in c.execute(f"SELECT m.if_index,COUNT(DISTINCT m.mac) macs,COUNT(DISTINCT m.vlan_id) vlans,COUNT(DISTINCT a.ip) ips FROM network_mac_entries m LEFT JOIN network_arp_entries a ON a.device_id=m.device_id AND a.if_index=m.if_index WHERE m.device_id=? AND m.if_index IN ({imarks}) GROUP BY m.if_index",ipar).fetchall():
                    iface_ctx.setdefault(int(fr['if_index']),{}).update({'fanout_macs':int(fr['macs'] or 0),'fanout_vlans':int(fr['vlans'] or 0),'fanout_ips':int(fr['ips'] or 0)})
                for nr in c.execute(f"SELECT * FROM network_neighbors WHERE device_id=? AND local_if_index IN ({imarks})",ipar).fetchall():
                    if _neighbor_proves_transit(dict(nr)):
                        iface_ctx.setdefault(int(nr['local_if_index']),{})['inter_switch']=True

            def fast_classify(r, arp_if=0, target_vlan=None):
                idx=int(r.get('if_index') or 0)
                ctx=iface_ctx.get(idx,{})
                name=str(ctx.get('if_name') or r.get('resolved_if_name') or r.get('if_name') or '').strip()
                descr=str(ctx.get('if_descr') or '')
                fan_m=int(ctx.get('fanout_macs') or 0); fan_v=int(ctx.get('fanout_vlans') or 0); fan_i=int(ctx.get('fanout_ips') or 0)
                inter=bool(ctx.get('inter_switch'))
                textual=any(k in descr.lower() for k in ('uplink','trunk','peer','vpc','port-channel','port channel','to sw','to switch','switch-to-switch'))
                fanout_candidate=_fanout_is_probable_uplink(fan_m,fan_v)
                # Use the same fanout rule as Discovery and global Search.
                # A few MACs/IPs alone are not evidence of an uplink.
                high_fanout=_fanout_is_probable_uplink(fan_m,fan_v)
                strong_uplink=bool(inter or _is_port_channel_name(name) or textual or high_fanout)
                probable=bool(strong_uplink or fanout_candidate)
                if not _is_physical_interface_name(name):
                    return None
                if strong_uplink:
                    # This is a transit/trunk observation, not a directly attached
                    # endpoint.  Keep the raw FDB data untouched, but do not expose
                    # all downstream IPs as if they were connected to this switch.
                    return None
                vlan_match=bool(target_vlan not in (None,'','—') and str(r.get('vlan_id'))==str(target_vlan))
                arp_match=bool(arp_if and idx==int(arp_if))
                oper=str(ctx.get('oper_status') or r.get('status') or '').lower()
                score=40+(30 if arp_match else 0)+(15 if vlan_match else 0)+(5 if oper in ('up','up(1)','1') else 0)
                # Strong transit observations were already excluded above.
                port_type='uplink' if fanout_candidate else 'access'
                evidence='FDB / Switch filter'
                if inter: evidence+=' · LLDP/CDP'
                if textual: evidence+=' · description'
                if fanout_candidate: evidence+=f' · fanout={fan_m} MAC/{fan_i} IP'
                score += 5 if probable else max(0,10-min(fan_m,10))
                return {'switch_name':sw_name,'switch_ip':sw_ip,'port':name,'port_status':ctx.get('oper_status') or r.get('status') or 'learned','vlan':r.get('vlan_id'),'last_discovery':r.get('last_seen') or '','if_descr':descr,'if_index':idx,'mac_if_index':idx,'arp_if_index':int(arp_if or 0),'inter_switch':bool(inter),'port_type':port_type,'role_evidence':evidence,'probable_uplink':probable,'confidence':max(0,min(100,score)),'fanout_macs':fan_m,'fanout_vlans':fan_v,'fanout_ips':fan_i,'_score':score}

            locations={}; located=0; sw_name=str(sw['name']); sw_ip=str(sw['management_ip'] or '')
            for target in targets:
                cm=[]; m0=normalize_mac(target.get('mac') or '')
                if m0: cm.append(m0.lower())
                for a in arp_by_ip.get(str(target['ip']),[]):
                    m=normalize_mac(a.get('mac') or '')
                    if m and m not in cm: cm.append(m)
                for m in dhcp_by_ip.get(str(target['ip']),set()):
                    if m not in cm: cm.append(m)
                candidates=[]; seen=set(); target_vlan=target.get('vlan')
                for m in cm:
                    for r in fdb_by_mac.get(m,[]):
                        item=fast_classify(r,0,target_vlan)
                        if not item: continue
                        key=(item['port'],int(item.get('vlan') or 0),int(item.get('if_index') or 0))
                        if key in seen: continue
                        seen.add(key); item['_mac_pref']=1 if (m0 and m==m0.lower()) else 0
                        candidates.append(item)
                # ARP interface is strong local evidence and should win when
                # it points to a real endpoint-facing physical port.
                for a in arp_by_ip.get(str(target['ip']),[]):
                    idx=int(a.get('if_index') or 0)
                    name=str(a.get('if_name') or '').strip()
                    if not idx or not _is_physical_interface_name(name): continue
                    pseudo={'if_index':idx,'if_name':name,'if_descr':iface_ctx.get(idx,{}).get('if_descr',''),'oper_status':iface_ctx.get(idx,{}).get('oper_status','learned'),'status':'learned','vlan_id':target_vlan}
                    item=fast_classify(pseudo,idx,target_vlan)
                    if item:
                        key=(item['port'],int(item.get('vlan') or 0),idx)
                        if key not in seen: seen.add(key); candidates.append(item)
                for item in candidates: item.pop('_mac_pref',None)
                if candidates:
                    candidates.sort(key=lambda x:(int(x.get('confidence') or 0), -int(x.get('fanout_macs') or 0), str(x.get('last_discovery') or '')), reverse=True)
                    locations[str(target['id'])]=[candidates[0]]; located+=1
            return {'locations':locations,'switch_ports':switch_ports,'matched_ips':total,'matched_ids':[int(r['id']) for r in targets],'rows':targets,'offset':offset,'limit':limit,'returned_ips':len(targets),'next_offset':(offset+len(targets) if offset+len(targets)<total else None),'located_ips':located}
            return {'locations':locations,'matched_ips':total,'matched_ids':[int(r['id']) for r in targets],'rows':targets,'offset':offset,'limit':limit,'returned_ips':len(targets),'next_offset':(offset+len(targets) if offset+len(targets)<total else None),'located_ips':located}

        sql="SELECT id,ip,mac,vlan,hostname,device,user_dept,notes FROM ips WHERE 1=1"; args=[]
        if q:
            nq=re.sub(r"[.\-:\s]", "", q.lower())
            sql += " AND (ip ILIKE ? OR mac ILIKE ? OR hostname ILIKE ? OR device ILIKE ? OR user_dept ILIKE ? OR notes ILIKE ? OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(mac,'')),':',''),'-',''),'.','') LIKE ?)"
            args += [f"%{q}%"]*6 + [f"%{nq}%"]
        if status: sql += " AND status=?"; args.append(status)
        if vlan: sql += " AND vlan=?"; args.append(vlan)
        if ip: sql += " AND ip ILIKE ?"; args.append(f"%{ip}%")
        if mac: sql += " AND mac ILIKE ?"; args.append(f"%{mac}%")
        if hostname: sql += " AND hostname ILIKE ?"; args.append(f"%{hostname}%")
        if device: sql += " AND device ILIKE ?"; args.append(f"%{device}%")
        if user: sql += " AND user_dept ILIKE ?"; args.append(f"%{user}%")
        if notes: sql += " AND notes ILIKE ?"; args.append(f"%{notes}%")
        # Switch filter: preselect IPs whose current/IPAM MAC, ARP MAC, DHCP
        # lease MAC, or reservation MAC is learned by the requested device.
        # The final resolver still applies the strict client-port gate, so an
        # uplink-only match is never shown as an endpoint.
        if switch_port:
            swsql="""
            AND id IN (
              SELECT i.id FROM ips i JOIN network_mac_entries m ON LOWER(m.mac)=LOWER(i.mac)
                JOIN network_devices d ON d.id=m.device_id
               WHERE LOWER(d.name)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
              UNION
              SELECT i.id FROM ips i JOIN network_arp_entries a ON a.ip=i.ip
                JOIN network_devices d ON d.id=a.device_id
               WHERE LOWER(d.name)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
              UNION
              SELECT i.id FROM ips i JOIN dhcp_leases h ON h.ip=i.ip
                JOIN network_mac_entries m ON LOWER(m.mac)=LOWER(h.mac)
                JOIN network_devices d ON d.id=m.device_id
               WHERE LOWER(d.name)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
              UNION
              SELECT i.id FROM ips i JOIN dhcp_reservations h ON h.ip=i.ip
                JOIN network_mac_entries m ON LOWER(m.mac)=LOWER(h.mac)
                JOIN network_devices d ON d.id=m.device_id
               WHERE LOWER(d.name)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
            )"""
            sql += swsql; args += [switch_port,switch_port,switch_port,switch_port]
        matched_ids=[]
        if switch_port:
            matched_ids=[int(r[0]) for r in c.execute("SELECT id FROM ("+sql+") ORDER BY id",args).fetchall()]
            total=len(matched_ids)
        else:
            total=int(c.execute("SELECT COUNT(*) FROM ("+sql+")",args).fetchone()[0])
        targets=[dict(r) for r in c.execute(sql+" ORDER BY id LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()]
        locs=_bulk_ip_network_locations(c,targets,switch_port=switch_port)
        return {'locations':locs,'matched_ips':total,'matched_ids':matched_ids,'offset':offset,'limit':limit,
                'returned_ips':len(targets),'next_offset':(offset+len(targets) if offset+len(targets)<total else None),
                'located_ips':len(locs)}
    finally:
        c.close()

@app.get("/api/ips")
def get_ips(q:str="", status:str="", vlan:str="", ip:str="", mac:str="", hostname:str="", device:str="", user:str="", notes:str="", switch_port:str="", enrich:int=1):
    c=db()
    sql="SELECT * FROM ips WHERE 1=1"; args=[]
    if q:
        # Global search: keep it single-query and index-friendly for common
        # exact/prefix IP lookups, while retaining contains search for text.
        nq=re.sub(r"[.\-:\s]", "", q.lower())
        sql += " AND (ip ILIKE ? OR mac ILIKE ? OR hostname ILIKE ? OR device ILIKE ? OR user_dept ILIKE ? OR notes ILIKE ? OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(mac,'')),':',''),'-',''),'.','') LIKE ?)"
        args += [f"%{q}%"]*6 + [f"%{nq}%"]
    if status: sql += " AND status=?"; args.append(status)
    if vlan: sql += " AND vlan=?"; args.append(vlan)
    if ip: sql += " AND ip ILIKE ?"; args.append(f"%{ip}%")
    if mac: sql += " AND mac ILIKE ?"; args.append(f"%{mac}%")
    if hostname: sql += " AND hostname ILIKE ?"; args.append(f"%{hostname}%")
    if device: sql += " AND device ILIKE ?"; args.append(f"%{device}%")
    if user: sql += " AND user_dept ILIKE ?"; args.append(f"%{user}%")
    if notes: sql += " AND notes ILIKE ?"; args.append(f"%{notes}%")
    if switch_port:
        sw=c.execute("SELECT id FROM network_devices WHERE LOWER(name)=LOWER(?) LIMIT 1",(str(switch_port),)).fetchone()
        if not sw:
            c.close()
            return []
        swid=int(sw[0])
        # Resolve candidate IPs from the selected switch first, then join to
        # IPAM. This avoids scanning the entire IP table through four correlated
        # subqueries for every filter request.
        sql = sql + " AND id IN (" + " UNION ".join([
            "SELECT i.id FROM ips i JOIN network_mac_entries m ON m.device_id=? AND LOWER(m.mac)=LOWER(i.mac)",
            "SELECT i.id FROM ips i JOIN network_arp_entries a ON a.device_id=? AND a.ip=i.ip",
            "SELECT i.id FROM ips i JOIN dhcp_leases h ON h.ip=i.ip JOIN network_mac_entries m ON m.device_id=? AND LOWER(m.mac)=LOWER(h.mac)",
            "SELECT i.id FROM ips i JOIN dhcp_reservations h ON h.ip=i.ip JOIN network_mac_entries m ON m.device_id=? AND LOWER(m.mac)=LOWER(h.mac)"
        ]) + ")"
        args += [swid]*4
    rows=[row_dict(r) for r in c.execute(sql,args).fetchall()]
    # Fast path for Dashboard status switching. Return the IPAM rows immediately
    # and let the client request Discovery enrichment separately in the background.
    # This keeps STATIC/DHCP/RESERVED/FREE navigation responsive even when the
    # network contains many switches/MAC/ARP records.
    if not int(enrich or 0):
        c.close()
        return rows
    # Read-only enrichment from Network Discovery. Resolve the MAC globally
    # across all live switches, then prefer a true endpoint-facing physical port.
    # This is essential when the selected Access switch learns Core-side VLAN
    # clients on its trunk/uplink (e.g. Te1/0/4).
    arps=[dict(r) for r in c.execute("""SELECT a.ip,a.mac,a.if_name,a.if_index,a.device_id,
        d.name switch_name,d.management_ip,d.last_discovery,d.status,d.discovery_interval_minutes
        FROM network_arp_entries a JOIN network_devices d ON d.id=a.device_id
        WHERE LOWER(COALESCE(d.status,''))='up' AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
          AND d.last_discovery >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
    """,(DISCOVERY_AUTO_DEFAULT_MINUTES,)).fetchall()]
    arp_by_ip={}
    for a in arps:
        arp_by_ip.setdefault(a['ip'],[]).append(a)

    for row in rows:
        candidates=[]
        # Strongest source: IPAM's recorded MAC.
        ipam_mac=normalize_mac(row.get('mac') or '')
        if ipam_mac:
            candidates.extend(_global_mac_locations(c,ipam_mac,vlan_hint=row.get('vlan')))
        # Also resolve current ARP observations for this IP.
        for a in arp_by_ip.get(row['ip'],[]):
            amac=normalize_mac(a.get('mac') or '')
            if not amac: continue
            candidates.extend(_global_mac_locations(c,amac,vlan_hint=row.get('vlan'),
                preferred_device_id=a.get('device_id'),preferred_arp_if_index=a.get('if_index') or 0))
        uniq={};
        for z in candidates:
            key=(int(z.get('device_id') or 0),str(z.get('mac') or '').lower(),int(z.get('if_index') or 0),int(z.get('vlan_id') or 0))
            if key not in uniq: uniq[key]=z
        locs=[]
        for z in uniq.values():
            locs.append({
                'switch_name':z.get('switch_name'),'switch_ip':z.get('switch_ip'),
                'port':z.get('if_name') or '—','port_status':z.get('status') or 'learned',
                'vlan':z.get('vlan_id'),'last_discovery':z.get('last_discovery'),
                'if_descr':z.get('if_descr') or '','if_index':z.get('if_index') or 0,
                'mac_if_index':z.get('if_index') or 0,'arp_if_index':0,
                'inter_switch':bool(z.get('inter_switch')),'port_type':z.get('port_type') or 'unknown',
                'role_evidence':z.get('role_evidence') or '',
                'historical':bool(z.get('historical')),
                'last_known':z.get('last_known') or '',
                'confidence':int(z.get('location_confidence') or 0),
                'fanout_macs':int(z.get('fanout_macs') or 0),'fanout_vlans':int(z.get('fanout_vlans') or 0)
            })
        # Do not use a trunk/uplink as the final answer when a direct physical
        # endpoint observation exists elsewhere in the network.
        row['network_locations']=_collapse_network_locations(locs)
    rows.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    c.close()
    return rows

@app.get("/api/ips/{id}/endpoint-history")
def get_endpoint_history(id:int, limit:int=50):
    """Detail history uses the same persistent observation as the History page."""
    c=db()
    try:
        row=c.execute("SELECT id,ip,mac FROM ips WHERE id=?",(id,)).fetchone()
        if not row: raise HTTPException(404,"Không tìm thấy IP")
        ip=row['ip'];mac=normalize_mac(row['mac'] or '')
    finally:
        c.close()
    items=get_endpoint_history_page(q=mac,limit=max(1,min(100,int(limit or 50))))['rows'] if mac else []
    return {'ip':ip,'mac':mac or '', 'retention_days':14,'items':items}


@app.get("/api/history/options")
def get_endpoint_history_options():
    """Return lightweight filter options for the 14-day Endpoint History view.

    Switches come from managed devices (not raw FDB observations), while VLANs
    come from retained history. This avoids running the expensive endpoint
    resolver just to populate dropdowns and keeps page-open latency predictable.
    """
    c=db()
    try:
        switches=[str(r[0]) for r in c.execute("""
            SELECT name FROM network_devices
            WHERE LOWER(COALESCE(status,''))='up' AND TRIM(COALESCE(name,''))<>''
            ORDER BY name
        """).fetchall() if r[0]]
        vlans=[str(r[0]) for r in c.execute("""
            SELECT DISTINCT vlan_id
            FROM network_mac_port_history
            WHERE last_seen >= TO_CHAR(clock_timestamp() - INTERVAL '14 days', 'YYYY-MM-DD HH24:MI:SS')
              AND vlan_id IS NOT NULL
            ORDER BY vlan_id
        """).fetchall() if r[0] is not None]
        # Classification is calculated from current IP Address/FDB evidence,
        # so these are the only two supported endpoint modes.
        return {"switches":switches,"vlans":vlans,"types":["access","uplink"]}
    finally:
        c.close()

@app.get("/api/history")
def get_endpoint_history_page(q: str = "", limit: int = 300, offset: int = 0, switch_name: str = "", vlan: str = "", type: str = "", port: str = ""):
    """Safe 14-day Endpoint History reader.

    This endpoint deliberately does NOT call the IP/Discovery resolver.  The
    resolver is useful for the live IP Address page, but it is the wrong
    dependency for historical data and was the source of PostgreSQL failures
    in the previous Endpoint History implementation.

    Source priority:
      1. network_mac_port_history (persistent endpoint-facing history)
      2. network_mac_entries (read-only current-FDB fallback when history has
         not been populated yet)

    The endpoint is read-only and bounded.  It never writes, never performs a
    table-wide resolver walk, and never holds a transaction open while doing
    per-MAC Python work.
    """
    limit=max(1,min(500,int(limit or 300)))
    offset=max(0,int(offset or 0))
    q=str(q or '').strip()
    switch_name=str(switch_name or '').strip()
    vlan=str(vlan or '').strip()
    type=str(type or '').strip().lower()
    port=str(port or '').strip()
    c=db()
    try:
        cutoff="TO_CHAR(clock_timestamp() - INTERVAL '14 days', 'YYYY-MM-DD HH24:MI:SS')"
        where=["TRIM(COALESCE(h.mac,''))<>''", "TRIM(COALESCE(h.if_name,''))<>''", f"h.last_seen >= {cutoff}"]
        args=[]

        if switch_name:
            where.append("LOWER(d.name)=LOWER(%s)")
            args.append(switch_name)
        if vlan:
            where.append("CAST(h.vlan_id AS TEXT)=%s")
            args.append(vlan)
        if type in ('access','uplink'):
            where.append("LOWER(COALESCE(h.port_type,''))=%s")
            args.append(type)
        if port:
            where.append("LOWER(COALESCE(h.if_name,'')) LIKE LOWER(%s)")
            args.append(f"%{port}%")
        if q:
            # Fast-path the two most common searches (IP/MAC).  Generic text
            # search remains available for hostname/switch/VLAN/port, but the
            # exact branches avoid a wide OR/ILIKE scan for normal lookups.
            mac_q=normalize_mac(q)
            ip_q=None
            try:
                ipaddress.ip_address(q)
                ip_q=q
            except Exception:
                pass
            if mac_q and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac_q.lower()):
                where.append("LOWER(h.mac)=LOWER(%s)")
                args.append(mac_q)
            elif ip_q:
                where.append("EXISTS (SELECT 1 FROM ips ix WHERE ix.ip=%s AND ix.mac IS NOT NULL AND LOWER(ix.mac)=LOWER(h.mac))")
                args.append(ip_q)
            else:
                like=f"%{q}%"
                where.append("""(
                    h.mac ILIKE %s
                    OR CAST(h.vlan_id AS TEXT) ILIKE %s
                    OR d.name ILIKE %s
                    OR h.if_name ILIKE %s
                    OR EXISTS (
                        SELECT 1 FROM ips ix
                        WHERE ix.mac IS NOT NULL
                          AND LOWER(ix.mac)=LOWER(h.mac)
                          AND (ix.ip ILIKE %s OR ix.hostname ILIKE %s)
                    )
                )""")
                args.extend([like,like,like,like,like,like])

        # Re-evaluate the role only when this exact historical observation is
        # still in the current FDB. Never replace its port or timestamps.
        role_cte="""WITH port_fanout AS (
            SELECT device_id,if_index,COUNT(DISTINCT mac) macs,COUNT(DISTINCT vlan_id) vlans
            FROM network_mac_entries GROUP BY device_id,if_index
        ), observations AS (
            SELECT old.id,old.device_id,old.mac,old.vlan_id,old.if_index,old.if_name,
                   old.first_seen,old.last_seen,old.seen_count,old.confidence,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM network_mac_entries live
                       WHERE live.device_id=old.device_id AND live.if_index=old.if_index
                         AND LOWER(live.mac)=LOWER(old.mac) AND live.vlan_id=old.vlan_id
                   ) THEN CASE
                       WHEN LOWER(COALESCE(old.if_name,'')) ~ '^(po|lag)'
                         OR LOWER(COALESCE(ni.if_descr,'')) ~ '(uplink|trunk|peer|vpc|port.channel|to sw|to switch)'
                         OR EXISTS (SELECT 1 FROM network_neighbors n WHERE n.device_id=old.device_id AND n.local_if_index=old.if_index)
                         OR COALESCE(f.macs,0)>=128 OR COALESCE(f.vlans,0)>=8
                         OR (COALESCE(f.macs,0)>=32 AND COALESCE(f.vlans,0)>=3) THEN 'uplink'
                       WHEN LOWER(COALESCE(old.if_name,'')) ~ '^(gi|te|fa|et|ge|xe|port[0-9])' THEN 'access'
                       ELSE old.port_type END
                   ELSE old.port_type END AS port_type
            FROM network_mac_port_history old
            LEFT JOIN network_interfaces ni ON ni.device_id=old.device_id AND ni.if_index=old.if_index
            LEFT JOIN port_fanout f ON f.device_id=old.device_id AND f.if_index=old.if_index
        ) """
        w=' AND '.join(where)
        # Pick the newest endpoint observation per MAC after applying the
        # requested filters.  This is index-friendly and avoids GROUP BY over
        # the complete 14-day table.
        sql=role_cte+f"""
            SELECT DISTINCT ON (LOWER(TRIM(h.mac)))
                   h.id,h.mac,h.vlan_id,h.if_index,h.if_name,h.port_type,
                   h.first_seen,h.last_seen,h.seen_count,h.confidence,
                   d.name AS switch_name,d.management_ip AS switch_ip
            FROM observations h
            JOIN network_devices d ON d.id=h.device_id
            WHERE {w}
            ORDER BY LOWER(TRIM(h.mac)),CASE WHEN LOWER(h.port_type)='access' THEN 0 WHEN LOWER(h.port_type)='uplink' THEN 1 ELSE 2 END,h.last_seen DESC,h.id DESC
            LIMIT %s OFFSET %s
        """
        total_row=c.execute(role_cte+f"""
            SELECT COUNT(DISTINCT LOWER(TRIM(h.mac))) AS total
            FROM observations h
            JOIN network_devices d ON d.id=h.device_id
            WHERE {w}
        """,args).fetchone()
        total=int((total_row or {}).get('total') or 0)

        rows=c.execute(sql,args+[limit+1,offset]).fetchall()
        has_more=total>offset+limit
        if has_more:
            rows=rows[:limit]

        # If persistent history is empty (for example immediately after a
        # PostgreSQL migration before the next Discovery cycle), do not show a
        # broken page.  Use the current FDB as a read-only bootstrap fallback.
        if not rows and offset==0:
            fb_where=["TRIM(COALESCE(m.mac,''))<>''",
                      "LOWER(COALESCE(d.status,''))='up'",
                      "d.last_discovery IS NOT NULL",
                      "TRIM(COALESCE(m.if_name,''))<>''",
                      "LOWER(COALESCE(m.if_name,'')) NOT LIKE 'po%'",
                      "LOWER(COALESCE(m.if_name,'')) NOT LIKE '%port-channel%'",
                      "LOWER(COALESCE(m.if_name,'')) NOT LIKE 'lag%'",
                      "LOWER(COALESCE(m.if_name,'')) NOT LIKE 'vlan%'",
                      "LOWER(COALESCE(m.if_name,'')) NOT LIKE 'loopback%'"]
            fb_args=[]
            if switch_name:
                fb_where.append("LOWER(d.name)=LOWER(%s)"); fb_args.append(switch_name)
            if vlan:
                fb_where.append("CAST(m.vlan_id AS TEXT)=%s"); fb_args.append(vlan)
            if type and type!='access':
                rows=[]
            if port:
                fb_where.append("LOWER(COALESCE(m.if_name,'')) LIKE LOWER(%s)"); fb_args.append(f"%{port}%")
            if q:
                mac_q=normalize_mac(q)
                ip_q=None
                try:
                    ipaddress.ip_address(q)
                    ip_q=q
                except Exception:
                    pass
                if mac_q and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac_q.lower()):
                    fb_where.append("LOWER(m.mac)=LOWER(%s)"); fb_args.append(mac_q)
                elif ip_q:
                    fb_where.append("EXISTS (SELECT 1 FROM ips ix WHERE ix.ip=%s AND ix.mac IS NOT NULL AND LOWER(ix.mac)=LOWER(m.mac))"); fb_args.append(ip_q)
                else:
                    like=f"%{q}%"
                    fb_where.append("""(
                        m.mac ILIKE %s OR CAST(m.vlan_id AS TEXT) ILIKE %s OR
                        d.name ILIKE %s OR m.if_name ILIKE %s OR EXISTS (
                            SELECT 1 FROM ips ix WHERE ix.mac IS NOT NULL
                            AND LOWER(ix.mac)=LOWER(m.mac)
                            AND (ix.ip ILIKE %s OR ix.hostname ILIKE %s)
                        )
                    )""")
                    fb_args.extend([like,like,like,like,like,like])
            if rows==[] and (not type or type=='access'):
                fb_total_row=c.execute(f"""
                    SELECT COUNT(DISTINCT LOWER(TRIM(m.mac))) AS total
                    FROM network_mac_entries m
                    JOIN network_devices d ON d.id=m.device_id
                    WHERE {' AND '.join(fb_where)}
                """,fb_args).fetchone()
                total=int((fb_total_row or {}).get('total') or 0)

                rows=c.execute(f"""
                    SELECT DISTINCT ON (LOWER(TRIM(m.mac)))
                           m.id,m.mac,m.vlan_id,m.if_index,m.if_name,
                           COALESCE(m.port_type,'access') AS port_type,
                           d.last_discovery AS first_seen,d.last_discovery AS last_seen,
                           1 AS seen_count,m.confidence,
                           d.name AS switch_name,d.management_ip AS switch_ip
                    FROM network_mac_entries m
                    JOIN network_devices d ON d.id=m.device_id
                    WHERE {' AND '.join(fb_where)}
                    ORDER BY LOWER(TRIM(m.mac)),d.last_discovery DESC,m.id DESC
                    LIMIT %s
                """,fb_args+[limit+1]).fetchall()
                has_more=total>offset+limit
                if has_more:
                    rows=rows[:limit]

        if not rows:
            return {"rows":[],"total":0,"limit":limit,"offset":offset,"retention_days":14}

        # Bulk-load the current IPAM identity for the visible MACs.  No per-row
        # resolver and no N+1 queries.
        mac_keys=[]
        for r in rows:
            m=normalize_mac(r.get('mac') or '') or str(r.get('mac') or '').strip().lower()
            if m: mac_keys.append(m.lower())
        mac_keys=sorted(set(mac_keys))
        ip_by={}
        if mac_keys:
            ph=','.join('%s' for _ in mac_keys)
            iprows=c.execute(f"""
                SELECT DISTINCT ON (LOWER(i.mac))
                       i.id,i.ip,i.mac,i.hostname,i.vlan,i.updated_at
                FROM ips i
                WHERE i.mac IS NOT NULL AND LOWER(i.mac) IN ({ph})
                ORDER BY LOWER(i.mac),i.updated_at DESC NULLS LAST,i.id DESC
            """,mac_keys).fetchall()
            ip_by={str(r['mac']).lower():dict(r) for r in iprows if r.get('mac')}

        # Statistics belong to the selected historical port, never other switches.
        out=[]
        for r in rows:
            key=normalize_mac(r.get('mac') or '') or str(r.get('mac') or '').strip().lower()
            d=dict(r)
            ipr=ip_by.get(key.lower(),{})
            ptype=str(r.get('port_type') or 'access').lower()
            if ptype not in ('access','uplink'): ptype='access'
            out.append({
                'mac':normalize_mac(r.get('mac') or '') or r.get('mac') or '',
                'first_seen':d.get('first_seen') or r.get('first_seen') or '',
                'last_seen':d.get('last_seen') or r.get('last_seen') or '',
                'vlan_id':d.get('vlan_id') or (str(r.get('vlan_id')) if r.get('vlan_id') is not None else ''),
                'vlan_count':int(d.get('vlan_count') or (1 if r.get('vlan_id') is not None else 0)),
                'seen_count':int(d.get('seen_count') or r.get('seen_count') or 1),
                'ip':ipr.get('ip') or '',
                'hostname':ipr.get('hostname') or '',
                'switch_name':r.get('switch_name') or '—',
                'switch_ip':r.get('switch_ip') or '',
                'if_name':r.get('if_name') or '—',
                'if_index':int(r.get('if_index') or 0),
                'port_type':ptype,
                'port_mac_count':1,
                'confidence':int(r.get('confidence') or 0),
            })

        return {"rows":out,"total":int(total),"has_more":bool(has_more),"limit":limit,"offset":offset,"retention_days":14}
    except Exception as exc:
        # Return a useful API error instead of leaving the UI in an endless
        # loading state.  Do not mutate or rollback unrelated application data.
        import logging as _logging
        _logging.getLogger("ipam.history").exception("Endpoint History query failed: %s", exc)
        raise HTTPException(500, f"Endpoint History query lỗi: {exc}")
    finally:
        c.close()


@app.get("/api/ips/{id}/detail")
def get_ip_detail(id: int):
    c=db()
    r=c.execute("SELECT * FROM ips WHERE id=?", (id,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404, "Không tìm thấy IP")
    d=row_dict(r)
    # Resolve the most specific configured subnet for this address.
    try:
        addr=ipaddress.ip_address(d["ip"])
    except Exception:
        addr=None
    best=None
    if addr:
        for sn in c.execute("SELECT vlan,name,cidr,gateway,dns,dhcp FROM subnets").fetchall():
            try:
                net=ipaddress.ip_network(sn["cidr"], strict=False)
                if addr.version == net.version and addr in net:
                    if best is None or net.prefixlen > best[0]:
                        best=(net.prefixlen, dict(sn))
            except Exception:
                continue
    d["subnet"] = best[1] if best else None
    # Show fresh DHCP source(s) currently associated with this IP.
    dhcp=[]
    for q in c.execute("SELECT source_id,mac,hostname,last_seen FROM dhcp_leases WHERE ip=? ORDER BY last_seen DESC", (d["ip"],)).fetchall():
        dhcp.append({"type":"lease", **dict(q)})
    for q in c.execute("SELECT source_id,mac,hostname,last_seen FROM dhcp_reservations WHERE ip=? ORDER BY last_seen DESC", (d["ip"],)).fetchall():
        dhcp.append({"type":"reservation", **dict(q)})
    d["dhcp_sources"] = dhcp
    # Persistent MAC/Port history for this IP. This is read-only and limited
    # to the most recent moves/observations so the detail view stays fast.
    try:
        hm=normalize_mac(d.get("mac") or "")
        if hm:
            hist=c.execute("""SELECT h.mac,h.vlan_id,h.if_index,h.if_name,h.first_seen,h.last_seen,h.seen_count,
                              h.port_type,dv.name switch_name,dv.management_ip switch_ip
                       FROM network_mac_port_history h
                       JOIN network_devices dv ON dv.id=h.device_id
                       WHERE h.mac COLLATE NOCASE=?
                       ORDER BY h.last_seen DESC,h.id DESC LIMIT 30""",(hm,)).fetchall()
            d["port_history"]=[dict(x) for x in hist]
        else:
            d["port_history"]=[]
    except Exception:
        d["port_history"]=[]
    # V75.2.18: resolve Switch/Port for this one IP immediately.  The
    # expensive all-IP enrichment remains a separate background operation.
    try:
        d["network_locations"] = _cached_ip_locations(id, d.get("ip",""), d.get("mac",""), d.get("vlan"))
    except Exception:
        # Detail data must remain usable even if Discovery correlation is
        # temporarily unavailable.
        d["network_locations"] = []
    c.close()
    return d

@app.get("/api/ips/filters")
def get_ip_filters():
    c=db()
    out={}
    for key, col in (("vlan","vlan"),("device","device"),("user","user_dept"),("status","status")):
        rows=c.execute(f"SELECT DISTINCT {col} v FROM ips WHERE TRIM(COALESCE({col},''))<>'' ORDER BY {col}").fetchall()
        out[key]=[r["v"] for r in rows]
    # Device selector: list every configured network device. Health/staleness
    # must not make a switch disappear from the Switch/Port filter.
    try:
        rows=c.execute("""SELECT id,name,management_ip,status,last_discovery
            FROM network_devices
            WHERE TRIM(COALESCE(name,''))<>''
            ORDER BY name COLLATE NOCASE""").fetchall()
        out["network_devices"]=[
            {"value":r["name"],"label":r["name"],"ip":r["management_ip"],
             "status":r["status"],"last_discovery":r["last_discovery"]}
            for r in rows
        ]
    except Exception:
        out["network_devices"]=[]
    c.close()
    return out

@app.get("/api/stats")
def stats():
    """Return IPAM pool statistics. FREE is derived from configured subnets.

    An address is allocated only when it is STATIC/RESERVED/DHCP.
    Explicit FREE rows are ignored for pool arithmetic, so FREE never has
    to be pre-created for every address in a subnet.
    """
    c=db()
    rows=c.execute("SELECT status,COUNT(*) n FROM ips GROUP BY status").fetchall()
    d={r["status"]:r["n"] for r in rows}
    registered_count=sum(d.values())

    subnets=c.execute("SELECT cidr,gateway FROM subnets").fetchall()
    networks=[]; pool_total=0
    for r in subnets:
        try:
            net=ipaddress.ip_network(r["cidr"], strict=False)
            total=(max(0, net.num_addresses-2) if net.version==4 and net.prefixlen < 31 else net.num_addresses)
            pool_total += total
            networks.append((net, r["gateway"] or ""))
        except Exception:
            continue

    def in_pool(ip):
        try:
            a=ipaddress.ip_address(ip)
            return any(a in net for net,_ in networks)
        except Exception:
            return False

    allocated={r["ip"] for r in c.execute(
        "SELECT ip FROM ips WHERE status IN ('used','reserved','dhcp')"
    ).fetchall() if in_pool(r["ip"])}
    # Gateways are part of the subnet definition, not free addresses.
    gateways=set()
    for net,g in networks:
        if g:
            gi=valid_ip(g)
            if gi and ipaddress.ip_address(gi) in net:
                gateways.add(gi)
    # Exclude gateway from FREE even when it was not registered as an IP row.
    free=max(0, pool_total - len(allocated) - len(gateways - allocated))

    c.close()
    return {
        "total": pool_total if pool_total else sum(d.values()),
        "pool_total": pool_total,
        "registered_count": registered_count,
        "used": d.get("used",0),
        "free": free if pool_total else d.get("free",0),
        "reserved": d.get("reserved",0),
        "dhcp": d.get("dhcp",0),
        "allocated": len(allocated),
        "managed_subnets": len(networks)
    }

@app.get("/api/dashboard")
def dashboard_data():
    """Dashboard data: pool capacity, subnet usage, DHCP health and IP conflicts.

    Conflict rule: an IP is a conflict only when the same IP is associated
    with more than one distinct MAC. One MAC having multiple IPs is normal.
    DHCP1/DHCP2 reporting the same IP+MAC is deduplicated. Only fresh DHCP
    snapshots are considered so stale data cannot create false conflicts.
    """
    c=db()

    # ---------- Subnet / Pool ----------
    subnet_rows=[dict(r) for r in c.execute("SELECT * FROM subnets ORDER BY id DESC").fetchall()]
    networks=[]
    pool_total=0
    gateways=set()
    for r in subnet_rows:
        try:
            net=ipaddress.ip_network(r["cidr"], strict=False)
            total=(max(0, net.num_addresses-2) if net.version==4 and net.prefixlen < 31 else net.num_addresses)
            pool_total += total
            networks.append((net, r.get("gateway") or ""))
            g=valid_ip(r.get("gateway") or "")
            if g and ipaddress.ip_address(g) in net:
                gateways.add(g)
        except Exception:
            r["total_ips"]=r["free_ips"]=r["allocated_ips"]=0
            r["usage_pct"]=0

    allocated_rows=c.execute(
        "SELECT ip FROM ips WHERE status IN ('used','reserved','dhcp')"
    ).fetchall()
    allocated_ips=set()
    for r in allocated_rows:
        try:
            a=ipaddress.ip_address(r["ip"])
            if any(a in n for n,_ in networks):
                allocated_ips.add(r["ip"])
        except Exception:
            pass

    for r in subnet_rows:
        try:
            net=ipaddress.ip_network(r["cidr"], strict=False)
            alloc=sum(1 for ip in allocated_ips if ipaddress.ip_address(ip) in net)
            g=valid_ip(r.get("gateway") or "")
            gw=1 if g and ipaddress.ip_address(g) in net else 0
            total=(max(0, net.num_addresses-2) if net.version==4 and net.prefixlen < 31 else net.num_addresses)
            r["total_ips"]=total
            r["allocated_ips"]=alloc
            r["free_ips"]=max(0,total-alloc-gw)
            r["usage_pct"]=round((alloc/total*100),1) if total else 0
        except Exception:
            r["total_ips"]=r["allocated_ips"]=r["free_ips"]=0
            r["usage_pct"]=0

    # ---------- DHCP health ----------
    sources=[dict(r) for r in c.execute(
        "SELECT * FROM dhcp_sources ORDER BY source_id"
    ).fetchall()]
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    now=datetime.now(timezone.utc)
    for r in sources:
        age=None
        if r.get("last_sync"):
            try:
                dt=datetime.strptime(r["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age=max(0, round((now-dt).total_seconds()/60,1))
                r["last_sync_local"]=dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                r["last_sync_local"]=""
        else:
            r["last_sync_local"]=""
        r["age_minutes"]=age
        r["online"]=age is not None and age <= DHCP_SYNC_STALE_MINUTES
        # Keep status='ok' semantics for existing UI, but expose online reliably.
        r["status"]= "ok" if r["online"] else (r.get("status") or "offline")
        r["reservation_count"]=c.execute(
            "SELECT COUNT(*) n FROM dhcp_reservations WHERE source_id=?",(r["source_id"],)
        ).fetchone()["n"]

    active_lease=c.execute("""
        SELECT COUNT(DISTINCT l.ip) n
        FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id
        WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchone()["n"]
    active_res=c.execute("""
        SELECT COUNT(DISTINCT r.ip) n
        FROM dhcp_reservations r JOIN dhcp_sources s ON s.source_id=r.source_id
        WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchone()["n"]

    # ---------- IP Conflict ----------
    # Map ONE IP -> DISTINCT MACs. Never map MAC -> IPs for conflict detection.
    ip_mac_map={}

    # Registered IPAM records.
    for r in c.execute("""
        SELECT ip,mac FROM ips
        WHERE TRIM(COALESCE(mac,''))<>''
          AND status IN ('used','reserved','dhcp')
    """).fetchall():
        m=normalize_mac(r["mac"])
        if m:
            ip_mac_map.setdefault(r["ip"],set()).add(m)

    # Only DHCP snapshots from currently fresh sources count.
    for r in c.execute("""
        SELECT l.ip,l.mac
        FROM dhcp_leases l
        JOIN dhcp_sources s ON s.source_id=l.source_id
        WHERE s.last_sync >= datetime('now', ?)
          AND TRIM(COALESCE(l.mac,''))<>''
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall():
        m=normalize_mac(r["mac"])
        if m:
            ip_mac_map.setdefault(r["ip"],set()).add(m)

    for r in c.execute("""
        SELECT r.ip,r.mac
        FROM dhcp_reservations r
        JOIN dhcp_sources s ON s.source_id=r.source_id
        WHERE s.last_sync >= datetime('now', ?)
          AND TRIM(COALESCE(r.mac,''))<>''
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall():
        m=normalize_mac(r["mac"])
        if m:
            ip_mac_map.setdefault(r["ip"],set()).add(m)

    conflicts=[]
    def ip_sort_key(x):
        try:
            return tuple(int(p) for p in x.split('.'))
        except Exception:
            return (999,999,999,999)

    for ip in sorted(ip_mac_map, key=ip_sort_key):
        macs=ip_mac_map[ip]
        if len(macs) > 1:
            conflicts.append({
                "ip": ip,
                "type": "ip_multiple_mac",
                "macs": sorted(macs),
                "message": f"IP {ip} đang xuất hiện trên {len(macs)} MAC khác nhau"
            })

    # ---------- Output ----------
    counts={r["status"]:r["n"] for r in c.execute(
        "SELECT status,COUNT(*) n FROM ips GROUP BY status"
    ).fetchall()}
    registered_count=sum(counts.values())
    top_subnets=sorted(
        subnet_rows,
        key=lambda r:(r.get("usage_pct",0),r.get("allocated_ips",0)),
        reverse=True
    )[:8]
    allocated_count=len(allocated_ips) if pool_total else sum(
        counts.get(k,0) for k in ("used","reserved","dhcp")
    )
    free_count=(
        max(0,pool_total-allocated_count-len(gateways-allocated_ips))
        if pool_total else counts.get("free",0)
    )
    c.close()

    return {
        "pool_total": pool_total,
        "registered_count": registered_count,
        "allocated": allocated_count,
        "free": free_count,
        "used": counts.get("used",0),
        "reserved": counts.get("reserved",0),
        "dhcp": counts.get("dhcp",0),
        "subnet_count": len(subnet_rows),
        "subnets": top_subnets,
        "dhcp_sources": sources,
        "live_dhcp_count": sum(1 for x in sources if x.get("online")),
        "active_leases": active_lease,
        "active_reservations": active_res,
        "conflicts": conflicts[:20],
    }

@app.post("/api/ips")
def add_ip(x:IPIn):
    ip=valid_ip(x.ip)
    if not ip: raise HTTPException(400,"IP không hợp lệ")
    mac=normalize_mac(x.mac)
    c=db()
    vlan=(x.vlan or "").strip() or _dhcp_vlan_for_ip(c, ip)
    user_dept=(x.user_dept or "").strip() or _dhcp_user_dept_for_vlan(vlan)
    try:
        c.execute("""INSERT INTO ips(ip,vlan,hostname,device,user_dept,status,notes,mac,vendor,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                  (ip,vlan,x.hostname,x.device,user_dept,x.status.lower(),x.notes,mac,x.vendor))
        c.commit()
    except DBIntegrityError: raise HTTPException(409,"IP đã tồn tại")
    c.close(); audit("ADD",ip,"Đăng ký IP")
    return {"ok":True}

@app.post("/api/ips/bulk-delete")
def bulk_delete(x:BulkDelete):
    if not x.ids: return {"ok":True,"deleted":0}
    c=db(); qmarks=",".join("?"*len(x.ids))
    rows=c.execute(f"SELECT ip FROM ips WHERE id IN ({qmarks})",x.ids).fetchall()
    cur=c.execute(f"DELETE FROM ips WHERE id IN ({qmarks})",x.ids); n=cur.rowcount; c.commit(); c.close()
    audit("BULK_DELETE",str(n),"Xóa nhiều IP")
    return {"ok":True,"deleted":n}


@app.get("/api/check-ip")
def check_ip(ip: str = ""):
    """Quick IP check: query IPAM DB and actively ping from the IPAM server/container."""
    target = valid_ip((ip or "").strip())
    if not target:
        raise HTTPException(400, "IP không hợp lệ")

    c = db()
    r = c.execute("SELECT * FROM ips WHERE ip=?", (target,)).fetchone()
    c.close()

    online = False
    latency_ms = None
    ping_error = ""
    try:
        # Linux container/server: one ICMP echo with a short timeout.
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", target],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        if proc.returncode == 0:
            online = True
            m = re.search(r"time[=<]([0-9]+(?:\\.[0-9]+)?)\\s*ms", proc.stdout or "")
            if m:
                latency_ms = float(m.group(1))
                if latency_ms.is_integer():
                    latency_ms = int(latency_ms)
        else:
            ping_error = (proc.stderr or proc.stdout or "Ping failed").strip()[-300:]
    except subprocess.TimeoutExpired:
        ping_error = "Ping timeout"
    except FileNotFoundError:
        ping_error = "Lệnh ping chưa được cài đặt trong container"
    except Exception as e:
        ping_error = str(e)[:300]

    return {
        "ip": target,
        "registered": bool(r),
        "record": row_dict(r) if r else None,
        "online": online,
        "latency_ms": latency_ms,
        "ping_error": ping_error,
    }



@app.get("/api/tools/ping-stream")
def tool_ping_stream(target: str = "", count: int = 4):
    target = valid_ip((target or "").strip())
    if not target:
        raise HTTPException(400, "IP không hợp lệ")
    count = max(1, min(int(count or 4), 10))

    def generate():
        proc = None
        received = 0
        avg = None
        loss = None
        try:
            # Line-oriented output gives the browser the same progressive
            # feedback users see in a terminal instead of waiting for all
            # packets to finish.
            cmd = ["ping", "-c", str(count), "-W", "1", "-i", "0.2", target]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for raw in iter(proc.stdout.readline, ""):
                line = (raw or "").rstrip("\r\n")
                if not line:
                    continue
                mreply = re.search(r"(\d+) bytes from ([^: ]+): icmp_seq=(\d+) ttl=(\d+) time[=<]([0-9.]+) ?ms", line)
                if mreply:
                    received += 1
                    yield json.dumps({"type":"reply","seq":int(mreply.group(3)),"time_ms":float(mreply.group(5)),"line":line}, ensure_ascii=False) + "\n"
                elif re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,\s*([0-9.]+)% packet loss", line):
                    mm=re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,\s*([0-9.]+)% packet loss", line)
                    received=int(mm.group(2)); loss=float(mm.group(3))
                    yield json.dumps({"type":"line","line":line}, ensure_ascii=False)+"\n"
                elif re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/", line):
                    mm=re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/", line)
                    avg=float(mm.group(2))
                    yield json.dumps({"type":"line","line":line}, ensure_ascii=False)+"\n"
                else:
                    yield json.dumps({"type":"line","line":line}, ensure_ascii=False)+"\n"
            proc.wait(timeout=2)
            yield json.dumps({"type":"done","ok":proc.returncode==0 or received>0,"target":target,"count":count,"received":received,"loss_percent":loss if loss is not None else (0.0 if proc.returncode==0 else 100.0),"avg_ms":avg}, ensure_ascii=False)+"\n"
        except Exception as e:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            yield json.dumps({"type":"done","ok":False,"target":target,"count":count,"received":received,"loss_percent":100.0,"avg_ms":avg,"error":str(e)}, ensure_ascii=False)+"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.get("/api/tools/ping")
def tool_ping(target: str = "", count: int = 4):
    target = valid_ip((target or "").strip())
    if not target:
        raise HTTPException(400, "IP không hợp lệ")
    count = max(1, min(int(count or 4), 10))
    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", "1", target],
            capture_output=True, text=True, timeout=count * 2.0 + 2,
        )
        out = (proc.stdout or "")
        lines = out.splitlines()
        transmitted = received = None
        loss = None
        avg = None
        replies = []
        for line in lines:
            # Linux iputils ping reply, e.g. 64 bytes from 10.0.0.20: icmp_seq=1 ttl=64 time=0.711 ms
            mreply = re.search(r"(\d+) bytes from ([^: ]+): icmp_seq=(\d+) ttl=(\d+) time[=<]([0-9.]+) ?ms", line)
            if mreply:
                replies.append({
                    "seq": int(mreply.group(3)),
                    "bytes": int(mreply.group(1)),
                    "address": mreply.group(2),
                    "ttl": int(mreply.group(4)),
                    "time_ms": float(mreply.group(5)),
                    "ok": True,
                })
            mtimeout = re.search(r"From ([^ ]+) .*icmp_seq[= ](\d+).*", line)
            if mtimeout and "Destination" not in line:
                seq = int(mtimeout.group(2))
                if not any(r.get("seq") == seq for r in replies):
                    replies.append({"seq": seq, "bytes": None, "address": mtimeout.group(1), "ttl": None, "time_ms": None, "ok": False})
            m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,\s*([0-9.]+)% packet loss", line)
            if m:
                transmitted, received, loss = int(m.group(1)), int(m.group(2)), float(m.group(3))
            m = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/", line)
            if m:
                avg = float(m.group(2))
        replies.sort(key=lambda r: r.get("seq", 0))
        received_n = received if received is not None else len([r for r in replies if r.get("ok")])
        return {
            "ok": proc.returncode == 0,
            "target": target,
            "count": transmitted or count,
            "received": received_n,
            "loss_percent": loss if loss is not None else (0.0 if proc.returncode == 0 else 100.0),
            "avg_ms": int(avg) if avg is not None and avg.is_integer() else avg,
            "replies": replies,
            "output": out[-4000:],
            "error": (proc.stderr or "").strip()[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "target": target, "count": count, "received": 0, "loss_percent": 100.0, "avg_ms": None, "output": "", "error": "Ping timeout"}
    except FileNotFoundError:
        raise HTTPException(500, "Lệnh ping chưa được cài đặt trong container")

@app.get("/api/tools/trace")
def tool_trace(target: str = "", max_hops: int = 15):
    target = valid_ip((target or "").strip())
    if not target:
        raise HTTPException(400, "IP không hợp lệ")
    max_hops = max(1, min(int(max_hops or 15), 30))
    try:
        proc = subprocess.run(
            ["traceroute", "-n", "-w", "1", "-q", "1", "-m", str(max_hops), target],
            capture_output=True, text=True, timeout=max_hops * 1.5 + 5,
        )
        return {
            "ok": proc.returncode == 0,
            "target": target,
            "output": (proc.stdout or proc.stderr or "").strip()[-12000:],
            "error": (proc.stderr or "").strip()[-500:] if proc.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "target": target, "output": "", "error": "Trace timeout"}
    except FileNotFoundError:
        raise HTTPException(500, "Lệnh traceroute chưa được cài đặt trong container")

@app.put("/api/ips/{id}")
def edit_ip(id:int,x:IPIn):
    ip=valid_ip(x.ip)
    if not ip: raise HTTPException(400,"IP không hợp lệ")
    c=db()
    r=c.execute("SELECT ip FROM ips WHERE id=?",(id,)).fetchone()
    if not r: raise HTTPException(404,"Không tìm thấy IP")
    vlan=(x.vlan or "").strip() or _dhcp_vlan_for_ip(c, ip)
    user_dept=(x.user_dept or "").strip() or _dhcp_user_dept_for_vlan(vlan)
    c.execute("""UPDATE ips SET ip=?,vlan=?,hostname=?,device=?,user_dept=?,status=?,notes=?,mac=?,vendor=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
              (ip,vlan,x.hostname,x.device,user_dept,x.status.lower(),x.notes,normalize_mac(x.mac),x.vendor,id))
    c.commit(); c.close(); audit("EDIT",ip,"Cập nhật IP"); return {"ok":True}

@app.delete("/api/ips/{id}")
def delete_ip(id:int):
    c=db(); r=c.execute("SELECT ip FROM ips WHERE id=?",(id,)).fetchone()
    if not r: raise HTTPException(404,"Không tìm thấy IP")
    c.execute("DELETE FROM ips WHERE id=?",(id,)); c.commit(); c.close(); audit("DELETE",r["ip"],"Xóa IP"); return {"ok":True}

class FreeQuery(BaseModel):
    cidr: str
    vlan: str = ""
    gateway: str = ""
    start: str = ""
    end: str = ""
    limit: int = 100

class FreeCreate(BaseModel):
    ips: list[str]
    vlan: str = ""
    notes: str = ""

@app.post("/api/free/candidates")
def free_candidates(x:FreeQuery):
    try:
        net=ipaddress.ip_network(x.cidr,strict=False)
    except Exception:
        raise HTTPException(400,"CIDR không hợp lệ")
    if net.version!=4 or net.prefixlen<16:
        raise HTTPException(400,"Chỉ hỗ trợ IPv4 và tối đa /16")
    gateway=valid_ip(x.gateway) if x.gateway else ""
    lo=valid_ip(x.start) if x.start else None
    hi=valid_ip(x.end) if x.end else None
    c=db()
    # Only allocated addresses block the pool. Existing FREE rows are not blockers.
    used={r["ip"] for r in c.execute("SELECT ip FROM ips WHERE status IN ('used','reserved','dhcp')").fetchall()}
    # Fresh DHCP data is also considered occupied, even if the IP row has not yet been reconciled.
    fresh_dhcp={r["ip"] for r in c.execute("""
        SELECT DISTINCT ip FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id
        WHERE s.last_sync >= datetime('now', ?)
        UNION
        SELECT DISTINCT ip FROM dhcp_reservations r JOIN dhcp_sources s ON s.source_id=r.source_id
        WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes", f"-{DHCP_SYNC_STALE_MINUTES} minutes")).fetchall()}
    used |= fresh_dhcp
    # If the CIDR matches a configured subnet and gateway was omitted, use the subnet gateway.
    if not gateway:
        row=c.execute("SELECT gateway FROM subnets WHERE cidr=?", (str(net),)).fetchone()
        if row and row["gateway"]:
            gateway=valid_ip(row["gateway"]) or ""
    c.close()
    out=[]
    for addr in net.hosts():
        ip=str(addr)
        if gateway and ip==gateway: continue
        if lo and int(addr)<int(ipaddress.ip_address(lo)): continue
        if hi and int(addr)>int(ipaddress.ip_address(hi)): continue
        if ip in used: continue
        out.append(ip)
        if len(out)>=max(1,min(x.limit,1000)): break
    return {"cidr":str(net),"available":len(out),"ips":out}

@app.post("/api/free/create")
def free_create(x:FreeCreate):
    if not x.ips: return {"ok":True,"added":0,"skipped":0}
    c=db(); added=skipped=0
    for raw in x.ips:
        ip=valid_ip(raw)
        if not ip: skipped+=1; continue
        try:
            c.execute("INSERT INTO ips(ip,vlan,status,notes) VALUES(?,?,?,?)",(ip,x.vlan,"free",x.notes)); added+=1
        except DBIntegrityError: skipped+=1
    c.commit(); c.close(); audit("FREE_CREATE",str(added),f"Sinh {added} IP FREE; VLAN={x.vlan}")
    return {"ok":True,"added":added,"skipped":skipped}

@app.post("/api/ips/bulk-metadata")
def bulk_metadata(x:BulkMetadata):
    if not x.ids:
        return {"ok": True, "updated": 0}
    if x.device is None and x.user_dept is None:
        raise HTTPException(400, "Chưa chọn Thiết bị hoặc User / Phòng ban")
    c=db(); qmarks=",".join("?"*len(x.ids))
    sets=[]; vals=[]
    if x.device is not None:
        sets.append("device=?"); vals.append(str(x.device)[:255])
    if x.user_dept is not None:
        sets.append("user_dept=?"); vals.append(str(x.user_dept)[:255])
    sets.append("updated_at=CURRENT_TIMESTAMP")
    cur=c.execute(f"UPDATE ips SET {', '.join(sets)} WHERE id IN ({qmarks})", vals + [int(i) for i in x.ids])
    n=cur.rowcount
    c.commit(); c.close()
    audit("BULK_METADATA",str(n),f"Cập nhật hàng loạt: device={x.device!r}; user_dept={x.user_dept!r}")
    return {"ok":True,"updated":n}


@app.post("/api/ips/bulk-status")
def bulk_status(x:BulkStatus):
    if x.status not in {"free","used","reserved","dhcp"}: raise HTTPException(400,"Status không hợp lệ")
    if not x.ids: return {"ok":True}
    c=db(); qmarks=",".join("?"*len(x.ids))
    c.execute(f"UPDATE ips SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ({qmarks})",[x.status,*x.ids])
    c.commit(); c.close(); audit("BULK_STATUS",str(len(x.ids)),f"Đổi status {x.status}"); return {"ok":True}

@app.post("/api/ips/bulk-register-scan")
def bulk_register_scan(x:ScanRegister):
    if x.status not in {"free","used","reserved","dhcp"}:
        raise HTTPException(400,"Status không hợp lệ")
    if not x.records:
        return {"ok":True,"added":0,"updated":0,"skipped":0}
    c=db(); added=updated=skipped=0
    for r in x.records:
        ip=valid_ip(str(r.get("ip", "")))
        if not ip:
            skipped+=1; continue
        mac=normalize_mac(r.get("mac", ""))
        hostname=str(r.get("hostname", "") or "")[:255]
        vendor=str(r.get("vendor", "") or "")[:255]
        exists=c.execute("SELECT id,status FROM ips WHERE ip=?",(ip,)).fetchone()
        if exists:
            # Never overwrite an existing registration with scan data.
            c.execute("UPDATE ips SET mac=CASE WHEN ?<>'' THEN ? ELSE mac END, vendor=CASE WHEN ?<>'' THEN ? ELSE vendor END, hostname=CASE WHEN ?<>'' THEN ? ELSE hostname END, updated_at=CURRENT_TIMESTAMP WHERE ip=?",
                      (mac,mac,vendor,vendor,hostname,hostname,ip))
            updated+=1
        else:
            vlan=_dhcp_vlan_for_ip(c, ip)
        user_dept=_dhcp_user_dept_for_vlan(vlan)
        c.execute("INSERT INTO ips(ip,vlan,hostname,user_dept,status,mac,vendor) VALUES(?,?,?,?,?,?,?)",
                  (ip,vlan,hostname,user_dept,x.status,mac,vendor)); added+=1
    c.commit(); c.close()
    audit("SCAN_REGISTER",str(added+updated),f"Thêm {added}, cập nhật {updated}, status={x.status}")
    return {"ok":True,"added":added,"updated":updated,"skipped":skipped}

def _parse_dhcp_range(value: str):
    """Parse a DHCP pool written as start-end, CIDR, or comma-separated ranges."""
    out=[]
    for token in re.split(r"[,;\s]+", (value or "").strip()):
        if not token: continue
        if "/" in token:
            try: out.append(ipaddress.ip_network(token, strict=False)); continue
            except Exception: continue
        if "-" in token:
            a,b=token.split("-",1)
            try: out.append((ipaddress.ip_address(a.strip()), ipaddress.ip_address(b.strip()))); continue
            except Exception: continue
        try:
            ip=ipaddress.ip_address(token); out.append((ip,ip))
        except Exception: pass
    return out

def _subnet_validation(c, row, exclude_id=None):
    warnings=[]
    errors=[]
    try: net=ipaddress.ip_network(row["cidr"], strict=False)
    except Exception:
        return ["CIDR không hợp lệ"], []
    if net.version != 4: errors.append("Chỉ hỗ trợ IPv4")
    if row.get("gateway"):
        try:
            gw=ipaddress.ip_address(row["gateway"])
            if gw not in net: errors.append("Gateway không thuộc subnet")
            elif gw == net.network_address: warnings.append("Gateway đang trùng Network Address")
            elif net.version==4 and net.prefixlen < 31 and gw == net.broadcast_address: warnings.append("Gateway đang trùng Broadcast Address")
        except Exception: errors.append("Gateway không hợp lệ")
    if row.get("dns"):
        for token in re.split(r"[,;\s]+", row["dns"].strip()):
            if token:
                try: ipaddress.ip_address(token)
                except Exception: warnings.append(f"DNS không hợp lệ: {token}")
    for r in c.execute("SELECT id,vlan,name,cidr FROM subnets").fetchall():
        if exclude_id and r["id"] == exclude_id: continue
        try:
            other=ipaddress.ip_network(r["cidr"], strict=False)
            if net.version == other.version and net.overlaps(other):
                warnings.append(f"Subnet overlap với VLAN {r['vlan'] or '—'} ({r['cidr']})")
        except Exception: pass
    for item in _parse_dhcp_range(row.get("dhcp", "")):
        try:
            if isinstance(item, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                if item.version != net.version or not item.subnet_of(net): warnings.append(f"DHCP Pool {item} nằm ngoài subnet")
            else:
                a,b=item
                if a.version != net.version or b.version != net.version or a not in net or b not in net or int(a)>int(b): warnings.append(f"DHCP Pool {a}-{b} nằm ngoài subnet")
        except Exception: pass
    return errors, warnings

@app.get("/api/subnets")
def get_subnets():
    c=db()
    rows=[dict(r) for r in c.execute("SELECT * FROM subnets ORDER BY id DESC").fetchall()]
    allocated_rows=[x[0] for x in c.execute("SELECT ip FROM ips WHERE status IN ('used','reserved','dhcp')").fetchall()]
    parsed_alloc=[]
    for raw in allocated_rows:
        try: parsed_alloc.append(ipaddress.ip_address(raw))
        except Exception: pass
    for r in rows:
        try:
            net=ipaddress.ip_network(r["cidr"], strict=False)
            total=max(0, net.num_addresses-2) if net.version==4 and net.prefixlen < 31 else net.num_addresses
            allocated=sum(1 for addr in parsed_alloc if addr.version==net.version and addr in net)
            gateway_count=0
            if r["gateway"]:
                try: gateway_count=1 if ipaddress.ip_address(r["gateway"]) in net else 0
                except Exception: pass
            free=max(0,total-allocated-gateway_count)
            errors,warnings=_subnet_validation(c,r,r["id"])
            active_dhcp=0
            for rr in c.execute("SELECT DISTINCT ip FROM dhcp_leases").fetchall():
                try:
                    if ipaddress.ip_address(rr[0]) in net: active_dhcp+=1
                except Exception: pass
            reservations=0
            for rr in c.execute("SELECT DISTINCT ip FROM dhcp_reservations").fetchall():
                try:
                    if ipaddress.ip_address(rr[0]) in net: reservations+=1
                except Exception: pass
            r.update(total_ips=total,registered_ips=allocated,free_ips=free,usage_pct=round((allocated/total*100),1) if total else 0,dhcp_leases=active_dhcp,dhcp_reservations=reservations,validation_errors=errors,validation_warnings=warnings)
        except Exception:
            r.update(total_ips=0,registered_ips=0,free_ips=0,usage_pct=0,dhcp_leases=0,dhcp_reservations=0,validation_errors=["CIDR không hợp lệ"],validation_warnings=[])
    c.close(); return rows

@app.get("/api/subnets/{id}/detail")
def subnet_detail(id:int):
    c=db(); row=c.execute("SELECT * FROM subnets WHERE id=?",(id,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"Không tìm thấy subnet")
    r=dict(row)
    try: net=ipaddress.ip_network(r["cidr"],strict=False)
    except Exception:
        c.close(); raise HTTPException(400,"CIDR không hợp lệ")
    total=max(0,net.num_addresses-2) if net.version==4 and net.prefixlen<31 else net.num_addresses
    counts={k:0 for k in ("used","reserved","dhcp","free")}; ip_rows=[]
    for rr in c.execute("SELECT ip,status FROM ips").fetchall():
        try:
            if ipaddress.ip_address(rr["ip"]) in net:
                st=rr["status"] if rr["status"] in counts else "free"; counts[st]+=1; ip_rows.append(rr["ip"])
        except Exception: pass
    gw=0
    try: gw=1 if r["gateway"] and ipaddress.ip_address(r["gateway"]) in net else 0
    except Exception: pass
    errors,warnings=_subnet_validation(c,r,id)
    lease_ips=[]; res_ips=[]
    for rr in c.execute("SELECT DISTINCT ip FROM dhcp_leases").fetchall():
        try:
            if ipaddress.ip_address(rr[0]) in net: lease_ips.append(rr[0])
        except Exception: pass
    for rr in c.execute("SELECT DISTINCT ip FROM dhcp_reservations").fetchall():
        try:
            if ipaddress.ip_address(rr[0]) in net: res_ips.append(rr[0])
        except Exception: pass
    sources=[dict(x) for x in c.execute("SELECT source_id,name,server_ip,status,last_sync,lease_count FROM dhcp_sources ORDER BY source_id").fetchall()]
    allocated=counts["used"]+counts["reserved"]+counts["dhcp"]
    r.update(total_ips=total,allocated_ips=allocated,free_ips=max(0,total-allocated-gw),usage_pct=round(allocated/total*100,1) if total else 0,static_ips=counts["used"],reserved_ips=counts["reserved"],dhcp_ips=counts["dhcp"],dhcp_leases=len(lease_ips),dhcp_reservations=len(res_ips),dhcp_sources=sources,validation_errors=errors,validation_warnings=warnings)
    c.close(); return r

@app.post("/api/subnets")
def create_subnet(x:SubnetIn):
    try:
        net=ipaddress.ip_network(x.cidr, strict=False)
    except Exception:
        raise HTTPException(400,"CIDR không hợp lệ")
    if net.version != 4:
        raise HTTPException(400,"Chỉ hỗ trợ IPv4")
    gateway=valid_ip(x.gateway) if x.gateway else ""
    if gateway and ipaddress.ip_address(gateway) not in net:
        raise HTTPException(400,"Gateway không thuộc subnet")
    c=db()
    try:
        c.execute("INSERT INTO subnets(vlan,name,cidr,gateway,dns,dhcp,notes,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                  (x.vlan.strip(),x.name.strip(),str(net),gateway,x.dns.strip(),x.dhcp.strip(),x.notes.strip()))
        c.commit()
    except DBIntegrityError:
        c.close(); raise HTTPException(409,"Subnet/CIDR đã tồn tại")
    c.close(); audit("SUBNET_ADD",str(net),f"VLAN={x.vlan}; {x.name}"); return {"ok":True}

@app.put("/api/subnets/{id}")
def edit_subnet(id:int,x:SubnetIn):
    try:
        net=ipaddress.ip_network(x.cidr, strict=False)
    except Exception:
        raise HTTPException(400,"CIDR không hợp lệ")
    gateway=valid_ip(x.gateway) if x.gateway else ""
    if gateway and ipaddress.ip_address(gateway) not in net:
        raise HTTPException(400,"Gateway không thuộc subnet")
    c=db(); r=c.execute("SELECT id FROM subnets WHERE id=?",(id,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404,"Không tìm thấy subnet")
    try:
        c.execute("UPDATE subnets SET vlan=?,name=?,cidr=?,gateway=?,dns=?,dhcp=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (x.vlan.strip(),x.name.strip(),str(net),gateway,x.dns.strip(),x.dhcp.strip(),x.notes.strip(),id))
        c.commit()
    except DBIntegrityError:
        c.close(); raise HTTPException(409,"Subnet/CIDR đã tồn tại")
    c.close(); audit("SUBNET_EDIT",str(net),f"VLAN={x.vlan}; {x.name}"); return {"ok":True}

@app.delete("/api/subnets/{id}")
def delete_subnet(id:int):
    c=db(); r=c.execute("SELECT cidr,vlan FROM subnets WHERE id=?",(id,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404,"Không tìm thấy subnet")
    c.execute("DELETE FROM subnets WHERE id=?",(id,)); c.commit(); c.close(); audit("SUBNET_DELETE",r["cidr"],f"VLAN={r['vlan']}"); return {"ok":True}


# ---------------- Network Discovery / Multi-SNMP ----------------
def _snmp_timeout_args(profile):
    return ["-t", str(max(1, min(int(profile["timeout"] or 3), 30))), "-r", str(max(0, min(int(profile["retries"] or 1), 5)))]

def _snmp_base_args(profile, community_override=None):
    ver=str(profile["version"] or "2c").lower()
    if ver in ("1","v1"):
        return ["-v1", "-c", community_override if community_override is not None else profile["community"]]
    if ver in ("3","v3"):
        level=str(profile["security_level"] or "authPriv")
        level = level.strip() or "authPriv"
        auth = str(profile["auth_protocol"] or "SHA").strip().upper()
        priv = str(profile["priv_protocol"] or "AES").strip().upper()
        auth = {"SHA1":"SHA", "SHA-1":"SHA", "SHA256":"SHA-256", "SHA-256":"SHA-256", "SHA512":"SHA-512", "SHA-512":"SHA-512", "MD5":"MD5"}.get(auth, auth)
        priv = {"AES128":"AES", "AES-128":"AES", "DES":"DES", "AES":"AES"}.get(priv, priv)
        args=["-v3", "-l", level, "-u", str(profile["username"] or "").strip()]
        if level in ("authNoPriv","authPriv"):
            args += ["-a", auth, "-A", str(profile["auth_password"] or "")]
        if level == "authPriv":
            args += ["-x", priv, "-X", str(profile["priv_password"] or "")]
        return args
    return ["-v2c", "-c", community_override if community_override is not None else profile["community"]]

def _snmp_agent_target(target, port):
    # Recent net-snmp no longer accepts the legacy -p port option.
    # Put the destination port directly on the AGENT as HOST:PORT.
    target=str(target or "").strip()
    port=int(port or 161)
    if ":" in target and not target.startswith("["):
        # IPv6 literal: net-snmp expects [IPv6]:PORT.
        return f"[{target}]:{port}"
    return f"{target}:{port}"

def _run_snmp(profile, target, oid, walk=False, community_override=None):
    agent=_snmp_agent_target(target, profile["port"] or 161)
    cmd=["snmpwalk" if walk else "snmpget"] + _snmp_base_args(profile, community_override) + _snmp_timeout_args(profile) + ["-On", agent, oid]
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=max(5,int(profile["timeout"] or 3))*(int(profile["retries"] or 1)+1)+3)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127,"","snmp command not installed"
    except subprocess.TimeoutExpired:
        return 124,"","SNMP timeout"
    except Exception as e:
        return 1,"",str(e)

def _snmp_value(text):
    text=(text or "").strip()
    if not text:return ""
    if " = " in text: text=text.split(" = ",1)[1]
    if text.startswith("STRING:"):
        v=text[7:].strip()
        if len(v)>=2 and v[0]=='"' and v[-1]=='"': v=v[1:-1]
        return v
    if text.startswith("Hex-STRING:"): return text[11:].strip()
    if text.startswith("Timeticks:"): return text.split(":",1)[1].strip()
    if text.startswith("INTEGER:"): return text[8:].strip()
    if text.startswith("Counter32:"): return text[10:].strip()
    if text.startswith("Gauge32:"): return text[8:].strip()
    if text.startswith("OID:"): return text[4:].strip()
    if text.startswith("IpAddress:"): return text[10:].strip()
    return text

def _parse_walk_lines(text):
    rows=[]
    for line in (text or "").splitlines():
        if " = " not in line: continue
        left,right=line.split(" = ",1)
        m=re.search(r'\.([0-9]+)\s*$',left.strip())
        if not m: continue
        rows.append((int(m.group(1)), _snmp_value(right), right.strip()))
    return rows

def _parse_walk_oid_lines(text):
    rows=[]
    for line in (text or "").splitlines():
        if " = " not in line: continue
        left,right=line.split(" = ",1)
        nums=re.findall(r'\d+',left.strip())
        if not nums: continue
        rows.append((tuple(int(x) for x in nums), _snmp_value(right), right.strip()))
    return rows

def _normalize_mac(value):
    v=(value or "").strip().replace('"','')
    hexchars=re.findall(r'[0-9A-Fa-f]{2}', v)
    if len(hexchars)>=6 and ('-' not in v or ' ' in v or ':' not in v):
        return ':'.join(x.lower() for x in hexchars[-6:])
    v=re.sub(r'[^0-9A-Fa-f]','',v)
    if len(v)==12:
        return ':'.join(v[i:i+2].lower() for i in range(0,12,2))
    return ""

def _mac_from_suffix(suffix):
    if len(suffix)>=6:
        vals=suffix[-6:]
        if all(0<=x<=255 for x in vals):
            return ':'.join(f'{x:02x}' for x in vals)
    return ""

def _parse_int_value(value):
    m=re.search(r'-?\d+',str(value or ''))
    return int(m.group(0)) if m else 0

def _walk(profile,target,oid,community_override=None):
    rc,out,err=_run_snmp(profile,target,oid,True,community_override=community_override)
    return rc,out,err

def _hex_bytes(value):
    return [int(x,16) for x in re.findall(r'(?i)(?:^|\s)([0-9a-f]{2})(?=\s|$)', str(value or ''))]

def _parse_ipv4_from_snmp(value):
    v=_snmp_value(value)
    # Direct IpAddress rendering.
    try:
        ipaddress.ip_address(v)
        return v if v.count('.')==3 else ''
    except Exception:
        pass
    b=_hex_bytes(v)
    if len(b)>=4:
        cand=b[-4:]
        if all(0<=x<=255 for x in cand):
            return '.'.join(map(str,cand))
    nums=[int(x) for x in re.findall(r'\d+',v)]
    if len(nums)>=4 and all(0<=x<=255 for x in nums[-4:]):
        return '.'.join(map(str,nums[-4:]))
    return ''

def _interface_speed(c,device_id,if_name):
    if not if_name: return 0
    r=c.execute("SELECT speed_bps FROM network_interfaces WHERE device_id=? AND if_name=? ORDER BY if_index LIMIT 1",(device_id,if_name)).fetchone()
    return int(r[0] or 0) if r else 0

def _port_role(c,device_id,if_index,if_name):
    if not if_name:
        return 'unknown',0
    if _is_port_channel_name(if_name):
        return 'lag',90
    if _location_inter_switch(c,device_id,if_index):
        return 'uplink',95
    r=c.execute("SELECT if_descr,oper_status FROM network_interfaces WHERE device_id=? AND if_index=?",(device_id,int(if_index or 0))).fetchone()
    desc=str(r['if_descr'] if r else '').lower()
    op=str(r['oper_status'] if r else '').lower()
    if any(k in desc for k in ('uplink','trunk','peer','vpc','port-channel','port channel','to sw','to switch')):
        return 'uplink',90
    if op in ('up','up(1)','1'):
        return 'access',75
    return 'unknown',45

def _neighbor_column(text, root):
    """Join SNMP columns on their table index, never on the complete OID."""
    prefix=tuple(int(x) for x in root.strip('.').split('.'))
    return {oid[len(prefix):]:value for oid,value,_ in _parse_walk_oid_lines(text)
            if oid[:len(prefix)]==prefix and len(oid)>len(prefix)}

def _discover_neighbors(c,d,target):
    """Discover switch-to-switch links using CDP + LLDP with stable identity evidence.

    LLDP is preferred when both protocols describe the same neighbor. CDP/LLDP
    observations are intentionally retained separately in the DB so legacy
    behavior remains intact; topology correlation later merges them.
    """
    pending=[]
    ifmap={int(r["if_index"]):r["if_name"] for r in c.execute("SELECT if_index,if_name FROM network_interfaces WHERE device_id=?",(d["id"],))}
    count=0

    # CDP: device-id, port-id, address.
    base='.1.3.6.1.4.1.9.9.23.1.2.1.1.'
    walks={}
    for key,suffix in [('id','6'),('port','7'),('addr','4'),('addr_type','3'),('platform','8'),('capabilities','9')]:
        rc,out,_=_walk(d,target,base+suffix)
        walks[key]=_neighbor_column(out,base+suffix) if rc==0 else {}
    for suf,remote in walks['id'].items():
        if not suf: continue
        local_idx=int(suf[0]); local_name=ifmap.get(local_idx,'')
        if not local_name: continue
        rport=walks['port'].get(suf,'')
        rip=''
        if walks['addr'].get(suf):
            rip=_parse_ipv4_from_snmp(walks['addr'][suf])
        speed=_interface_speed(c,d['id'],local_name)
        role,role_conf=_port_role(c,d['id'],local_idx,local_name)
        conf=min(100,70+(10 if rip else 0)+(10 if rport else 0)+(5 if role=='uplink' else 0))
        pending.append(("INSERT OR IGNORE INTO network_neighbors(device_id,local_if_index,local_if_name,protocol,remote_device,remote_port,remote_chassis_id,remote_port_id,remote_ip,confidence,link_speed_bps,remote_platform,remote_capabilities,remote_port_subtype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(d['id'],local_idx,local_name,'CDP',str(remote)[:255],str(rport)[:255],str(remote)[:255],str(rport)[:255],rip,conf,speed,str(walks['platform'].get(suf,'') or '')[:255],str(walks['capabilities'].get(suf,'') or '')[:255],'IF_NAME')))
        count+=1

    # LLDP standard MIB. local port number -> ifIndex, then remote system/chassis/port.
    lrc,lout,_=_walk(d,target,'.1.0.8802.1.1.2.1.3.7.1.4')
    local_if_by_port={}
    from .port_mapping import normalize_interface
    by_name={normalize_interface(name):idx for idx,name in ifmap.items()}
    prc,pout,_=_walk(d,target,'.1.0.8802.1.1.2.1.3.7.1.3')
    if prc==0:
        for suffix,value in _neighbor_column(pout,'.1.0.8802.1.1.2.1.3.7.1.3').items():
            idx=by_name.get(normalize_interface(value))
            if idx and suffix: local_if_by_port[suffix[0]]=idx
    if lrc==0:
        for suf,val,_ in _parse_walk_oid_lines(lout):
            if suf:
                idx=by_name.get(normalize_interface(val))
                if idx: local_if_by_port[int(suf[-1])]=idx
    rchrc,rchout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.1.1.5')
    rprc,rprout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.1.1.7')
    rptrc,rptrout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.1.1.6')
    rsrc,rsout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.1.1.9')
    rdsrc,rsdout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.1.1.10')
    # LLDP management address table. Sophos Firewall exposes the standard
    # interface/IP MIBs but may not expose LLDP itself; when a Cisco switch
    # reports a Sophos neighbor, this remote management IP lets topology bind
    # the LLDP observation to the managed Sophos device even if its system name
    # differs from the device record.
    rmarc,rmarout,_=_walk(d,target,'.1.0.8802.1.1.2.1.4.2.1.3')
    rem_ch=_neighbor_column(rchout,'.1.0.8802.1.1.2.1.4.1.1.5') if rchrc==0 else {}
    rem_port=_neighbor_column(rprout,'.1.0.8802.1.1.2.1.4.1.1.7') if rprc==0 else {}
    rem_port_subtype=_neighbor_column(rptrout,'.1.0.8802.1.1.2.1.4.1.1.6') if rptrc==0 else {}
    rem_sys=_neighbor_column(rsout,'.1.0.8802.1.1.2.1.4.1.1.9') if rsrc==0 else {}
    rem_desc=_neighbor_column(rsdout,'.1.0.8802.1.1.2.1.4.1.1.10') if rdsrc==0 else {}
    rem_mgmt={}
    if rmarc==0 and rmarout:
        for suf,val in _neighbor_column(rmarout,'.1.0.8802.1.1.2.1.4.2.1.3').items():
            # Index: timeMark, localPort, remIndex, addrSubtype, addr(4 bytes).
            if len(suf)<9: continue
            try:
                subtype=int(suf[3])
                if subtype==1 and suf[4]==4:
                    ip_parts=suf[5:9]
                    if all(0<=x<=255 for x in ip_parts):
                        rem_mgmt[tuple(suf[:3])]='.'.join(str(x) for x in ip_parts)
            except Exception:
                pass
    for suf,remote in rem_sys.items():
        if len(suf)<2: continue
        local_num=int(suf[-2]); local_idx=local_if_by_port.get(local_num,0); local_name=ifmap.get(local_idx,'')
        if not local_idx or not local_name: continue
        rport=rem_port.get(suf,''); rch_raw=rem_ch.get(suf,''); rch=_normalize_mac(rch_raw) or str(rch_raw)
        rip=rem_mgmt.get(tuple(suf[:3]),'')
        speed=_interface_speed(c,d['id'],local_name)
        role,role_conf=_port_role(c,d['id'],local_idx,local_name)
        conf=min(100,80+(10 if rch else 0)+(5 if rport else 0)+(5 if rip else 0)+(5 if role=='uplink' else 0))
        pending.append(("INSERT OR IGNORE INTO network_neighbors(device_id,local_if_index,local_if_name,protocol,remote_device,remote_port,remote_chassis_id,remote_port_id,remote_ip,confidence,link_speed_bps,remote_platform,remote_capabilities,remote_port_subtype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(d['id'],local_idx,local_name,'LLDP',str(remote)[:255],str(rport)[:255],str(rch)[:255],str(rport)[:255],rip,conf,speed,str(rem_desc.get(suf,'') or '')[:255],'',str(rem_port_subtype.get(suf,'') or '')[:32])))
        count+=1
    if pending:
        c.execute("DELETE FROM network_neighbors WHERE device_id=?",(d["id"],))
        for statement,params in pending:c.execute(statement,params)
        c.commit()
    return count

def _neighbor_proves_transit(row):
    """An anonymous LLDP MAC-only endpoint does not prove a switch uplink.

    Windows NICs advertise LLDP Port ID subtype MAC address. Preserve named,
    managed/addressed neighbors and independent fanout/trunk/LAG safeguards.
    """
    name=str(row.get('remote_device') or '').strip('" ')
    port=str(row.get('remote_port') or '').strip('" ')
    subtype=str(row.get('remote_port_subtype') or '').lower()
    mac_port=subtype in ('3','macaddress','macaddress(3)') or bool(re.fullmatch(r'(?:[0-9a-fA-F]{2}[ :.-]?){6}',port))
    return not (str(row.get('protocol') or '').upper()=='LLDP' and mac_port and not name and not row.get('remote_ip'))

def _inter_switch_ifindexes(c,device_id):
    return {int(r["local_if_index"]) for r in c.execute("SELECT * FROM network_neighbors WHERE device_id=?",(device_id,)).fetchall() if _neighbor_proves_transit(dict(r))}

def _location_inter_switch(c,device_id,if_index):
    try:
        return int(if_index or 0) in _inter_switch_ifindexes(c,device_id)
    except Exception:
        return False

def _port_fanout(c, device_id, if_index):
    """Return distinct learned endpoint MAC/VLAN fanout on one interface.

    A switch uplink/trunk often learns dozens/hundreds of MACs.  Relying only
    on LLDP/CDP is unsafe because a device may not expose either protocol over
    SNMP.  Fanout is therefore secondary evidence used to reject obvious
    transit ports, not as a standalone proof of an uplink.
    """
    try:
        r=c.execute(
            "SELECT COUNT(DISTINCT mac) macs, COUNT(DISTINCT vlan_id) vlans "
            "FROM network_mac_entries WHERE device_id=? AND if_index=?",
            (int(device_id), int(if_index or 0))
        ).fetchone()
        return int(r['macs'] or 0), int(r['vlans'] or 0)
    except Exception:
        return 0,0


def _port_uplink_evidence(c, device_id, if_index):
    """Return MAC/IP/VLAN fanout evidence for Switch/Port labeling.

    Requested heuristic: >=2 distinct MACs or IPs on one physical port is an
    uplink candidate. This is classification only; raw FDB/ARP rows remain
    untouched. LLDP/CDP and explicit trunk/LAG descriptions remain stronger
    evidence.
    """
    try:
        r=c.execute(
            "SELECT COUNT(DISTINCT m.mac) macs, COUNT(DISTINCT a.ip) ips, COUNT(DISTINCT m.vlan_id) vlans "
            "FROM network_mac_entries m LEFT JOIN network_arp_entries a "
            "ON a.device_id=m.device_id AND a.if_index=m.if_index "
            "WHERE m.device_id=? AND m.if_index=?",
            (int(device_id), int(if_index or 0))
        ).fetchone()
        macs=int(r['macs'] or 0); ips=int(r['ips'] or 0); vlans=int(r['vlans'] or 0)
        return macs,ips,vlans,(macs>=2 or ips>=2)
    except Exception:
        return 0,0,0,False

def _interface_context(c, device_id, if_index, if_name=''):
    """Build authoritative context for a learned FDB interface."""
    idx=int(if_index or 0)
    name=str(if_name or '').strip()
    descr=''
    oper=''
    if idx:
        r=c.execute("SELECT if_name,if_descr,oper_status FROM network_interfaces WHERE device_id=? AND if_index=?",(device_id,idx)).fetchone()
        if r:
            name=name or str(r['if_name'] or '')
            descr=str(r['if_descr'] or '')
            oper=str(r['oper_status'] or '')
    inter=_location_inter_switch(c,device_id,idx) if idx else False
    macs,vlans=_port_fanout(c,device_id,idx) if idx else (0,0)
    fanout_candidate=_fanout_is_probable_uplink(macs,vlans)
    desc_l=descr.lower()
    textual=any(k in desc_l for k in ('uplink','trunk','peer','vpc','port-channel','port channel','to sw','to switch'))
    probable=bool(inter or _is_port_channel_name(name) or textual or fanout_candidate)
    role='uplink' if probable else ('access' if _is_physical_interface_name(name) else 'unknown')
    evidence=[]
    if inter: evidence.append('LLDP/CDP')
    if textual: evidence.append('description')
    if fanout_candidate: evidence.append(f'fanout={macs} MAC/{vlans} VLAN')
    if macs>=32: evidence.append(f'fanout={macs} MAC')
    if vlans>=3: evidence.append(f'VLANs={vlans}')
    return {'if_index':idx,'if_name':name,'if_descr':descr,'oper_status':oper,'inter_switch':inter,
            'fanout_macs':macs,'fanout_vlans':vlans,'probable_uplink':probable,'role':role,
            'role_evidence':', '.join(evidence)}


def _find_neighbor_device(c, remote_ip='', remote_device=''):
    """Resolve a discovered LLDP/CDP neighbor to a managed device id."""
    rip=str(remote_ip or '').strip()
    rname=str(remote_device or '').strip()
    if rip:
        row=c.execute("SELECT * FROM network_devices WHERE management_ip=? AND LOWER(COALESCE(status,''))='up' LIMIT 1",(rip,)).fetchone()
        if row: return row
    if rname:
        row=c.execute("SELECT * FROM network_devices WHERE (LOWER(name)=LOWER(?) OR LOWER(COALESCE(sys_name,''))=LOWER(?)) AND LOWER(COALESCE(status,''))='up' LIMIT 1",(rname,rname)).fetchone()
        if row: return row
        # CDP may return a qualified device-id. Match the first label/name conservatively.
        short=rname.split('.')[0].strip()
        if short and short!=rname:
            row=c.execute("SELECT * FROM network_devices WHERE (LOWER(name)=LOWER(?) OR LOWER(COALESCE(sys_name,''))=LOWER(?)) AND LOWER(COALESCE(status,''))='up' LIMIT 1",(short,short)).fetchone()
            if row: return row
    return None


def _recursive_mac_endpoint_locations(c, mac, vlan_hint=None, preferred_device_id=None, max_hops=8):
    """Walk MAC observations across LLDP/CDP uplinks until an endpoint port is found.

    MAC/FDB tells us where a frame is learned; LLDP/CDP tells us which switch is
    on an inter-switch port.  Combining both lets IPAM follow the same MAC from
    an upstream trunk/LAG into downstream switches instead of treating the
    transit interface as the endpoint.  The walk is bounded and loop-safe.
    """
    mac=normalize_mac(mac) or str(mac or '').strip().lower()
    if not mac: return []

    def live_devices_rows(device_id=None):
        if device_id is None:
            return c.execute("""
                SELECT m.*,d.name switch_name,d.management_ip switch_ip,d.status device_status,
                       d.last_discovery,d.discovery_interval_minutes,d.sys_name,
                       COALESCE(ni.if_name,m.if_name,'') resolved_if_name,
                       COALESCE(ni.if_descr,'') if_descr,COALESCE(ni.oper_status,'') oper_status
                FROM network_mac_entries m JOIN network_devices d ON d.id=m.device_id
                LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
                WHERE LOWER(m.mac)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
                  AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
                  AND d.last_discovery >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
                ORDER BY d.name,m.vlan_id,m.if_index""",(mac,DISCOVERY_AUTO_DEFAULT_MINUTES)).fetchall()
        return c.execute("""
            SELECT m.*,d.name switch_name,d.management_ip switch_ip,d.status device_status,
                   d.last_discovery,d.discovery_interval_minutes,d.sys_name,
                   COALESCE(ni.if_name,m.if_name,'') resolved_if_name,
                   COALESCE(ni.if_descr,'') if_descr,COALESCE(ni.oper_status,'') oper_status
            FROM network_mac_entries m JOIN network_devices d ON d.id=m.device_id
            LEFT JOIN network_interfaces ni ON ni.device_id=m.device_id AND ni.if_index=m.if_index
            WHERE m.device_id=? AND LOWER(m.mac)=LOWER(?) AND LOWER(COALESCE(d.status,''))='up'
              AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
              AND d.last_discovery >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
            ORDER BY m.vlan_id,m.if_index""",(int(device_id),mac,DISCOVERY_AUTO_DEFAULT_MINUTES)).fetchall()

    def classify(r):
        ctx=_interface_context(c,int(r['device_id']),int(r['if_index'] or 0),r['resolved_if_name'] or r['if_name'])
        item=dict(r); item.update(ctx)
        item['vlan_match']=bool(vlan_hint not in (None,'','—') and str(r['vlan_id'])==str(vlan_hint))
        item['preferred_device']=bool(preferred_device_id and int(r['device_id'])==int(preferred_device_id))
        score=20
        if _is_physical_interface_name(ctx['if_name']): score+=30
        if ctx['role']=='access': score+=25
        if item['vlan_match']: score+=15
        if str(ctx['oper_status']).lower() in ('up','up(1)','1'): score+=5
        if item['preferred_device']: score+=2
        if ctx['probable_uplink']: score-=70
        if _is_port_channel_name(ctx['if_name']): score-=40
        item['location_confidence']=max(0,min(100,score))
        item['port_type']='uplink' if ctx['probable_uplink'] else ('access' if ctx['role']=='access' else 'unknown')
        return item

    roots=[classify(r) for r in live_devices_rows()]
    direct=[x for x in roots if not x['probable_uplink'] and _is_physical_interface_name(x['if_name'])]
    if direct:
        direct.sort(key=lambda x:(x['location_confidence'],x['vlan_match'],x['preferred_device']),reverse=True)
        return direct[:1]

    # Breadth-first walk from every upstream observation.  We only traverse
    # interfaces explicitly identified by LLDP/CDP or a logical LAG/uplink role.
    queue=[]
    visited={(int(x['device_id']),int(x['if_index'] or 0)) for x in roots}
    for x in roots:
        if not x['probable_uplink']: continue
        nbrs=c.execute("SELECT * FROM network_neighbors WHERE device_id=? AND local_if_index=? ORDER BY confidence DESC",(int(x['device_id']),int(x['if_index'] or 0))).fetchall()
        for n in nbrs:
            nd=_find_neighbor_device(c,n['remote_ip'],n['remote_device'])
            if nd:
                queue.append((int(nd['id']),1))

    seen_devices=set()
    best=[]
    while queue and len(seen_devices)<64:
        did,hops=queue.pop(0)
        if did in seen_devices or hops>max_hops: continue
        seen_devices.add(did)
        rows=[classify(r) for r in live_devices_rows(did)]
        directs=[x for x in rows if not x['probable_uplink'] and _is_physical_interface_name(x['if_name'])]
        if directs:
            best.extend(directs); continue
        for x in rows:
            if not x['probable_uplink']: continue
            key=(did,int(x['if_index'] or 0))
            if key in visited: continue
            visited.add(key)
            nbrs=c.execute("SELECT * FROM network_neighbors WHERE device_id=? AND local_if_index=? ORDER BY confidence DESC",(did,int(x['if_index'] or 0))).fetchall()
            for n in nbrs:
                nd=_find_neighbor_device(c,n['remote_ip'],n['remote_device'])
                if nd and int(nd['id']) not in seen_devices:
                    queue.append((int(nd['id']),hops+1))

    if best:
        best.sort(key=lambda x:(x['location_confidence'],x['vlan_match'],x['preferred_device']),reverse=True)
        return best[:1]
    return []

def _historical_mac_locations(c, mac, vlan_hint=None, preferred_device_id=None, limit=4):
    """Return last-known endpoint locations from persistent MAC/port history.

    History is intentionally independent from current FDB freshness.  It is
    evidence of where the MAC was last discovered, not proof that the host is
    online now.  Current live FDB observations always take precedence.
    """
    mac=normalize_mac(mac) or str(mac or '').strip().lower()
    if not mac: return []
    params=[mac]
    vlan_sql=''
    _vh=str(vlan_hint or '').strip()
    # vlan_hint may come from aggregated history (e.g. "1, 10, 11").
    # Such a value is NOT a scalar VLAN and must never be bound to BIGINT.
    if _vh and _vh != '—' and re.fullmatch(r'\d+', _vh):
        vlan_sql=' AND h.vlan_id=?'; params.append(int(_vh))
    device_sql=''
    if preferred_device_id:
        device_sql=' ORDER BY CASE WHEN h.device_id=? THEN 0 ELSE 1 END, h.last_seen DESC, h.id DESC'
        params.append(int(preferred_device_id))
    else:
        device_sql=' ORDER BY h.last_seen DESC, h.id DESC'
    params.append(int(limit))
    rows=c.execute(f"""SELECT h.*,d.name switch_name,d.management_ip switch_ip,d.status device_status,
               d.last_discovery,d.sys_name,COALESCE(ni.if_name,h.if_name,'') resolved_if_name,
               COALESCE(ni.if_descr,'') if_descr,COALESCE(ni.oper_status,'') oper_status
        FROM network_mac_port_history h JOIN network_devices d ON d.id=h.device_id
        LEFT JOIN network_interfaces ni ON ni.device_id=h.device_id AND ni.if_index=h.if_index
        WHERE h.mac COLLATE NOCASE=?{vlan_sql}
        {device_sql} LIMIT ?""",params).fetchall()
    out=[]; seen=set()
    for r in rows:
        key=(int(r['device_id']),int(r['if_index'] or 0),int(r['vlan_id'] or 0))
        if key in seen: continue
        seen.add(key)
        name=str(r['resolved_if_name'] or r['if_name'] or '').strip()
        # Historical rows may point to an interface no longer present in the
        # current interface table. Keep the recorded if_name in that case.
        ctx=_interface_context(c,int(r['device_id']),int(r['if_index'] or 0),name)
        item=dict(r); item.update(ctx)
        item['if_name']=name or ctx.get('if_name') or r['if_name'] or ''
        item['port_type']=str(r['port_type'] or ('uplink' if ctx.get('probable_uplink') else 'access'))
        item['location_confidence']=max(25,min(90,int(r['confidence'] or 40)))
        item['historical']=True
        item['probable_uplink']=bool(ctx.get('probable_uplink'))
        item['last_known']=r['last_seen'] or ''
        out.append(item)
    # Prefer a real physical endpoint over an old logical/transit observation.
    direct=[x for x in out if _is_physical_interface_name(x.get('if_name','')) and not x.get('probable_uplink') and str(x.get('port_type','')).lower()!='uplink']
    return (direct or out)[:int(limit)]

def _global_mac_locations(c, mac, vlan_hint=None, preferred_device_id=None, preferred_arp_if_index=0):
    """Resolve a MAC globally, preferring a real endpoint-facing port.

    The old implementation correlated an IP only against the switch currently
    being viewed. That is incorrect for an access switch: a MAC belonging to a
    host behind Core is legitimately learned on the access switch's trunk.
    We now inspect the same MAC across all live managed switches and select the
    strongest endpoint-facing observation.
    """
    mac=normalize_mac(mac) or str(mac or '').strip().lower()
    if not mac: return []
    # V75.2.25: resolve through discovered inter-switch links first. This prevents
    # an upstream FDB observation from being mistaken for the endpoint port.
    recursive=_recursive_mac_endpoint_locations(c,mac,vlan_hint=vlan_hint,preferred_device_id=preferred_device_id)
    if recursive:
        return recursive
    rows=c.execute("""
        SELECT m.*,d.name switch_name,d.management_ip switch_ip,d.status device_status,
               d.last_discovery,d.discovery_interval_minutes,d.sys_name
        FROM network_mac_entries m JOIN network_devices d ON d.id=m.device_id
        WHERE m.mac COLLATE NOCASE = ? COLLATE NOCASE AND LOWER(COALESCE(d.status,''))='up'
          AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
          AND d.last_discovery >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
        ORDER BY d.name,m.vlan_id,m.if_index
    """,(mac,DISCOVERY_AUTO_DEFAULT_MINUTES)).fetchall()
    out=[]
    for r in rows:
        ctx=_interface_context(c,r['device_id'],r['if_index'],r['if_name'])
        item=dict(r)
        item.update(ctx)
        item['vlan_match']=bool(vlan_hint not in (None,'','—') and str(r['vlan_id'])==str(vlan_hint))
        item['preferred_device']=bool(preferred_device_id and int(r['device_id'])==int(preferred_device_id))
        item['arp_if_match']=bool(preferred_arp_if_index and int(r['if_index'] or 0)==int(preferred_arp_if_index))
        score=20
        if _is_physical_interface_name(ctx['if_name']): score+=30
        if ctx['role']=='access': score+=25
        if item['vlan_match']: score+=15
        if item['arp_if_match']: score+=12
        if str(ctx['oper_status']).lower() in ('up','up(1)','1'): score+=5
        if item['preferred_device']: score+=2
        if ctx['probable_uplink']: score-=70
        if _is_port_channel_name(ctx['if_name']): score-=40
        item['location_confidence']=max(0,min(100,score))
        item['port_type']='uplink' if ctx['probable_uplink'] else ('access' if ctx['role']=='access' else 'unknown')
        out.append(item)
    out.sort(key=lambda x:(x['location_confidence'], not x['probable_uplink'], x['vlan_match'], x['arp_if_match']),reverse=True)
    # Prefer one endpoint location. If none exists, expose one best transit
    # observation with an explicit uplink role rather than inventing a client port.
    direct=[x for x in out if not x['probable_uplink'] and _is_physical_interface_name(x['if_name'])]
    if direct:
        return direct[:4]
    # No current endpoint-facing FDB observation. Fall back to the last known
    # historical endpoint instead of returning an empty Switch/Port value.
    historical=_historical_mac_locations(c,mac,vlan_hint=vlan_hint,preferred_device_id=preferred_device_id,limit=4)
    return historical[:4] if historical else out[:4]

def _discovery_source_live(device_row):
    """Return True only for a device whose discovery data is currently trusted.

    Discovery records are intentionally retained when a switch disappears or
    temporarily stops responding, so operators do not lose historical evidence.
    Those stale records must not, however, be used to answer a *current*
    IP->switch->port question.  A successful discovery marks the source UP; a
    failed discovery marks it DOWN/ERROR.  The freshness window also protects
    against an old UP record when auto-discovery has been disabled or stopped.
    """
    if not device_row:
        return False
    device_row = dict(device_row) if not isinstance(device_row, dict) else device_row
    if str(device_row.get("status") or "").lower() != "up":
        return False
    last=str(device_row.get("last_discovery") or "").strip()
    if not last:
        return False
    try:
        import datetime as _dt
        ts=_dt.datetime.fromisoformat(last.replace("Z","+00:00"))
        if ts.tzinfo is None:
            ts=ts.replace(tzinfo=_dt.timezone.utc)
        now=_dt.datetime.now(_dt.timezone.utc)
        interval=max(1,int(device_row.get("discovery_interval_minutes") or DISCOVERY_AUTO_DEFAULT_MINUTES))
        # Allow a few missed polls without making stale data look current.
        max_age=max(15, interval*3)
        return (now-ts).total_seconds() <= max_age*60
    except Exception:
        return False

def _discover_interfaces(c,d,target):
    # Keep IF-MIB discovery sequential. Cisco/SNMP agents can rate-limit or
    # serialize walks; parallel walks may return partial tables and make the
    # Switch/Port filter incomplete.
    #
    # IMPORTANT: never replace a known-good interface snapshot with a partial
    # SNMP walk.  A timeout on just one IF-MIB OID used to make idxs incomplete,
    # then DELETE + INSERT erased previously discovered ports.  That is exactly
    # the failure mode where some switches suddenly lose ports from
    # Switch/Port correlation even though the switch itself is healthy.
    walks=[
        ("names",".1.3.6.1.2.1.31.1.1.1.1"),
        ("descr",".1.3.6.1.2.1.2.2.1.2"),
        ("oper",".1.3.6.1.2.1.2.2.1.8"),
        ("admin",".1.3.6.1.2.1.2.2.1.7"),
        ("speed",".1.3.6.1.2.1.2.2.1.5"),
    ]
    parsed={}
    failed=[]
    for key,oid in walks:
        rc,raw,err=_walk(d,target,oid)
        if rc!=0:
            failed.append(f"{key}: {err or raw or 'SNMP error'}")
            parsed[key]={}
            continue
        parsed[key]=dict((idx,val) for idx,val,_ in _parse_walk_lines(raw))
        # A successful response with no interface data is not a safe basis for
        # replacing a populated snapshot.  Treat it as incomplete only when
        # the device already has interface rows.
        if not parsed[key]:
            existing_count=int(c.execute(
                "SELECT COUNT(*) FROM network_interfaces WHERE device_id=?",(d["id"],)
            ).fetchone()[0] or 0)
            if existing_count:
                failed.append(f"{key}: empty IF-MIB response")
    if failed:
        existing=c.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN LOWER(COALESCE(oper_status,'')) IN ('up','up(1)','1') THEN 1 ELSE 0 END) up "
            "FROM network_interfaces WHERE device_id=?",(d["id"],)
        ).fetchone()
        # Preserve the complete last-known snapshot.  The caller still marks
        # the device discovery as successful because system/MAC/ARP discovery
        # can be valid; the exact IF-MIB failure is recorded for diagnostics.
        c.execute(
            "UPDATE network_devices SET last_error=? ,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ("IF-MIB snapshot preserved: "+"; ".join(failed)[:900], d["id"])
        )
        return int(existing["n"] or 0), int(existing["up"] or 0)

    names,descr,oper,admin,speed=(parsed["names"],parsed["descr"],parsed["oper"],parsed["admin"],parsed["speed"])
    idxs=sorted(set(names)|set(descr)|set(oper)|set(admin)|set(speed))
    if not idxs:
        existing=c.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN LOWER(COALESCE(oper_status,'')) IN ('up','up(1)','1') THEN 1 ELSE 0 END) up "
            "FROM network_interfaces WHERE device_id=?",(d["id"],)
        ).fetchone()
        return int(existing["n"] or 0), int(existing["up"] or 0)

    # Some SNMP agents can terminate a walk with rc=0 even after returning a
    # suspiciously small table.  Do not destroy a populated snapshot in that
    # case.  The 60% guard is deliberately conservative so a genuine hardware
    # change still gets accepted while a truncated response is ignored.
    existing_count=int(c.execute(
        "SELECT COUNT(*) FROM network_interfaces WHERE device_id=?",(d["id"],)
    ).fetchone()[0] or 0)
    if existing_count >= 20 and len(idxs) < max(10, int(existing_count*0.60)):
        existing=c.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN LOWER(COALESCE(oper_status,'')) IN ('up','up(1)','1') THEN 1 ELSE 0 END) up "
            "FROM network_interfaces WHERE device_id=?",(d["id"],)
        ).fetchone()
        return int(existing["n"] or 0), int(existing["up"] or 0)

    hrc,hout,_=_walk(d,target,'.1.3.6.1.2.1.31.1.1.1.15')
    highspeed={i:_parse_int_value(v)*1000000 for i,v,_ in _parse_walk_lines(hout)} if hrc==0 else {}
    # All IF-MIB walks completed successfully: this is now safe to atomically
    # replace the old snapshot with the new complete snapshot.
    c.execute("DELETE FROM network_interfaces WHERE device_id=?",(d["id"],))
    up=0
    for idx in idxs:
        op=str(oper.get(idx,"")); adm=str(admin.get(idx,""))
        if op.lower() in ("up(1)","up","1"): up+=1
        sp=highspeed.get(idx) or _parse_int_value(speed.get(idx,"0"))
        if sp==4294967295:sp=0
        c.execute(
            "INSERT INTO network_interfaces(device_id,if_index,if_name,if_descr,admin_status,oper_status,speed_bps) "
            "VALUES(?,?,?,?,?,?,?)",
            (d["id"],idx,names.get(idx,""),descr.get(idx,""),adm,op,sp)
        )
    return len(idxs),up

def _cisco_vtp_port_modes(config_walk, operational_walk, ifmap):
    """Join CISCO-VTP-MIB trunk state columns to known IF-MIB indices.

    DynamicStatus is the live operational state (1=trunking, 2=notTrunking);
    DynamicState is the configured negotiation state. Neither is inferred
    from LLDP, MAC fanout, or a Port-channel name.
    """
    config={}
    operational={}
    for suffix,value,_ in _parse_walk_oid_lines(config_walk or ''):
        if suffix: config[int(suffix[-1])]=_parse_int_value(value)
    for suffix,value,_ in _parse_walk_oid_lines(operational_walk or ''):
        if suffix: operational[int(suffix[-1])]=_parse_int_value(value)
    configured_labels={1:'ON',2:'OFF',3:'DESIRABLE',4:'AUTO',5:'ON_NO_NEGOTIATE'}
    operational_labels={1:'TRUNKING',2:'NOT_TRUNKING'}
    rows=[]
    for idx in sorted(set(config)&set(operational)&set(ifmap)):
        cfg=configured_labels.get(config[idx]); op=operational_labels.get(operational[idx])
        if cfg and op:
            rows.append({'if_index':idx,'if_name':str(ifmap[idx]),'configured_mode':cfg,'operational_mode':op})
    return rows

def _ciscosb_port_modes(mode_walk, ifmap):
    """Normalize Cisco Small Business vlanPortModeState rows by IF-MIB index.

    SG300 reports 2=access and 3=trunk; SG350/SG350X reports 11=access and
    12=trunk. Both families are documented by Cisco and expose the current
    switchport mode in CISCOSB-vlan-MIB. Other supported modes are retained
    as non-trunk so the API never promotes them to trunk accidentally.
    """
    labels={1:'GENERAL',2:'ACCESS',3:'TRUNK',7:'CUSTOMER',
            10:'GENERAL',11:'ACCESS',12:'TRUNK',13:'PVLAN_PROMISCUOUS',
            14:'PVLAN_HOST',15:'CUSTOMER'}
    rows=[]
    for suffix,value,_ in _parse_walk_oid_lines(mode_walk or ''):
        if not suffix:
            continue
        idx=int(suffix[-1]); code=_parse_int_value(value); mode=labels.get(code)
        if idx not in ifmap or not mode:
            continue
        rows.append({'if_index':idx,'if_name':str(ifmap[idx]),
                     'configured_mode':mode,
                     'operational_mode':'TRUNKING' if mode=='TRUNK' else 'NOT_TRUNKING'})
    return rows

def _discover_cisco_trunk_modes(c,d,target):
    """Persist a complete Cisco trunk-mode snapshot without erasing manual data.

    Nexus/Catalyst use CISCO-VTP-MIB; Cisco Small Business uses its
    vlanPortModeState table. Failed or partial walks retain the last good
    snapshot and return None.
    """
    if _device_is_smallbiz(d):
        mode_oid='.1.3.6.1.4.1.9.6.1.101.48.22.1.1'
        rc,mode_raw,_=_walk(d,target,mode_oid)
        if rc!=0 or not mode_raw or 'no such object' in mode_raw.lower():
            return None
        ifmap={int(r['if_index']):r['if_name'] for r in c.execute(
            'SELECT if_index,if_name FROM network_interfaces WHERE device_id=?',(int(d['id']),)).fetchall() if r['if_name']}
        rows=_ciscosb_port_modes(mode_raw,ifmap)
        expected=len(ifmap)
        if not rows or (expected>=10 and len(rows)<int(expected*0.60)):
            return None
        c.execute('DELETE FROM network_port_modes WHERE device_id=?',(int(d['id']),))
        for row in rows:
            c.execute('''INSERT INTO network_port_modes(device_id,if_index,configured_mode,operational_mode,source,last_seen)
                         VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(device_id,if_index) DO UPDATE SET
                         configured_mode=excluded.configured_mode,operational_mode=excluded.operational_mode,
                         source=excluded.source,last_seen=excluded.last_seen''',
                      (int(d['id']),row['if_index'],row['configured_mode'],row['operational_mode'],'CISCOSB-VLAN-MIB'))
        return {'total':len(rows),'trunking':sum(1 for row in rows if row['operational_mode']=='TRUNKING'),'supported':True}
    if not (_device_is_nexus(d) or _device_is_cisco_catalyst(d)):
        return {'total':0,'trunking':0,'supported':False}
    config_oid='.1.3.6.1.4.1.9.9.46.1.6.1.1.13'
    operational_oid='.1.3.6.1.4.1.9.9.46.1.6.1.1.14'
    crc,config_raw,_=_walk(d,target,config_oid)
    orc,operational_raw,_=_walk(d,target,operational_oid)
    if crc!=0 or orc!=0 or not config_raw or not operational_raw:
        return None
    ifmap={int(r['if_index']):r['if_name'] for r in c.execute(
        'SELECT if_index,if_name FROM network_interfaces WHERE device_id=?',(int(d['id']),)).fetchall() if r['if_name']}
    rows=_cisco_vtp_port_modes(config_raw,operational_raw,ifmap)
    # Treat a short but syntactically successful walk as incomplete. This
    # guards the saved snapshot from partial SNMP agent responses.
    expected=len(ifmap)
    if not rows or (expected>=10 and len(rows)<int(expected*0.60)):
        return None
    c.execute('DELETE FROM network_port_modes WHERE device_id=?',(int(d['id']),))
    for row in rows:
        c.execute('''INSERT INTO network_port_modes(device_id,if_index,configured_mode,operational_mode,source,last_seen)
                     VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(device_id,if_index) DO UPDATE SET
                     configured_mode=excluded.configured_mode,operational_mode=excluded.operational_mode,
                     source=excluded.source,last_seen=excluded.last_seen''',
                  (int(d['id']),row['if_index'],row['configured_mode'],row['operational_mode'],'CISCO-VTP-MIB'))
    return {'total':len(rows),'trunking':sum(1 for row in rows if row['operational_mode']=='TRUNKING'),'supported':True}

def _discover_vpc(c,d,target):
    if 'nexus' not in (str(d.get('device_type',''))+' '+str(d.get('sys_descr',''))).lower():return
    try:
        from .vpc_evidence import parse,save
        rc,text,_=_walk(d,target,'.1.3.6.1.4.1.9.9.807')
        if rc!=0:return
        rows=[(oid,value) for oid,value,_ in _parse_walk_oid_lines(text) if 'no such' not in str(value).lower()]
        if not rows:return
        ifmap={int(r['if_index']):r['if_name'] for r in c.execute('SELECT if_index,if_name FROM network_interfaces WHERE device_id=?',(d['id'],))}
        save(d['id'],parse(rows,ifmap))
    except (OSError,ValueError,KeyError):return

def _discover_lag_members(c,d,target,persist=False):
    """Return logical LAG/Port-Channel ifIndex -> physical member interfaces.

    IF-MIB ifStackTable is widely exposed by Cisco devices and is a safer
    generic mapping than guessing Cisco-specific LAG OIDs. A MAC learned on
    a Port-Channel is legitimately associated with the logical interface;
    we therefore expose its physical members as additional context rather
    than claiming an exact member link for that MAC.
    """
    ifmap={int(r["if_index"]):r["if_name"] for r in c.execute(
        "SELECT if_index,if_name FROM network_interfaces WHERE device_id=?",(d["id"],))}
    out={}
    rc,raw,_=_walk(d,target,".1.3.6.1.2.1.31.1.2.1.3")
    if rc!=0: raw=""
    for suffix,val,_ in _parse_walk_oid_lines(raw):
        # ifStackStatus is active(1), notInService(2), or notReady(3).
        # Only active stack relationships are current LAG membership evidence.
        if not re.search(r"(?:active\(1\)|\b1\b)",str(val),re.I):
            continue
        if len(suffix)<2:
            continue
        # ifStackStatus index = higherLayer.ifIndex.lowerLayer.ifIndex
        higher,lower=suffix[-2],suffix[-1]
        hname=ifmap.get(higher,"")
        lname=ifmap.get(lower,"")
        if not hname or not lname:
            continue
        if re.match(r"^(po|port-channel|lag)\s*\d+",hname,re.I):
            out.setdefault(higher,[]).append({"if_index":lower,"if_name":lname})
    # IEEE8023-LAG-MIB and CISCO-PAGP-MIB report member ifIndex -> aggregate ifIndex.
    # The returned aggregate must itself exist as a logical interface in IF-MIB.
    for oid in ('.1.2.840.10006.300.43.1.2.1.1.13','.1.3.6.1.4.1.9.9.98.1.1.1.1.8'):
        code,text,_=_walk(d,target,oid)
        if code!=0:continue
        for suffix,value in _neighbor_column(text,oid).items():
            if len(suffix)!=1:continue
            member=int(suffix[0]);aggregate=_parse_int_value(value)
            if aggregate==member or not re.match(r'^(po|port-channel|lag)\s*\d+',ifmap.get(aggregate,''),re.I):continue
            if not ifmap.get(member) or re.match(r'^(po|port-channel|lag)',ifmap[member],re.I):continue
            out.setdefault(aggregate,[]).append({'if_index':member,'if_name':ifmap[member]})
    for k in out:
        seen=set(); uniq=[]
        for x in out[k]:
            key=(x["if_index"],x["if_name"])
            if key not in seen:
                seen.add(key); uniq.append(x)
        out[k]=uniq
    if persist and out:
        c.execute("DELETE FROM network_lag_members WHERE device_id=?",(d["id"],))
        for aggregate_idx,members in out.items():
            aggregate_name=ifmap.get(int(aggregate_idx),"")
            for member in members:
                c.execute("INSERT INTO network_lag_members(device_id,aggregate_if_index,aggregate_if_name,member_if_index,member_if_name,last_seen) VALUES(?,?,?,?,?,now())",
                    (d["id"],int(aggregate_idx),aggregate_name,int(member["if_index"]),member["if_name"]))
    return out

def _physical_port_context(c,d,target,ifidx,ifname):
    if not ifname:
        return []
    lags=_discover_lag_members(c,d,target)
    members=lags.get(int(ifidx),[])
    return members

def _is_non_endpoint_mac(mac):
    """Return True for multicast/reserved MACs that must not become endpoint ports."""
    try:
        b=bytes.fromhex(re.sub(r'[^0-9A-Fa-f]','',str(mac or '')))
        if len(b)!=6:
            return True
        # I/G bit: multicast/broadcast. Also explicitly ignore common control
        # protocols (STP/CDP/LLDP/EAPOL) which otherwise pollute access-port data.
        if b[0] & 1:
            return True
        if b in (bytes.fromhex('0180c2000000'), bytes.fromhex('0180c200000e'),
                 bytes.fromhex('0180c2000003'), bytes.fromhex('01000ccccccd')):
            return True
        return False
    except Exception:
        return True


def _device_is_cisco_catalyst(d):
    """Identify Cisco Catalyst/IOS switches, while honoring explicit SG350X inventory type.

    A few Small Business units expose an Enterprise/Catalyst-looking sysObjectID/sysDescr.
    The configured SG350X family is intentionally authoritative for that narrow case so
    those devices use the Small Business FDB recovery path. Explicit Catalyst inventory
    types continue to use the Catalyst path unchanged.
    """
    d = dict(d) if not isinstance(d, dict) else d
    configured=str(d.get("device_type") or "").strip().lower()
    oid=str(d.get("sys_object_id") or "").lower()
    descr=str(d.get("sys_descr") or "").lower()
    # Live SNMP identity wins over stale/manual inventory labels. Cisco
    # Enterprise OIDs (.1.3.6.1.4.1.9.1.*) and explicit IOS/C2960 model text
    # are Catalyst evidence, including C2960G-24TC-L.
    if oid.startswith(".1.3.6.1.4.1.9.1."):
        return True
    if any(k in descr for k in ("catalyst","c2960","c3560","c3750","c3850","c9300","c9200","c2960x","c2960-l","cisco ios software","ios xe","ios-xe")):
        return True
    if configured in ("cisco sg350x","sg350x","cisco sg350","sg350"):
        return False
    text=(str(d.get("vendor") or "")+" "+str(d.get("device_type") or "")+" "+descr+" "+str(d.get("sys_name") or "")+" "+str(d.get("name") or "")).lower()
    # Enterprise Cisco object-id space is stronger than a manually entered
    # device_type/name. This fixes records named SG350X whose live agent is a
    # Catalyst/C2960 and therefore must use the Catalyst FDB recovery path.
    if "nexus" in text or "nx-os" in text or "nxos" in text:
        return False
    return any(k in text for k in ("catalyst","cisco ios","ios xe","ios-xe","c2960","c3560","c3750","c3850","c9300","c9200","c2960x","c2960-l"))

def _device_is_smallbiz(d):
    """Identify Cisco Small Business SG-series after Catalyst identity."""
    d = dict(d) if not isinstance(d, dict) else d
    if _device_is_cisco_catalyst(d):
        return False
    text=(str(d.get("vendor") or "")+" "+str(d.get("device_type") or "")+" "+str(d.get("sys_descr") or "")+" "+str(d.get("sys_name") or "")+" "+str(d.get("name") or "")).lower()
    return any(k in text for k in ("sg200","sg300","sg350","sg500","sg550","small business","small-business"))

def _device_is_sophos(d):
    """Best-effort Sophos Firewall identification for SNMP/topology enrichment."""
    d=dict(d) if not isinstance(d,dict) else d
    text=(str(d.get("vendor") or "")+" "+str(d.get("device_type") or "")+" "+
          str(d.get("sys_descr") or "")+" "+str(d.get("sys_name") or "")).lower()
    return any(k in text for k in ("sophos", "sfos", "xg firewall", "xgs firewall", "xg-firewall"))

def _device_is_nexus(d):
    d=dict(d) if not isinstance(d,dict) else d
    text=(str(d.get("vendor") or "")+" "+str(d.get("device_type") or "")+" "+
          str(d.get("sys_descr") or "")+" "+str(d.get("sys_name") or "")+" "+
          str(d.get("name") or "")).lower()
    return "nexus" in text or "nx-os" in text or "nxos" in text

def _discover_vlan_ids(d,target):
    """Discover operational VLAN IDs using the most native table for the device family."""
    vlan_ids=set()
    is_cat=_device_is_cisco_catalyst(d)
    is_smb=_device_is_smallbiz(d)

    # Catalyst/IOS: Cisco documents VTP-MIB vtpVlanState as the reliable VLAN
    # inventory before querying BRIDGE-MIB with community@VLAN.
    if is_cat:
        rc,out,_=_walk(d,target,".1.3.6.1.4.1.9.9.46.1.3.1.1.2")
        if rc==0 and out:
            for suf,val,_ in _parse_walk_oid_lines(out):
                if suf:
                    v=int(suf[-1]); state=_parse_int_value(val)
                    if 1<=v<=4094 and state in (0,1):
                        vlan_ids.add(v)

    # SG-series and other devices: Q-BRIDGE VLAN tables are retained because
    # they are the historical/proven path for Small Business switches.
    for oid in (".1.3.6.1.2.1.17.7.1.4.3.1.1", ".1.3.6.1.2.1.17.7.1.4.3.1.4"):
        rc,out,_=_walk(d,target,oid)
        if rc==0 and out:
            for suf,_,_ in _parse_walk_oid_lines(out):
                if suf:
                    v=int(suf[-1])
                    if 1<=v<=4094: vlan_ids.add(v)

    if not vlan_ids:
        vlan_ids.update((1,10,20,30,40,50,100,172,200,300,400,500,555,999))
    return sorted(vlan_ids)[:128]

def _discover_mac_table(c,d,target):
    """Discover MAC/FDB entries with device-aware, MAC-specific correlation.

    Stable Catalyst behavior from V75.2.38 is preserved. SG/Nexus get
    contextual recovery only when the global FDB is clearly collapsed.
    """
    bpmap={idx:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(_walk(d,target,".1.3.6.1.2.1.17.1.4.1.2")[1]) for idx in ([suffix[-1]] if suffix else [])}
    ifmap={int(r["if_index"]):r["if_name"] for r in c.execute("SELECT if_index,if_name FROM network_interfaces WHERE device_id=?",(d["id"],))}
    irc,irout,_=_walk(d,target,".1.3.6.1.2.1.31.1.1.1.1")
    if irc==0 and irout:
        live_ifmap={}
        for suffix,val,_ in _parse_walk_oid_lines(irout):
            if suffix and val: live_ifmap[int(suffix[-1])]=str(val)
        if live_ifmap: ifmap.update(live_ifmap)
    entries=[]; method=""; bpmap_by_vlan={}

    def collect_qbridge(rc,qout,src_status=None,fdb_to_vlan=None,forced_vlan=None):
        got=[]
        if rc!=0 or not qout: return got
        for suffix,val,_ in _parse_walk_oid_lines(qout):
            if len(suffix)<7: continue
            fdb_id=suffix[-7]; mac=_mac_from_suffix(suffix); bp=_parse_int_value(val)
            if not mac or not bp or _is_non_endpoint_mac(mac): continue
            st=(src_status or {}).get(suffix)
            if st is not None and st not in (3,): continue
            vlan=int(forced_vlan) if forced_vlan is not None else int((fdb_to_vlan or {}).get(fdb_id,fdb_id))
            if not (1<=vlan<=4094): continue
            got.append((vlan,mac,bp))
        return got

    fdb_to_vlan={}
    frc,fout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.4.2.1.3")
    if frc==0:
        for suf,val,_ in _parse_walk_oid_lines(fout):
            if suf:
                vlan=int(suf[-1]); fdb=int(_parse_int_value(val))
                if 1<=vlan<=4094 and fdb>0: fdb_to_vlan[fdb]=vlan

    qrc,qout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.2")
    # A status column cannot classify any rows when its FDB walk failed or is empty.
    # Keep every VLAN and both Q-BRIDGE/BRIDGE attempts; skip only that unused walk.
    src_status={}
    src_rc,src_out,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.3") if qrc==0 and qout else (qrc,"","")
    if src_rc==0 and src_out:
        src_status={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(src_out)}
    generic_qbridge=collect_qbridge(qrc,qout,src_status,fdb_to_vlan)

    def collect_bridge(community_override=None):
        got=[]
        arc,aout,_=_walk(d,target,".1.3.6.1.2.1.17.4.3.1.2",community_override)
        ast={}
        src2rc,src2out,_=_walk(d,target,".1.3.6.1.2.1.17.4.3.1.3",community_override) if arc==0 and aout else (arc,"","")
        if src2rc==0 and src2out:
            ast={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(src2out)}
        if arc==0 and aout:
            for suffix,val,_ in _parse_walk_oid_lines(aout):
                mac=_mac_from_suffix(suffix); bp=_parse_int_value(val)
                if not mac or not bp or _is_non_endpoint_mac(mac): continue
                st=ast.get(suffix)
                if st is not None and st not in (3,): continue
                got.append((mac,bp))
        return got

    ver=str(d["version"] or "").lower(); community=str(d["community"] or "").strip()
    is_cat=_device_is_cisco_catalyst(d)
    is_smb=_device_is_smallbiz(d)
    # A small number of legacy devices in the inventory are manually labelled
    # SG350X while their live SNMP agent identifies itself with a Catalyst/IOS
    # OID.  Do not globally reclassify Catalyst devices: that would risk
    # regressing the C2960XR path that is already stable.  Instead, enable the
    # SG-family VLAN-context repair only for this explicit manual SG350X intent.
    manual_sg350x=(str(d["device_type"] or "").strip().lower() in ("cisco sg350x","sg350x"))
    legacy_sg350x_mismatch=bool(manual_sg350x and is_cat and ("c2960" in str(d["sys_descr"] or "").lower() or str(d["sys_object_id"] or "").lower().startswith(".1.3.6.1.4.1.9.1.")))
    indexed_candidates=[]

    # Catalyst: NEVER use the unscoped Q-BRIDGE result when VLAN-context
    # discovery is available. The unscoped table can represent a virtual FDB.
    if is_cat and ver in ("1","v1","2c","v2c","v2") and community:
        for vlan in _discover_vlan_ids(d,target):
            ci=f"{community}@{vlan}"
            map_rc,map_out,_=_walk(d,target,".1.3.6.1.2.1.17.1.4.1.2",ci)
            vlan_bp={}
            if map_rc==0 and map_out:
                for suffix,val,_ in _parse_walk_oid_lines(map_out):
                    if suffix: vlan_bp[int(suffix[-1])]=_parse_int_value(val)
            if vlan_bp: bpmap_by_vlan[int(vlan)]=vlan_bp
            brc,bout,_=_walk(d,target,".1.3.6.1.2.1.17.4.3.1.2",ci)
            if brc!=0 or not bout: continue
            bsrc,bsout,_=_walk(d,target,".1.3.6.1.2.1.17.4.3.1.3",ci)
            bst={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(bsout)} if bsrc==0 and bsout else {}
            for suffix,val,_ in _parse_walk_oid_lines(bout):
                mac=_mac_from_suffix(suffix); bp=_parse_int_value(val)
                if not mac or not bp or _is_non_endpoint_mac(mac): continue
                st=bst.get(suffix)
                if st is not None and st not in (3,): continue
                indexed_candidates.append((int(vlan),mac,bp))
        if indexed_candidates:
            entries=indexed_candidates; method="bridge@vlan"
        else:
            bridge=collect_bridge()
            if bridge:
                entries=[(0,mac,bp) for mac,bp in bridge]; method="bridge"

        # Legacy SG350X-labelled devices may expose a Catalyst-style identity
        # but still require the Small-Business community@VLAN FDB view.  Run a
        # bounded repair only when the Catalyst result is clearly collapsed to
        # one physical bridge-port.  The existing Catalyst path remains the
        # primary source; the repair replaces it only when it provides richer
        # VLAN/port coverage.
        if legacy_sg350x_mismatch and entries and community:
            pcounts={}
            for _v,_m,_b in entries: pcounts[int(_b)]=pcounts.get(int(_b),0)+1
            total=len(entries)
            dominant=max(pcounts.values()) if pcounts else 0
            if total>=8 and dominant/max(1,total)>=0.90:
                repair=[]
                observed=sorted({int(v) for v,_,_ in entries if 1<=int(v)<=4094})
                vlan_list=observed or [1]
                vlan_list=sorted(set(vlan_list)|set(_discover_vlan_ids(d,target)))[:24]
                for vlan in vlan_list:
                    ci=f"{community}@{vlan}"
                    br=collect_bridge(ci)
                    if br:
                        repair.extend((int(vlan),mac,bp) for mac,bp in br)
                    if len(repair)>=max(32,total*2):
                        break
                def _repair_score(rows):
                    if not rows: return (-1,0,0,0)
                    uniq={(int(v),str(m).lower()) for v,m,_ in rows}
                    ports={(int(v),int(b)) for v,_,b in rows}
                    physical=0; mapped=0
                    for v,_m,b in rows:
                        ii=bpmap_by_vlan.get(int(v),{}).get(int(b),0) or bpmap.get(int(b),0)
                        if ii: mapped+=1
                        if _is_physical_interface_name(ifmap.get(int(ii),"")): physical+=1
                    return (len(uniq)*100+len(ports)*10+physical*8+mapped,len(uniq),len(ports),physical)
                if _repair_score(repair) > _repair_score(entries):
                    entries=repair; method="sg350x-bridge@vlan-repair"

    # SG350/SG350X: keep the proven global Q-BRIDGE path when it is healthy.
    # When an SG agent collapses the FDB to one VLAN/port, collect BOTH
    # VLAN-scoped Q-BRIDGE and BRIDGE-MIB candidates and select the mapping
    # with the richer per-port correlation. Some SG firmware exposes one of
    # these views correctly while the other view is virtualized.
    elif is_smb and ver in ("1","v1","2c","v2c","v2") and community:
        if generic_qbridge:
            vlan_counts={}; port_counts={}
            for v,m,b in generic_qbridge:
                vlan_counts[v]=vlan_counts.get(v,0)+1
                port_counts[b]=port_counts.get(b,0)+1
            total_g=len(generic_qbridge)
            dominant_vlan=max(vlan_counts.values()) if vlan_counts else 0
            dominant_port=max(port_counts.values()) if port_counts else 0
            # SG350X can return a partial global FDB even when it is not strictly
            # collapsed. For an explicit SG350/SG350X inventory entry, trigger the
            # VLAN-context recovery when most observations still point to one port.
            # This remains intentionally narrow and does not affect Catalyst/Nexus.
            is_explicit_sg=str(d.get("device_type") or "").strip().lower() in ("cisco sg350x","sg350x","cisco sg350","sg350")
            suspicious=(total_g>=4 and (
                dominant_vlan/total_g>=0.90 or
                dominant_port/total_g>=0.80 or
                (is_explicit_sg and total_g>=6 and dominant_port/total_g>=0.65)
            ))
        else:
            suspicious=True
        if not suspicious:
            entries=generic_qbridge; method="qbridge"
        else:
            context_q=[]; context_b=[]
            # Adaptive recovery: do not walk every VLAN unless the global FDB
            # is collapsed to one VLAN. Q-BRIDGE is tried first; BRIDGE-MIB is
            # only used as an alternate when Q-BRIDGE maps to a logical LAG.
            observed_vlans=sorted({int(v) for v,_,_ in generic_qbridge if 1<=int(v)<=4094})
            discovered_vlans=set(_discover_vlan_ids(d,target))
            known_db_vlans=set()
            try:
                for rr in c.execute("SELECT vlan FROM subnets WHERE TRIM(COALESCE(vlan,''))<>''").fetchall():
                    vv=_parse_int_value(rr[0])
                    if 1<=vv<=4094: known_db_vlans.add(vv)
            except Exception:
                pass
            # Include VLANs already known by IPAM. Some SG350X firmware does not
            # expose the complete VLAN inventory through the standard VLAN table,
            # but still answers the FDB walk correctly with community@VLAN.
            vlan_list=sorted(set(observed_vlans)|discovered_vlans|known_db_vlans)
            # Collect independent VLAN contexts concurrently; merge in VLAN
            # order on this thread. No DB connection or shared mapping is used
            # by workers. Coverage, parsing and candidate scoring stay unchanged.
            from concurrent.futures import ThreadPoolExecutor
            def collect_vlan_context(vlan):
                ci=f"{community}@{vlan}"
                irc,iout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.2",ci)
                ist={}
                isrc,isout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.3",ci) if irc==0 and iout else (irc,"","")
                if isrc==0 and isout:
                    ist={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(isout)}
                got=collect_qbridge(irc,iout,ist,None,forced_vlan=int(vlan))
                vbp={}
                if got:
                    map_rc,map_out,_=_walk(d,target,".1.3.6.1.2.1.17.1.4.1.2",ci)
                    if map_rc==0 and map_out:
                        for suffix,val,_ in _parse_walk_oid_lines(map_out):
                            if suffix: vbp[int(suffix[-1])]=_parse_int_value(val)
                    names=[ifmap.get(vbp.get(int(b),0) or bpmap.get(int(b),0),"") for _,_,b in got]
                    logical_ratio=(sum(1 for n in names if _is_port_channel_name(n))/len(names)) if names else 0
                    if logical_ratio < 0.80:
                        return vlan,got,vbp,[]
                br=collect_bridge(ci)
                return vlan,got,vbp,[(int(vlan),mac,bp) for mac,bp in br]
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix="ipam-vlan") as vlan_pool:
                for vlan,got,vbp,br in vlan_pool.map(collect_vlan_context,vlan_list[:64]):
                    context_q.extend(got)
                    if vbp: bpmap_by_vlan[int(vlan)]=vbp
                    context_b.extend(br)

            def _candidate_score(cands):
                if not cands: return (-1,0,0,0)
                uniq={(int(v),str(m).lower()) for v,m,_ in cands}
                ports={(int(v),int(b)) for v,_,b in cands}
                physical=0; logical=0; mapped=0
                for v,m,b in cands:
                    ii=bpmap_by_vlan.get(int(v),{}).get(int(b),0) or bpmap.get(int(b),0)
                    name=ifmap.get(int(ii),"")
                    if ii: mapped += 1
                    if _is_physical_interface_name(name): physical += 1
                    if _is_port_channel_name(name): logical += 1
                # Prefer complete MAC coverage, then distinct ports and real
                # physical interfaces. Penalize a collapsed Port-Channel view.
                score=(len(uniq)*100 + len(ports)*10 + physical*6 + mapped - logical*8)
                return (score,len(uniq),physical,logical)

            candidates=[(generic_qbridge,"qbridge")]
            if context_q: candidates.append((context_q,"qbridge@vlan"))
            if context_b: candidates.append((context_b,"bridge@vlan"))
            best,method=max(candidates,key=lambda x:_candidate_score(x[0]))
            if best:
                entries=best
            else:
                entries=generic_qbridge; method="qbridge-fallback"

    # Nexus/other Cisco families. Nexus NX-OS can expose a valid Q-BRIDGE
    # table whose bridge-port mapping collapses physical MACs onto a logical
    # Port-Channel. For Nexus we therefore always sample the VLAN-scoped
    # BRIDGE/Q-BRIDGE views for the VLANs actually observed, then choose the
    # richest mapping. Other Cisco families keep the old conservative path.
    else:
        is_nexus=_device_is_nexus(d)
        if generic_qbridge:
            vlan_counts={}; port_counts={}
            for v,m,b in generic_qbridge:
                vlan_counts[v]=vlan_counts.get(v,0)+1
                port_counts[b]=port_counts.get(b,0)+1
            total=len(generic_qbridge)
            dominant_vlan=max(vlan_counts.values()) if vlan_counts else 0
            dominant_port=max(port_counts.values()) if port_counts else 0
            suspicious=(total>=12 and (dominant_vlan/total>=0.95 or dominant_port/total>=0.95))
        else:
            suspicious=True

        if is_nexus and ver in ("1","v1","2c","v2c","v2") and community and generic_qbridge:
            observed=sorted({int(v) for v,_,_ in generic_qbridge if 1<=int(v)<=4094})
            context_q=[]; context_b=[]
            for vlan in observed[:64]:
                ci=f"{community}@{vlan}"
                qvrc,qvout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.2",ci)
                qvst={}
                qsrc,qout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.3",ci) if qvrc==0 and qvout else (qvrc,"","")
                if qsrc==0 and qout:
                    qvst={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(qout)}
                got=collect_qbridge(qvrc,qvout,qvst,None,forced_vlan=int(vlan))
                if got:
                    context_q.extend(got)
                    map_rc,map_out,_=_walk(d,target,".1.3.6.1.2.1.17.1.4.1.2",ci)
                    if map_rc==0 and map_out:
                        vbp={}
                        for suffix,val,_ in _parse_walk_oid_lines(map_out):
                            if suffix: vbp[int(suffix[-1])]=_parse_int_value(val)
                        if vbp: bpmap_by_vlan[int(vlan)]=vbp
                    names=[ifmap.get(bpmap_by_vlan.get(int(vlan),{}).get(int(b),0) or bpmap.get(int(b),0),"") for _,_,b in got]
                    logical_ratio=(sum(1 for n in names if _is_port_channel_name(n))/len(names)) if names else 0
                    if logical_ratio < 0.80:
                        continue
                br=collect_bridge(ci)
                if br: context_b.extend((int(vlan),mac,bp) for mac,bp in br)

            def _nx_score(cands):
                if not cands: return -1
                uniq={(int(v),str(m).lower()) for v,m,_ in cands}
                ports={(int(v),int(b)) for v,_,b in cands}
                physical=0; logical=0; mapped=0
                for v,m,b in cands:
                    ii=bpmap_by_vlan.get(int(v),{}).get(int(b),0) or bpmap.get(int(b),0)
                    name=ifmap.get(int(ii),"")
                    if ii: mapped += 1
                    if _is_physical_interface_name(name): physical += 1
                    if _is_port_channel_name(name): logical += 1
                return len(uniq)*100 + len(ports)*10 + physical*8 + mapped - logical*10

            candidates=[(generic_qbridge,"qbridge")]
            if context_q: candidates.append((context_q,"qbridge@vlan"))
            if context_b: candidates.append((context_b,"bridge@vlan"))
            best,method=max(candidates,key=lambda x:_nx_score(x[0]))
            entries=best if best else generic_qbridge
        elif generic_qbridge:
            # Non-Nexus Cisco/other devices keep the proven global path unless
            # it is obviously collapsed.
            if suspicious and ver in ("1","v1","2c","v2c","v2") and community:
                observed=set(v for v,_,_ in generic_qbridge if 1<=int(v)<=4094)
                vlan_candidates=sorted(set(observed)|set(_discover_vlan_ids(d,target)))[:64]
                contextual=[]
                for vlan in vlan_candidates:
                    ci=f"{community}@{vlan}"
                    map_rc,map_out,_=_walk(d,target,".1.3.6.1.2.1.17.1.4.1.2",ci)
                    vbp={}
                    if map_rc==0 and map_out:
                        for suffix,val,_ in _parse_walk_oid_lines(map_out):
                            if suffix: vbp[int(suffix[-1])]=_parse_int_value(val)
                    if vbp: bpmap_by_vlan[int(vlan)]=vbp
                    qvrc,qvout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.2",ci)
                    qvst={}
                    qsrc,qout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.3",ci) if qvrc==0 and qvout else (qvrc,"","")
                    if qsrc==0 and qout:
                        qvst={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(qout)}
                    got=collect_qbridge(qvrc,qvout,qvst,None,forced_vlan=int(vlan))
                    if got: contextual.extend(got)
                    br=collect_bridge(ci)
                    if br: contextual.extend((int(vlan),mac,bp) for mac,bp in br)
                if contextual:
                    entries=contextual; method="qbridge@vlan-recovery"
                else:
                    entries=generic_qbridge; method="qbridge"
            else:
                entries=generic_qbridge; method="qbridge"
        if not entries:
            bridge=collect_bridge()
            if bridge:
                entries=[(0,mac,bp) for mac,bp in bridge]; method="bridge"

    # Historical Small Business fallback remains available if a device had no
    # generic/indexed data at all.
    if not entries and is_smb and ver in ("1","v1","2c","v2c","v2") and community:
        fallback_vlans=set(_discover_vlan_ids(d,target))
        try:
            for rr in c.execute("SELECT vlan FROM subnets WHERE TRIM(COALESCE(vlan,''))<>''").fetchall():
                vv=_parse_int_value(rr[0])
                if 1<=vv<=4094: fallback_vlans.add(vv)
        except Exception:
            pass
        for vlan in sorted(fallback_vlans)[:64]:
            ci=f"{community}@{vlan}"
            irc,iout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.2",ci)
            ist={}
            isrc,isout,_=_walk(d,target,".1.3.6.1.2.1.17.7.1.2.2.1.3",ci) if irc==0 and iout else (irc,"","")
            if isrc==0 and isout:
                ist={suffix:_parse_int_value(val) for suffix,val,_ in _parse_walk_oid_lines(isout)}
            got=collect_qbridge(irc,iout,ist,None,forced_vlan=int(vlan))
            if got:
                entries.extend(got); method="qbridge@vlan"; continue
            br=collect_bridge(ci)
            if br:
                entries.extend((int(vlan),mac,bp) for mac,bp in br); method="bridge@vlan"

    # Current FDB remains ephemeral: it represents what the switch reported in
    # this discovery. The historical table is append/update-only and is NEVER
    # cleared when a MAC disappears from the switch FDB.
    #
    # V14.7 IMPORTANT: do NOT classify a port while the old FDB is still in the
    # database. The previous implementation calculated fanout before the new
    # FDB rows were inserted, so stale rows from the previous discovery could
    # make a normal Access port look like an Uplink. That also made Endpoint
    # History disagree with the IP Address resolver.
    #
    # Phase 1: replace the current FDB with RAW observations only.
    c.execute("DELETE FROM network_mac_entries WHERE device_id=?",(d["id"],))
    raw_rows=[]; seen=set()
    for vlan,mac,bp in entries:
        ifidx=bpmap_by_vlan.get(int(vlan or 0),{}).get(int(bp),0)
        if not ifidx: ifidx=bpmap.get(bp,0)
        if not ifidx and bp in ifmap: ifidx=bp
        ifname=ifmap.get(ifidx,"")
        key=(int(vlan or 0),str(mac or '').lower())
        if key in seen: continue
        seen.add(key)
        raw_rows.append((int(vlan or 0),mac,int(bp or 0),int(ifidx or 0),ifname))

    for vlan,mac,bp,ifidx,ifname in raw_rows:
        # Provisional type is deliberately neutral. The final role is computed
        # only after the complete current FDB has been inserted.
        provisional='lag' if _is_port_channel_name(ifname) else ('access' if _is_physical_interface_name(ifname) else 'unknown')
        provisional_conf=90 if _is_physical_interface_name(ifname) else (70 if ifname else 40)
        c.execute("INSERT OR IGNORE INTO network_mac_entries(device_id,vlan_id,mac,bridge_port,if_index,if_name,status,port_type,confidence) VALUES(?,?,?,?,?,?,?,?,?)",
                  (d['id'],vlan,mac,bp,ifidx,ifname,"learned",provisional,provisional_conf))

    # Phase 2: classify from the COMPLETE current FDB + current LLDP/CDP +
    # interface metadata. This is the same evidence used by IP Address.
    # History receives only the authoritative final classification.
    count=0
    # The complete FDB and interface/neighbor evidence are fixed in this phase.
    # Resolve each interface once, rather than once for every learned MAC.
    interface_contexts={}
    for vlan,mac,bp,ifidx,ifname in raw_rows:
        context_key=(ifidx,ifname)
        if context_key not in interface_contexts:
            interface_contexts[context_key]=_interface_context(c,d['id'],ifidx,ifname)
        ctx=interface_contexts[context_key]
        probable=bool(ctx.get('probable_uplink'))
        role=str(ctx.get('role') or 'unknown').lower()
        if role not in ('access','uplink'):
            role='access' if _is_physical_interface_name(ifname) and not probable else 'unknown'
        ptype='uplink' if probable else ('access' if role=='access' else 'unknown')
        # Match the IP Address resolver's confidence basis without changing
        # its existing scoring rules.
        conf=30 if probable else (90 if role=='access' else 40)
        if ifidx and ifidx in ifmap and ifname: conf=min(100,conf+5)
        c.execute("UPDATE network_mac_entries SET port_type=?,confidence=? WHERE device_id=? AND vlan_id=? AND mac=? AND if_index=?",
                  (ptype,int(conf),d['id'],vlan,mac,ifidx))

        # Endpoint History stores both Access and explicit Uplink observations
        # when they are genuinely classified as such. It never uses a stale
        # previous-FDB fanout value to make the decision.
        prev=c.execute("""SELECT id,if_index,if_name,last_seen FROM network_mac_port_history
                         WHERE device_id=? AND vlan_id=? AND mac COLLATE NOCASE=?
                         ORDER BY last_seen DESC,id DESC LIMIT 1""",
                       (d["id"],vlan,mac.lower())).fetchone()
        if prev and int(prev["if_index"] or 0)!=int(ifidx or 0) and str(prev["if_name"] or "")!=str(ifname or ""):
            audit("MAC_PORT_MOVE",mac,
                  f"Device={d['name']}; VLAN={vlan}; {prev['if_name'] or prev['if_index'] or '—'} → {ifname or ifidx or '—'}; previous_seen={prev['last_seen'] or '—'}",
                  conn=c)
        hist=c.execute("""SELECT id FROM network_mac_port_history
                         WHERE device_id=? AND vlan_id=? AND mac COLLATE NOCASE=? AND if_index=?
                         ORDER BY id DESC LIMIT 1""",
                       (d["id"],vlan,mac.lower(),ifidx)).fetchone()
        if hist:
            c.execute("""UPDATE network_mac_port_history
                         SET if_name=?,bridge_port=?,port_type=?,confidence=?,last_seen=CURRENT_TIMESTAMP,seen_count=seen_count+1
                         WHERE id=?""",(ifname,bp,ptype,int(conf),int(hist["id"])))
        else:
            c.execute("""INSERT INTO network_mac_port_history
                         (device_id,vlan_id,mac,bridge_port,if_index,if_name,port_type,confidence)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (d["id"],vlan,mac,bp,ifidx,ifname,ptype,int(conf)))
        count+=1

    return count,(method or "none")

def _discover_arp_table(c,d,target):
    """Discover IPv4 ARP with RFC1213 and Cisco Small Business VLAN-indexed fallback."""
    ifmap={int(r["if_index"]):r["if_name"] for r in c.execute("SELECT if_index,if_name FROM network_interfaces WHERE device_id=?",(d["id"],))}
    entries=[]

    def collect(raw, type_raw=""):
        got=[]; types={}
        if type_raw:
            for suffix,val,_ in _parse_walk_oid_lines(type_raw):
                if suffix: types[tuple(suffix[-5:])]=val
        for suffix,val,rawline in _parse_walk_oid_lines(raw or ""):
            if len(suffix)<5: continue
            idx=suffix[-5]; ip_parts=suffix[-4:]
            if not all(0<=x<=255 for x in ip_parts): continue
            ip='.'.join(str(x) for x in ip_parts); mac=_normalize_mac(val)
            if not mac or _is_non_endpoint_mac(mac): continue
            got.append((idx,ip,mac,types.get(tuple(suffix[-5:]),"")))
        return got

    rc,out,err=_walk(d,target,".1.3.6.1.2.1.4.22.1.2")
    trc,tout,_=_walk(d,target,".1.3.6.1.2.1.4.22.1.4")
    if rc==0 and out: entries=collect(out,tout if trc==0 else "")
    method="rfc1213" if entries else ""

    # Modern IP-MIB fallback used by newer Cisco IOS images.
    if not entries:
        prc,pout,_=_walk(d,target,".1.3.6.1.2.1.4.35.1.4")  # ipNetToPhysicalPhysAddress
        if prc==0 and pout:
            # RFC4293 index is address family + interface + address type; the
            # IPv4 address is the final four index components.
            for suffix,val,_ in _parse_walk_oid_lines(pout):
                if len(suffix)<4: continue
                ip_parts=suffix[-4:]
                if not all(0<=x<=255 for x in ip_parts): continue
                mac=_normalize_mac(val)
                if not mac or _is_non_endpoint_mac(mac): continue
                idx=0
                # Most agents place ifIndex immediately before the IPv4 octets.
                if len(suffix)>=5: idx=int(suffix[-5])
                entries.append((idx,'.'.join(str(x) for x in ip_parts),mac,""))
            if entries: method="ipNetToPhysical"

    # SG300/SG350 can require community@VLAN for the IP-MIB ARP view. Cisco
    # documents ipNetToMediaPhysAddress as the standard ARP object; this retry
    # only runs when the normal walk is empty.
    if not entries and _device_is_smallbiz(d) and str(d["version"]).lower() in ("1","v1","2c","v2c","v2"):
        community=str(d["community"] or "").strip()
        for vlan in _discover_vlan_ids(d,target):
            indexed=f"{community}@{vlan}"
            irc,iout,_=_walk(d,target,".1.3.6.1.2.1.4.22.1.2",indexed)
            itrc,itout,_=_walk(d,target,".1.3.6.1.2.1.4.22.1.4",indexed)
            got=collect(iout,itout if itrc==0 else "") if irc==0 and iout else []
            if got:
                entries.extend(got); method="rfc1213@vlan"

    c.execute("DELETE FROM network_arp_entries WHERE device_id=?",(d["id"],))
    seen=set(); count=0
    for idx,ip,mac,atype in entries:
        key=(int(idx or 0),ip)
        if key in seen: continue
        seen.add(key)
        c.execute("INSERT OR IGNORE INTO network_arp_entries(device_id,if_index,if_name,ip,mac,arp_type) VALUES(?,?,?,?,?,?)",(d["id"],idx,ifmap.get(idx,""),ip,mac,atype))
        count+=1
    return count, (method or (err or "ARP table không trả dữ liệu"))

def _discover_device_identity(c,d,target):
    """Best-effort stable device identity. Never fails a discovery if an OID is absent."""
    chassis=""
    # dot1dBaseBridgeAddress is widely available on Cisco L2/L3 switches.
    rc,out,_=_run_snmp(d,target,".1.3.6.1.2.1.17.1.1.0")
    if rc==0 and out:
        chassis=_normalize_mac(_snmp_value(out))
    if not chassis:
        rc,out,_=_run_snmp(d,target,".1.0.8802.1.1.2.1.3.2.0")
        if rc==0 and out:
            chassis=_snmp_value(out)
    serial=""
    # ENTITY-MIB serials are indexed by physical entities; select the first
    # non-empty serial exposed by the chassis/agent. This is enrichment only.
    rc,out,_=_walk(d,target,".1.3.6.1.2.1.47.1.1.1.1.11")
    if rc==0:
        for _,val,_raw in _parse_walk_lines(out):
            v=str(val or "").strip()
            if v and v.lower() not in ("n/a","unknown","none"):
                serial=v[:255]; break
    return chassis,serial

def _quick_discovery_signature(d, target, results):
    """Cheap change detector used by Auto/Discover All.

    Interface names/state and CDP/LLDP detect topology changes only.
    Endpoint MAC/ARP tables must still refresh when this signature is unchanged:
    hosts can move, wake up or age out without any interface/topology change.
    """
    parts=[('neighbor_parser','v2-indexed-columns')]
    for label, oid in (
        ("ifname", ".1.3.6.1.2.1.31.1.1.1.1"),
        ("ifoper", ".1.3.6.1.2.1.2.2.1.8"),
        ("cdp", ".1.3.6.1.4.1.9.9.23.1.2.1.1.6"),
        ("lldp_ch", ".1.0.8802.1.1.2.1.4.1.1.5"),
        ("lldp_port", ".1.0.8802.1.1.2.1.4.1.1.7"),
    ):
        rc,out,_=_walk(d,target,oid)
        if rc==0:
            vals=[]
            for suf,val,_raw in _parse_walk_oid_lines(out):
                vals.append((tuple(suf),str(val or "").strip()))
            parts.append((label,vals))
        else:
            parts.append((label,["SNMP_ERROR"]))
    payload=json.dumps({"system":results.get("sys_name","")+"|"+results.get("sys_object_id","")+"|"+results.get("sys_descr","")[:160],"parts":parts},sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8","ignore")).hexdigest()


def _discover_device_sync(device_id, force_full=False, smart=True):
    c=db(); d=c.execute("SELECT d.*,p.id AS profile_id,p.name AS profile_name,p.version,p.community,p.username,p.security_level,p.auth_protocol,p.auth_password,p.priv_protocol,p.priv_password,p.port,p.timeout,p.retries FROM network_devices d JOIN snmp_profiles p ON p.id=d.snmp_profile_id WHERE d.id=?",(device_id,)).fetchone()
    if not d: c.close(); raise ValueError("Device không tồn tại")
    # Materialize sqlite3.Row to a plain dict before passing the device
    # through discovery helpers. Several helpers intentionally use mapping
    # .get() for optional fields; sqlite3.Row has no .get(). This is a
    # compatibility fix only and does not alter the database schema/data.
    d = dict(d)
    try: ipaddress.ip_address(d["management_ip"])
    except ValueError: c.close(); raise ValueError("Management IP không hợp lệ")
    target=d["management_ip"]
    results={}; errors=[]
    system_oids=(("sys_name",".1.3.6.1.2.1.1.5.0"),) if (smart and not force_full) else (("sys_name",".1.3.6.1.2.1.1.5.0"),("sys_descr",".1.3.6.1.2.1.1.1.0"),("sys_object_id",".1.3.6.1.2.1.1.2.0"),("sys_uptime",".1.3.6.1.2.1.1.3.0"))
    for key,oid in system_oids:
        rc,out,err=_run_snmp(d,target,oid)
        if rc==0 and out: results[key]=_snmp_value(out)
        else: errors.append(f"{key}: {err or out or 'SNMP error'}")
    if not results:
        c.execute("UPDATE network_devices SET status='down',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",("; ".join(errors)[:1000],device_id)); c.commit(); c.close(); raise ValueError("SNMP không phản hồi: "+("; ".join(errors)[:700]))
    # Use live SNMP identity for the discovery driver. This is important for
    # SW-VP87-L5: it is a Catalyst C2960G-24TC-L, not an SG350X. The live
    # identity must therefore select the Catalyst FDB path even if an older
    # inventory record contains a stale/manual device_type.
    for _k in ("sys_name","sys_descr","sys_object_id","sys_uptime"):
        if results.get(_k):
            d[_k] = results[_k]
    signature=_quick_discovery_signature(d,target,results) if smart else ""
    old_sig=str(d["discovery_signature"] or "").strip() if "discovery_signature" in d.keys() else ""
    if smart and not force_full and old_sig and signature==old_sig:
        _discover_vpc(c,d,target)
        trunk_modes=_discover_cisco_trunk_modes(c,d,target)
        # Keep cached interface/topology metadata, but never skip endpoint data.
        mac_count,mac_method=_discover_mac_table(c,d,target)
        arp_count,arp_state=_discover_arp_table(c,d,target)
        partial=[]
        if trunk_modes is None and (_device_is_nexus(d) or _device_is_cisco_catalyst(d) or _device_is_smallbiz(d)):
            partial.append("Cisco SNMP port-mode table unavailable; previous port-mode snapshot retained")
        if not mac_count: partial.append("MAC table không có dữ liệu hoặc thiết bị không expose Q-BRIDGE/BRIDGE FDB")
        if not arp_count: partial.append("ARP table không có dữ liệu")
        c.execute("UPDATE network_devices SET status='up',last_discovery=CURRENT_TIMESTAMP,last_error=?,sys_name=?,sys_descr=?,sys_object_id=?,sys_uptime=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",("; ".join(errors+partial)[:1000],results.get("sys_name",d["sys_name"]),results.get("sys_descr",d["sys_descr"]),results.get("sys_object_id",d["sys_object_id"]),results.get("sys_uptime",d["sys_uptime"]),device_id))
        c.execute("INSERT INTO discovery_jobs(device_id,state,finished_at,detail) VALUES(?,?,CURRENT_TIMESTAMP,?)",(device_id,"done",f"smart endpoint refresh; topology unchanged; mac={mac_count}; arp={arp_count}; mac_method={mac_method}"))
        c.commit(); c.close()
        _invalidate_ip_location_cache()
        audit("DISCOVERY_SMART_SKIP",target,f"{d['name']}; topology unchanged; mac={mac_count}; arp={arp_count}")
        return {"ok":True,"device_id":device_id,"system":results,"interfaces":int(d["interface_count"] or 0),"interfaces_up":int(d["interface_up_count"] or 0),"neighbors":0,"mac_entries":mac_count,"arp_entries":arp_count,"port_modes_trunking":trunk_modes.get('trunking',0),"partial_errors":partial,"smart_skipped":True,"endpoint_refreshed":True}
    interfaces,up=_discover_interfaces(c,d,target)
    trunk_modes=_discover_cisco_trunk_modes(c,d,target)
    chassis_id,serial_number=_discover_device_identity(c,d,target)
    neighbor_count=_discover_neighbors(c,d,target)
    _discover_vpc(c,d,target)
    # Persist a complete, successful IF-MIB stack snapshot. Unsupported or
    # timed-out walks leave the previous LAG membership evidence untouched.
    try:
        _discover_lag_members(c,d,target,persist=True)
    except Exception as lag_error:
        errors.append("LAG membership unavailable: "+str(lag_error)[:180])
    mac_count,mac_method=_discover_mac_table(c,d,target)
    arp_count,arp_state=_discover_arp_table(c,d,target)
    partial=[]
    if trunk_modes is None and (_device_is_nexus(d) or _device_is_cisco_catalyst(d) or _device_is_smallbiz(d)):
        partial.append("Cisco SNMP port-mode table unavailable; previous port-mode snapshot retained")
    if not mac_count: partial.append("MAC table không có dữ liệu hoặc thiết bị không expose Q-BRIDGE/BRIDGE FDB")
    if not arp_count: partial.append("ARP table không có dữ liệu")
    errtext="; ".join(errors+partial)[:1000]
    c.execute("UPDATE network_devices SET status='up',last_discovery=CURRENT_TIMESTAMP,last_error=?,sys_name=?,sys_descr=?,sys_object_id=?,sys_uptime=?,interface_count=?,interface_up_count=?,chassis_id=COALESCE(NULLIF(?,''),chassis_id),serial_number=COALESCE(NULLIF(?,''),serial_number),discovery_signature=?,last_full_discovery=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(errtext,results.get("sys_name",d["sys_name"]),results.get("sys_descr",d["sys_descr"]),results.get("sys_object_id",d["sys_object_id"]),results.get("sys_uptime",d["sys_uptime"]),interfaces,up,chassis_id,serial_number,signature if smart else str(d["discovery_signature"] or ""),device_id))
    c.execute("INSERT INTO discovery_jobs(device_id,state,finished_at,detail) VALUES(?,?,CURRENT_TIMESTAMP,?)",(device_id,"done",f"system OK; interfaces={interfaces}; up={up}; neighbors={neighbor_count}; mac={mac_count}; arp={arp_count}; mac_method={mac_method}"))
    c.commit(); c.close()
    # Discovery replaced the FDB/ARP snapshot, so any per-IP correlation
    # cached from the previous snapshot must be discarded immediately.
    _invalidate_ip_location_cache()
    audit("DISCOVERY",target,f"{d['name']}; profile={d['version']}; interfaces={interfaces}; up={up}; mac={mac_count}; arp={arp_count}")
    return {"ok":True,"device_id":device_id,"system":results,"interfaces":interfaces,"interfaces_up":up,"neighbors":neighbor_count,"mac_entries":mac_count,"arp_entries":arp_count,"port_modes_trunking":(trunk_modes or {}).get('trunking',0),"partial_errors":partial}

@app.get("/api/discovery/profiles")
def discovery_profiles():
    c=db(); rows=c.execute("SELECT id,name,version,community,username,security_level,auth_protocol,priv_protocol,port,timeout,retries,created_at,updated_at FROM snmp_profiles ORDER BY name").fetchall(); c.close(); return [dict(r) for r in rows]

@app.post("/api/discovery/profiles")
def discovery_profile_create(x:SNMPProfileIn):
    v=str(x.version).lower().replace("v","")
    if v not in ("1","2c","3"): raise HTTPException(400,"SNMP Version phải là v1, v2c hoặc v3")
    if not x.name.strip(): raise HTTPException(400,"Tên Profile không được trống")
    if v in ("1","2c") and not x.community.strip(): raise HTTPException(400,"Community không được trống")
    if v=="3" and not x.username.strip(): raise HTTPException(400,"Username SNMPv3 không được trống")
    if v=="3" and x.security_level not in ("noAuthNoPriv","authNoPriv","authPriv"): raise HTTPException(400,"Security Level SNMPv3 không hợp lệ")
    if v=="3" and x.auth_protocol.upper() not in ("SHA","SHA1","SHA-1","SHA256","SHA-256","SHA512","SHA-512","MD5"): raise HTTPException(400,"Auth Protocol SNMPv3 không được hỗ trợ")
    if v=="3" and x.priv_protocol.upper() not in ("AES","AES128","AES-128","DES"): raise HTTPException(400,"Privacy Protocol SNMPv3 không được hỗ trợ")
    c=db()
    try:
        cur=c.execute("INSERT INTO snmp_profiles(name,version,community,username,security_level,auth_protocol,auth_password,priv_protocol,priv_password,port,timeout,retries) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(x.name.strip(),v,x.community,x.username,x.security_level,x.auth_protocol,x.auth_password,x.priv_protocol,x.priv_password,max(1,min(x.port,65535)),max(1,min(x.timeout,30)),max(0,min(x.retries,5))))
        c.commit(); pid=cur.fetchone()[0]
    except DBIntegrityError: c.close(); raise HTTPException(400,"Tên SNMP Profile đã tồn tại")
    c.close(); audit("DISCOVERY_PROFILE_CREATE",x.name.strip(),f"SNMP {v}"); return {"id":pid}

@app.put("/api/discovery/profiles/{pid}")
def discovery_profile_update(pid:int,x:SNMPProfileIn):
    c=db(); old=c.execute("SELECT * FROM snmp_profiles WHERE id=?",(pid,)).fetchone()
    if not old: c.close(); raise HTTPException(404,"SNMP Profile không tồn tại")
    v=str(x.version).lower().replace("v","")
    if v not in ("1","2c","3"): c.close(); raise HTTPException(400,"SNMP Version không hợp lệ")
    if v in ("1","2c") and not (str(x.community or "").strip() or str(old["community"] or "").strip()):
        c.close(); raise HTTPException(400,"Community không được trống")
    if v=="3" and not x.username.strip(): c.close(); raise HTTPException(400,"Username SNMPv3 không được trống")
    # Empty secrets on edit preserve existing credentials.
    community=x.community if x.community else old["community"]
    authp=x.auth_password if x.auth_password else old["auth_password"]
    privp=x.priv_password if x.priv_password else old["priv_password"]
    c.execute("UPDATE snmp_profiles SET name=?,version=?,community=?,username=?,security_level=?,auth_protocol=?,auth_password=?,priv_protocol=?,priv_password=?,port=?,timeout=?,retries=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(x.name.strip(),v,community,x.username,x.security_level,x.auth_protocol,authp,x.priv_protocol,privp,max(1,min(x.port,65535)),max(1,min(x.timeout,30)),max(0,min(x.retries,5)),pid))
    c.commit(); c.close(); audit("DISCOVERY_PROFILE_EDIT",x.name.strip(),f"SNMP {v}"); return {"ok":True}

@app.delete("/api/discovery/profiles/{pid}")
def discovery_profile_delete(pid:int):
    c=db(); used=c.execute("SELECT COUNT(*) n FROM network_devices WHERE snmp_profile_id=?",(pid,)).fetchone()[0]
    if used: c.close(); raise HTTPException(400,f"Profile đang được dùng bởi {used} device")
    r=c.execute("SELECT name FROM snmp_profiles WHERE id=?",(pid,)).fetchone()
    if not r: c.close(); raise HTTPException(404,"Profile không tồn tại")
    c.execute("DELETE FROM snmp_profiles WHERE id=?",(pid,)); c.commit(); c.close(); audit("DISCOVERY_PROFILE_DELETE",r["name"],"Xóa SNMP Profile"); return {"ok":True}

@app.get("/api/discovery/devices")
def discovery_devices():
    c=db(); rows=[dict(r) for r in c.execute("SELECT d.*,p.name profile_name,p.version profile_version FROM network_devices d JOIN snmp_profiles p ON p.id=d.snmp_profile_id ORDER BY d.name").fetchall()]; c.close()
    for r in rows:
        r["last_discovery_local"]=_local_time(r.get("last_discovery"))
        r["updated_at_local"]=_local_time(r.get("updated_at"))
    return rows

@app.post("/api/discovery/devices/replace")
def discovery_device_replace(x:NetworkDeviceIn):
    """Safely replace an existing Device definition without touching IPAM/DHCP data.

    This is intended for a switch that was physically replaced while reusing the
    same management IP/name. Discovery-derived rows are purged before the new
    definition is inserted, preventing the old switch from continuing to appear
    in Switch/Port filters or topology.
    """
    try: ipaddress.ip_address(x.management_ip.strip())
    except: raise HTTPException(400,"Management IP không hợp lệ")
    name=x.name.strip(); mgmt_ip=x.management_ip.strip()
    if not name: raise HTTPException(400,"Device Name không được để trống")
    c=db()
    if not c.execute("SELECT id FROM snmp_profiles WHERE id=?",(x.snmp_profile_id,)).fetchone():
        c.close(); raise HTTPException(400,"SNMP Profile không tồn tại")
    ids={int(r[0]) for r in c.execute("SELECT id FROM network_devices WHERE lower(name)=lower(?) OR management_ip=?",(name,mgmt_ip)).fetchall()}
    for did in ids:
        for table in ("network_neighbors","network_interfaces","network_mac_entries","network_mac_port_history","network_arp_entries","topology_positions"):
            c.execute(f"DELETE FROM {table} WHERE device_id=?",(did,))
        c.execute("DELETE FROM topology_manual_links WHERE source_device_id=? OR target_device_id=?",(did,did))
        c.execute("DELETE FROM discovery_jobs WHERE device_id=?",(did,))
        c.execute("DELETE FROM network_devices WHERE id=?",(did,))
    interval=max(1,min(int(x.discovery_interval_minutes),1440))
    try:
        cur=c.execute("INSERT INTO network_devices(name,management_ip,vendor,device_type,snmp_profile_id,auto_discovery_enabled,discovery_interval_minutes) VALUES(?,?,?,?,?,?,?) RETURNING id",(name,mgmt_ip,x.vendor.strip(),x.device_type.strip(),x.snmp_profile_id,1 if x.auto_discovery_enabled else 0,interval))
        c.commit(); did=cur.fetchone()[0]
    except DBIntegrityError as e:
        c.rollback(); c.close(); raise HTTPException(400,"Không thể thay thế Device: dữ liệu bị trùng hoặc không hợp lệ")
    c.close(); audit("DISCOVERY_DEVICE_REPLACE",mgmt_ip,f"{name}; replaced={sorted(ids)}; profile={x.snmp_profile_id}")
    return {"ok":True,"id":did,"replaced_device_ids":sorted(ids)}

@app.post("/api/discovery/devices")
def discovery_device_create(x:NetworkDeviceIn, replace_existing:int=1):
    """Create a managed Discovery device.

    CREATE is intentionally idempotent for name/management IP: if an old
    Discovery Device owns either value, it is replaced automatically. This
    prevents a stale DB row from blocking creation after a switch replacement.
    Only Discovery/Topology-owned data is purged; IPAM/DHCP data is untouched.
    """
    try: ipaddress.ip_address(x.management_ip.strip())
    except: raise HTTPException(400,"Management IP không hợp lệ")
    name=x.name.strip(); mgmt_ip=x.management_ip.strip()
    if not name: raise HTTPException(400,"Device Name không được để trống")
    c=db()
    if not c.execute("SELECT id FROM snmp_profiles WHERE id=?",(x.snmp_profile_id,)).fetchone():
        c.close(); raise HTTPException(400,"SNMP Profile không tồn tại")
    conflicts=[dict(r) for r in c.execute("SELECT id,name,management_ip,status FROM network_devices WHERE lower(name)=lower(?) OR management_ip=? ORDER BY id",(name,mgmt_ip)).fetchall()]
    replaced=[]
    for old in conflicts:
        did=int(old["id"])
        for table in ("network_neighbors","network_interfaces","network_mac_entries","network_mac_port_history","network_arp_entries","topology_positions","discovery_jobs"):
            c.execute(f"DELETE FROM {table} WHERE device_id=?",(did,))
        c.execute("DELETE FROM topology_manual_links WHERE source_device_id=? OR target_device_id=?",(did,did))
        c.execute("DELETE FROM network_devices WHERE id=?",(did,))
        replaced.append(old)
    # Defensive cleanup: no orphan topology/discovery rows may survive a replace.
    for table in ("network_neighbors","network_interfaces","network_mac_entries","network_mac_port_history","network_arp_entries","topology_positions","discovery_jobs"):
        c.execute(f"DELETE FROM {table} WHERE device_id IS NOT NULL AND device_id NOT IN (SELECT id FROM network_devices)")
    c.execute("DELETE FROM topology_manual_links WHERE source_device_id NOT IN (SELECT id FROM network_devices) OR target_device_id NOT IN (SELECT id FROM network_devices)")
    try:
        interval=max(1,min(int(x.discovery_interval_minutes),1440))
        cur=c.execute("INSERT INTO network_devices(name,management_ip,vendor,device_type,snmp_profile_id,auto_discovery_enabled,discovery_interval_minutes) VALUES(?,?,?,?,?,?,?) RETURNING id",(name,mgmt_ip,x.vendor.strip(),x.device_type.strip(),x.snmp_profile_id,1 if x.auto_discovery_enabled else 0,interval))
        c.commit(); did=cur.fetchone()[0]
    except DBIntegrityError as e:
        c.rollback(); c.close(); raise HTTPException(400,"Không thể tạo Device: dữ liệu bị trùng hoặc không hợp lệ")
    c.close()
    audit("DISCOVERY_DEVICE_CREATE",mgmt_ip,f"{name}; profile={x.snmp_profile_id}; replaced={[(r['id'],r['name'],r['management_ip']) for r in replaced]}")
    return {"ok":True,"id":did,"replaced_device_ids":[r["id"] for r in replaced],"replaced":replaced}

@app.put("/api/discovery/devices/{did}")
def discovery_device_update(did:int,x:NetworkDeviceIn):
    c=db(); old=c.execute("SELECT * FROM network_devices WHERE id=?",(did,)).fetchone()
    if not old: c.close(); raise HTTPException(404,"Device không tồn tại")
    try: ipaddress.ip_address(x.management_ip.strip())
    except: c.close(); raise HTTPException(400,"Management IP không hợp lệ")
    interval=max(1,min(int(x.discovery_interval_minutes),1440))
    c.execute("UPDATE network_devices SET name=?,management_ip=?,vendor=?,device_type=?,snmp_profile_id=?,auto_discovery_enabled=?,discovery_interval_minutes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(x.name.strip(),x.management_ip.strip(),x.vendor.strip(),x.device_type.strip(),x.snmp_profile_id,1 if x.auto_discovery_enabled else 0,interval,did)); c.commit(); c.close(); audit("DISCOVERY_DEVICE_EDIT",x.management_ip,f"{x.name}; auto={1 if x.auto_discovery_enabled else 0}; interval={interval}m"); return {"ok":True}

@app.delete("/api/discovery/devices/{did}")
def discovery_device_delete(did:int, name:str="", management_ip:str=""):
    """Delete a Network Device and purge every Discovery-owned record.

    The optional name/IP are supplied by the UI so a stale browser row can
    still delete the real device if its numeric ID changed after a replace.
    This endpoint is intentionally idempotent for an already-removed ID.
    """
    c=db()
    selectors=["id=?"]; args=[did]
    if name.strip(): selectors.append("lower(name)=lower(?)"); args.append(name.strip())
    if management_ip.strip(): selectors.append("management_ip=?"); args.append(management_ip.strip())
    rows=c.execute("SELECT id,name,management_ip FROM network_devices WHERE "+" OR ".join(selectors),tuple(args)).fetchall()
    ids={int(r["id"]) for r in rows}
    if not ids:
        # Idempotent delete: the UI can safely retry after a previous delete.
        c.close(); return {"ok":True,"deleted_device_ids":[],"already_deleted":True}
    deleted=[]
    for target_id in sorted(ids):
        r=c.execute("SELECT name,management_ip FROM network_devices WHERE id=?",(target_id,)).fetchone()
        if not r: continue
        for table in ("network_neighbors","network_interfaces","network_mac_entries","network_mac_port_history","network_arp_entries","topology_positions","discovery_jobs"):
            c.execute(f"DELETE FROM {table} WHERE device_id=?",(target_id,))
        c.execute("DELETE FROM topology_manual_links WHERE source_device_id=? OR target_device_id=?",(target_id,target_id))
        c.execute("DELETE FROM network_devices WHERE id=?",(target_id,))
        deleted.append({"id":target_id,"name":r["name"],"management_ip":r["management_ip"]})
    c.commit()
    # Verify the primary rows are really gone before returning success.
    remaining=[int(r["id"]) for r in c.execute("SELECT id FROM network_devices WHERE id IN (%s)" % ",".join("?"*len(ids)),tuple(ids)).fetchall()] if ids else []
    c.close()
    if remaining:
        raise HTTPException(500,"Không thể xóa triệt để Network Device; database vẫn còn Device ID "+",".join(map(str,remaining)))
    for r in deleted:
        audit("DISCOVERY_DEVICE_DELETE",r["management_ip"],r["name"])
    return {"ok":True,"deleted_device_ids":[r["id"] for r in deleted],"deleted":deleted,"already_deleted":False}

@app.post("/api/discovery/devices/{did}/test")
def discovery_device_test(did:int):
    c=db(); d=c.execute("SELECT d.*,p.id AS profile_id,p.name AS profile_name,p.version,p.community,p.username,p.security_level,p.auth_protocol,p.auth_password,p.priv_protocol,p.priv_password,p.port,p.timeout,p.retries FROM network_devices d JOIN snmp_profiles p ON p.id=d.snmp_profile_id WHERE d.id=?",(did,)).fetchone(); c.close()
    if not d: raise HTTPException(404,"Device không tồn tại")
    rc,out,err=_run_snmp(d,d["management_ip"],".1.3.6.1.2.1.1.5.0")
    if rc!=0:
        detail=(err or out or "unknown error").strip()
        hint=""
        ver=str(d["version"]).lower()
        if "unknown user name" in detail.lower():
            hint="; SNMPv3: kiểm tra đúng Username + Security Level + Auth/Privacy trên thiết bị; Sophos cần bật Accept queries cho user"
        elif "timeout" in detail.lower() or rc==124:
            hint="; kiểm tra UDP/161, Device Access/zone trên Sophos và IP nguồn của IPAM có được phép query"
        elif ver in ("1","v1","2c","v2c","v2") and ("authentication failure" in detail.lower() or "no such" in detail.lower()):
            hint="; SNMPv1/v2c: kiểm tra Community và mục Accept queries/manager IP trên Sophos"
        audit("DISCOVERY_SNMP_TEST_ERROR",d["management_ip"],detail+hint)
        raise HTTPException(400,"SNMP Test thất bại: "+detail+hint)
    val=_snmp_value(out); audit("DISCOVERY_SNMP_TEST",d["management_ip"],f"sysName={val}"); return {"ok":True,"sys_name":val,"version":d["version"]}

_discover_all_jobs={}
_discover_all_jobs_lock=threading.RLock()


def _run_discover_all_job(job_id, ids):
    total=len(ids)
    with _discover_all_jobs_lock:
        _discover_all_jobs[job_id]={"job_id":job_id,"state":"running","total":total,"current":0,"done":0,"errors":0,"busy":0,"current_device_id":None,"current_device_name":"","results":[]}
    # V75.2.50.17: Discover All uses a small worker pool instead of a single
    # serial worker.  Per-device locks are still respected, but a device that
    # is temporarily busy is waited on rather than being counted as a permanent
    # failure. This makes large Discover All runs much faster and prevents the
    # UI from ending at e.g. 19/21 simply because Auto Discovery overlapped.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def run_one(pos_did):
        pos,did=pos_did
        c=db(); row=c.execute("SELECT name FROM network_devices WHERE id=?",(did,)).fetchone(); c.close()
        name=str(row["name"] if row else did)
        with _discover_all_jobs_lock:
            j=_discover_all_jobs[job_id]; j.update({"current":pos,"current_device_id":did,"current_device_name":name})
        lock=_get_discovery_lock(did)
        # Wait briefly for an overlapping Auto Discovery cycle. We do not mark
        # it as a failed/busy device merely because another scheduler tick won
        # the lock first.
        acquired=lock.acquire(timeout=max(30,int(os.getenv("DISCOVERY_ALL_LOCK_WAIT", "180"))))
        if not acquired:
            return {"device_id":did,"state":"busy","error":"Device vẫn đang được Discovery xử lý sau thời gian chờ"}
        try:
            r=_discover_device_sync(did,False,True)
            return {"device_id":did,"state":"done","interfaces":r.get("interfaces",0),"interfaces_up":r.get("interfaces_up",0),"neighbors":r.get("neighbors",0),"mac_entries":r.get("mac_entries",0),"arp_entries":r.get("arp_entries",0),"smart_skipped":bool(r.get("smart_skipped")),"partial_errors":r.get("partial_errors",[])}
        except Exception as e:
            # One bounded retry handles transient SNMP timeouts/connection
            # hiccups without creating an unbounded Discovery loop.
            first_error=str(e)[:900]
            try:
                time.sleep(0.35)
                r=_discover_device_sync(did,False,True)
                return {"device_id":did,"state":"done","interfaces":r.get("interfaces",0),"interfaces_up":r.get("interfaces_up",0),"neighbors":r.get("neighbors",0),"mac_entries":r.get("mac_entries",0),"arp_entries":r.get("arp_entries",0),"smart_skipped":bool(r.get("smart_skipped")),"partial_errors":r.get("partial_errors",[]),"retry":True}
            except Exception as retry_error:
                return {"device_id":did,"state":"error","error":str(retry_error)[:900],"first_error":first_error,"retry":True}
        finally:
            lock.release()

    workers=max(2,min(4,int(os.getenv("DISCOVERY_ALL_WORKERS","3"))))
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="ipam-discover") as pool:
        futures=[pool.submit(run_one,(pos,did)) for pos,did in enumerate(ids,1)]
        for fut in as_completed(futures):
            try:
                item=fut.result()
            except Exception as exc:
                item={"state":"error","error":str(exc)[:900]}
            with _discover_all_jobs_lock:
                j=_discover_all_jobs[job_id]
                state=item.get("state")
                if state=="done": j["done"]+=1
                elif state=="busy": j["busy"]+=1
                else: j["errors"]+=1
                j["results"].append(item)
                j["completed"]=j["done"]+j["errors"]+j["busy"]

    with _discover_all_jobs_lock:
        j=_discover_all_jobs[job_id]; j["state"]="done"; j["current_device_id"]=None; j["current_device_name"]=""
        total=j["total"]; done=j["done"]; errors=j["errors"]; busy=j["busy"]
        j["workers"]=workers
        j["completed"]=done+errors+busy
    audit("DISCOVERY_ALL","all",f"devices={total}; done={done}; errors={errors}; busy={busy}; workers={workers}; smart=1")


@app.post("/api/discovery/devices/discover-all")
def discovery_devices_discover_all():
    """Start Smart Discover All in background and return a pollable job id."""
    c=db(); ids=[int(r["id"]) for r in c.execute("SELECT id FROM network_devices ORDER BY name COLLATE NOCASE").fetchall()]; c.close()
    if not ids: return {"ok":True,"job_id":"","total":0,"state":"done","done":0,"errors":0,"busy":0}
    job_id=uuid.uuid4().hex
    with _discover_all_jobs_lock:
        # Reuse an active batch across browser tabs/users. Per-device locks alone
        # serialize duplicate batches but still repeat the whole network scan.
        for active in _discover_all_jobs.values():
            if active.get("state") in ("queued","running"):
                return {"ok":True,"job_id":active["job_id"],"total":active["total"],"state":active["state"],"reused":True}
        _discover_all_jobs[job_id]={"job_id":job_id,"state":"queued","total":len(ids),"current":0,"done":0,"errors":0,"busy":0,"current_device_id":None,"current_device_name":"","results":[]}
    threading.Thread(target=_run_discover_all_job,args=(job_id,ids),daemon=True,name="ipam-discover-all").start()
    return {"ok":True,"job_id":job_id,"total":len(ids),"state":"queued","done":0,"errors":0,"busy":0}


@app.get("/api/discovery/devices/discover-all/{job_id}")
def discovery_devices_discover_all_status(job_id:str):
    with _discover_all_jobs_lock:
        j=_discover_all_jobs.get(job_id)
        if not j: raise HTTPException(404,"Discover All job không tồn tại")
        return dict(j)

@app.post("/api/discovery/devices/{did}/discover")
def discovery_device_discover(did:int):
    lock=_get_discovery_lock(did)
    if not lock.acquire(blocking=False):
        raise HTTPException(409,"Device đang được Auto Discovery xử lý")
    try:
        return _discover_device_sync(did,True,False)
    except HTTPException: raise
    except Exception as e:
        c=db(); c.execute("INSERT INTO discovery_jobs(device_id,state,finished_at,detail) VALUES(?,?,CURRENT_TIMESTAMP,?)",(did,"error",str(e)[:1000])); c.execute("UPDATE network_devices SET status='error',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(str(e)[:1000],did)); c.commit(); c.close(); raise HTTPException(400,str(e))
    finally:
        lock.release()

@app.get("/api/discovery/devices/{did}/interfaces")
def discovery_device_interfaces(did:int):
    c=db(); rows=c.execute("SELECT * FROM network_interfaces WHERE device_id=? ORDER BY if_index",(did,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.get("/api/discovery/devices/{did}/mac-table")
def discovery_device_mac_table(did:int):
    c=db(); d=c.execute("SELECT d.*,p.id AS profile_id,p.name AS profile_name,p.version,p.community,p.username,p.security_level,p.auth_protocol,p.auth_password,p.priv_protocol,p.priv_password,p.port,p.timeout,p.retries FROM network_devices d JOIN snmp_profiles p ON p.id=d.snmp_profile_id WHERE d.id=?",(did,)).fetchone()
    rows=c.execute("SELECT * FROM network_mac_entries WHERE device_id=? ORDER BY vlan_id,mac,if_index",(did,)).fetchall()
    if not d:
        c.close(); raise HTTPException(404,"Device không tồn tại")
    # MAC table itself must never fail just because optional LAG enrichment
    # (IF-MIB ifStackTable) is unsupported/blocked on a switch such as SG350X.
    # The FDB rows are already valid data and should still be returned.
    try:
        lag_members=_discover_lag_members(c,d,d["management_ip"])
    except Exception:
        lag_members={}
    out=[]
    for r in rows:
        item=dict(r)
        members=lag_members.get(int(r["if_index"] or 0),[])
        item["physical_ports"]=members
        item["port_type"]="lag" if members else (r["port_type"] or "physical")
        out.append(item)
    c.close(); return out

@app.get("/api/discovery/devices/{did}/arp-table")
def discovery_device_arp_table(did:int):
    c=db(); rows=c.execute("SELECT * FROM network_arp_entries WHERE device_id=? ORDER BY ip",(did,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.get("/api/discovery/devices/{did}/correlation")
def discovery_device_correlation(did:int):
    c=db()
    dev=c.execute("SELECT d.*,p.id AS profile_id,p.name AS profile_name,p.version,p.community,p.username,p.security_level,p.auth_protocol,p.auth_password,p.priv_protocol,p.priv_password,p.port,p.timeout,p.retries FROM network_devices d JOIN snmp_profiles p ON p.id=d.snmp_profile_id WHERE d.id=?",(did,)).fetchone()
    if not dev:
        c.close(); raise HTTPException(404,"Device không tồn tại")
    arps=c.execute("SELECT * FROM network_arp_entries WHERE device_id=? ORDER BY ip",(did,)).fetchall()
    ips={r["ip"]:dict(r) for r in c.execute("SELECT ip,mac,vlan,hostname,device,user_dept,status FROM ips WHERE mac<>''").fetchall()}
    out=[]
    for r in arps:
        mac=normalize_mac(r['mac'] or '')
        candidates=_global_mac_locations(c,mac,vlan_hint=(ips.get(r['ip']) or {}).get('vlan'),
            preferred_device_id=did,preferred_arp_if_index=r['if_index'] or 0) if mac else []
        selected=[]
        for p in candidates:
            item=dict(p)
            item['physical_ports']=[]
            item['if_descr']=p.get('if_descr') or ''
            item['_location']={
                'switch_name':p.get('switch_name'),'switch_ip':p.get('switch_ip'),
                'port':p.get('if_name') or '—','port_status':p.get('status') or 'learned',
                'vlan':p.get('vlan_id'),'last_discovery':p.get('last_discovery'),
                'if_descr':p.get('if_descr') or '','if_index':p.get('if_index') or 0,
                'mac_if_index':p.get('if_index') or 0,'arp_if_index':r['if_index'] or 0,
                'inter_switch':bool(p.get('inter_switch'))
            }
            selected.append(item)
        item=dict(r); item['switch_ports']=selected; item['ipam']=ips.get(r['ip']) or {}
        out.append(item)
    c.close()
    return out


@app.get("/api/discovery/topology")
def discovery_topology():
    """Return only live managed topology and clean orphaned topology rows."""
    c=db()
    for table in ("network_neighbors","network_interfaces","network_mac_entries","network_mac_port_history","network_arp_entries","topology_positions","discovery_jobs"):
        c.execute(f"DELETE FROM {table} WHERE device_id IS NOT NULL AND device_id NOT IN (SELECT id FROM network_devices)")
    c.execute("DELETE FROM topology_manual_links WHERE source_device_id NOT IN (SELECT id FROM network_devices) OR target_device_id NOT IN (SELECT id FROM network_devices)")
    c.commit()
    # Topology must never render an old/stale managed device as a node.
    # Discovery links already had this freshness guard, but the node list did
    # not, which is why a retired switch could remain visibly "hanging" in
    # Topology even after its discovery data was purged.
    # Topology is a last-known network map, not a live health dashboard.
    # Do NOT hide nodes/links merely because a sequential discovery run is
    # still in progress or a device missed one poll. This is especially
    # important for large Cisco environments where MAC discovery can take
    # longer than the topology freshness window.
    devices=[dict(r) for r in c.execute("""
        SELECT id,name,management_ip,device_type,status,sys_name,last_discovery,chassis_id,serial_number,discovery_interval_minutes
        FROM network_devices
        ORDER BY name
    """).fetchall()]
    active_ids={int(d['id']) for d in devices}
    # Positions/manual overrides belonging to retired devices are ignored by
    # the response. They remain harmlessly stored and are removed when the
    # device itself is deleted/replaced.
    known={}
    for d in devices:
        for value in (d.get('name'),d.get('sys_name'),d.get('management_ip'),d.get('chassis_id'),d.get('serial_number')):
            raw=str(value or '').strip()
            keys={raw.lower()} if raw else set()
            nm=_normalize_mac(raw)
            if nm: keys.add(nm.lower())
            for k in keys:
                if k: known[k]=d

    positions={int(r['device_id']):{'x':float(r['x']),'y':float(r['y'])} for r in c.execute("SELECT device_id,x,y FROM topology_positions").fetchall()}
    if active_ids:
        ph=','.join('?'*len(active_ids))
        vals=tuple(sorted(active_ids))
        manual=[dict(r) for r in c.execute(f"SELECT * FROM topology_manual_links WHERE enabled=1 AND source_device_id IN ({ph}) AND target_device_id IN ({ph}) ORDER BY id", vals+vals).fetchall()]
    else:
        manual=[]
    manual_keys={tuple(sorted((int(r['source_device_id']),int(r['target_device_id'])))) for r in manual}
    # Keep the most recently known LLDP/CDP observations available even when
    # one discovery attempt is stale/down. A discovery failure must not erase
    # the last-known physical topology from the map.
    rows=[dict(r) for r in c.execute("""
        SELECT n.*, d.name local_device, d.management_ip local_ip,
               d.device_type local_type, d.status local_status, d.chassis_id local_chassis_id,
               d.last_discovery local_last_discovery, d.discovery_interval_minutes local_discovery_interval
        FROM network_neighbors n JOIN network_devices d ON d.id=n.device_id
        ORDER BY d.name,n.local_if_name,n.protocol,n.remote_device,n.remote_port
    """).fetchall()]

    def match_managed(raw='', remote_ip=''):
        # CDP/LLDP identity is the primary correlation key.  A remote
        # management/native address can be misleading or stale, so it must
        # not override an explicit device-id/sysName/chassis identity.
        raw=str(raw or '').strip().strip('"')
        if raw:
            k=raw.lower()
            if k in known: return known[k]
            nm=_normalize_mac(raw)
            if nm and nm.lower() in known: return known[nm.lower()]
            base=k.split('(',1)[0].split('.',1)[0].strip()
            for d in devices:
                for cand in (d.get('name'),d.get('sys_name')):
                    ck=str(cand or '').strip().lower()
                    cb=ck.split('(',1)[0].split('.',1)[0].strip()
                    if cb and (base==cb or k.startswith(ck+'(') or k.startswith(ck+'.')):
                        return d
        rip=str(remote_ip or '').strip().lower()
        if rip:
            for d in devices:
                if str(d.get('management_ip') or '').strip().lower()==rip:
                    return d
        return None

    nodes=[]
    for d in devices:
        dtype=str(d.get('device_type') or 'Cisco'); text=(str(d.get('name') or '')+' '+dtype+' '+str(d.get('vendor') or '')).lower()
        layer='firewall' if ('sophos' in text or 'firewall' in text or 'xgs' in text or 'xg ' in text) else ('core' if ('n3k' in text or 'nexus n3' in text) else ('distribution' if ('n9k' in text or 'nexus n9' in text or 'catalyst 9500' in text) else ('access' if ('sg350' in text or 'catalyst 2960' in text or 'catalyst 3560' in text or 'catalyst 3750' in text or 'catalyst 3850' in text or 'catalyst 9200' in text or 'catalyst 9300' in text or 'access' in text) else 'other')))
        q=positions.get(int(d['id']))
        nodes.append({'id':f"d:{d['id']}",'device_id':d['id'],'name':d['name'],'sys_name':d.get('sys_name') or d['name'],'management_ip':d['management_ip'],'device_type':dtype,'status':d.get('status') or 'unknown','managed':True,'layer':layer,'position':q,'chassis_id':d.get('chassis_id') or '','serial_number':d.get('serial_number') or ''})

    grouped={}
    unknowns={}
    def unknown_key(n):
        rip=str(n.get('remote_ip') or '').strip().lower()
        ch=_normalize_mac(n.get('remote_chassis_id') or '')
        name=str(n.get('remote_device') or '').strip().lower()
        port=str(n.get('remote_port') or '').strip().lower()
        if rip: return 'ip:'+rip
        if ch: return 'ch:'+ch
        if name: return 'name:'+name
        return 'obs:'+str(n.get('device_id'))+':'+str(n.get('local_if_index'))+':'+str(n.get('protocol'))+':'+port

    for n in rows:
        remote=match_managed(n.get('remote_device'), n.get('remote_ip')) or match_managed(n.get('remote_chassis_id'), n.get('remote_ip'))
        if not remote:
            # CDP/LLDP can legitimately expose unmanaged switches, APs, phones,
            # routers, servers, hypervisors, etc. Keep them as ghost nodes rather
            # than silently dropping the observation from the topology.
            uk=unknown_key(n)
            u=unknowns.setdefault(uk, {'key':uk,'name':str(n.get('remote_device') or n.get('remote_platform') or n.get('remote_chassis_id') or 'Unknown Neighbor')[:120],
                'management_ip':str(n.get('remote_ip') or ''), 'chassis_id':str(n.get('remote_chassis_id') or ''),
                'platform':str(n.get('remote_platform') or ''), 'protocols':set(), 'observed_from':set(), 'ports':set(),
                'confidence':[], 'last_seen':n.get('last_seen') or ''})
            if n.get('remote_ip'): u['management_ip']=str(n.get('remote_ip'))
            if n.get('remote_device'): u['name']=str(n.get('remote_device'))[:120]
            if n.get('remote_platform'): u['platform']=str(n.get('remote_platform'))[:255]
            u['protocols'].add(str(n.get('protocol') or 'DISCOVERY').upper()); u['observed_from'].add(int(n.get('device_id') or 0));
            u['ports'].add((str(n.get('local_if_name') or '—'),str(n.get('remote_port') or '—'))); u['confidence'].append(int(n.get('confidence') or 0))
            if str(n.get('last_seen') or '')>str(u['last_seen'] or ''): u['last_seen']=n.get('last_seen')
            # Add one observation link per managed source to the unknown node.
            source=f"d:{n['device_id']}"; target=f"u:{uk}"
            pair=(source,target)
            g=grouped.setdefault(pair,{'source':source,'target':target,'pairs':{},'protocols':set(),'last_seen':n.get('last_seen') or '', 'confidences':[], 'speeds':[], 'observed_from':set(), 'unknown':True})
            g['pairs'][(str(n.get('local_if_name') or '—'),str(n.get('remote_port') or '—'))]=True; g['protocols'].add(str(n.get('protocol') or 'DISCOVERY').upper()); g['observed_from'].add(int(n.get('device_id') or 0)); g['confidences'].append(int(n.get('confidence') or 0)); g['speeds'].append(int(n.get('link_speed_bps') or 0))
            if str(n.get('last_seen') or '')>str(g['last_seen'] or ''): g['last_seen']=n.get('last_seen')
            continue
        if int(remote['id'])==int(n['device_id']): continue
        pair=tuple(sorted((int(n['device_id']),int(remote['id']))))
        if pair in manual_keys: continue
        a,b=sorted((f"d:{n['device_id']}",f"d:{remote['id']}"))
        lp=str(n.get('local_if_name') or '—'); rp=str(n.get('remote_port') or '—')
        if f"d:{n['device_id']}"!=a: lp,rp=rp,lp
        g=grouped.setdefault((a,b),{'source':a,'target':b,'pairs':{},'protocols':set(),'last_seen':n.get('last_seen') or '', 'confidences':[], 'speeds':[], 'observed_from':set(), 'unknown':False})
        g['pairs'][(lp,rp)]=True; g['protocols'].add(str(n.get('protocol') or 'DISCOVERY').upper())
        g['observed_from'].add(int(n.get('device_id') or 0))
        g['confidences'].append(int(n.get('confidence') or 0)); g['speeds'].append(int(n.get('link_speed_bps') or 0))
        if str(n.get('last_seen') or '')>str(g['last_seen'] or ''): g['last_seen']=n.get('last_seen')

    links=[]
    for idx,(key,g) in enumerate(grouped.items(),1):
        pairs=list(g['pairs'].keys()); ps=g['protocols']; proto='LLDP' if 'LLDP' in ps else ('CDP' if 'CDP' in ps else sorted(ps)[0])
        bidirectional=len(g['observed_from'])>=2
        avg_conf=round(sum(g['confidences'])/len(g['confidences'])) if g['confidences'] else 0
        link_conf=min(100,avg_conf+(10 if bidirectional else 0)+(5 if len(ps)>=2 else 0))
        speed=max(g['speeds']) if g['speeds'] else 0
        links.append({'id':f"auto:{idx}",'source':g['source'],'target':g['target'],'protocol':proto,'protocols':sorted(ps),'link_count':len(pairs),'port_pairs':[{'local':x[0],'remote':x[1]} for x in pairs],'local_port':pairs[0][0] if pairs else '—','remote_port':pairs[0][1] if pairs else '—','last_seen':g['last_seen'],'manual':False,'bidirectional':bidirectional,'confidence':link_conf,'speed_bps':speed,'link_role':'neighbor' if g.get('unknown') else 'inter-switch','unknown_neighbor':bool(g.get('unknown'))})

    for uk,u in unknowns.items():
        nid=f"u:{uk}"
        protos=sorted(u['protocols']); conf=round(sum(u['confidence'])/len(u['confidence'])) if u['confidence'] else 0
        nodes.append({'id':nid,'device_id':None,'name':u['name'],'sys_name':u['name'],'management_ip':u['management_ip'] or 'Chưa quản lý','device_type':u['platform'] or 'CDP / LLDP Neighbor','status':'unknown','managed':False,'layer':'other','position':None,'chassis_id':u['chassis_id'],'serial_number':'','protocols':protos,'confidence':conf,'last_seen':u['last_seen']})

    base=len(links)
    for r in manual:
        links.append({'id':f"manual:{r['id']}",'source':f"d:{r['source_device_id']}",'target':f"d:{r['target_device_id']}",'protocol':'MANUAL','protocols':['MANUAL'],'link_count':1,'port_pairs':[{'local':r.get('local_port') or '—','remote':r.get('remote_port') or '—'}],'local_port':r.get('local_port') or '—','remote_port':r.get('remote_port') or '—','last_seen':r.get('updated_at') or '','manual':True,'manual_id':r['id']})
    c.close()
    managed_count=sum(1 for n in nodes if n.get('managed')); unknown_count=sum(1 for n in nodes if not n.get('managed'))
    return {'nodes':nodes,'links':links,'managed_devices':managed_count,'unknown_neighbors':unknown_count,'link_count':len(links),'raw_observations':len(rows),'manual_link_count':len(manual)}

@app.get("/api/discovery/topology/validate")
def validate_discovery_topology():
    """Validate discovered links without changing any topology data."""
    c=db()
    devices=[dict(r) for r in c.execute("SELECT id,name,chassis_id,management_ip FROM network_devices").fetchall()]
    bykey={}
    for d in devices:
        for v in (d.get('name'),d.get('management_ip'),d.get('chassis_id')):
            k=str(v or '').strip().lower()
            if k: bykey[k]=d['id']
    rows=[dict(r) for r in c.execute("""SELECT n.* FROM network_neighbors n
        JOIN network_devices d ON d.id=n.device_id
        WHERE LOWER(COALESCE(d.status,''))='up'
          AND d.last_discovery IS NOT NULL AND d.last_discovery<>''
          AND d.last_discovery >= datetime('now', '-' || MAX(15, COALESCE(d.discovery_interval_minutes, ?)*3) || ' minutes')
        ORDER BY n.device_id,n.local_if_index,n.protocol""", (DISCOVERY_AUTO_DEFAULT_MINUTES,)).fetchall()]
    observations=[]; suspicious=[]; pair_seen={}
    for r in rows:
        remote=str(r.get('remote_chassis_id') or r.get('remote_device') or '').strip().lower()
        rid=bykey.get(remote)
        if rid is None:
            base=remote.split('.',1)[0]
            for d in devices:
                if base and base==str(d.get('name') or '').strip().lower().split('.',1)[0]: rid=d['id']; break
        if rid is None or int(rid)==int(r['device_id']):
            suspicious.append({'type':'unresolved_neighbor','device_id':r['device_id'],'port':r.get('local_if_name') or '—','remote':r.get('remote_device') or r.get('remote_chassis_id') or '—','protocol':r.get('protocol')})
            continue
        key=tuple(sorted((int(r['device_id']),int(rid))))
        pair_seen.setdefault(key,[]).append(r)
    for key,obs in pair_seen.items():
        sources={int(x['device_id']) for x in obs}
        protocols={str(x.get('protocol') or '').upper() for x in obs}
        if len(sources)<2:
            suspicious.append({'type':'one_way','source_device_id':key[0],'target_device_id':key[1],'protocols':sorted(protocols)})
        if len(protocols)>=2:
            # Same physical neighbor observed by both protocols is healthy, but
            # differing port pairs should be surfaced for operator review.
            pairs={(str(x.get('local_if_name') or ''),str(x.get('remote_port') or '')) for x in obs}
            if len(pairs)>2:
                suspicious.append({'type':'port_conflict','source_device_id':key[0],'target_device_id':key[1],'protocols':sorted(protocols),'pairs':sorted(pairs)})
        observations.append({'source_device_id':key[0],'target_device_id':key[1],'bidirectional':len(sources)>=2,'protocols':sorted(protocols),'observations':len(obs)})
    c.close()
    confirmed=sum(1 for x in observations if x['bidirectional'])
    return {'ok':True,'devices':len(devices),'observations':len(rows),'links':len(observations),'confirmed_links':confirmed,'suspicious_count':len(suspicious),'suspicious':suspicious,'health':'healthy' if not suspicious else ('warning' if len(suspicious)<3 else 'attention')}

@app.post("/api/discovery/topology/positions")
def save_topology_positions(payload: dict):
    positions=payload.get('positions') or []
    c=db()
    for item in positions:
        try:
            did=int(item.get('device_id')); x=float(item.get('x')); y=float(item.get('y'))
            if not c.execute("SELECT 1 FROM network_devices WHERE id=?",(did,)).fetchone(): continue
            c.execute("INSERT INTO topology_positions(device_id,x,y,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(device_id) DO UPDATE SET x=excluded.x,y=excluded.y,updated_at=CURRENT_TIMESTAMP",(did,x,y))
        except Exception: continue
    c.commit(); c.close(); audit('TOPOLOGY_LAYOUT_SAVE','topology',f'{len(positions)} vị trí'); return {'ok':True}

@app.post("/api/discovery/topology/links")
def save_topology_link(payload: dict):
    try:
        source=int(payload.get('source_device_id')); target=int(payload.get('target_device_id'))
    except Exception: raise HTTPException(400,'Device không hợp lệ')
    if source==target: raise HTTPException(400,'Không thể nối một switch với chính nó')
    c=db()
    if not c.execute('SELECT 1 FROM network_devices WHERE id=?',(source,)).fetchone() or not c.execute('SELECT 1 FROM network_devices WHERE id=?',(target,)).fetchone():
        c.close(); raise HTTPException(404,'Device không tồn tại')
    # Canonical ordering keeps one manual override per device pair.
    a,b=sorted((source,target))
    local_port=str(payload.get('local_port') or '').strip()[:100]; remote_port=str(payload.get('remote_port') or '').strip()[:100]
    if source!=a: local_port,remote_port=remote_port,local_port
    # Studio and the legacy editor share physical endpoint reservations.
    if c.execute("SELECT to_regclass('public.studio_link_endpoints')").fetchone()[0]:
        c.execute("LOCK TABLE studio_link_endpoints IN SHARE ROW EXCLUSIVE MODE")
        from .studio_models import port_key
        reserved=c.execute("SELECT device_id,if_name FROM studio_link_endpoints WHERE device_id=? OR device_id=?",(a,b)).fetchall()
        if any((int(r['device_id'])==a and port_key(r['if_name'])==port_key(local_port)) or (int(r['device_id'])==b and port_key(r['if_name'])==port_key(remote_port)) for r in reserved):
            c.close(); raise HTTPException(409,'Cổng đang được sử dụng trong Topology Studio')
    # Do not depend on a PostgreSQL ON CONFLICT constraint here.  Some migrated
    # production databases intentionally have no UNIQUE(source_device_id,target_device_id)
    # constraint, and the old statement therefore failed with InvalidColumnReference.
    # Manual links are still canonicalized to one row per device pair.
    existing=c.execute("SELECT id FROM topology_manual_links WHERE source_device_id=? AND target_device_id=? ORDER BY id LIMIT 1",(a,b)).fetchone()
    if existing:
        c.execute("UPDATE topology_manual_links SET local_port=?,remote_port=?,protocol='MANUAL',enabled=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(local_port,remote_port,int(existing['id'])))
    else:
        c.execute("INSERT INTO topology_manual_links(source_device_id,target_device_id,local_port,remote_port,protocol,enabled,updated_at) VALUES(?,?,?,?, 'MANUAL',1,CURRENT_TIMESTAMP)",(a,b,local_port,remote_port))
    c.commit(); c.close(); audit('TOPOLOGY_LINK_SAVE','topology',f'{a} ↔ {b}'); return {'ok':True}

@app.delete("/api/discovery/topology/links/{lid}")
def delete_topology_link(lid:int):
    c=db(); c.execute('DELETE FROM topology_manual_links WHERE id=?',(lid,)); c.commit(); c.close(); audit('TOPOLOGY_LINK_DELETE','topology',str(lid)); return {'ok':True}

@app.get("/api/audit")
def get_audit():
    c=db(); rows=[dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")]; c.close()
    for r in rows:
        r["created_at_local"]=_local_time(r.get("created_at"))
    return rows


def _check_dhcp_token(authorization: str = ""):
    # Accept Authorization: Bearer <token> only. Keep the sync endpoint separate from the UI.
    value = (authorization or "").strip()
    if not value.startswith("Bearer ") or value[7:].strip() != DHCP_SYNC_TOKEN:
        raise HTTPException(401, "DHCP Sync token không hợp lệ")


def _refresh_dhcp_ip_status(c):
    """Reconcile DHCP state from fresh leases + reservations across all sources.

    Reservation wins over an active lease. Only IPs previously managed by the
    DHCP sync are eligible to become FREE when they disappear from all fresh
    DHCP sources.
    """
    active_lease_rows = c.execute("""
        SELECT DISTINCT l.ip
        FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id
        WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall()
    active_lease = {r["ip"] for r in active_lease_rows}
    reserved_rows = c.execute("""
        SELECT DISTINCT r.ip
        FROM dhcp_reservations r JOIN dhcp_sources s ON s.source_id=r.source_id
        WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall()
    reserved = {r["ip"] for r in reserved_rows}
    active = active_lease | reserved

    for ip in active:
        c.execute("INSERT OR IGNORE INTO dhcp_managed_ips(ip) VALUES(?)", (ip,))
        if ip in reserved:
            c.execute("UPDATE ips SET status='reserved', updated_at=CURRENT_TIMESTAMP WHERE ip=?", (ip,))
        else:
            c.execute("UPDATE ips SET status='dhcp', updated_at=CURRENT_TIMESTAMP WHERE ip=?", (ip,))

    managed_rows = c.execute("SELECT ip FROM dhcp_managed_ips").fetchall()
    freed = 0
    for r in managed_rows:
        ip = r["ip"]
        if ip not in active:
            current = c.execute("SELECT status FROM ips WHERE ip=?", (ip,)).fetchone()
            if current and current["status"] in ("dhcp", "reserved"):
                c.execute("UPDATE ips SET status='free', updated_at=CURRENT_TIMESTAMP WHERE ip=?", (ip,))
                freed += 1
            c.execute("DELETE FROM dhcp_managed_ips WHERE ip=?", (ip,))
    return len(active_lease), len(reserved), freed

@app.get("/api/dhcp/status")
def dhcp_status():
    c=db()
    rows=[dict(r) for r in c.execute("SELECT * FROM dhcp_sources ORDER BY source_id").fetchall()]
    active=c.execute("""
      SELECT COUNT(DISTINCT l.ip) n
      FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id
      WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchone()["n"]
    active_res=c.execute("""
      SELECT COUNT(DISTINCT r.ip) n
      FROM dhcp_reservations r JOIN dhcp_sources s ON s.source_id=r.source_id
      WHERE s.last_sync >= datetime('now', ?)
    """, (f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchone()["n"]
    c.close()
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    for r in rows:
        if r.get("last_sync"):
            try:
                dt=datetime.strptime(r["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                r["age_minutes"]=max(0, round((datetime.now(timezone.utc)-dt).total_seconds()/60,1))
                r["last_sync_local"]=dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                r["age_minutes"]=None; r["last_sync_local"]=""
        else:
            r["age_minutes"]=None; r["last_sync_local"]=""
        r["online"] = r.get("age_minutes") is not None and r["age_minutes"] <= DHCP_SYNC_STALE_MINUTES
        # Per-source reservation count (all rows in the last successful snapshot).
        c2=db()
        r["reservation_count"]=c2.execute("SELECT COUNT(*) n FROM dhcp_reservations WHERE source_id=?",(r["source_id"],)).fetchone()["n"]
        c2.close()
    return {"sources":rows,"active_leases":active,"active_reservations":active_res,"stale_minutes":DHCP_SYNC_STALE_MINUTES}

@app.get("/api/dhcp/leases")
def dhcp_leases():
    c=db()
    rows=[dict(r) for r in c.execute("""
      SELECT l.source_id,s.name source_name,s.server_ip,l.ip,l.mac,l.hostname,l.last_seen
      FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id
      ORDER BY l.ip
    """).fetchall()]
    c.close(); return rows


@app.get("/api/dhcp/reservations")
def dhcp_reservations():
    c=db()
    rows=[dict(r) for r in c.execute("""
      SELECT r.source_id,s.name source_name,s.server_ip,r.ip,r.mac,r.hostname,r.last_seen
      FROM dhcp_reservations r JOIN dhcp_sources s ON s.source_id=r.source_id
      ORDER BY r.ip
    """).fetchall()]
    c.close(); return rows


@app.post("/api/dhcp/sync-now/{source_id}")
def dhcp_sync_now(source_id: str):
    """Queue an immediate sync request for a Windows DHCP agent.

    The IPAM server cannot directly execute PowerShell on the DHCP host.
    The resident agent polls this queue and performs the sync locally.
    """
    sid=(source_id or "").strip().lower()
    c=db()
    src=c.execute("SELECT source_id,name,server_ip FROM dhcp_sources WHERE source_id=?",(sid,)).fetchone()
    if not src:
        c.close(); raise HTTPException(404,"Không tìm thấy DHCP source")
    pending=c.execute("SELECT id FROM dhcp_sync_requests WHERE source_id=? AND status='pending' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
    if pending:
        c.close()
        return {"ok":True,"request_id":pending["id"],"source_id":sid,"message":"Đã có yêu cầu đồng bộ đang chờ DHCP Agent xử lý."}
    req_row=c.execute("INSERT INTO dhcp_sync_requests(source_id,status) VALUES(?, 'pending') RETURNING id",(sid,)).fetchone()
    if not req_row:
        c.rollback(); c.close(); raise HTTPException(500,"Không tạo được DHCP sync request")
    req_id=req_row["id"]
    c.commit(); c.close()
    audit("DHCP_SYNC_REQUEST",sid,f"Yêu cầu đồng bộ thủ công từ giao diện · request={req_id}")
    return {"ok":True,"request_id":req_id,"source_id":sid,"message":"Đã gửi yêu cầu đồng bộ. DHCP Agent sẽ thực hiện ngay."}

@app.post("/api/dhcp/sync-now")
def dhcp_sync_now_all():
    c=db()
    rows=c.execute("SELECT source_id FROM dhcp_sources ORDER BY source_id").fetchall()
    if not rows:
        c.close(); raise HTTPException(404,"Chưa có DHCP source")
    ids=[r["source_id"] for r in rows]
    req_ids=[]
    for sid in ids:
        pending=c.execute("SELECT id FROM dhcp_sync_requests WHERE source_id=? AND status='pending' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
        if pending:
            req_ids.append(pending["id"])
        else:
            req_row=c.execute("INSERT INTO dhcp_sync_requests(source_id,status) VALUES(?, 'pending') RETURNING id",(sid,)).fetchone()
            if not req_row:
                c.rollback(); c.close(); raise HTTPException(500,"Không tạo được DHCP sync request cho "+sid)
            req_ids.append(req_row["id"])
    c.commit(); c.close()
    audit("DHCP_SYNC_REQUEST","ALL",f"Yêu cầu đồng bộ thủ công DHCP Failover · requests={','.join(map(str,req_ids))}")
    return {"ok":True,"source_ids":ids,"request_ids":req_ids,"message":"Đã gửi yêu cầu đồng bộ cho toàn bộ DHCP Failover."}

@app.get("/api/dhcp/sync-request")
def dhcp_sync_request(source_id: str, authorization: str = Header(default="")):
    _check_dhcp_token(authorization)
    sid=(source_id or "").strip().lower()
    c=db()
    row=c.execute("SELECT id,source_id,requested_at FROM dhcp_sync_requests WHERE source_id=? AND status='pending' ORDER BY id ASC LIMIT 1",(sid,)).fetchone()
    c.close()
    if not row:
        return {"pending":False}
    return {"pending":True,"request_id":row["id"],"source_id":row["source_id"],"requested_at":row["requested_at"]}

@app.get("/api/dhcp/sync-request/status/{request_id}")
def dhcp_sync_request_status(request_id: int):
    c=db()
    row=c.execute("SELECT id,source_id,status,requested_at,processed_at FROM dhcp_sync_requests WHERE id=?",(request_id,)).fetchone()
    c.close()
    if not row: raise HTTPException(404,"Không tìm thấy yêu cầu đồng bộ")
    return dict(row)

@app.post("/api/dhcp/sync-request/ack")
def dhcp_sync_request_ack(request_id: int, source_id: str, authorization: str = Header(default="")):
    _check_dhcp_token(authorization)
    sid=(source_id or "").strip().lower()
    c=db()
    c.execute("UPDATE dhcp_sync_requests SET status='processed',processed_at=CURRENT_TIMESTAMP WHERE id=? AND source_id=? AND status='pending'",(request_id,sid))
    changed=c.total_changes
    c.commit(); c.close()
    return {"ok":True,"processed":bool(changed)}

@app.get("/api/dhcp/agent-check")
def dhcp_agent_check(source_id: str, authorization: str = Header(default="")):
    """Lightweight read-only check used by the Windows agent for connectivity.

    This never changes DHCP/IPAM data; it only confirms token, source registration
    and returns the last successful sync timestamp.
    """
    _check_dhcp_token(authorization)
    sid=(source_id or "").strip().lower()
    c=db()
    try:
        row=c.execute("SELECT source_id,name,server_ip,status,last_sync,lease_count FROM dhcp_sources WHERE source_id=?",(sid,)).fetchone()
        if not row: raise HTTPException(404,"DHCP source chưa được đăng ký: "+sid)
        return {"ok":True,"source":dict(row)}
    finally:
        c.close()

@app.post("/api/dhcp/sync")
def dhcp_sync(x:DHCPSyncIn, authorization: str = Header(default="")):
    _check_dhcp_token(authorization)
    sid=(x.source_id or "").strip().lower()
    name=(x.name or "").strip()
    if not sid or not name: raise HTTPException(400,"Thiếu source_id/name")
    if len(x.leases)>10000 or len(x.reservations)>10000: raise HTTPException(400,"Số lease/reservation vượt giới hạn 10000")
    c=db()
    c.execute("""INSERT INTO dhcp_sources(source_id,name,server_ip,status,last_error) VALUES(?,?,?,'syncing','')
                 ON CONFLICT(source_id) DO UPDATE SET name=excluded.name,server_ip=excluded.server_ip,status='syncing',last_error=''""",
              (sid,name,x.server_ip.strip()))
    # Snapshot semantics: replace only this source's lease and reservation sets.
    c.execute("DELETE FROM dhcp_leases WHERE source_id=?",(sid,))
    c.execute("DELETE FROM dhcp_reservations WHERE source_id=?",(sid,))
    clean_leases=[]; clean_res=[]; seen=set()
    for lease in x.leases:
        ip=valid_ip(lease.ip)
        if not ip or ip in seen: continue
        seen.add(ip)
        mac=normalize_mac(lease.mac)
        host=(lease.hostname or "").strip().rstrip(".").split(".",1)[0]
        clean_leases.append((sid,ip,mac,host))
    seen_res=set()
    for res in x.reservations:
        ip=valid_ip(res.ip)
        if not ip or ip in seen_res: continue
        seen_res.add(ip)
        mac=normalize_mac(res.mac)
        host=(res.hostname or "").strip().rstrip(".").split(".",1)[0]
        clean_res.append((sid,ip,mac,host))
    c.executemany("INSERT INTO dhcp_leases(source_id,ip,mac,hostname,last_seen) VALUES(?,?,?, ?,CURRENT_TIMESTAMP)",clean_leases)
    c.executemany("INSERT INTO dhcp_reservations(source_id,ip,mac,hostname,last_seen) VALUES(?,?,?, ?,CURRENT_TIMESTAMP)",clean_res)

    added=updated=0
    # A reservation is authoritative for MAC/hostname when the same IP also has a lease.
    combined={}
    for _,ip,mac,host in clean_leases: combined[ip]=(mac,host,"dhcp")
    for _,ip,mac,host in clean_res: combined[ip]=(mac,host,"reserved")
    for ip,(mac,host,kind) in combined.items():
        vlan = _dhcp_vlan_for_ip(c, ip)
        user_dept = _dhcp_user_dept_for_vlan(vlan)
        exists=c.execute("SELECT id FROM ips WHERE ip=?",(ip,)).fetchone()
        if exists:
            sets=[]; params=[]
            if mac: sets.append("mac=?"); params.append(mac)
            if host: sets.append("hostname=?"); params.append(host)
            if vlan: sets.append("vlan=?"); params.append(vlan)
            if user_dept: sets.append("user_dept=?"); params.append(user_dept)
            sets.append("status=?"); params.append(kind)
            sets.append("updated_at=CURRENT_TIMESTAMP"); params.append(ip)
            c.execute(f"UPDATE ips SET {', '.join(sets)} WHERE ip=?",params)
            updated+=1
        else:
            c.execute("INSERT INTO ips(ip,vlan,hostname,user_dept,status,mac) VALUES(?,?,?,?,?,?)",(ip,vlan,host,user_dept,kind,mac))
            added+=1
        c.execute("INSERT OR IGNORE INTO dhcp_managed_ips(ip) VALUES(?)",(ip,))

    # Mark this source freshly synced before reconciliation.
    c.execute("""UPDATE dhcp_sources SET last_sync=CURRENT_TIMESTAMP,lease_count=?,added_count=?,updated_count=?,status='ok',last_error='' WHERE source_id=?""",
              (len(clean_leases),added,updated,sid))
    active_lease,active_res,freed=_refresh_dhcp_ip_status(c)
    c.commit(); c.close()
    audit("DHCP_SYNC",sid,f"{name} {x.server_ip}: leases={len(clean_leases)}, reservations={len(clean_res)}, thêm={added}, cập nhật={updated}, free={freed}")
    return {"ok":True,"source_id":sid,"leases":len(clean_leases),"reservations":len(clean_res),"added":added,"updated":updated,"freed":freed,"active_leases":active_lease,"active_reservations":active_res}

@app.post("/api/import")
async def import_file(file: UploadFile=File(...)):
    """Import IPAM CSV/XLSX, including Windows DHCP Get-DhcpServerv4Lease CSV."""
    raw = await file.read()
    filename = (file.filename or "").lower()

    aliases = {
      "ip":["ip","ip address","ipaddress","client ip address","clientipaddress","địa chỉ ip","ipaddressv4"],
      "vlan":["vlan","vlan id","vlanid"],
      "hostname":["hostname","host name","host","client name","clientname","name"],
      "device":["thietbi","thiết bị","device","device name"],
      "user_dept":["user","user/phòng ban","user / phòng ban","phong ban","phòng ban"],
      "status":["status","trạng thái","trang thai","addressstate"],
      "notes":["ghichu","ghi chú","ghi chu","notes","description"],
      "mac":["mac","mac address","mac-address","địa chỉ mac","client id","clientid","client identifier","clientidentifier","unique id","uniqueid","unique identifier"],
      "vendor":["vendor","nhà sản xuất","nha san xuat"],
      "scope_id":["scopeid","scope id"],
      "lease_expiry":["leaseexpirytime","lease expiry time","lease expiry","lease expiration"],
      "client_type":["clienttype","client type","type"],
      "policy_name":["policyname","policy name"]
    }

    # Common Windows DHCP CSV headers produced by PowerShell, for example:
    # Get-DhcpServerv4Lease ... | Export-Csv ...
    windows_dhcp_headers = {
        "ipaddress", "scopeid", "clientid", "hostname", "addressstate",
        "leaseexpirytime", "description", "clienttype", "policyname"
    }

    def norm_header(h):
        h = str(h or "").strip().lower().lstrip("\ufeff")
        h = h.replace("_", " ").replace("-", " ")
        h = re.sub(r"\s+", " ", h)
        return h

    def build_mapping(headers):
        mapping={}
        normalized={norm_header(h):h for h in headers if h is not None}
        for key, als in aliases.items():
            for a in als:
                na=norm_header(a)
                if na in normalized:
                    mapping[key]=normalized[na]
                    break
        return mapping

    def is_windows_dhcp(mapping, headers):
        nh={norm_header(h).replace(" ","") for h in headers if h is not None}
        manager_format=("clientipaddress" in nh and "uniqueid" in nh and "leaseexpiration" in nh)
        powershell_format=("ipaddress" in nh and "clientid" in nh and ("addressstate" in nh or "scopeid" in nh or "leaseexpirytime" in nh))
        return manager_format or powershell_format

    def dhcp_status(value):
        v=norm_header(value).replace(" ", "")
        if v in {"active", "activedhcp", "offered", "renewing", "rebinding"}:
            return "dhcp"
        if v in {"declined"}:
            return "reserved"
        return ""

    if filename.endswith(".xlsx"):
        try:
            wb=load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws=wb[wb.sheetnames[0]]
            rows_iter=ws.iter_rows(values_only=True)
            try:
                headers=next(rows_iter)
            except StopIteration:
                raise HTTPException(400,"File Excel không có dữ liệu")
            mapping=build_mapping(headers)
            if "ip" not in mapping:
                raise HTTPException(400,"Excel phải có cột IP")
            records=[]
            for row in rows_iter:
                values=list(row)
                rec={}
                for idx,h in enumerate(headers):
                    if h is not None:
                        rec[h]="" if idx>=len(values) or values[idx] is None else str(values[idx]).strip()
                records.append(rec)
            wb.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400,f"Không đọc được file Excel: {e}")
    else:
        try:
            # Windows DHCP / PowerShell CSV may be UTF-8, UTF-8 BOM, UTF-16LE/BE.
            # Windows PowerShell 5.1 Export-Csv commonly produces UTF-16LE.
            if raw.startswith(b"\xff\xfe"):
                text=raw.decode("utf-16-le",errors="replace")
            elif raw.startswith(b"\xfe\xff"):
                text=raw.decode("utf-16-be",errors="replace")
            elif raw.startswith(b"\xef\xbb\xbf"):
                text=raw.decode("utf-8-sig",errors="replace")
            elif b"\x00" in raw[:4096]:
                # BOM-less UTF-16, frequently seen with Windows-generated CSV.
                text=raw.decode("utf-16-le",errors="replace")
            else:
                try:
                    text=raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text=raw.decode("cp1258",errors="replace")
        except Exception:
            raise HTTPException(400,"Không đọc được file CSV")
        sample=text[:8192]
        try:
            dialect=csv.Sniffer().sniff(sample,delimiters=",;\\t")
        except Exception:
            dialect=csv.excel
        reader=csv.DictReader(io.StringIO(text),dialect=dialect)
        if not reader.fieldnames:
            raise HTTPException(400,"CSV không có header")
        mapping=build_mapping(reader.fieldnames)
        if "ip" not in mapping:
            raise HTTPException(400,"CSV phải có cột IP hoặc IPAddress")
        records=list(reader)

    dhcp_mode=is_windows_dhcp(mapping, (headers if filename.endswith(".xlsx") else reader.fieldnames))

    # Windows DHCP does not normally export VLAN. ScopeId is the DHCP subnet.
    # If that subnet already exists in IPAM, automatically resolve its VLAN.
    c=db()
    subnet_rows=c.execute("SELECT cidr,vlan FROM subnets WHERE TRIM(COALESCE(vlan,''))<>''").fetchall()
    subnet_map={str(r["cidr"]):str(r["vlan"]) for r in subnet_rows}

    added=updated=skipped=0
    for r in records:
        ip=valid_ip(str(r.get(mapping["ip"], "") or "").strip())
        if not ip:
            skipped+=1
            continue

        vals={k:str(r.get(v,"" ) or "").strip() for k,v in mapping.items()}

        # Windows DHCP import: only synchronize IP, MAC and Hostname.
        # For HostName/FQDN, keep only the short hostname before the first dot.
        # All other DHCP fields are intentionally ignored.
        if dhcp_mode:
            if vals.get("hostname"):
                vals["hostname"]=vals["hostname"].strip().rstrip(".").split(".",1)[0]
            vals={k:vals.get(k,"") for k in ("hostname","mac")}

        if "mac" in vals:
            original_client_id=vals.get("mac","")
            vals["mac"]=normalize_mac(original_client_id)
            # Windows DHCP import is intentionally limited to IP + MAC + Hostname.
            # Invalid/non-MAC ClientId values are ignored (never written to Notes).

        if dhcp_mode:
            # Intentionally no VLAN/status/notes/lease/policy updates for Windows DHCP.
            # Only IP + MAC + short Hostname are imported.
            # Also clean legacy DHCP notes created by older importer versions, while
            # preserving any manually-entered notes that do not match that pattern.
            pass

        if "status" in vals:
            vals["status"]=vals.get("status","").lower()
            if vals["status"] not in {"free","used","reserved","dhcp"}:
                vals.pop("status",None)

        exists=c.execute("SELECT * FROM ips WHERE ip=?",(ip,)).fetchone()
        if exists:
            # MERGE mode: only non-empty imported columns are updated.
            updates=[]; params=[]
            for field in ("vlan","hostname","device","user_dept","status","notes","mac","vendor"):
                if field in vals and str(vals.get(field,"" )).strip() != "":
                    updates.append(f"{field}=?"); params.append(vals[field])
            if dhcp_mode:
                # Remove only legacy DHCP-import notes, never overwrite a real/manual note.
                existing_notes=str(exists["notes"] or "")
                if existing_notes.startswith("DHCP LeaseExpiry:") or existing_notes.startswith("DHCP ClientId:"):
                    updates.append("notes=?"); params.append("")
            if updates:
                updates.append("updated_at=CURRENT_TIMESTAMP"); params.append(ip)
                c.execute(f"UPDATE ips SET {', '.join(updates)} WHERE ip=?", params)
            updated+=1
        else:
            newvals={
                "vlan": "",
                "hostname": vals.get("hostname", ""),
                "device": "",
                "user_dept": "",
                "status": "free",
                "notes": "",
                "mac": vals.get("mac", ""),
                "vendor": ""
            }
            c.execute("""INSERT INTO ips(ip,vlan,hostname,device,user_dept,status,notes,mac,vendor)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
              (ip,newvals["vlan"],newvals["hostname"],newvals["device"],newvals["user_dept"],
               newvals["status"],newvals["notes"],newvals["mac"],newvals["vendor"]))
            added+=1

    c.commit(); c.close()
    mode="Windows DHCP" if dhcp_mode else "CSV/XLSX"
    audit("IMPORT",file.filename or mode,f"{mode}: Thêm {added}, cập nhật {updated}, bỏ qua {skipped}")
    return {"ok":True,"added":added,"updated":updated,"skipped":skipped,"format":mode}

@app.get("/api/export")
def export_csv():
    c=db(); rows=c.execute("SELECT ip,vlan,hostname,device,user_dept,status,notes,mac,vendor FROM ips ORDER BY id").fetchall(); c.close()
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(["IP","VLAN","HOSTNAME","THIETBI","USER","STATUS","GHI CHU","MAC","VENDOR"])
    for r in rows: w.writerow(list(r))
    return StreamingResponse(iter([out.getvalue().encode("utf-8-sig")]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=ip-manager-export.csv"})

@app.get("/api/export-xlsx")
def export_xlsx():
    """Export the complete IPAM database to a formatted XLSX workbook."""
    c=db()
    rows=c.execute("SELECT ip,vlan,hostname,device,user_dept,status,notes,mac,vendor FROM ips ORDER BY id").fetchall()
    c.close()

    wb=Workbook()
    ws=wb.active
    ws.title="IPAM"
    headers=["IP","VLAN","HOSTNAME","THIETBI","USER","STATUS","GHI CHU","MAC","VENDOR"]
    ws.append(headers)

    header_fill=PatternFill("solid", fgColor="1769FF")
    header_font=Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill=header_fill
        cell.font=header_font
        cell.alignment=Alignment(horizontal="center", vertical="center")
    ws.freeze_panes="A2"
    ws.auto_filter.ref=f"A1:I{max(1,ws.max_row)}"

    for r in rows:
        ws.append(list(r))

    widths=[18,10,24,22,24,14,38,22,24]
    for i,w in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(i)].width=w

    # Keep long IP/MAC/notes values readable and enable filtering in Excel.
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment=Alignment(vertical="top")
    for cell in ws["A"]:
        cell.number_format="@"
    for cell in ws["H"]:
        cell.number_format="@"

    out=io.BytesIO()
    wb.save(out)
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":"attachment; filename=ip-manager-export.xlsx"}
    )

async def run_cmd(cmd, timeout=60, cancel_event=None):
    try:
        p=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return "", f"command-not-found: {cmd[0]}"
    task=asyncio.create_task(p.communicate())
    try:
        if cancel_event is None:
            out,err=await asyncio.wait_for(task,timeout)
        else:
            cancel_task=asyncio.create_task(cancel_event.wait())
            done,_=await asyncio.wait({task,cancel_task},timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel_event.is_set():
                try:p.kill()
                except ProcessLookupError:pass
                out,err=await task
                return out.decode(errors="replace"), "cancelled"
            cancel_task.cancel()
            if not done:
                try:p.kill()
                except ProcessLookupError:pass
                out,err=await task
                return out.decode(errors="replace"), "timeout"
            out,err=await task
    except asyncio.TimeoutError:
        try:p.kill()
        except ProcessLookupError:pass
        await task
        return "", "timeout"
    return out.decode(errors="replace"), err.decode(errors="replace")

def parse_arp_scan(text):
    found={}
    for line in text.splitlines():
        m=re.match(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})(?:\s+(.*))?$",line)
        if m:
            ip,mac,v=m.groups()
            found[ip]={"ip":ip,"online":True,"mac":normalize_mac(mac),"vendor":(v or "").strip(),"method":"ARP"}
    return found

async def get_route_info(target_ip):
    out,_=await run_cmd(["ip","route","get",target_ip],timeout=2)
    m=re.search(r"\bdev\s+(\S+)",out)
    # A direct route has no 'via'; routed destinations should skip ARP immediately.
    direct=(m is not None and not re.search(r"\bvia\s+",out))
    return (m.group(1) if m else None), direct

COMMON_TCP_PORTS = (22, 53, 80, 135, 139, 443, 445, 3389, 5432, 8080, 8443, 9100)

async def _tcp_probe(ip, port, timeout=0.45):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return port
    except Exception:
        return None

async def _scan_tcp_ports(ip, cancel_event):
    sem=asyncio.Semaphore(256)
    async def one(port):
        if cancel_event.is_set():
            return None
        async with sem:
            return await _tcp_probe(ip, port)
    vals=await asyncio.gather(*(one(port) for port in COMMON_TCP_PORTS))
    return [p for p in vals if p is not None]

ALL_TCP_PORTS=range(1,65536)

async def _all_tcp_port_scan_fallback(ip, cancel_event, job=None):
    """Fallback full TCP connect scan when nmap is unavailable."""
    sem=asyncio.Semaphore(512)
    open_ports=[]
    ports=list(ALL_TCP_PORTS)
    checked=0
    for start in range(0,len(ports),1024):
        if cancel_event.is_set():
            return open_ports, checked
        batch=ports[start:start+1024]
        async def one(port):
            if cancel_event.is_set():
                return None
            async with sem:
                return await _tcp_probe(ip, port, timeout=0.35)
        vals=await asyncio.gather(*(one(port) for port in batch))
        open_ports.extend([x for x in vals if x is not None])
        checked += len(batch)
        if job is not None:
            job["checked"]=checked
            job["total"]=65535
            job["results"]=[{"port":int(p),"state":"open"} for p in sorted(open_ports)]
    return sorted(open_ports), checked

async def port_scan_job(job_id, ip):
    job=jobs[job_id]
    cancel_event=_scan_cancel_events.setdefault(job_id, asyncio.Event())
    job.update({"state":"running","started":time.time(),"target":ip,"total":65535,"checked":0,"results":[],"open_ports":[]})
    try:
        addr=ipaddress.ip_address(ip)
        if addr.version != 4:
            raise ValueError("Chỉ hỗ trợ IPv4")
    except Exception:
        job.update({"state":"error","error":"IP không hợp lệ","finished":time.time()})
        return

    # Prefer nmap connect scan: it is substantially faster and more reliable
    # for a complete 1-65535 TCP scan than creating 65k Python socket probes.
    nmap_path=shutil.which("nmap") if "shutil" in globals() else None
    if nmap_path is None:
        import shutil as _shutil
        nmap_path=_shutil.which("nmap")
    if nmap_path:
        try:
            out,err=await run_cmd([
                nmap_path,"-sT","-Pn","-n","-p-","--open","-T4",
                "--max-retries","1","--host-timeout","90s",ip
            ],timeout=120,cancel_event=cancel_event)
            if cancel_event.is_set():
                job.update({"state":"cancelled","finished":time.time()})
                return
            ports=[]
            for line in out.splitlines():
                m=re.match(r"^\s*(\d+)/tcp\s+open\s+(.*)$",line)
                if m:
                    try:
                        ports.append({"port":int(m.group(1)),"state":"open","service":m.group(2).strip()})
                    except Exception:
                        pass
            job.update({"state":"done","checked":65535,"total":65535,"results":ports,"open_ports":[x["port"] for x in ports],"finished":time.time(),"method":"nmap"})
            audit("PORT_SCAN",ip,f"{len(ports)} open TCP ports; method=nmap; all=1-65535")
            return
        except Exception as e:
            if cancel_event.is_set():
                job.update({"state":"cancelled","finished":time.time()})
                return
            # Fall through to socket scanner; the error is not fatal.
            job["fallback_error"]=str(e)

    try:
        ports,checked=await _all_tcp_port_scan_fallback(ip,cancel_event,job)
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        job.update({"state":"done","checked":checked,"total":65535,"results":[{"port":p,"state":"open"} for p in ports],"open_ports":ports,"finished":time.time(),"method":"async-tcp"})
        audit("PORT_SCAN",ip,f"{len(ports)} open TCP ports; method=async-tcp; all=1-65535")
    except Exception as e:
        job.update({"state":"error","error":str(e),"finished":time.time()})

async def port_scan_job_safe(job_id,ip):
    try:
        await port_scan_job(job_id,ip)
    except Exception as e:
        job=jobs.get(job_id,{})
        job.update({"state":"error","error":str(e),"finished":time.time()})
        try:
            audit("PORT_SCAN_ERROR",ip,str(e))
        except Exception:
            pass
    finally:
        _scan_cancel_events.pop(job_id,None)

async def _reverse_dns(ip):
    try:
        name, aliases, addrs = await asyncio.to_thread(socket.gethostbyaddr, ip)
        return str(name or '').rstrip('.')
    except Exception:
        return ''

async def _ping_stats(ip, cancel_event):
    if cancel_event.is_set():
        return {}
    out, err = await run_cmd([
        "ping", "-c", "3", "-W", "1", "-i", "0.1", ip
    ], timeout=5, cancel_event=cancel_event)
    text="\n".join(x for x in (out,err) if x)
    m=re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,\s*([0-9.]+)% packet loss", text)
    loss=float(m.group(3)) if m else None
    times=[]
    for line in text.splitlines():
        mm=re.search(r"time[=<]\s*([0-9.]+)\s*ms", line)
        if mm:
            try: times.append(float(mm.group(1)))
            except Exception: pass
    stat={}
    if m:
        stat["sent"]=int(m.group(1)); stat["received"]=int(m.group(2))
        stat["loss_percent"]=loss
    if times:
        stat.update({"min_ms":min(times),"avg_ms":sum(times)/len(times),"max_ms":max(times)})
    return stat

async def scan_job(job_id,cidr,use_nmap,tcp_scan=False,reverse_dns=False,ping_stats=False):
    job=jobs[job_id]
    cancel_event=_scan_cancel_events.setdefault(job_id, asyncio.Event())
    job.update({"state":"running","started":time.time(),"results":[],"online":0,"checked":0})
    try:
        net=ipaddress.ip_network(cidr,strict=False)
    except Exception:
        job.update({"state":"error","error":"CIDR không hợp lệ"}); return
    if net.version!=4 or net.prefixlen<16:
        job.update({"state":"error","error":"Chỉ hỗ trợ IPv4 và tối đa /16"}); return

    total=net.num_addresses-(2 if net.prefixlen<31 else 0)
    job["total"]=total
    results={}

    # Batch hosts of the original network; internal /24 boundaries remain valid.
    scan_batches=list(_scan_host_batches(net))
    job['phase']='discovery'

    async def scan_one(sub_hosts):
        allowed=set(sub_hosts)
        route_dev,direct_route=await get_route_info(sub_hosts[0])

        async def do_arp():
            if not (route_dev and direct_route):
                return {}
            out,arp_err=await run_cmd([
                "arp-scan","--interface",route_dev,"--plain","--ignoredups",
                "--retry=1","--timeout=150",*sub_hosts
            ],timeout=min(30,max(5,len(sub_hosts)//300+5)),cancel_event=cancel_event)
            if arp_err=="timeout" or str(arp_err).startswith("command-not-found:"):
                job["warning"]="ARP không hoàn tất; chỉ dùng ICMP cho các host còn lại."
            return {ip:data for ip,data in parse_arp_scan(out).items() if ip in allowed}

        async def do_fping():
            out,err=await run_cmd([
                "fping","-a","-e",
                "-r","0","-t",str(TIMEOUT_MS),"-i","1",*sub_hosts
            ],timeout=min(30,max(10,len(sub_hosts)//40+10)),cancel_event=cancel_event)
            if str(err)=="timeout": raise RuntimeError("fping quá thời gian; kết quả quét chưa đầy đủ")
            alive={}
            fping_text="\n".join(x for x in (out,err) if x)
            for line in fping_text.splitlines():
                line=line.strip()
                if not line: continue
                m=re.match(r"(\d+\.\d+\.\d+\.\d+)\s+is alive(?:\s+\(([^)]+)\))?",line)
                if not m:
                    bare=re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)(?:\s+\(([0-9.]+)\s*ms\))?",line)
                    if bare and bare.group(1) in allowed:
                        alive[bare.group(1)]={"latency_ms":float(bare.group(2)) if bare.group(2) else None}
                    continue
                ip,lat=m.groups()
                if valid_ip(ip) and ip in allowed:
                    latency=None
                    if lat:
                        mm=re.search(r"([0-9.]+)\s*ms",lat)
                        if mm:
                            try: latency=float(mm.group(1))
                            except: pass
                    alive[ip]={"latency_ms":latency}
            if alive or not str(err).startswith("command-not-found: fping"):
                return alive

            sem=asyncio.Semaphore(min(128,CONCURRENCY))
            async def one_ping(ip):
                async with sem:
                    o,e=await run_cmd(["ping","-c","1","-W",str(max(1,int((TIMEOUT_MS+999)//1000))),ip],timeout=3,cancel_event=cancel_event)
                    if e.startswith("command-not-found:"):
                        return None
                    m=re.search(r"time[=<]\s*([0-9.]+)\s*ms",o)
                    if " 1 received" in o or " 1 packets received" in o:
                        return (ip,{"latency_ms":float(m.group(1)) if m else None})
                    return None
            vals=await asyncio.gather(*(one_ping(ip) for ip in sub_hosts))
            return {ip:data for item in vals if item is not None for ip,data in [item]}

        if route_dev and direct_route:
            return await asyncio.gather(do_arp(),do_fping())
        return {}, await do_fping()

    # Scan host batches sequentially. This is deliberately conservative for
    # /16 so a busy LAN is not flooded with 65k probes at once. Smaller CIDRs
    # still use exactly the same fast path, with no functional change.
    for sub_hosts in scan_batches:
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        arp,icmp=await scan_one(sub_hosts)
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        mac_counts={}
        for d in arp.values():
            mac=d.get("mac","")
            if mac: mac_counts[mac]=mac_counts.get(mac,0)+1
        online=set(icmp) | set(arp)
        for ip in sorted(online,key=lambda s:tuple(map(int,s.split(".")))):
            a=arp.get(ip,{})
            i=icmp.get(ip,{})
            proxy_suspect=bool(a.get("mac") and mac_counts.get(a.get("mac"),0)>=4)
            if proxy_suspect and ip not in icmp:
                continue
            results[ip]={
                "ip":ip,"online":True,
                "mac":a.get("mac",""),"vendor":a.get("vendor","") or "",
                "hostname":"","latency_ms":i.get("latency_ms"),
                "method":"ARP+ICMP" if ip in arp and ip in icmp else ("ARP" if ip in arp else "ICMP"),
                "registered":False,"status":"unknown"
            }
        chunk_size = len(sub_hosts)
        job["checked"]=min(total, job.get("checked",0)+chunk_size)
        job["online"]=len(results)
        job["results"]=[dict(v) for v in sorted(results.values(),key=lambda x:tuple(map(int,x["ip"].split("."))))]
        _save_scan_job(job)

    if cancel_event.is_set():
        job.update({"state":"cancelled","finished":time.time()})
        return

    job['phase']='enrichment'
    _save_scan_job(job,True)

    # Optional Nmap enrichment only after fast discovery.
    online_ips=sorted(results,key=lambda s:tuple(map(int,s.split("."))))
    if use_nmap and online_ips:
        for i in range(0,len(online_ips),128):
            if cancel_event.is_set():
                job.update({"state":"cancelled","finished":time.time()})
                return
            batch=online_ips[i:i+128]
            out,_=await run_cmd([
                "nmap","-sn","-n","-T4","--max-retries","1",
                "--host-timeout","2s","--send-ip",*batch
            ],timeout=max(10,5+len(batch)//16),cancel_event=cancel_event)
            current_ip=None
            for line in out.splitlines():
                m=re.search(r"Nmap scan report for (\d+\.\d+\.\d+\.\d+)",line)
                if m:
                    current_ip=m.group(1); continue
                mm=re.search(r"MAC Address:\s+([0-9A-Fa-f:]{17})(?:\s+\((.*?)\))?",line)
                if current_ip and mm and current_ip in results:
                    results[current_ip]["mac"]=normalize_mac(mm.group(1)) or results[current_ip].get("mac","")
                    results[current_ip]["vendor"]=(mm.group(2) or "").strip()
            job["results"]=[dict(results[ip]) for ip in online_ips]

    # Optional Phase 1 enrichment. These are strictly opt-in so the existing
    # fast ICMP+ARP path remains unchanged when all new options are disabled.
    if tcp_scan and online_ips:
        sem=asyncio.Semaphore(32)
        async def tcp_host(ip):
            async with sem:
                return ip, await _scan_tcp_ports(ip, cancel_event)
        vals=await asyncio.gather(*(tcp_host(ip) for ip in online_ips))
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        for ip,ports in vals:
            if ip in results:
                results[ip]["open_ports"]=ports
        job["results"]=[dict(results[ip]) for ip in online_ips]

    if reverse_dns and online_ips:
        sem=asyncio.Semaphore(64)
        async def dns_host(ip):
            async with sem:
                return ip, await _reverse_dns(ip)
        vals=await asyncio.gather(*(dns_host(ip) for ip in online_ips))
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        for ip,name in vals:
            if ip in results and name:
                results[ip]["reverse_dns"]=name
                if not results[ip].get("hostname"):
                    results[ip]["hostname"]=name
        job["results"]=[dict(results[ip]) for ip in online_ips]

    if ping_stats and online_ips:
        sem=asyncio.Semaphore(64)
        async def ping_host(ip):
            async with sem:
                return ip, await _ping_stats(ip, cancel_event)
        vals=await asyncio.gather(*(ping_host(ip) for ip in online_ips))
        if cancel_event.is_set():
            job.update({"state":"cancelled","finished":time.time()})
            return
        for ip,stat in vals:
            if ip in results and stat:
                results[ip]["ping_stats"]=stat
        job["results"]=[dict(results[ip]) for ip in online_ips]

    job["phase"]="correlation"
    _save_scan_job(job,True)
    # Annotate existing IPAM records in one query.
    c=db(); registered={}
    if online_ips:
        marks=",".join("?"*len(online_ips))
        registered={r["ip"]:dict(r) for r in c.execute(
            f"SELECT * FROM ips WHERE ip IN ({marks})",online_ips).fetchall()
        }
    # Resolve VLAN / department from IPAM subnet configuration and detect
    # mismatches between what the scanner sees and what IPAM/DHCP expects.
    subnet_defs=[]
    for sn in c.execute("SELECT vlan,name,cidr FROM subnets").fetchall():
        try:
            subnet_defs.append((ipaddress.ip_network(sn["cidr"], strict=False), str(sn["vlan"] or "").strip(), str(sn["name"] or "").strip()))
        except Exception:
            continue

    def scan_subnet_for(ip):
        try:
            addr=ipaddress.ip_address(ip)
        except Exception:
            return None
        matches=[x for x in subnet_defs if addr in x[0]]
        return max(matches,key=lambda x:x[0].prefixlen) if matches else None

    # Current DHCP snapshots are useful for recognizing a live DHCP address
    # even when the IP does not yet exist in the IPAM ips table.
    dhcp_live={}
    for q in c.execute("SELECT l.ip,l.mac,l.hostname FROM dhcp_leases l JOIN dhcp_sources s ON s.source_id=l.source_id WHERE s.last_sync>=datetime('now', ?)",(f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall():
        dhcp_live[q["ip"]]={"mac":q["mac"] or "","hostname":q["hostname"] or "","status":"dhcp"}
    for q in c.execute("SELECT l.ip,l.mac,l.hostname FROM dhcp_reservations l JOIN dhcp_sources s ON s.source_id=l.source_id WHERE s.last_sync>=datetime('now', ?)",(f"-{DHCP_SYNC_STALE_MINUTES} minutes",)).fetchall():
        dhcp_live[q["ip"]]={"mac":q["mac"] or "","hostname":q["hostname"] or "","status":"reserved"}

    final=[]
    for ip in online_ips:
        d=results[ip]
        d["observed_mac"]=d.get("mac") or ""
        d["observed_at"]=job["started"]
        reg=registered.get(ip)
        sn=scan_subnet_for(ip)
        d["vlan"]= (reg["vlan"] if reg and reg["vlan"] else (sn[1] if sn else ""))
        d["subnet_name"]=sn[2] if sn else ""
        d["user_dept"]= (reg["user_dept"] if reg and reg["user_dept"] else _dhcp_user_dept_for_vlan(d["vlan"]))
        dh=dhcp_live.get(ip)
        d["dhcp_managed"]=bool(dh)
        d["conflict"]=False
        d["conflict_type"]=""
        d["conflict_message"]=""
        if reg:
            d["registered"]=True; d["status"]=reg["status"]
            d["hostname"]=d.get("hostname") or reg["hostname"]
            d["mac"]=d.get("mac") or reg["mac"]
            d["vendor"]=d.get("vendor") or reg["vendor"]
            ipam_mac=normalize_mac(reg["mac"])
            scan_mac=normalize_mac(d.get("mac"))
            if ipam_mac and scan_mac and ipam_mac != scan_mac:
                d["conflict"]=True; d["conflict_type"]="ipam_mac_mismatch"
                d["conflict_message"]=f"IPAM MAC {ipam_mac} ≠ Scanner MAC {scan_mac}"
            if dh and dh.get("mac") and ipam_mac and normalize_mac(dh["mac"]) != ipam_mac:
                d["conflict"]=True; d["conflict_type"]="dhcp_mac_mismatch"
                d["conflict_message"]=f"DHCP MAC {normalize_mac(dh['mac'])} ≠ IPAM MAC {ipam_mac}"
        else:
            if dh:
                d["registered"]=False; d["status"]=dh["status"]
                d["hostname"]=d.get("hostname") or dh.get("hostname","")
                d["mac"]=d.get("mac") or dh.get("mac","")
            else:
                d["registered"]=False; d["status"]="unknown"
        final.append(d)
    c.close()
    job["phase"]="done"
    job.update({"state":"done","checked":total,"online":len(final),"results":final,"finished":time.time()})
    method="ARP+ICMP" if arp else "ICMP"
    audit("SCAN",cidr,f"{len(final)} online; nmap={use_nmap}; tcp={tcp_scan}; dns={reverse_dns}; ping_stats={ping_stats}; method={method}")

async def scan_job_safe(job_id,cidr,use_nmap,tcp_scan=False,reverse_dns=False,ping_stats=False):
    try:
        await scan_job(job_id,cidr,use_nmap,tcp_scan,reverse_dns,ping_stats)
    except Exception as e:
        job=jobs.get(job_id,{})
        job.update({"state":"error","error":str(e),"finished":time.time()})
        try:
            audit("SCAN_ERROR",cidr,str(e))
        except Exception:
            pass
    finally:
        _save_scan_job(jobs[job_id],True)
        _scan_cancel_events.pop(job_id,None)

class PortScanIn(BaseModel):
    ip: str

@app.post("/api/port-scan")
async def start_port_scan(x:PortScanIn):
    try:
        addr=ipaddress.ip_address(x.ip.strip())
    except Exception:
        raise HTTPException(400,"IP không hợp lệ")
    if addr.version != 4:
        raise HTTPException(400,"Chỉ hỗ trợ IPv4")
    job_id=uuid.uuid4().hex[:12]
    jobs[job_id]={"id":job_id,"type":"port_scan","state":"queued","target":str(addr),"total":65535,"checked":0,"results":[],"open_ports":[]}
    _scan_cancel_events[job_id]=asyncio.Event()
    asyncio.create_task(port_scan_job_safe(job_id,str(addr)))
    return {"job_id":job_id}

@app.get("/api/port-scan/{job_id}")
def port_scan_status(job_id:str):
    if job_id not in jobs: raise HTTPException(404,"Port Scan không tồn tại")
    return jobs[job_id]

@app.delete("/api/port-scan/{job_id}")
def cancel_port_scan(job_id:str):
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,"Port Scan không tồn tại")
    ev=_scan_cancel_events.get(job_id)
    if ev: ev.set()
    if job.get("state") in ("queued","running"):
        job["state"]="cancelled"
        job["finished"]=time.time()
    return {"ok":True}

@app.post("/api/scan")
async def start_scan(x:ScanIn,request:Request):
    try:
        net=ipaddress.ip_network(x.cidr,strict=False)
    except: raise HTTPException(400,"CIDR không hợp lệ")
    if net.version!=4 or net.prefixlen<16: raise HTTPException(400,"Chỉ hỗ trợ IPv4 và tối đa /16")
    job_id=uuid.uuid4().hex[:12]
    jobs[job_id]={"id":job_id,"created_by":getattr(request.state,"user",{}).get("username",""),"state":"queued","cidr":str(net),"total":net.num_addresses-(2 if net.prefixlen<31 else 0),"checked":0,"online":0,"results":[]}
    _scan_cancel_events[job_id]=asyncio.Event()
    _save_scan_job(jobs[job_id],True)
    asyncio.create_task(scan_job_safe(job_id,str(net),x.nmap,x.tcp_scan,x.reverse_dns,x.ping_stats))
    return {"job_id":job_id}

@app.get("/api/scan/{job_id}")
def scan_status(job_id:str):
    if job_id not in jobs: return _load_scan_job(job_id)
    return jobs[job_id]

@app.delete("/api/scan/{job_id}")
def cancel_scan(job_id:str):
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,"Scan không tồn tại")
    if job.get("state") in ("done","error","cancelled"):
        return {"ok":True,"state":job.get("state")}
    ev=_scan_cancel_events.get(job_id)
    if ev: ev.set()
    job["state"]="cancelling"
    job["cancel_requested"]=True
    return {"ok":True,"state":"cancelling"}

# Durable files live in the existing /data bind mount (one application process).
_SMART_DIR = Path(os.getenv('IPAM_STATE_DIR', '/data/ipam-state'))
_smart_lock = threading.RLock()
_job_checkpoints = {}

def _state_read(name, default):
    p=_SMART_DIR/name
    if not p.exists(): return default
    return json.loads(p.read_text())

def _state_write(name, value):
    _SMART_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p=_SMART_DIR/name; tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x') as f:
            os.chmod(tmp,0o600); json.dump(value,f,ensure_ascii=False,default=str)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        tmp.unlink(missing_ok=True)

def _save_scan_job(job, force=False):
    now=time.monotonic(); jid=job['id']
    with _smart_lock:
        if not force and now-_job_checkpoints.get(jid,0)<5: return
        _state_write('scan-'+jid+'.json',job)
        _job_checkpoints[jid]=now

def _load_scan_job(jid):
    if not re.fullmatch(r'[a-f0-9]{12}',jid): raise HTTPException(404,'Scan không tồn tại')
    with _smart_lock:
        job=_state_read('scan-'+jid+'.json',None)
        if not job: raise HTTPException(404,'Scan không tồn tại')
        if job.get('state') in ('queued','running','cancelling'):
            job.update(state='interrupted',phase='interrupted',error='Ứng dụng đã khởi động lại; kết quả đã đo được giữ lại.',finished=time.time())
            _save_scan_job(job,True)
        return job

def _scan_host_batches(net, size=256):
    batch=[]
    for addr in net.hosts():
        batch.append(str(addr))
        if len(batch)==size:
            yield batch; batch=[]
    if batch: yield batch

@app.get('/healthz',include_in_schema=False)
def healthz():
    c=None
    try:
        c=db(); c.execute('SELECT 1').fetchone()
        return {'status':'ok'}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse({'status':'unavailable'},status_code=503)
    finally:
        if c: c.close()

@app.get('/api/scan-history')
def scan_history():
    with _smart_lock:
        files=sorted(_SMART_DIR.glob('scan-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
        rows=[]
        for p in files:
            job=jobs.get(p.stem[5:]) or _load_scan_job(p.stem[5:])
            rows.append({k:job.get(k) for k in ('id','cidr','state','phase','started','finished','online','checked','total','error','created_by')})
    return {'rows':rows,'limit':100}

def _age_minutes(value):
    if not value: return None
    from datetime import datetime,timezone
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return max(0,round((datetime.now(timezone.utc)-dt).total_seconds()/60,1))
    except (ValueError,TypeError): return None

def _location_evidence(z):
    age=_age_minutes(z.get('last_known') or z.get('last_discovery'))
    hist=bool(z.get('historical')); uplink=bool(z.get('probable_uplink'))
    return {'source':'Lịch sử Discovery' if hist else 'SNMP FDB / Discovery',
            'observed_at':z.get('last_known') or z.get('last_discovery') or '',
            'age_minutes':age,'confidence_score':int(z.get('confidence') or 0),
            'kind':'history' if hist else ('uplink' if uplink else 'direct'),
            'reason':z.get('role_evidence') or ('MAC được học trên cổng này; vị trí suy luận từ Discovery.'),
            'score_note':'Điểm quy tắc, không phải xác suất xác nhận vật lý.'}

def _insight_item(kind,key,message,evidence,severity='warning'):
    fingerprint=hashlib.sha256(json.dumps([kind,key,evidence],sort_keys=True,default=str).encode()).hexdigest()[:24]
    return {'id':fingerprint,'kind':kind,'message':message,'evidence':evidence,'severity':severity}

@app.get('/api/insights')
def get_insights(request:Request):
    user=_auth_context(request)
    c=db(); items=[]
    try:
        if _has_permission(user,'discovery.read'):
            for row in c.execute('SELECT id,name,status,last_discovery,discovery_interval_minutes,last_error FROM network_devices').fetchall():
                r=dict(row); age=_age_minutes(r['last_discovery']); threshold=max(15,int(r['discovery_interval_minutes'] or DISCOVERY_AUTO_DEFAULT_MINUTES)*3)
                if str(r['status']).lower()!='up' or age is None or age>threshold or r['last_error']:
                    items.append(_insight_item('discovery',str(r['id']),f"{r['name']}: Discovery cần kiểm tra",{'status':r['status'],'error':r['last_error'] or '', 'reason':'Chưa có dữ liệu' if age is None else ('Dữ liệu quá hạn' if age>threshold else 'Lỗi/trạng thái thiết bị')},'warning'))
        if _has_permission(user,'dhcp.read'):
            for row in c.execute('SELECT source_id,name,last_sync FROM dhcp_sources').fetchall():
                r=dict(row); age=_age_minutes(r['last_sync'])
                if age is None or age>DHCP_SYNC_STALE_MINUTES:
                    items.append(_insight_item('dhcp',r['source_id'],f"{r['name'] or r['source_id']}: DHCP chưa đồng bộ đúng hạn",{'source_id':r['source_id'],'reason':'Chưa sync' if age is None else 'Sync quá hạn'},'warning'))
        if _has_permission(user,'ip.read') and _has_permission(user,'dhcp.read'):
            rows=c.execute("""SELECT i.ip,i.mac registered_mac,l.mac observed_mac,l.source_id FROM ips i
                JOIN dhcp_leases l ON l.ip=i.ip JOIN dhcp_sources s ON s.source_id=l.source_id
                WHERE TRIM(COALESCE(i.mac,''))<>'' AND TRIM(COALESCE(l.mac,''))<>''
                AND s.last_sync>=datetime('now', ?)""",(f'-{DHCP_SYNC_STALE_MINUTES} minutes',)).fetchall()
            for r in rows:
                a=normalize_mac(r['registered_mac']); b=normalize_mac(r['observed_mac'])
                if a and b and a!=b:
                    items.append(_insight_item('mac_mismatch',r['ip'],f"{r['ip']}: MAC khai báo khác DHCP hiện tại",{'ip':r['ip'],'ipam_mac':a,'dhcp_mac':b,'source':r['source_id'],'interpretation':'Mâu thuẫn dữ liệu, chưa chứng minh xung đột IP đang xảy ra.'}))
        if _has_permission(user,'discovery.read') and _has_permission(user,'ip.read'):
            arps=c.execute("""SELECT a.ip,a.mac,d.name source FROM network_arp_entries a
                JOIN network_devices d ON d.id=a.device_id WHERE LOWER(d.status)='up'
                AND d.last_discovery>=datetime('now', ?)
                AND a.last_seen>=datetime('now', ?)""",('-5 minutes','-5 minutes')).fetchall()
            grouped={}
            for r in arps:
                mac=normalize_mac(r['mac'])
                if mac: grouped.setdefault(r['ip'],{}).setdefault(mac,set()).add(r['source'])
            for ip,macs in grouped.items():
                if len(macs)>1:
                    items.append(_insight_item('multiple_mac',ip,f"{ip}: nhiều MAC trong ARP gần đây — cần xác minh",{'ip':ip,'macs':{m:sorted(src) for m,src in sorted(macs.items())},'interpretation':'Quan sát trong 5 phút; kiểm tra VLAN/VRF và HA trước khi kết luận xung đột.'}))
        if _has_permission(user,'scanner'):
            registered_ips={r['ip'] for r in c.execute('SELECT ip FROM ips').fetchall()}
            seen_new=set()
            for p in sorted(_SMART_DIR.glob('scan-*.json'),key=lambda x:x.stat().st_mtime,reverse=True)[:20]:
                job=jobs.get(p.stem[5:]) or _state_read(p.name,{})
                if time.time()-(job.get('finished') or job.get('started') or 0)>86400: continue
                for r in job.get('results',[]):
                    if r.get('registered') or not r.get('online'): continue
                    if r.get('status')!='unknown': continue
                    if r['ip'] in registered_ips or r['ip'] in seen_new: continue
                    seen_new.add(r['ip'])
                    items.append(_insight_item('new_endpoint',r['ip'],f"{r['ip']}: endpoint trả lời SCAN chưa đăng ký",{'ip':r['ip'],'mac':r.get('observed_mac') or '', 'method':r.get('method') or ''},'info'))
    finally: c.close()
    unique={x['id']:x for x in items}
    with _smart_lock: ack=_state_read('insight-ack.json',{})
    out=[]
    for x in unique.values():
        x['acknowledged']=x['id'] in ack; out.append(x)
    out.sort(key=lambda x:(x['acknowledged'],x['severity']!='warning',x['message']))
    return {'items':out[:200],'total':len(out),'active':sum(not x['acknowledged'] for x in out),'checked_at':time.time(),'limit':200}

class InsightAckIn(BaseModel):
    acknowledged: bool = True

@app.post('/api/insights/{insight_id}/ack')
def acknowledge_insight(insight_id:str,x:InsightAckIn,request:Request):
    visible={a['id'] for a in get_insights(request)['items']}
    if insight_id not in visible: raise HTTPException(404,'Cảnh báo không còn hoạt động hoặc không thuộc quyền truy cập')
    with _smart_lock:
        ack=_state_read('insight-ack.json',{})
        if x.acknowledged: ack[insight_id]={'at':time.time()}
        else: ack.pop(insight_id,None)
        _state_write('insight-ack.json',ack)
    audit('INSIGHT_ACK',insight_id,'acknowledged' if x.acknowledged else 'reopened')
    return {'ok':True}

from .studio import register as register_studio
register_studio(app, db, _auth_context, _has_permission, audit)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host=os.getenv("APP_BIND","0.0.0.0"),port=PORT)
