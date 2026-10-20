'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const config = process.env.CONFIG_DIR || '/config';
const state = process.env.STATE_DIR || '/state';
function password(label) {
  return new Promise((resolve, reject) => {
    if (!process.stdin.isTTY) return reject(Error('Cần terminal tương tác để đặt mật khẩu.'));
    process.stdout.write(label);
    process.stdin.setRawMode(true); process.stdin.resume();
    let value = '';
    const onData = chunk => {
      for (const ch of chunk.toString()) {
        if (ch === '\u0003') { cleanup(); return reject(Error('Đã hủy.')); }
        if (ch === '\r' || ch === '\n') { cleanup(); return resolve(value); }
        if (ch === '\u007f' || ch === '\b') value = value.slice(0, -1);
        else if (ch >= ' ') value += ch;
      }
    };
    const cleanup = () => { process.stdin.removeListener('data', onData); process.stdin.setRawMode(false); process.stdin.pause(); process.stdout.write('\n'); };
    process.stdin.on('data', onData);
  });
}
async function main() {
  for (const dir of [config, state]) fs.mkdirSync(dir, { recursive: true });
  const auth = path.join(state, 'auth.json');
  if (fs.existsSync(auth)) throw Error('Đã có tài khoản. Setup dừng để giữ nguyên dữ liệu.');
  const a = await password('Mật khẩu mới cho admin (tối thiểu 8 ký tự): ');
  if (a.length < 8) throw Error('Mật khẩu cần ít nhất 8 ký tự.');
  if (a !== await password('Nhập lại mật khẩu: ')) throw Error('Mật khẩu không khớp.');
  const salt = crypto.randomBytes(32).toString('hex');
  const credentials = { username: 'admin', salt, hash: crypto.scryptSync(a, salt, 64).toString('hex') };
  require('./init').seed();
  fs.writeFileSync(auth, JSON.stringify(credentials), { flag: 'wx', mode: 0o600 });
  fs.chownSync(auth, 1000, 1000);
  for (const dir of [config, state]) { fs.chownSync(dir, 1000, 1000); fs.chmodSync(dir, dir === state ? 0o700 : 0o755); }
  console.log('Đã khởi tạo. Tài khoản: admin. Chạy docker compose up -d.');
}
main().catch(e => { console.error(e.message); process.exitCode = 1; });
