#!/usr/bin/env python3
"""Small, single-process NOC dashboard adapter for wg-easy v15.4.0.

The wg-easy credential is accepted only at login and held in process memory for
the life of the session. It is never written to disk, returned to the browser,
or included in logs. The adapter deliberately exposes only selected client
fields; private and preshared keys stay inside wg-easy.
"""

from __future__ import annotations

import base64
import json
import ipaddress
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "history.sqlite3"
WG_URL = os.environ.get("WG_EASY_URL", "http://wg-easy:51821").rstrip("/")
WG_VERSION = os.environ.get("WG_EASY_VERSION", "15.4.0")
WG_UI_URL = os.environ.get("WG_EASY_UI_URL", "http://localhost:51821")
VPN_ENDPOINT = os.environ.get("VPN_ENDPOINT", "vpn.example.com")
VPN_SUBNET = os.environ.get("VPN_SUBNET", "10.255.66.0/24")
VPN_DNS = os.environ.get("VPN_DNS", "1.1.1.1")
VPN_PORT = int(os.environ.get("VPN_PORT", "51820"))
COOKIE_NAME = "wg_noc_session"
SESSION_TTL = 30 * 60
POLL_SECONDS = 15
HISTORY_SECONDS = 60
RETENTION_SECONDS = 7 * 24 * 60 * 60
SESSION_LOCK = threading.RLock()
SESSIONS: dict[str, dict[str, Any]] = {}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
PREVIOUS_CPU: tuple[int, int] | None = None
PREVIOUS_CLIENTS: dict[str, dict[str, Any]] = {}
LAST_HISTORY_WRITE = 0.0

CLIENT_UPDATE_FIELDS = (
    "name", "enabled", "expiresAt", "ipv4Address", "ipv6Address", "preUp",
    "postUp", "preDown", "postDown", "allowedIps", "serverAllowedIps",
    "firewallIps", "mtu", "jC", "jMin", "jMax", "i1", "i2", "i3", "i4",
    "i5", "persistentKeepalive", "serverEndpoint", "dns",
)


