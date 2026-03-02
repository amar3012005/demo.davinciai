// ==UserScript==
// @name         TARA Direct Injector (Modular Only)
// @namespace    davinciai.tara
// @version      1.0.0
// @description  Inject TARA modular stack via tara-embed.js only (no monolithic fallback)
// @author       DaVinciAI
// @match        *://*/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    if (window.__TARA_MODULAR_INJECTED__) return;
    window.__TARA_MODULAR_INJECTED__ = true;

    // Keep local defaults explicit for dev/testing.
    const HOST = 'localhost';
    const PORT = '8004';
    const HTTP_BASE = `http://${HOST}:${PORT}`;
    const STATIC_BASE = `${HTTP_BASE}/static`;
    const WS_URL = `ws://${HOST}:${PORT}/ws`;
    const EMBED_URL = `${STATIC_BASE}/tara-embed.js`;

    function inject() {
        if (document.getElementById('tara-modular-embed-loader')) {
            return;
        }

        // Configure runtime before embed loads.
        const cfg = document.createElement('script');
        cfg.id = 'tara-modular-config-inline';
        cfg.textContent = `
            window.TARA_BASE_URL = ${JSON.stringify(STATIC_BASE)};
            window.TARA_WS_URL = ${JSON.stringify(WS_URL)};
            window.TARA_MODE = window.TARA_MODE || 'turbo';
            window.TARA_FORCE_LOCAL = true;
        `;
        (document.head || document.documentElement).appendChild(cfg);
        cfg.remove();

        const s = document.createElement('script');
        s.id = 'tara-modular-embed-loader';
        s.src = EMBED_URL;
        s.async = false;
        s.onload = function () {
            console.log('✅ DaVinciAI: Modular TARA embed injected:', EMBED_URL);
        };
        s.onerror = function (e) {
            console.error('❌ DaVinciAI: Failed to inject modular TARA embed:', EMBED_URL, e);
        };
        (document.head || document.documentElement).appendChild(s);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inject, { once: true });
    } else {
        inject();
    }
})();
