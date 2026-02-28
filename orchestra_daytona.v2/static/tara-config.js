/**
 * TARA Visual Co-Pilot — Configuration & Constants
 * Module: tara-config.js
 *
 * Owns: Environment detection, default parameters, orb caching,
 *       and all shared constants used across modules.
 */
(function () {
    'use strict';

    // Namespace
    window.TARA = window.TARA || {};

    // ─── Environment Detection ───────────────────────────────────────────
    function getEnvConfig() {
        // CRITICAL: Check for forced localhost first (from embed.js)
        if (window.TARA_FORCE_LOCAL && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')) {
            const port = window.location.port || '8004';
            const localUrl = `ws://${window.location.hostname}:${port}/ws`;
            console.log('🔒 [Config] FORCED localhost URL:', localUrl);
            return { wsUrl: localUrl };
        }

        // Try to get from window.TARA_ENV (set by backend or HTML)
        if (window.TARA_ENV && window.TARA_ENV.WS_URL) {
            console.log('🔧 [Config] Using window.TARA_ENV.WS_URL:', window.TARA_ENV.WS_URL);
            return { wsUrl: window.TARA_ENV.WS_URL };
        }

        // Try to get from data attribute on script tag
        const script = document.querySelector('script[src*="tara-config.js"], script[src*="tara-embed.js"], script[src*="tara-widget.js"]');
        if (script) {
            const wsUrl = script.getAttribute('data-ws-url');
            if (wsUrl) {
                console.log('🔧 [Config] Using data-ws-url:', wsUrl);
                return { wsUrl };
            }
        }

        // Check for localhost development (automatic detection)
        if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
            const port = window.location.port || '8004';
            const localUrl = `ws://${window.location.hostname}:${port}/ws`;
            console.log('🔧 [Config] Auto-detected localhost, using:', localUrl);
            return { wsUrl: localUrl };
        }

        // Default to localhost for development
        const port = window.location.port || '8004';
        const defaultLocal = `ws://localhost:${port}/ws`;
        console.log('🔧 [Config] Defaulting to localhost:', defaultLocal);
        return { wsUrl: defaultLocal };
    }

    const ENV_CONFIG = getEnvConfig() || {};

    // ─── Defaults ────────────────────────────────────────────────────────
    const DEFAULTS = {
        wsUrl: ENV_CONFIG.wsUrl || 'ws://localhost:8004/ws',
        orbSize: 48,
        position: 'bottom-right',
        colors: {
            core: '#1a1a1a',
            accent: '#333333',
            glow: 'rgba(255, 255, 255, 0.3)',
            highlight: '#ffffff',
            dim: 'rgba(0, 0, 0, 0.75)'
        },
        audio: {
            inputSampleRate: 16000,
            outputSampleRate: 44100,
            bufferSize: 4096
        },
        vad: {
            energyThreshold: 0.018,
            silenceThreshold: 0.015,
            minSpeechDuration: 250,
            silenceTimeout: 1000
        }
    };

    // Merge User Config if available
    const TARA_CONFIG = {
        ...DEFAULTS,
        ...(window.TARA_CONFIG || {})
    };

    // ─── Constants ───────────────────────────────────────────────────────
    const ORB_IMAGE_URL = (window.TARA_ASSET_BASE_URL || '').trim()
        ? `${window.TARA_ASSET_BASE_URL.replace(/\/$/, '')}/tara-orb.svg`
        : `${window.location.protocol}//${window.location.host}/static/tara-orb.svg`;
    const ORB_CACHE_KEY = 'tara_orb_svg_cache';
    const MISSION_STATE_KEY = 'tara_mission_state';

    // ─── Orb Cache Helpers ───────────────────────────────────────────────
    function getCachedOrbUrl() {
        try {
            const cached = localStorage.getItem(ORB_CACHE_KEY);
            if (cached) {
                return `data:image/svg+xml,${encodeURIComponent(cached)}`;
            }
        } catch (e) { /* localStorage not available */ }
        return null;
    }

    function cacheOrbSvg(svgContent) {
        try {
            localStorage.setItem(ORB_CACHE_KEY, svgContent);
        } catch (e) { /* quota exceeded or not available */ }
        return `data:image/svg+xml,${encodeURIComponent(svgContent)}`;
    }

    // ─── Export ──────────────────────────────────────────────────────────
    window.TARA.Config = TARA_CONFIG;
    window.TARA.Constants = {
        ORB_IMAGE_URL,
        ORB_CACHE_KEY,
        MISSION_STATE_KEY
    };
    window.TARA.getCachedOrbUrl = getCachedOrbUrl;
    window.TARA.cacheOrbSvg = cacheOrbSvg;

    console.log('✅ [TARA] Config module loaded');
})();
