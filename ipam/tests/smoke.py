"""Exercise a disposable new install without contacting network equipment."""
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

base = os.environ['IPAM_TEST_URL'].rstrip('/')
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(path, data=None, raw=False):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=30) as res:
        return res.read() if raw else json.load(res)


try:
    request('/api/auth/login', {'username': 'admin', 'password': 'admin'})
except urllib.error.HTTPError as exc:
    assert exc.code == 401, 'Default password rejection returned an unexpected response'
else:
    raise AssertionError('Insecure default admin password is accepted')
request('/api/auth/login', {'username': 'admin', 'password': os.environ['IPAM_TEST_PASSWORD']})
routes = ['/healthz', '/api/auth/me', '/api/stats', '/api/dashboard', '/api/ips', '/api/subnets',
          '/api/history', '/api/audit', '/api/scan-history', '/api/dhcp/status',
          '/api/discovery/devices', '/api/discovery/profiles', '/api/discovery/topology',
          '/api/studio/health', '/api/studio/inventory', '/api/studio/connections',
          '/api/studio/port-mapping', '/api/studio/ha', '/api/studio/servers', '/api/admin/roles']
for route in routes:
    request(route)
existing = '--existing' in sys.argv
servers = request('/api/studio/servers')['servers']
diagrams = request('/api/studio/diagrams')
if not existing:
    assert not request('/api/studio/inventory')['devices'], 'Fresh package contains network inventory'
    assert not servers and not diagrams, 'Fresh package contains Production documentation'
    request('/api/studio/servers', {'hostname': 'LAB-PACKAGE', 'management_ips': ['192.0.2.10'],
                                   'description': 'Kiểm thử đóng gói IPAM', 'interfaces': [{'name': 'eth0'}]})
    request('/api/studio/diagrams', {'title': 'Kiểm thử đóng gói', 'kind': 'physical',
                                    'document': {'nodes': [], 'edges': [], 'viewport': {}}})
else:
    assert any(s['hostname'] == 'LAB-PACKAGE' for s in servers), 'Server data was lost across restart/restore'
    assert any(d['title'] == 'Kiểm thử đóng gói' for d in diagrams), 'Diagram data was lost across restart/restore'
pdf = request('/api/studio/export/pdf?scope=all&documentation_scope=full', raw=True)
assert pdf.startswith(b'%PDF-') and len(pdf) > 1000, 'PDF export failed'
output = Path(os.environ.get('IPAM_TEST_OUTPUT', '/tmp/ipam-package-smoke'))
output.mkdir(parents=True, exist_ok=True)
(output / 'package-smoke.pdf').write_bytes(pdf)
html = request('/', raw=True).decode()
assert 'IP Manager' in html and 'studio.js' in html
for asset in ['studio.js', 'studio.css', 'topology-v2.js', 'server-connections.js', 'ha-management.js',
              'studio-export-v5.js', 'studio-export-ha-v6.js']:
    assert len(request('/studio/' + asset, raw=True)) > 100
report = {'result': 'PASS', 'phase': 'existing' if existing else 'fresh', 'api_routes': len(routes),
          'default_password_rejected': True, 'pdf_bytes': len(pdf), 'assets': 7,
          'production_data_included': False, 'network_scans_triggered': False}
(output / ('smoke-existing.json' if existing else 'smoke-fresh.json')).write_text(json.dumps(report, indent=2))
print(json.dumps(report))
