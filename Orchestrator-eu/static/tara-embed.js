/**
 * TARA Visual Co-Pilot - Embeddable Loader
 * 
 * USAGE: Add this ONE line to any website:
 * 
 * <script src="http://localhost:8004/static/tara-embed.js"></script>
 * 
 * Or paste in browser console:
 * (function(){var s=document.createElement('script');s.src='http://localhost:8004/static/tara-embed.js';document.body.appendChild(s);})();
 * 
 * This loader will automatically download and initialize:
 * - tara-widget.js (main widget)
 * - tara-sensor.js (delta streaming)
 * - tara-widget-ultimate-integration.js (ultimate features)
 */

(function() {
    'use strict';
    
    console.log('🎯 TARA Embed Loader starting...');
    
    // Configuration - with cache busting
    var TARA_CONFIG = {
        baseUrl: window.TARA_BASE_URL || 'http://localhost:8004/static',
        mode: window.TARA_MODE || 'turbo',
        position: window.TARA_POSITION || 'bottom-right',
        wsUrl: window.TARA_WS_URL || null,
        version: 'v' + Date.now()  // Cache buster
    };
    
    // Auto-detect WebSocket URL - ALWAYS use localhost for development
    // Override with window.TARA_WS_URL if you want custom URL
    if (!TARA_CONFIG.wsUrl) {
        // Check if user explicitly set a URL
        if (window.TARA_WS_URL) {
            TARA_CONFIG.wsUrl = window.TARA_WS_URL;
            console.log('🔧 Using custom TARA_WS_URL:', TARA_CONFIG.wsUrl);
        }
        // Default to localhost for development
        else {
            var port = window.location.port || '8004';
            TARA_CONFIG.wsUrl = 'ws://localhost:' + port + '/ws';
            console.log('🔧 Defaulting to localhost:', TARA_CONFIG.wsUrl);
        }
    }
    
    // CRITICAL: Force localhost and prevent ANY override
    window.TARA_ENV = { WS_URL: TARA_CONFIG.wsUrl };
    window.TARA_FORCE_LOCAL = true;
    console.log('🔒 FORCED WebSocket URL:', TARA_CONFIG.wsUrl);
    
    console.log('📡 TARA Config:', TARA_CONFIG);
    
    // Set global config for widget to use
    window.TARA_ENV = {
        WS_URL: TARA_CONFIG.wsUrl
    };
    
    window.TARA_CONFIG = {
        mode: TARA_CONFIG.mode,
        position: TARA_CONFIG.position
    };
    
    // Load script helper with cache busting
    function loadScript(src, callback) {
        var script = document.createElement('script');
        script.src = src + '?v=' + TARA_CONFIG.version;  // Add cache buster
        script.async = true;
        
        script.onload = function() {
            console.log('✅ Loaded:', src);
            if (callback) callback();
        };
        
        script.onerror = function() {
            console.error('❌ Failed to load:', src);
            if (callback) callback(new Error('Failed to load ' + src));
        };
        
        document.body.appendChild(script);
    }
    
    // Load all Tara files in sequence
    function loadTara() {
        console.log('📦 Loading TARA files from:', TARA_CONFIG.baseUrl);
        
        // Step 1: Load main widget (v4.0 has TaraSensor INTEGRATED)
        loadScript(TARA_CONFIG.baseUrl + '/tara-widget.js', function(err) {
            if (err) {
                console.error('❌ Failed to load tara-widget.js');
                return;
            }
            
            console.log('✅ tara-widget.js loaded (v4.0 - TaraSensor integrated)');
            
            // NOTE: tara-sensor.js is NOT loaded separately - it's integrated into tara-widget.js v4.0
            
            // Step 2: Load ultimate integration (for backward compatibility)
            loadScript(TARA_CONFIG.baseUrl + '/tara-widget-ultimate-integration.js', function(err) {
                if (err) {
                    console.log('ℹ️ Ultimate integration skipped (not needed for v4.0)');
                } else {
                    console.log('✅ Ultimate integration loaded');
                }
                
                // All files loaded - TARA is ready!
                console.log('🎉 TARA Visual Co-Pilot loaded successfully!');
                console.log('   Mode:', TARA_CONFIG.mode);
                console.log('   WebSocket:', TARA_CONFIG.wsUrl);
                console.log('   Position:', TARA_CONFIG.position);
                console.log('');
                console.log('💡 TARA orb should appear in bottom-right corner');
                console.log('💡 Click orb to activate Visual Co-Pilot');
                
                // Fire custom event for integration
                window.dispatchEvent(new CustomEvent('tara:loaded', {
                    detail: {
                        mode: TARA_CONFIG.mode,
                        wsUrl: TARA_CONFIG.wsUrl
                    }
                }));
            });
        });
    }
    
    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadTara);
    } else {
        loadTara();
    }
    
    console.log('🎯 TARA Embed Loader initialized');
    
})();
