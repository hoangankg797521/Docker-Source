#!/usr/bin/env python3
"""Restore a verified snapshot into an EMPTY deployment directory; does not start Docker."""
import hashlib, pathlib, shutil, sqlite3, sys, tarfile, tempfile
root=pathlib.Path(__file__).resolve().parent
if len(sys.argv)!=2: raise SystemExit('Usage: python3 restore.py /path/to/npm-state-YYYYMMDD.tar.gz')
for name in ('data','letsencrypt'):
 if (root/name).exists(): raise SystemExit(f'Refusing to overwrite {root/name}; use a new empty deployment folder')
with tempfile.TemporaryDirectory() as temp:
 p=pathlib.Path(temp)
 with tarfile.open(sys.argv[1]) as t: t.extractall(p,filter='data')
 if not (p/'VERIFIED').is_file() or (p/'INCOMPLETE').exists(): raise SystemExit('Snapshot not verified')
 for line in (p/'SHA256SUMS').read_text().splitlines():
  checksum,name=line.split('  ',1)
  f=p/name
  if f.parent!=p or hashlib.sha256(f.read_bytes()).hexdigest()!=checksum: raise SystemExit('Checksum mismatch')
 with tarfile.open(p/'npm-volumes.tar.gz') as t:
  for m in t.getmembers():
   parts=pathlib.PurePosixPath(m.name).parts
   if not parts or parts[0] not in ('data','letsencrypt') or '..' in parts: raise SystemExit('Unexpected volume path')
  t.extractall(p/'volumes',filter='data')
 shutil.copy2(p/'database.sqlite',p/'volumes/data/database.sqlite')
 with sqlite3.connect(f'file:{p}/volumes/data/database.sqlite?mode=ro',uri=True) as db:
  if db.execute('pragma integrity_check').fetchone()[0]!='ok': raise SystemExit('SQLite integrity check failed')
 for name in ('data','letsencrypt'): shutil.move(str(p/'volumes'/name),str(root/name))
print('Restored volumes and verified SQLite. Docker has NOT been started.')
