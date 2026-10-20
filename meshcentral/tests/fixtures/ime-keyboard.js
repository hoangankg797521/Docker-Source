// MeshCentral desktop IME bridge. Text stays in memory; preferences are per node.
var deskIme = { node: null, enabled: false, composing: false, sent: '', input: null };

function deskImeModule() {
    if (!desktop || desktop.State !== 3 || desktop.contype !== 1) return null;
    return desktop.m;
}

function deskImeAllowed() {
    var m = deskImeModule();
    var rights = currentNode ? GetNodeRights(currentNode) : 0;
    var allowed = (features2 & 0x2000) === 0 && (!currentNode.agent || currentNode.agent.id !== 14) && (rights === 0xFFFFFFFF || ((rights & 8) !== 0 && (rights & (256 | 4096)) === 0));
    return !!(m && allowed && !m.stopInput && deskIme.node === desktopNode._id && xxcurrentView === 11 && !xxdialogMode && Q('DeskControl').checked);
}

function deskImeReset() {
    if (!deskIme) return;
    deskIme.composing = false;
    deskIme.sent = '';
    if (deskIme.input) deskIme.input.value = '';
}

function deskImeQueue(m, steps) {
    if (!m._imeQueue) m._imeQueue = [];
    Array.prototype.push.apply(m._imeQueue, steps);
    if (m._imeBusy) return;
    function next() {
        if (m.State !== 3) { m._imeQueue = []; m._imeBusy = false; return; }
        var step = m._imeQueue.shift();
        if (!step) { m._imeBusy = false; return; }
        m._imeBusy = true;
        if (typeof step === 'number') { setTimeout(next, step); return; }
        step(); next();
    }
    next();
}

function deskImeSync() {
    if (!deskImeAllowed() || !deskIme.enabled) { deskImeReset(); return; }
    if (deskIme.composing) return;
    var text = deskIme.input.value.normalize('NFC');
    var before = Array.from(deskIme.sent), after = Array.from(text), same = 0;
    while (same < before.length && same < after.length && before[same] === after[same]) same++;
    var m = deskImeModule();
    // Telex can replace earlier letters in the current word, not just append.
    var remove = before.length - same, suffix = after.slice(same).join('');
    deskImeQueue(m, [function() {
        for (var i = 0; i < remove; i++) m.SendKeyMsgKC([[m.KeyAction.DOWN, 8], [m.KeyAction.UP, 8]]);
        suffix.split(/([\n\t])/).forEach(function(part) {
            if (part === '\n' || part === '\t') {
                var kc = part === '\n' ? 13 : 9;
                m.SendKeyMsgKC([[m.KeyAction.DOWN, kc], [m.KeyAction.UP, kc]]);
            } else if (part) m.SendStringUnicode(part);
        });
    }]);
    deskIme.sent = text;
    // Keep the current word available to the local input method, with a bound.
    if (text.length > 1024 && /\s$/.test(text)) deskImeReset();
}

function deskImeSendKey(e) {
    var m = deskImeModule(), mods = [];
    if (!m) return;
    var win = isWindowsNode(desktopNode);
    if (e.ctrlKey || (win && e.metaKey)) mods.push(17);
    if (e.metaKey && !win) mods.push(91);
    if (e.altKey) mods.push(18);
    if (e.shiftKey) mods.push(16);
    // Send complete transactions: no modifier remains held when focus changes.
    var steps = [];
    mods.forEach(function(k) {
        steps.push(function() { m.SendKeyMsgKC(m.KeyAction.DOWN, k); });
        if (!win) steps.push(35); // Let macOS apply modifier flags before the key.
    });
    steps.push(function() { m.SendKeyMsg(m.KeyAction.DOWN, e); m.SendKeyMsg(m.KeyAction.UP, e); });
    if (!win && mods.length) steps.push(20);
    mods.reverse().forEach(function(k) { steps.push(function() { m.SendKeyMsgKC(m.KeyAction.UP, k); }); });
    deskImeQueue(m, steps);
}

function deskImeKeyDown(e) {
    e.stopPropagation();
    if (!deskIme.enabled || !deskImeAllowed()) { e.preventDefault(); return; }
    setSessionActivity();
    if (deskIme.composing || e.isComposing || e.keyCode === 229) return;
    if (['Shift', 'Control', 'Alt', 'Meta'].indexOf(e.key) >= 0) return;
    // Paste is local text input, so it follows the same Unicode/IME path.
    if ((e.metaKey || e.ctrlKey) && e.code === 'KeyV' && !e.altKey) return;
    if ((e.metaKey || e.ctrlKey) || (e.key.length !== 1 && e.key !== 'Dead')) {
        if (e.key === 'Backspace' && !e.metaKey && !e.ctrlKey && deskIme.input.value.length) return;
        e.preventDefault();
        deskImeReset();
        deskImeSendKey(e);
    }
}

