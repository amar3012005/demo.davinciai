// ==UserScript==
// @name         TARA Direct Injector (Modular Only)
// @namespace    davinciai.tara
// @version      2.0.0
// @description  Inject TARA modular stack — CSP-bypass via GM_addElement, no inline scripts
// @author       DaVinciAI
// @match        *://*/*
// @noframes
// @run-at       document-start
// @grant        GM_addElement
// @grant        unsafeWindow
// ==/UserScript==

(function () {
    'use strict';

    // Do not inject into third-party iframes (Stripe/Intercom/etc).
    if (window.top !== window.self) return;

    if (unsafeWindow.__TARA_MODULAR_INJECTED__) return;
    unsafeWindow.__TARA_MODULAR_INJECTED__ = true;

    const HOST = 'localhost';
    const PORT = '8004';
    const STATIC_BASE = `http://${HOST}:${PORT}/static`;
    // For CSP-restricted sites (e.g. console.groq.com), localhost:8080 is whitelisted
    // in connect-src. We expose orchestrator on 8080->8004 in docker-compose.local.yml.
    const WS_URL = `ws://${HOST}:8080/ws`;
    const V = Date.now(); // cache-buster per page load

    // ── Expose runtime config via unsafeWindow (bypasses CSP — no inline script) ──
    unsafeWindow.TARA_BASE_URL = STATIC_BASE;
    unsafeWindow.TARA_WS_URL = WS_URL;
    unsafeWindow.TARA_MODE = unsafeWindow.TARA_MODE || 'turbo';
    unsafeWindow.TARA_FORCE_LOCAL = true;
    unsafeWindow.__TARA_MODULAR_LOADED__ = false; // let embed guard run fresh

    // ── Load a script via GM_addElement (bypasses page CSP) ──────────────────
    function addScript(src) {
        return new Promise(function (resolve, reject) {
            try {
                GM_addElement('script', {
                    src: src,
                    type: 'text/javascript',
                    async: false
                });
                // GM_addElement is synchronous-ish; give it a tick to register
                setTimeout(resolve, 0);
            } catch (e) {
                reject(e);
            }
        });
    }

    // ── Ordered modular files ─────────────────────────────────────────────────
    const MODULES = [
        'tara-config.js',
        'tara-styles.js',
        'tara-ghost-cursor.js',
        'tara-scanner.js',
        'tara-audio.js',
        'tara-vad.js',
        'tara-sensor.js',
        'tara-phoenix.js',
        'tara-executor.js',
        'tara-ws.js',
        'tara-ui.js',
        'tara-core.js'
    ];

    // ── Sequential loader ─────────────────────────────────────────────────────
    async function loadAll() {
        console.log('🎯 [TARA] Loading modular stack via GM_addElement (CSP-bypass)...');
        for (const file of MODULES) {
            const src = `${STATIC_BASE}/${file}?v=${V}`;
            try {
                await addScript(src);
                console.log('✅ [TARA]', file);
            } catch (e) {
                console.error('❌ [TARA] Failed to load:', file, e);
                return; // abort on first failure
            }
        }
        console.log('🎉 [TARA] Modular stack loaded (v2.0 CSP-bypass)');
    }

    // ── Boot: wait for body ───────────────────────────────────────────────────
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadAll, { once: true });
    } else {
        loadAll();
    }
})();
