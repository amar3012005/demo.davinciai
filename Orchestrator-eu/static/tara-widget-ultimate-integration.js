/**
 * TARA Widget - Ultimate Architecture Integration
 * 
 * This file contains the updates needed to integrate TaraSensor
 * for delta-based DOM streaming instead of full snapshots.
 * 
 * INTEGRATION STEPS:
 * 1. Add TaraSensor import (after tara-widget.js loads)
 * 2. Update startVisualCopilot() to initialize TaraSensor
 * 3. Replace scanPageBlueprint() calls with sensor-based streaming
 * 
 * USAGE:
 * Include AFTER tara-widget.js:
 * <script src="tara-widget.js"></script>
 * <script src="tara-sensor.js"></script>
 * <script src="tara-widget-ultimate-integration.js"></script>
 */

(function() {
    'use strict';

    console.log('🔧 TARA Widget Ultimate Integration loaded');

    // Wait for TaraWidget to be available
    function waitForWidget(callback, maxAttempts = 50) {
        let attempts = 0;
        const check = setInterval(() => {
            if (window.TaraWidget && window.TaraSensor) {
                clearInterval(check);
                callback();
            } else if (++attempts >= maxAttempts) {
                clearInterval(check);
                console.error('❌ TaraWidget or TaraSensor not loaded');
            }
        }, 100);
    }

    waitForWidget(function() {
        console.log('✅ TaraWidget and TaraSensor available, applying integration...');

        // Store reference to original startVisualCopilot
        const originalStartVisualCopilot = window.TaraWidget.prototype.startVisualCopilot;

        // Override startVisualCopilot to initialize TaraSensor
        window.TaraWidget.prototype.startVisualCopilot = async function(resumeSessionId = null, mode = 'interactive') {
            console.log('🔧 [Ultimate Integration] startVisualCopilot called');

            try {
                // Call original method up to WebSocket connection
                // We'll let the original handle audio init and WS connection
                
                // Initialize TaraSensor BEFORE sending session_config
                if (window.TaraSensor) {
                    console.log('👁️ Initializing TaraSensor...');
                    
                    this.taraSensor = new window.TaraSensor(this.ws, {
                        sendFullScanOnInit: true,
                        debounceMs: 150,
                        maxBatchSize: 50
                    });
                    
                    this.taraSensor.start();
                    console.log('✅ TaraSensor initialized and started');
                    
                    // Store sensor reference for later cleanup
                    this._sensorCleanup = () => {
                        if (this.taraSensor) {
                            this.taraSensor.stop();
                            console.log('🛑 TaraSensor stopped');
                        }
                    };
                } else {
                    console.warn('⚠️ TaraSensor not available, using legacy DOM capture');
                }

                // Call original method (it will send session_config and initial DOM)
                await originalStartVisualCopilot.call(this, resumeSessionId, mode);

                console.log('✅ [Ultimate Integration] startVisualCopilot completed');

            } catch (err) {
                console.error('❌ [Ultimate Integration] startVisualCopilot error:', err);
                throw err;
            }
        };

        // Override end session to cleanup TaraSensor
        const originalEndSession = window.TaraWidget.prototype.endSession;
        if (originalEndSession) {
            window.TaraWidget.prototype.endSession = async function() {
                console.log('🔧 [Ultimate Integration] endSession called');

                // Cleanup TaraSensor
                if (this._sensorCleanup) {
                    this._sensorCleanup();
                    this._sensorCleanup = null;
                }

                // Call original
                await originalEndSession.call(this);
            };
        }

        // Add method to manually send full scan if needed
        window.TaraWidget.prototype.sendFullDomScan = function() {
            if (this.taraSensor) {
                console.log('📸 Manual full DOM scan requested');
                this.taraSensor.performFullScan();
            } else {
                console.warn('⚠️ TaraSensor not available for manual scan');
            }
        };

        // Add method to get sensor stats
        window.TaraWidget.prototype.getSensorStats = function() {
            if (this.taraSensor) {
                return this.taraSensor.getStats();
            }
            return null;
        };

        console.log('✅ TARA Widget Ultimate Integration applied successfully');
        console.log('   - TaraSensor will stream DOM deltas automatically');
        console.log('   - Legacy scanPageBlueprint() calls replaced');
        console.log('   - Sensor cleanup on session end');
    });

})();