function deskImeFocus(e) {
    if (!deskIme.enabled || !deskImeAllowed() || !deskIme.input) return;
    // Position the invisible editable next to the caret click for IME candidates.
    if (e) {
        deskIme.input.style.left = Math.max(0, Math.min(e.clientX, window.innerWidth - 20)) + 'px';
        deskIme.input.style.top = Math.max(0, Math.min(e.clientY, window.innerHeight - 30)) + 'px';
    }
    deskIme.input.focus({ preventScroll: true });
}

function deskImeToggle() {
    if (!deskImeModule()) return;
    desktop.m.handleReleaseKeys();
    deskIme.enabled = !deskIme.enabled;
    putstore('DeskIme_' + deskIme.node, deskIme.enabled ? 1 : 0);
    deskImeReset();
    deskImeUpdate();
    if (deskIme.enabled) deskImeFocus();
    else { deskIme.input.blur(); Q('DeskImeToggle').blur(); }
}

function deskImeUpdate() {
    if (!deskIme || !Q('deskActionsBtn')) return;
    var button = Q('DeskImeToggle');
    if (!button) {
        button = document.createElement('input');
        button.id = 'DeskImeToggle'; button.type = 'button';
        button.style.marginLeft = '4px';
        button.onclick = deskImeToggle;
        button.onkeydown = button.onkeypress = function() { return false; };
        Q('deskActionsBtn').insertAdjacentElement('afterend', button);
        var input = document.createElement('textarea');
        input.id = 'DeskImeInput';
        input.setAttribute('aria-label', 'Bàn phím tiếng Việt cho Desktop');
        input.setAttribute('autocomplete', 'off'); input.setAttribute('autocapitalize', 'off');
        input.spellcheck = false; input.tabIndex = -1;
        input.style.cssText = 'position:fixed;left:50%;top:50%;width:2px;height:2px;opacity:.01;pointer-events:none;padding:0;border:0;';
        input.addEventListener('keydown', deskImeKeyDown);
        ['keyup', 'keypress'].forEach(function(name) {
            input.addEventListener(name, function(e) { e.stopPropagation(); });
        });
        input.addEventListener('compositionstart', function() { deskIme.composing = true; });
        input.addEventListener('compositionend', function() { deskIme.composing = false; deskImeSync(); });
        input.addEventListener('input', function(e) { if (!e.isComposing) deskImeSync(); });
        input.addEventListener('blur', deskImeReset);
        window.addEventListener('blur', deskImeReset);
        document.body.appendChild(input);
        deskIme.input = input;
        document.addEventListener('mouseup', function(e) {
            if (e.target && e.target.id === 'Desk' && deskIme.enabled) {
                deskImeReset(); deskImeFocus(e);
            }
        }, true);
    }
    var node = currentNode && currentNode._id;
    if (node !== deskIme.node) {
        deskImeReset(); deskIme.node = node;
        var macBrowser = /Mac|iPhone|iPad/.test(navigator.platform || '');
        deskIme.enabled = !!node && getstore('DeskIme_' + node, macBrowser ? 1 : 0) == 1;
    }
    if (!deskImeAllowed()) deskImeReset();
    button.disabled = !deskImeModule() || !deskImeAllowed();
    button.value = deskIme.enabled ? 'Bàn phím: Tự động' : 'Bàn phím: Gốc';
    button.setAttribute('aria-pressed', deskIme.enabled ? 'true' : 'false');
    button.title = 'Gõ Telex trực tiếp: bấm trong Desktop rồi gõ. Trên Windows, ⌘ dùng như Ctrl. Tắt để dùng bộ gõ của máy đích.';
    button.style.backgroundColor = deskIme.enabled ? '#c4efda' : '';
}

// Fix Command shortcuts in the original mode, and Mac agents' extended-key flags.
(function() {
    var toggle = toggleKvmControl;
    toggleKvmControl = function() { toggle(); deskImeUpdate(); };
    var create = CreateAgentRemoteDesktop;
    CreateAgentRemoteDesktop = function() {
        var m = create.apply(this, arguments), metaKeys = {};
        var mac = typeof desktopNode !== 'undefined' && desktopNode && /mac\s*os|os\s*x|darwin/i.test(desktopNode.osdesc || '');
        var send = m.SendKeyMsgKC;
        m.SendKeyMsgKC = function(action, kc, extended) {
            if (mac && typeof action === 'number') {
                if (action === m.KeyAction.EXDOWN) action = m.KeyAction.DOWN;
                else if (action === m.KeyAction.EXUP) action = m.KeyAction.UP;
            }
            return send.call(m, action, kc, extended);
        };
        ['xxKeyDown', 'xxKeyUp', 'xxKeyPress'].forEach(function(name) {
            var original = m[name];
            m[name] = function(e) {
                var code = e.code || e.keyCode;
                if (e.metaKey || metaKeys[code]) {
                    if (m.State === 3 && !m.stopInput) {
                        if (name === 'xxKeyDown') { metaKeys[code] = true; m.SendKeyMsg(m.KeyAction.DOWN, e); }
                        else if (name === 'xxKeyUp') { delete metaKeys[code]; m.SendKeyMsg(m.KeyAction.UP, e); }
                    }
                    e.preventDefault(); e.stopPropagation(); return false;
                }
                return original.call(m, e);
            };
        });
        return m;
    };
})();
