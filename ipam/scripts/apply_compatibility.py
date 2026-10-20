"""Apply the explicitly recorded portable-install patch to the image copy only."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
release = json.loads((root / 'distribution/release.json').read_text())
patch = release['distribution_patches'][0]
target = root / patch['file']
original = target.read_bytes()
assert hashlib.sha256(original).hexdigest() == patch['before_sha256'], 'Compatibility patch source mismatch'
needle = b'        if params:\n            s = s.replace("%s", "__PG_PARAM__")'
replacement = b'        if params is not None:\n            s = s.replace("%s", "__PG_PARAM__")'
assert original.count(needle) == 1, 'Expected parameter handling site not found'
updated = original.replace(needle, replacement)
assert hashlib.sha256(updated).hexdigest() == patch['after_sha256'], 'Compatibility patch result mismatch'
target.write_bytes(updated)
print('Applied portable-install fix: escape literal percent with empty parameter collections')
