'use strict';

const fs = require('fs');
const net = require('net');

function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function normalizeHostname(value) {
    let host = String(value || 'localhost').trim();
    if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
    if (!host || /[\s/;\[\]]/.test(host) || (host.includes(':') && net.isIP(host) !== 6) || (!host.includes(':') && !/^[A-Za-z0-9.-]+$/.test(host))) {
        throw new Error('HOSTNAME must be a DNS name or IP address without a scheme, port, or path');
    }
    return host;
}

function cspHost(hostname) {
    const host = normalizeHostname(hostname);
    return host.includes(':') ? `[${host}]` : host;
}

function defaultCsp(hostname) {
    const host = cspHost(hostname);
    return [
        "default-src 'none'",
        "font-src 'self' fonts.gstatic.com data:",
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
        `connect-src 'self' wss://${host}:*`,
        "img-src 'self' blob: data:",
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com",
        "frame-src 'self' blob: mcrouter:",
        "media-src 'self' blob:",
        "form-action 'self'",
        "manifest-src 'self'"
    ].join('; ');
}

function extendCsp(existing, hostname) {
    if (!existing) return defaultCsp(hostname);
    const hostSource = `wss://${cspHost(hostname)}:*`;
    const directives = existing.split(';').map((x) => x.trim()).filter(Boolean);
    let connect = directives.findIndex((x) => /^connect-src(?:\s|$)/i.test(x));
    if (connect < 0) {
        directives.push(`connect-src 'self' ${hostSource}`);
    } else if (!directives[connect].split(/\s+/).includes(hostSource)) {
        directives[connect] += ` ${hostSource}`;
    }
    let media = directives.findIndex((x) => /^media-src(?:\s|$)/i.test(x));
    if (media < 0) {
        directives.push("media-src 'self' blob:");
    } else if (!directives[media].split(/\s+/).includes('blob:')) {
        directives[media] += ' blob:';
    }
    return directives.join('; ');
}

function prepareRuntime({ loginTemplatePath, configTemplatePath, brandName, hostname, allowNewAccounts = false }) {
    const login = fs.readFileSync(loginTemplatePath, 'utf8');
    const brand = escapeHtml((String(brandName || 'Your Organization').trim() || 'Your Organization').toLocaleUpperCase('vi-VN'));
    fs.writeFileSync(loginTemplatePath, login.replaceAll('__MESH_BRAND_NAME__', brand));

    const config = JSON.parse(fs.readFileSync(configTemplatePath, 'utf8'));
    config.settings = config.settings || {};
    config.settings.cert = normalizeHostname(hostname);
    config.domains = config.domains || {};
    config.domains[''] = config.domains[''] || {};
    config.domains[''].NewAccounts = /^true$/i.test(String(allowNewAccounts));
    config.domains[''].httpHeaders = config.domains[''].httpHeaders || {};
    const headers = config.domains[''].httpHeaders;
    headers['Content-Security-Policy'] = extendCsp(headers['Content-Security-Policy'], hostname);
    fs.writeFileSync(configTemplatePath, `${JSON.stringify(config, null, 2)}\n`);
}

module.exports = { cspHost, defaultCsp, extendCsp, normalizeHostname, prepareRuntime };

if (require.main === module) {
    prepareRuntime({
        loginTemplatePath: '/opt/meshcentral/meshcentral-web/views/login2.handlebars',
        configTemplatePath: '/opt/meshcentral/config.json.template',
        brandName: process.env.MESH_BRAND_NAME,
        hostname: process.env.HOSTNAME,
        allowNewAccounts: process.env.ALLOW_NEW_ACCOUNTS
    });
}
