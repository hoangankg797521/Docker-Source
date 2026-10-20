import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wg_noc_server", ROOT / "server.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit=-1): return b'{"ok":true}'


class DashboardTests(unittest.TestCase):
    def test_normalize_client_drops_private_material_and_parses_fields(self):
        row = {"id":"peer_1", "name":"Laptop", "enabled":True,
               "privateKey":"NEVER_RETURN", "presharedKey":"NEVER_RETURN",
               "publicKey":"PUB", "ipv4Address":"10.255.66.2/32",
               "latestHandshakeAt":"2026-10-17T02:00:00Z", "transferRx":1024,
               "transferTx":2048, "allowedIps":["10.0.0.0/16"]}
        normalized = app.normalize_client(row)
        self.assertEqual(normalized["name"], "Laptop")
        self.assertEqual(normalized["rx"], 1024)
        self.assertEqual(normalized["allowedIps"], ["10.0.0.0/16"])
        self.assertNotIn("privateKey", normalized)
        self.assertNotIn("presharedKey", normalized)
        self.assertNotIn("NEVER_RETURN", json.dumps(normalized))

    def test_classification_thresholds(self):
        now = 1_800_000_000
        self.assertEqual(app.classify_client({"enabled":False,"handshake":now}, now), "disabled")
        self.assertEqual(app.classify_client({"enabled":True,"handshake":now-60}, now), "active")
        self.assertEqual(app.classify_client({"enabled":True,"handshake":now-400}, now), "recent")
        self.assertEqual(app.classify_client({"enabled":True,"handshake":now-1000}, now), "inactive")
        self.assertEqual(app.classify_client({"enabled":True,"handshake":None}, now), "unknown")

    def test_basic_auth_api_request_never_logs_or_persists_credentials(self):
        seen = {}
        def fake_open(req, timeout):
            seen["url"] = req.full_url
            seen["method"] = req.method
            seen["auth"] = req.get_header("Authorization")
            seen["timeout"] = timeout
            return Response()
        auth = base64.b64encode(b"admin:secret").decode()
        with patch.object(app.urllib.request, "urlopen", fake_open):
            status, body, _ = app.upstream_request(auth, "/api/client")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(seen["auth"], "Basic " + auth)
        self.assertEqual(seen["timeout"], 8)
        self.assertNotIn("secret", json.dumps(seen))

    def test_history_sampling_is_throttled_to_one_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_db, old_prev, old_last = app.DATA_DIR, app.DB_PATH, app.PREVIOUS_CLIENTS, app.LAST_HISTORY_WRITE
            try:
                app.DATA_DIR = Path(tmp)
                app.DB_PATH = app.DATA_DIR / "history.sqlite3"
                app.init_db()
                app.LAST_HISTORY_WRITE = 10**12
                app.PREVIOUS_CLIENTS = {}
                client = {"id":"peer1", "name":"test", "handshake":None, "rx":10, "tx":20, "enabled":True}
                app.record_snapshot([client], {})
                with app.sqlite3.connect(app.DB_PATH) as db:
                    count = db.execute("select count(*) from samples").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                app.DATA_DIR, app.DB_PATH, app.PREVIOUS_CLIENTS, app.LAST_HISTORY_WRITE = old_dir, old_db, old_prev, old_last

    def test_server_config_validation_accepts_host_and_dns(self):
        result = app.validated_server_config_input({
            "host": "vpn.example.com", "port": "51820", "dns": "192.168.50.53, 1.1.1.1",
            "allowedIps": "10.255.66.0/24\n172.16.8.0/22, 10.0.0.0/16"
        })
        self.assertEqual(result, {"host": "vpn.example.com", "port": 51820,
                                  "dns": ["192.168.50.53", "1.1.1.1"],
                                  "allowedIps": ["10.255.66.0/24", "172.16.8.0/22", "10.0.0.0/16"]})
        self.assertEqual(app.server_config_summary({"host": "203.0.113.10", "port": 51820,
            "defaultDns": ["192.168.50.53"], "defaultAllowedIps": ["10.0.0.0/16"]})["allowedIps"],
            ["10.0.0.0/16"])

    def test_server_config_validation_rejects_invalid_network_values(self):
        with self.assertRaises(app.UpstreamError):
            app.validated_server_config_input({"host": "vpn.example.com", "port": 70000,
                                               "dns": ["192.168.50.53"]})
        with self.assertRaises(app.UpstreamError):
            app.validated_server_config_input({"host": "vpn.example.com", "port": 51820,
                                               "dns": ["not-an-ip"], "allowedIps": ["10.0.0.0/16"]})
        with self.assertRaises(app.UpstreamError):
            app.validated_server_config_input({"host": "vpn.example.com", "port": 51820,
                                               "dns": [], "allowedIps": ["10.0.0.0/99"]})


if __name__ == "__main__":
    unittest.main()
