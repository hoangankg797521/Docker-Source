#!/usr/bin/env python3
"""Verify captured code/assets match Production and the schema contains no rows."""
import hashlib
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
release = json.loads((root / 'release.json').read_text())
actual = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted((root / 'app').rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
assert actual == release['application_files'], 'Application differs from captured Production files'
for path, key in [('db/schema.sql', 'schema_sha256'), ('requirements.lock', 'requirements_sha256')]:
    assert hashlib.sha256((root / path).read_bytes()).hexdigest() == release[key], path + ' checksum mismatch'
schema = (root / 'db/schema.sql').read_text()
assert not re.search(r'^COPY public\.|^INSERT INTO public\.|^SELECT pg_catalog.setval', schema, re.M), 'Unexpected data in schema'
assert not any(p.suffix in {'.db', '.dump', '.sqlite'} for p in (root / 'app').rglob('*')), 'Unexpected data file'
assert release['contains_production_data'] is False
print(f"PASS: {len(actual)} Production source/assets verified; {len(release['schema_tables'])} empty schema tables")