class UpstreamError(Exception):
    def __init__(self, status: int, message: str = "wg-easy API request failed"):
        super().__init__(message)
        self.status = status
        self.message = message


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER NOT NULL,
            peer_id TEXT NOT NULL,
            name TEXT NOT NULL,
            rx INTEGER,
            tx INTEGER,
            handshake INTEGER,
            enabled INTEGER NOT NULL,
            PRIMARY KEY (ts, peer_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            peer_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        db.commit()
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass


def _first(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = int(value)
        return n // 1000 if n > 10_000_000_000 else n
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _client_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("clients", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise UpstreamError(502, "wg-easy returned an unsupported client response")
    return [row for row in payload if isinstance(row, dict)]


def normalize_client(row: dict[str, Any]) -> dict[str, Any]:
    status_obj = row.get("status") if isinstance(row.get("status"), dict) else {}
    transfer_obj = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
    handshake = _timestamp(_first(row, "latestHandshakeAt", "latestHandshake", "lastHandshake", "handshakeAt")
                           or _first(status_obj, "latestHandshakeAt", "latestHandshake", "lastHandshake"))
    rx = _as_int(_first(row, "transferRx", "transferRxBytes", "rxBytes", "receivedBytes", "rx")
                 or _first(transfer_obj, "rx", "received", "receivedBytes"))
    tx = _as_int(_first(row, "transferTx", "transferTxBytes", "txBytes", "sentBytes", "tx")
                 or _first(transfer_obj, "tx", "sent", "sentBytes"))
    enabled = _first(row, "enabled", "isEnabled")
    if enabled is None:
        enabled = _first(status_obj, "enabled", "isEnabled")
    peer_id = _first(row, "id", "clientId")
    if peer_id is None:
        peer_id = _first(row, "publicKey")
    allowed = _first(row, "allowedIps", "allowedIPs", "allowed_ips")
    if isinstance(allowed, str):
        allowed = [v.strip() for v in allowed.split(",") if v.strip()]
    return {
        "id": str(peer_id or ""),
        "name": str(_first(row, "name", "clientName") or "Client"),
        "ipv4Address": _first(row, "ipv4Address", "ipv4", "address4"),
        "ipv6Address": _first(row, "ipv6Address", "ipv6", "address6"),
        "publicKey": _first(row, "publicKey"),
        "enabled": bool(enabled) if enabled is not None else None,
        "endpoint": _first(row, "endpoint", "latestEndpoint", "remoteEndpoint")
                    or _first(status_obj, "endpoint", "latestEndpoint"),
        "handshake": handshake,
        "rx": rx,
        "tx": tx,
        "allowedIps": allowed if isinstance(allowed, list) else [],
        "serverAllowedIps": _first(row, "serverAllowedIps", "serverAllowedIPs") or [],
        "dns": _first(row, "dns"),
        "expiresAt": _first(row, "expiresAt", "expireDate"),
        "createdAt": _first(row, "createdAt"),
        "mtu": _first(row, "mtu"),
        "persistentKeepalive": _first(row, "persistentKeepalive"),
    }


def classify_client(client: dict[str, Any], now: int | None = None) -> str:
    if client.get("enabled") is False:
        return "disabled"
    handshake = client.get("handshake")
    if not handshake:
        return "unknown"
    age = (now or int(time.time())) - int(handshake)
    if age < 0:
        return "unknown"
    if age <= 180:
        return "active"
    if age <= 900:
        return "recent"
    return "inactive"


def read_host_health() -> dict[str, Any]:
    global PREVIOUS_CPU
    cpu_pct = None
    try:
        first = Path("/host/proc/stat").read_text().splitlines()[0].split()
        vals = [int(x) for x in first[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        if PREVIOUS_CPU:
            dt = total - PREVIOUS_CPU[0]
            di = idle - PREVIOUS_CPU[1]
            if dt > 0:
                cpu_pct = round(max(0.0, min(100.0, (dt - di) * 100 / dt)), 1)
        PREVIOUS_CPU = (total, idle)
    except (OSError, IndexError, ValueError):
        pass
    memory = {}
    try:
        for line in Path("/host/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        total = memory.get("MemTotal")
        available = memory.get("MemAvailable")
        mem_used = max(0, total - available) if total is not None and available is not None else None
    except (OSError, ValueError):
        total, available, mem_used = None, None, None
    try:
        uptime = float(Path("/host/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    try:
        disk = shutil.disk_usage("/vm-disk-marker")
        used = disk.total - disk.free
        disk_data = {
            "total": disk.total,
            "used": used,
            "free": disk.free,
            "percent": round(used * 100 / disk.total, 1) if disk.total else None,
            "source": "Filesystem Ubuntu VM chứa /etc/hostname (mount chỉ đọc)",
        }
    except OSError:
        disk_data = {
            "total": None, "used": None, "free": None, "percent": None,
            "source": "Không đọc được filesystem Ubuntu VM",
        }
    return {
        "cpuPercent": cpu_pct,
        "memory": {"total": total, "used": mem_used, "available": available,
                    "percent": round(mem_used * 100 / total, 1) if total and mem_used is not None else None},
        "disk": disk_data,
        "uptimeSeconds": int(uptime) if uptime is not None else None,
        "sampledAt": int(time.time()),
    }


def upstream_request(auth: str, path: str, method: str = "GET", body: Any = None) -> tuple[int, bytes, str]:
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    req = urllib.request.Request(
        WG_URL + path,
        data=data,
        method=method,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, response.read(2_000_000), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(64_000), e.headers.get("Content-Type", "")
    except Exception as e:
        raise UpstreamError(502, "Unable to reach the wg-easy API") from e


def json_from_upstream(auth: str, path: str) -> Any:
    status, raw, _ = upstream_request(auth, path)
    if status == 401:
        raise UpstreamError(401, "wg-easy rejected the session; sign in again")
    if status < 200 or status >= 300:
        raise UpstreamError(status if 400 <= status <= 599 else 502, "wg-easy API request failed")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UpstreamError(502, "wg-easy returned invalid JSON") from e


def server_config_summary(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise UpstreamError(502, "wg-easy returned unsupported server configuration")
    dns = config.get("defaultDns", [])
    allowed_ips = config.get("defaultAllowedIps", [])
    if isinstance(dns, str):
        dns = [x.strip() for x in dns.split(",") if x.strip()]
    if isinstance(allowed_ips, str):
        allowed_ips = [x.strip() for x in allowed_ips.split(",") if x.strip()]
    return {"host": str(config.get("host", "")), "port": config.get("port"),
            "dns": dns if isinstance(dns, list) else [],
            "allowedIps": allowed_ips if isinstance(allowed_ips, list) else []}


def validated_server_config_input(body: dict[str, Any]) -> dict[str, Any]:
    host = str(body.get("host", "")).strip()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        raise UpstreamError(400, "Host phải là địa chỉ IP hoặc tên máy chủ hợp lệ")
    host_value = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if ":" in host_value and not (host.startswith("[") and host.endswith("]")):
        raise UpstreamError(400, "Địa chỉ IPv6 phải đặt trong dấu ngoặc vuông")
    try:
        ipaddress.ip_address(host_value)
    except ValueError:
        if ":" in host or not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?", host):
            raise UpstreamError(400, "Host phải là địa chỉ IP hoặc tên máy chủ hợp lệ")
    try:
        port = int(body.get("port"))
    except (TypeError, ValueError):
        raise UpstreamError(400, "Port phải nằm trong khoảng 1–65535")
    if not 1 <= port <= 65535:
        raise UpstreamError(400, "Port phải nằm trong khoảng 1–65535")
    raw_dns = body.get("dns", [])
    if isinstance(raw_dns, str):
        raw_dns = [x.strip() for x in raw_dns.replace("\n", ",").split(",") if x.strip()]
    if not isinstance(raw_dns, list) or len(raw_dns) > 8:
        raise UpstreamError(400, "DNS phải gồm tối đa 8 địa chỉ IP")
    dns = []
    for item in raw_dns:
        value = str(item).strip()
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise UpstreamError(400, "DNS chỉ chấp nhận địa chỉ IPv4 hoặc IPv6")
        if value not in dns:
            dns.append(value)
    raw_allowed = body.get("allowedIps", [])
    if isinstance(raw_allowed, str):
        raw_allowed = [x.strip() for x in raw_allowed.replace("\n", ",").split(",") if x.strip()]
    if not isinstance(raw_allowed, list) or not 1 <= len(raw_allowed) <= 64:
        raise UpstreamError(400, "Allowed IPs phải gồm từ 1 đến 64 địa chỉ hoặc mạng CIDR")
    allowed_ips = []
    for item in raw_allowed:
        value = str(item).strip()
        try:
            if "/" in value:
                normalized = str(ipaddress.ip_network(value, strict=False))
            else:
                normalized = str(ipaddress.ip_address(value))
        except ValueError:
            raise UpstreamError(400, "Allowed IPs chỉ chấp nhận địa chỉ IP hoặc mạng CIDR hợp lệ")
        if normalized not in allowed_ips:
            allowed_ips.append(normalized)
    return {"host": host, "port": port, "dns": dns, "allowedIps": allowed_ips}


def get_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict[str, Any] | None]:
    cookies = handler.headers.get("Cookie", "")
    sid = None
    for part in cookies.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            sid = v
            break
    if not sid:
        return None, None
    now = time.time()
    with SESSION_LOCK:
        session = SESSIONS.get(sid)
        if session and now - session["last"] <= SESSION_TTL:
            session["last"] = now
            return sid, session
        SESSIONS.pop(sid, None)
    return None, None


def ensure_auth(handler: BaseHTTPRequestHandler) -> tuple[str, dict[str, Any]]:
    sid, session = get_session(handler)
    if not sid or not session:
        raise UpstreamError(401, "Phiên đăng nhập đã hết hạn")
    return sid, session


def record_snapshot(clients: list[dict[str, Any]], health: dict[str, Any]) -> None:
    global PREVIOUS_CLIENTS, LAST_HISTORY_WRITE
    now = int(time.time())
    with sqlite3.connect(DB_PATH, timeout=5) as db:
        write_sample = now - LAST_HISTORY_WRITE >= HISTORY_SECONDS
        for c in clients:
            if not c["id"]:
                continue
            prev = PREVIOUS_CLIENTS.get(c["id"])
            if prev and c.get("handshake") and c.get("handshake") != prev.get("handshake"):
                db.execute("INSERT INTO events(ts,peer_id,name,kind,detail) VALUES(?,?,?,?,?)",
                           (now, c["id"], c["name"], "handshake", "Bắt tay mới được quan sát"))
            if write_sample:
                db.execute("INSERT OR REPLACE INTO samples(ts,peer_id,name,rx,tx,handshake,enabled) VALUES(?,?,?,?,?,?,?)",
                           (now, c["id"], c["name"], c.get("rx"), c.get("tx"), c.get("handshake"),
                            1 if c.get("enabled") else 0))
        if write_sample:
            LAST_HISTORY_WRITE = now
        cutoff = now - RETENTION_SECONDS
        db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        db.commit()
    PREVIOUS_CLIENTS = {c["id"]: c for c in clients if c.get("id")}


def collector() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        now = time.time()
        with SESSION_LOCK:
            for sid in list(SESSIONS):
                if now - SESSIONS[sid]["last"] > SESSION_TTL:
                    SESSIONS.pop(sid, None)
            active = next(iter(SESSIONS.values()), None)
        if not active:
            continue
        try:
            payload = json_from_upstream(active["auth"], "/api/client")
            clients = [normalize_client(r) for r in _client_rows(payload)]
            record_snapshot(clients, read_host_health())
        except Exception:
            continue


def _required_origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    host = handler.headers.get("Host", "")
    return origin in ("http://" + host, "https://" + host)


def proxy_acl(handler, session, write=False):
    # Revalidate admin permission with wg-easy on every ACL operation.
    json_from_upstream(session["auth"], "/api/admin/userconfig")
    base = os.environ.get("ACL_BACKEND_URL", "")
    if not base:
        raise UpstreamError(503, "Unified ACL backend chưa được cấu hình")
    path = handler.path.replace("/api/acl", "/v1", 1)
    if not re.fullmatch(r"/v1/clients(?:/[A-Za-z0-9_-]{1,64}(?:/audit)?)?", path):
        raise UpstreamError(404, "ACL route not found")
    try:
        token = Path(os.environ.get("ACL_TOKEN_FILE", "/run/secrets/noc_token")).read_text().strip()
        payload = json.dumps(handler.read_json()).encode() if write else None
        req = urllib.request.Request(base + path, data=payload, method="POST" if write else "GET",
            headers={"Authorization": "Bearer " + token, "X-ACL-Actor": session["username"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                handler.send_json(response.status, json.loads(response.read(262144)))
        except urllib.error.HTTPError as e:
            result = json.loads(e.read(8192))
            handler.send_json(e.code, {"message": result.get("error", "ACL request failed")})
    except (OSError, ValueError):
        raise UpstreamError(503, "ACL backend không sẵn sàng; không xác nhận áp dụng thành công")


class Handler(BaseHTTPRequestHandler):
    server_version = "WG-NOC/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid logging request bodies, query contents, peer IDs or credentials.
        safe_path = re.sub(r"(/api/clients/)[A-Za-z0-9_-]+", r"\1[peer]", self.path.split("?", 1)[0])
        print(f"{self.log_date_time_string()} {self.command} {safe_path} {args[1] if len(args) > 1 else ''}")

    def _headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def send_json(self, status: int, value: Any, extra_headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self._headers()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 16_384:
            raise UpstreamError(400, "Invalid request body")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise UpstreamError(400, "Invalid JSON") from e
        if not isinstance(data, dict):
            raise UpstreamError(400, "JSON object required")
        return data

    def do_GET(self) -> None:
        try:
            if self.path == "/api/health":
                self.send_json(200, {"ok": True, "version": WG_VERSION})
                return
            if self.path == "/api/session":
                _, session = get_session(self)
                if not session:
                    self.send_json(401, {"error": "not_authenticated"})
                    return
                self.send_json(200, {"username": session["username"], "csrf": session["csrf"],
                                     "expiresIn": max(0, SESSION_TTL - int(time.time() - session["last"]))})
                return
            _, session = ensure_auth(self)
            if self.path.startswith("/api/acl/"):
                proxy_acl(self, session)
                return
            if self.path == "/api/config":
                config = json_from_upstream(session["auth"], "/api/admin/userconfig")
                self.send_json(200, server_config_summary(config))
                return
            if self.path == "/api/overview":
                payload = json_from_upstream(session["auth"], "/api/client")
                clients = [normalize_client(row) for row in _client_rows(payload)]
                now = int(time.time())
                counts = {k: 0 for k in ("active", "recent", "inactive", "disabled", "unknown")}
                for client in clients:
                    counts[classify_client(client, now)] += 1
                health = read_host_health()
                server = {"version": WG_VERSION, "api": "online", "wgRuntime": "not independently queried",
                          "vpnEndpoint": VPN_ENDPOINT, "vpnSubnet": VPN_SUBNET, "dns": VPN_DNS,
                          "udpPort": VPN_PORT, "interface": "wg0", "uptimeSeconds": health["uptimeSeconds"]}
                summary = {"totalClients": len(clients), "counts": counts,
                           "rx": sum(c["rx"] or 0 for c in clients),
                           "tx": sum(c["tx"] or 0 for c in clients),
                           "trafficAvailable": any(c["rx"] is not None or c["tx"] is not None for c in clients)}
                record_snapshot(clients, health)
                self.send_json(200, {"server": server, "summary": summary, "health": health,
                                     "clients": clients, "sampledAt": now,
                                     "statusDefinition": {"activeSeconds": 180, "recentSeconds": 900,
                                         "note": "Handshake gần đây là dấu hiệu hoạt động, không chứng minh peer vẫn online."}})
                return
            if self.path.startswith("/api/history"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                span = query.get("range", ["1h"])[0]
                durations = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": RETENTION_SECONDS}
                if span not in durations:
                    raise UpstreamError(400, "Unsupported time range")
                self._history(session, span, durations[span])
                return
            if self.path == "/api/events":
                with sqlite3.connect(DB_PATH) as db:
                    rows = db.execute("SELECT ts,peer_id,name,kind,detail FROM events ORDER BY ts DESC LIMIT 200").fetchall()
                self.send_json(200, {"items": [dict(zip(("timestamp", "clientId", "name", "kind", "detail"), r)) for r in rows],
                                     "note": "Sự kiện được suy ra từ các lần lấy mẫu khi có phiên quản trị đăng nhập."})
                return
            if self.path == "/api/clients":
                payload = json_from_upstream(session["auth"], "/api/client")
                now = int(time.time())
                rows = [normalize_client(r) for r in _client_rows(payload)]
                for row in rows:
                    row["state"] = classify_client(row, now)
                self.send_json(200, {"items": rows, "sampledAt": now})
                return
            match = re.fullmatch(r"/api/clients/([A-Za-z0-9_-]+)/(configuration|qrcode\.svg)", self.path)
            if match:
                client_id, kind = match.groups()
                suffix = "configuration" if kind == "configuration" else "qrcode.svg"
                status, raw, content_type = upstream_request(session["auth"], f"/api/client/{client_id}/{suffix}")
                if status == 401:
                    raise UpstreamError(401, "wg-easy rejected the session; sign in again")
                if not 200 <= status < 300:
                    raise UpstreamError(status if 400 <= status <= 599 else 502, "wg-easy export failed")
                safe_name = f"wireguard-client-{client_id}.conf" if kind == "configuration" else ""
                self.send_response(200)
                if kind == "configuration":
                    response_type = "application/octet-stream"
                else:
                    # wg-easy v15 returns SVG for some builds and a PNG data URL
                    # for others. Preserve only a known image MIME type for the UI.
                    response_type = "image/png" if content_type.lower().startswith("image/png") or raw.startswith(b"data:image/png;base64,") else "image/svg+xml"
                    if raw.startswith(b"data:image/png;base64,"):
                        try:
                            import base64
                            raw = base64.b64decode(raw.split(b",", 1)[1], validate=True)
                        except Exception as e:
                            raise UpstreamError(502, "wg-easy returned invalid QR data") from e
                    elif content_type.lower().startswith("image/png") and raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        pass
                    elif raw.lstrip().startswith(b"<svg") or b"<svg" in raw[:256]:
                        response_type = "image/svg+xml"
                    else:
                        raise UpstreamError(502, "wg-easy returned an unsupported QR image")
                self._headers(response_type)
                if safe_name:
                    self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_json(404, {"error": "not_found"})
        except UpstreamError as e:
            if e.status == 401:
                self.send_json(401, {"error": "authentication_required", "message": e.message},
                               {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            else:
                self.send_json(e.status, {"error": "request_failed", "message": e.message})
        except Exception:
            self.send_json(500, {"error": "internal_error", "message": "Internal dashboard error"})

    def _history(self, session: dict[str, Any], span: str, duration: int) -> None:
        cutoff = int(time.time()) - duration
        with sqlite3.connect(DB_PATH) as db:
            rows = db.execute("SELECT ts,peer_id,name,rx,tx FROM samples WHERE ts>=? ORDER BY peer_id,ts", (cutoff,)).fetchall()
        by_peer: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            by_peer.setdefault(row[1], []).append(row)
        interval = 60 if span == "1h" else 300 if span == "6h" else 900 if span == "24h" else 3600
        buckets: dict[int, dict[str, int]] = {}
        clients: dict[str, dict[str, int | str]] = {}
        points = 0
        for peer_id, history in by_peer.items():
            previous = None
            for ts, _, name, rx, tx in history:
                if previous:
                    prev_rx, prev_tx = previous[1], previous[2]
                    # Counter reset/restart: drop negative deltas instead of drawing a spike.
                    drx = max(0, rx - prev_rx) if rx is not None and prev_rx is not None else 0
                    dtx = max(0, tx - prev_tx) if tx is not None and prev_tx is not None else 0
                    if rx is not None and prev_rx is not None and rx < prev_rx:
                        drx = 0
                    if tx is not None and prev_tx is not None and tx < prev_tx:
                        dtx = 0
                    bucket = int(ts // interval * interval)
                    acc = buckets.setdefault(bucket, {"rx": 0, "tx": 0})
                    acc["rx"] += drx
                    acc["tx"] += dtx
                    c = clients.setdefault(peer_id, {"name": name, "rx": 0, "tx": 0})
                    c["rx"] += drx
                    c["tx"] += dtx
                    points += 1
                previous = (ts, rx, tx)
        series = [{"timestamp": ts, "rx": value["rx"], "tx": value["tx"]}
                  for ts, value in sorted(buckets.items())]
        top = sorted(({"clientId": k, **v} for k, v in clients.items()),
                     key=lambda item: item["rx"] + item["tx"], reverse=True)[:8]
        self.send_json(200, {"range": span, "series": series, "topClients": top,
                             "samples": len(rows), "deltaPairs": points,
                             "note": "Dữ liệu lịch sử chỉ được thu khi phiên quản trị còn hiệu lực; khoảng trống được giữ nguyên, không nội suy."})

    def do_POST(self) -> None:
        try:
            if not _required_origin_ok(self):
                raise UpstreamError(403, "Cross-origin request blocked")
            if self.path == "/api/login":
                ip = self.client_address[0]
                now = time.time()
                attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 60]
                if len(attempts) >= 5:
                    raise UpstreamError(429, "Too many sign-in attempts; wait one minute")
                body = self.read_json()
                username = str(body.get("username", ""))[:128]
                password = str(body.get("password", ""))[:512]
                if not username or not password:
                    raise UpstreamError(400, "Username and password are required")
                auth = base64.b64encode((username + ":" + password).encode()).decode()
                try:
                    payload = json_from_upstream(auth, "/api/client")
                    _client_rows(payload)
                except UpstreamError as e:
                    attempts.append(now)
                    LOGIN_ATTEMPTS[ip] = attempts
                    if e.status == 401:
                        raise UpstreamError(401, "Đăng nhập wg-easy thất bại. API Basic Auth không hỗ trợ tài khoản bật 2FA.")
                    raise
                sid = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(24)
                with SESSION_LOCK:
                    SESSIONS[sid] = {"auth": auth, "username": username, "csrf": csrf, "last": now}
                LOGIN_ATTEMPTS.pop(ip, None)
                self.send_json(200, {"username": username, "csrf": csrf, "expiresIn": SESSION_TTL},
                               {"Set-Cookie": f"{COOKIE_NAME}={sid}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict"})
                clients = [normalize_client(r) for r in _client_rows(payload)]
                record_snapshot(clients, read_host_health())
                return
            sid, session = ensure_auth(self)
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise UpstreamError(403, "Invalid CSRF token")
            if self.path.startswith("/api/acl/"):
                proxy_acl(self, session, write=True)
                return
            if self.path == "/api/config":
                body = validated_server_config_input(self.read_json())
                current = json_from_upstream(session["auth"], "/api/admin/userconfig")
                if not isinstance(current, dict):
                    raise UpstreamError(502, "wg-easy returned unsupported server configuration")
                current["host"] = body["host"]
                current["port"] = body["port"]
                current["defaultDns"] = body["dns"]
                current["defaultAllowedIps"] = body["allowedIps"]
                status, raw, _ = upstream_request(session["auth"], "/api/admin/userconfig", "POST", current)
                self._mutation_response(status, raw)
                return
            if self.path == "/api/logout":
                with SESSION_LOCK:
                    SESSIONS.pop(sid, None)
                self.send_json(200, {"ok": True},
                               {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
                return
            if self.path == "/api/clients":
                body = self.read_json()
                name = str(body.get("name", "")).strip()
                if not name or len(name) > 64:
                    raise UpstreamError(400, "Client name must contain 1–64 characters")
                expires = body.get("expiresAt")
                if expires is not None:
                    try:
                        datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    except ValueError:
                        raise UpstreamError(400, "Expiration date must be ISO 8601")
                status, raw, _ = upstream_request(session["auth"], "/api/client", "POST",
                                                   {"name": name, "expiresAt": expires})
                self._mutation_response(status, raw)
                return
            match = re.fullmatch(r"/api/clients/([A-Za-z0-9_-]+)/(enable|disable)", self.path)
            if match:
                client_id, action = match.groups()
                status, raw, _ = upstream_request(session["auth"], f"/api/client/{client_id}/{action}", "POST", {})
                self._mutation_response(status, raw)
                return
            match = re.fullmatch(r"/api/clients/([A-Za-z0-9_-]+)", self.path)
            if match:
                client_id = match.group(1)
                body = self.read_json()
                name = str(body.get("name", "")).strip()
                if not name or len(name) > 64:
                    raise UpstreamError(400, "Client name must contain 1–64 characters")
                detail = json_from_upstream(session["auth"], f"/api/client/{client_id}")
                if not isinstance(detail, dict):
                    raise UpstreamError(502, "wg-easy returned unsupported client details")
                safe_update = {field: detail[field] for field in CLIENT_UPDATE_FIELDS if field in detail}
                safe_update["name"] = name
                missing = [field for field in CLIENT_UPDATE_FIELDS if field not in safe_update]
                if missing:
                    raise UpstreamError(501, "This wg-easy API response lacks fields needed for a safe edit; use the native UI")
                status, raw, _ = upstream_request(session["auth"], f"/api/client/{client_id}", "POST", safe_update)
                self._mutation_response(status, raw)
                return
            raise UpstreamError(404, "not_found")
        except UpstreamError as e:
            self.send_json(e.status, {"error": "request_failed", "message": e.message})
        except Exception:
            self.send_json(500, {"error": "internal_error", "message": "Internal dashboard error"})

    def do_DELETE(self) -> None:
        try:
            if not _required_origin_ok(self):
                raise UpstreamError(403, "Cross-origin request blocked")
            sid, session = ensure_auth(self)
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise UpstreamError(403, "Invalid CSRF token")
            match = re.fullmatch(r"/api/clients/([A-Za-z0-9_-]+)", self.path)
            if not match:
                raise UpstreamError(404, "not_found")
            status, raw, _ = upstream_request(session["auth"], f"/api/client/{match.group(1)}", "DELETE")
            self._mutation_response(status, raw)
        except UpstreamError as e:
            self.send_json(e.status, {"error": "request_failed", "message": e.message})
        except Exception:
            self.send_json(500, {"error": "internal_error", "message": "Internal dashboard error"})

    def _mutation_response(self, status: int, raw: bytes) -> None:
        if status == 401:
            raise UpstreamError(401, "wg-easy rejected the session; sign in again")
        if not 200 <= status < 300:
            message = "wg-easy rejected the change"
            try:
                parsed = json.loads(raw)
                candidate = parsed.get("statusMessage") or parsed.get("message")
                if isinstance(candidate, str) and len(candidate) < 240:
                    message = candidate
            except Exception:
                pass
            raise UpstreamError(status if 400 <= status <= 599 else 502, message)
        try:
            data = json.loads(raw) if raw else {"success": True}
        except json.JSONDecodeError:
            data = {"success": True}
        self.send_json(200, {"success": True, "result": data})

    def do_GET_static(self) -> None:
        request_path = urllib.parse.urlsplit(self.path).path
        target = STATIC / "index.html" if request_path == "/" else (STATIC / request_path.lstrip("/"))
        try:
            resolved = target.resolve()
            if not resolved.is_relative_to(STATIC.resolve()) or not resolved.is_file():
                self.send_error(404)
                return
            mime = "text/html; charset=utf-8" if resolved.suffix == ".html" else "text/css; charset=utf-8" if resolved.suffix == ".css" else "application/javascript; charset=utf-8" if resolved.suffix == ".js" else "image/png" if resolved.suffix == ".png" else "application/octet-stream"
            raw = resolved.read_bytes()
            self.send_response(200)
            self._headers(mime)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            self.send_error(404)

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/"):
            self.send_response(405)
            self.end_headers()
            return
        self.path = "/" if self.path == "/" else self.path
        target = STATIC / "index.html" if self.path == "/" else STATIC / self.path.lstrip("/")
        try:
            resolved = target.resolve()
            raw = resolved.read_bytes()
            self.send_response(200)
            self._headers()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
        except OSError:
            self.send_error(404)


_do_get_api = Handler.do_GET

def _dispatch_get(self: Handler) -> None:
    if self.path.startswith("/api/"):
        _do_get_api(self)
    else:
        self.do_GET_static()

Handler.do_GET = _dispatch_get


def main() -> None:
    os.umask(0o077)
    init_db()
    threading.Thread(target=collector, name="wg-history-collector", daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"wg-noc dashboard listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
