/**
 * TARA Visual Co-Pilot - Embeddable Loader (Modular v5)
 *
 * USAGE: Add this ONE line to any website:
 *   <script src="http://localhost:8004/static/tara-embed.js"></script>
 *
 * Or paste in browser console:
 *   (function(){var s=document.createElement('script');s.src='http://localhost:8004/static/tara-embed.js';document.body.appendChild(s);})();
 *
 * This loader downloads and initialises the full modular stack:
 *   tara-config.js → tara-styles.js → tara-ghost-cursor.js →
 *   tara-scanner.js → tara-audio.js → tara-vad.js →
 *   tara-phoenix.js → tara-executor.js → tara-ws.js →
 *   tara-ui.js → tara-core.js
 */

(function () {
    'use strict';

    // Prevent double-injection
    if (window.__TARA_MODULAR_LOADED__) {
        console.warn('⚠️ TARA modular stack already loaded, skipping.');
        return;
    }
    window.__TARA_MODULAR_LOADED__ = true;

    console.log('🎯 TARA Embed Loader starting (modular v5)...');

    // ── Configuration ─────────────────────────────────────────────
    var BASE_URL = window.TARA_BASE_URL || 'http://localhost:8004/static';
    var WS_URL = window.TARA_WS_URL || null;
    var MODE = window.TARA_MODE || 'turbo';
    var POSITION = window.TARA_POSITION || 'bottom-right';
    var VERSION = 'v' + Date.now();   // cache-buster

    // Auto-detect WS URL
    if (!WS_URL) {
        var port = window.location.port || '8004';
        WS_URL = 'ws://localhost:' + port + '/ws';
    }

    console.log('🔒 FORCED WebSocket URL:', WS_URL);
    console.log('📡 TARA Config:', { baseUrl: BASE_URL, mode: MODE, position: POSITION, wsUrl: WS_URL, version: VERSION });

    // Expose config for modules
    window.TARA_ENV = { WS_URL: WS_URL };
    window.TARA_CONFIG = { mode: MODE, position: POSITION };

    // ── Ordered modular file list ─────────────────────────────────
    // Each module depends on the ones before it — must load in sequence.
    var MODULES = [
        'tara-config.js',
        'tara-styles.js',
        'tara-ghost-cursor.js',
        'tara-scanner.js',
        'tara-audio.js',
        'tara-vad.js',
        'tara-phoenix.js',
        'tara-executor.js',
        'tara-ws.js',
        'tara-ui.js',
        'tara-core.js'
    ];

    // ── Script loader ─────────────────────────────────────────────
    function loadScript(src, callback) {
        var script = document.createElement('script');
        script.src = src + '?v=' + VERSION;
        script.async = false;   // preserve order within each sequential call

        script.onload = function () {
            console.log('✅ Loaded:', src);
            if (callback) callback(null);
        };

        script.onerror = function () {
            var msg = 'Failed to load ' + src;
            console.error('❌ ' + msg);
            if (callback) callback(new Error(msg));
        };

        document.body.appendChild(script);
    }

    // ── Sequential loader ─────────────────────────────────────────
    function loadSequential(files, done) {
        if (files.length === 0) { done(null); return; }
        var file = files[0];
        var rest = files.slice(1);
        loadScript(BASE_URL + '/' + file, function (err) {
            if (err) {
                console.error('❌ Aborting modular load — could not load:', file);
                done(err);
                return;
            }
            loadSequential(rest, done);
        });
    }

    // ── Boot ──────────────────────────────────────────────────────
    function boot() {
        console.log('📦 Loading TARA modular stack from:', BASE_URL);
        loadSequential(MODULES, function (err) {
            if (err) {
                console.error('❌ TARA modular stack failed to load:', err);
                return;
            }
            console.log('🎉 TARA Visual Co-Pilot (modular v5) loaded successfully!');
            console.log('   Mode:', MODE);
            console.log('   WebSocket:', WS_URL);
            console.log('   Position:', POSITION);
            console.log('💡 Click the orb to activate Visual Co-Pilot');

            window.dispatchEvent(new CustomEvent('tara:loaded', {
                detail: { mode: MODE, wsUrl: WS_URL }
            }));
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }

    console.log('🎯 TARA Embed Loader (modular v5) initialised');
})();
