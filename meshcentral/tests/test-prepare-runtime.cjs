'use strict';
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { cspHost, defaultCsp, extendCsp, normalizeHostname, prepareRuntime } = require('../custom/prepare-runtime');

assert.equal(normalizeHostname('mesh.example.org'), 'mesh.example.org');
assert.equal(normalizeHostname('2001:db8::10'), '2001:db8::10');
assert.equal(cspHost('2001:db8::10'), '[2001:db8::10]');
assert.throws(() => normalizeHostname('mesh.example.org:8443'));
assert.throws(() => normalizeHostname('https://mesh.example.org'));
const policy = defaultCsp('mesh.example.org');
assert(policy.includes("media-src 'self' blob:"));
assert(policy.includes("connect-src 'self' wss://mesh.example.org:*"));
const preserved = extendCsp("default-src 'none'; connect-src 'self'; media-src 'self'; frame-ancestors 'self'", 'remote.example.org');
assert(preserved.includes('default-src \'none\''));
assert(preserved.includes("connect-src 'self' wss://remote.example.org:*"));
assert(preserved.includes("media-src 'self' blob:"));
assert(preserved.includes("frame-ancestors 'self'"));

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'meshcentral-runtime-'));
try {
    const login = path.join(tmp, 'login2.handlebars');
    const config = path.join(tmp, 'config.json.template');
    fs.writeFileSync(login, '<h1>__MESH_BRAND_NAME__</h1><footer>__MESH_BRAND_NAME__</footer>');
    fs.writeFileSync(config, JSON.stringify({ settings: { cert: 'placeholder' }, domains: { '': { NewAccounts: true } } }));
    prepareRuntime({ loginTemplatePath: login, configTemplatePath: config, brandName: 'Công ty A & B', hostname: 'mesh.example.org' });
    const branded = fs.readFileSync(login, 'utf8');
    assert(branded.includes('CÔNG TY A &amp; B'));
    const seeded = JSON.parse(fs.readFileSync(config, 'utf8'));
    assert.equal(seeded.settings.cert, 'mesh.example.org');
    assert.equal(seeded.domains[''].NewAccounts, false);
    assert(seeded.domains[''].httpHeaders['Content-Security-Policy'].includes("media-src 'self' blob:"));
    assert(seeded.domains[''].httpHeaders['Content-Security-Policy'].includes('wss://mesh.example.org:*'));
} finally {
    fs.rmSync(tmp, { recursive: true, force: true });
}
console.log('PASS: brand escaping, hostname validation, CSP directives, initial hostname and signup defaults.');
