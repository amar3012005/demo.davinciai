/**
 * TARA Visual Co-Pilot — WebSocket Handler
 * Module: tara-ws.js
 *
 * Owns: WebSocket connection, audio WS, message routing, Phoenix resume.
 * Depends on: tara-config.js, tara-phoenix.js, tara-scanner.js, tara-ui.js
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    const WS = {
        /**
         * Connect the main control WebSocket.
         * @param {Object} widget - TaraWidget instance
         * @returns {Promise}
         */
        connectWebSocket(widget) {
            return new Promise((resolve, reject) => {
                const recoveredSessionId = sessionStorage.getItem('tara_session_id') ||
                    localStorage.getItem('tara_session_id');
                let wsUrl = widget.config.wsUrl;
                if (recoveredSessionId) {
                    try {
                        const parsed = new URL(wsUrl, window.location.href);
                        parsed.searchParams.set('session_id', recoveredSessionId);
                        wsUrl = parsed.toString();
                    } catch (e) {
                        const sep = wsUrl.includes('?') ? '&' : '?';
                        wsUrl = `${wsUrl}${sep}session_id=${encodeURIComponent(recoveredSessionId)}`;
                    }
                }
                widget._requestedSessionId = recoveredSessionId || null;
                console.log('🔌 Connecting to WebSocket:', wsUrl);

                widget.ws = new WebSocket(wsUrl);
                widget.ws.binaryType = 'arraybuffer';

                widget.ws.onopen = () => {
                    console.log('✅ WebSocket connected');

                    if (widget.screenOverlay) widget.screenOverlay.classList.add('active');

                    // Request orb SVG if not cached
                    if (!window.TARA.getCachedOrbUrl()) {
                        widget.ws.send(JSON.stringify({ type: 'request_asset', asset: 'tara-orb.svg' }));
                        console.log('📦 Requesting orb SVG via WebSocket...');
                    }

                    // Phoenix Protocol: Hydrate goal from storage (actual resume
                    // messages are sent by startVisualCopilot AFTER session_config,
                    // so the backend has mission state before execution_complete).
                    if (widget.wasNavigating || widget.isRestoredSession) {
                        const restoredGoal = sessionStorage.getItem('tara_goal') || localStorage.getItem('tara_goal');
                        if (restoredGoal && !widget.pendingMissionGoal) {
                            widget.pendingMissionGoal = restoredGoal;
                            widget._currentMissionGoal = restoredGoal;
                            console.log('🎯 Phoenix: Restored goal:', restoredGoal);
                        }
                        // Flag that resume messages still need to be sent
                        widget._phoenixResumeNeeded = true;
                        console.log('📡 Phoenix: Resume deferred until after session_config');
                    }

                    resolve();
                };

                widget.ws.onmessage = (e) => {
                    if (e.data instanceof ArrayBuffer) {
                        if (!widget.audioStreamActive) {
                            widget.binaryQueue.push(e.data);
                        }
                    } else {
                        let data;
                        try {
                            data = JSON.parse(e.data);
                        } catch (err) {
                            console.error('❌ JSON Parse Error:', err, e.data);
                            return;
                        }

                        if (data.type === 'audio_chunk') {
                            if (widget.sessionMode === 'turbo') {
                                if (data.binary_sent && widget.binaryQueue.length > 0) widget.binaryQueue.shift();
                            } else if (widget.audioStreamActive) {
                                if (data.binary_sent && widget.binaryQueue.length > 0) widget.binaryQueue.shift();
                            } else {
                                if (data.binary_sent && widget.binaryQueue.length > 0) {
                                    const chunk = widget.binaryQueue.shift();
                                    if (widget.audioManager && !widget.isVoiceMuted) {
                                        widget.audioManager.playChunk(chunk, data.format || 'pcm_f32le', data.sample_rate || 44100);
                                    }
                                } else if (data.data || data.audio) {
                                    const b64 = data.data || data.audio;
                                    const binaryString = atob(b64);
                                    const bytes = new Uint8Array(binaryString.length);
                                    for (let i = 0; i < binaryString.length; i++) {
                                        bytes[i] = binaryString.charCodeAt(i);
                                    }
                                    if (widget.audioManager && !widget.isVoiceMuted) {
                                        widget.audioManager.playChunk(bytes.buffer, data.format || 'pcm_f32le', data.sample_rate || 44100);
                                    }
                                }
                            }
                        }

                        WS.handleBackendMessage(widget, data);
                    }
                };

                widget.ws.onclose = () => {
                    console.log('🔌 WebSocket closed');
                    if (widget.screenOverlay) widget.screenOverlay.classList.remove('active');
                    if (widget.isActive) {
                        // Always preserve Phoenix data during WS closes (reloads, unexpected drops)
                        // Manual deliberate stops (UI button) will call stopVisualCopilot(false) directly
                        console.log('🛡️ WS closed — preserving Phoenix data for resilience');
                        widget.stopVisualCopilot(true); // keepPhoenixData=true
                    }
                };

                widget.ws.onerror = (err) => {
                    console.error('❌ WebSocket error:', err);
                    reject(err);
                };
            });
        },

        /**
         * Connect dedicated audio WebSocket.
         */
        connectAudioWebSocket(widget, sessionId) {
            if (!sessionId || widget.sessionMode === 'turbo') return;

            const audioUrl = widget.config.wsUrl.replace('/ws', '/stream') +
                '?session_id=' + encodeURIComponent(sessionId);

            console.log('🔊 Connecting audio WebSocket:', audioUrl);
            widget.audioWs = new WebSocket(audioUrl);
            widget.audioWs.binaryType = 'arraybuffer';

            widget.audioWs.onopen = () => {
                console.log('✅ Audio WebSocket connected');
                widget.audioStreamActive = true;
            };

            widget.audioWs.onmessage = (e) => {
                if (e.data instanceof ArrayBuffer) {
                    WS.handleAudioStreamChunk(widget, e.data);
                } else {
                    try {
                        const data = JSON.parse(e.data);
                        if (data.type === 'audio_stream_ready') {
                            console.log('🔊 Audio stream ready:', data);
                        } else if (data.type === 'audio_stream_end') {
                            WS.flushAudioPreBuffer(widget);
                        }
                    } catch (err) {
                        console.error('Audio WS JSON error:', err);
                    }
                }
            };

            widget.audioWs.onclose = () => {
                console.log('🔊 Audio WebSocket closed');
                widget.audioStreamActive = false;
            };

            widget.audioWs.onerror = (err) => {
                console.warn('⚠️ Audio WebSocket error:', err);
                widget.audioStreamActive = false;
            };
        },

        handleAudioStreamChunk(widget, buffer) {
            if (widget.sessionMode === 'turbo' || widget.isVoiceMuted) return;
            widget.audioPreBuffer.push(buffer);
            if (widget.audioPreBuffer.length >= widget.audioPreBufferSize) {
                WS.flushAudioPreBuffer(widget);
            }
        },

        flushAudioPreBuffer(widget) {
            if (!widget.audioManager || widget.audioPreBuffer.length === 0) return;
            for (const chunk of widget.audioPreBuffer) {
                widget.audioManager.playChunk(chunk, 'pcm_f32le', 44100);
            }
            widget.audioPreBuffer = [];
        },

        /**
         * Send Phoenix resume messages (request_history + execution_complete).
         * Called by startVisualCopilot AFTER session_config is sent, so the
         * backend has is_mission_active=true before execution_complete arrives.
         */
        sendPhoenixResume(widget) {
            if (!widget._phoenixResumeNeeded) return;
            widget._phoenixResumeNeeded = false;

            const sessionId = sessionStorage.getItem('tara_session_id') ||
                localStorage.getItem('tara_session_id');

            console.log('📡 Phoenix: Sending resume messages after session_config...', sessionId);

            // Phase 1: Request History (for chat turns + bridging)
            if (sessionId && widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                widget.ws.send(JSON.stringify({
                    type: 'request_history',
                    session_id: sessionId,
                    mission_goal: widget.pendingMissionGoal || widget._currentMissionGoal || ''
                }));
            }

            // Phase 2: Signal Execution Complete (to trigger next step)
            if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                widget.ws.send(JSON.stringify({
                    type: 'execution_complete',
                    session_id: sessionId,
                    current_url: window.location.href,
                    mission_goal: widget.pendingMissionGoal || widget._currentMissionGoal || '',
                    mission_id: sessionStorage.getItem('tara_mission_id') || localStorage.getItem('tara_mission_id') || null,
                    subgoal_index: parseInt(sessionStorage.getItem('tara_subgoal_index') || localStorage.getItem('tara_subgoal_index') || '0', 10),
                    step_count: parseInt(sessionStorage.getItem('tara_step_count') || localStorage.getItem('tara_step_count') || '0', 10),
                    dom_context: window.TARA.Scanner.scanPageBlueprint(true) || []
                }));
            }

            // Fresh DOM scan
            try {
                const blueprint = window.TARA.Scanner.scanPageBlueprint(true);
                if (blueprint && widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                    widget.ws.send(JSON.stringify({
                        type: 'dom_update',
                        elements: blueprint,
                        url: window.location.href,
                        title: document.title
                    }));
                    console.log('📡 Phoenix: Fresh DOM sent');
                }
            } catch (e) {
                console.warn('Phoenix DOM scan failed:', e);
            }

            widget.wasNavigating = false;
            widget.isRestoredSession = false;
        },

        /**
         * Route all backend messages to appropriate handlers.
         */
        async handleBackendMessage(widget, msg) {
            console.log('📨 Backend message:', msg);
            const UI = window.TARA.UI;
            const Scanner = window.TARA.Scanner;

            if (msg.type === 'asset_data') {
                if (msg.asset === 'tara-orb.svg' && msg.data) {
                    const dataUrl = window.TARA.cacheOrbSvg(msg.data);
                    if (widget.orbImg) {
                        widget.orbImg.src = dataUrl;
                        widget.orbImg.style.opacity = '1';
                    }
                    console.log('📦 Orb SVG cached');
                }
                return;
            }
            else if (msg.type === 'session_created' || msg.type === 'session_ready') {
                sessionStorage.setItem('tara_session_id', msg.session_id);
                localStorage.setItem('tara_session_id', msg.session_id);
                sessionStorage.setItem('tara_active_session', msg.session_id);
                console.log('💾 Session ID saved:', msg.session_id);
                if (widget.sessionMode === 'interactive') {
                    WS.connectAudioWebSocket(widget, msg.session_id);
                }
                // Proactively seed backend with one screenshot so pre-router map hints
                // can use vision even before first execution_complete.
                (async () => {
                    try {
                        const screenshotB64 = await Scanner.captureScreenshot();
                        const elements = Scanner.scanPageBlueprint(true) || [];
                        if (widget.ws && widget.ws.readyState === WebSocket.OPEN && screenshotB64) {
                            widget.ws.send(JSON.stringify({
                                type: 'dom_update',
                                elements,
                                url: window.location.href,
                                title: document.title,
                                active_states: Scanner.detectActiveStates(),
                                data_tables: Scanner.extractVisibleTables(),
                                screenshot_b64: screenshotB64,
                                source: 'session_ready_bootstrap'
                            }));
                            console.log(`📸 Session bootstrap screenshot sent (${Math.round(screenshotB64.length / 1024)}KB)`);
                        }
                    } catch (snapErr) {
                        console.warn('📸 Session bootstrap screenshot failed:', snapErr);
                    }
                })();
            }
            else if (msg.type === 'request_screenshot') {
                try {
                    const screenshotB64 = await Scanner.captureScreenshot();
                    const elements = Scanner.scanPageBlueprint(true) || [];
                    if (widget.ws && widget.ws.readyState === WebSocket.OPEN && screenshotB64) {
                        widget.ws.send(JSON.stringify({
                            type: 'dom_update',
                            elements,
                            url: window.location.href,
                            title: document.title,
                            active_states: Scanner.detectActiveStates(),
                            data_tables: Scanner.extractVisibleTables(),
                            screenshot_b64: screenshotB64,
                            source: 'request_screenshot'
                        }));
                        console.log(`📸 Screenshot response sent (${Math.round(screenshotB64.length / 1024)}KB)`);
                    } else {
                        console.warn('📸 request_screenshot: capture failed or WS closed');
                    }
                } catch (snapErr) {
                    console.warn('📸 request_screenshot failed:', snapErr);
                }
            }
            else if (msg.type === 'history_restore') {
                if (msg.turns && msg.turns.length > 0) {
                    if (widget.chatMessages) widget.chatMessages.innerHTML = '';
                    msg.turns.forEach(turn => {
                        UI.appendChatMessage(widget, turn.text, turn.role === 'user' ? 'user' : 'ai', false);
                    });
                    console.log(`🔥 Phoenix: Restored ${msg.turns.length} history turns`);
                }
                if (msg.mission_goal) {
                    widget._currentMissionGoal = msg.mission_goal;
                    console.log('🎯 Phoenix: Mission goal restored:', msg.mission_goal);
                }
            }
            else if (msg.type === 'mission_started') {
                widget._currentMissionGoal = msg.goal;
                // Save mission context for persistence
                if (msg.mission_id) {
                    sessionStorage.setItem('tara_mission_id', msg.mission_id);
                    localStorage.setItem('tara_mission_id', msg.mission_id);
                }
                sessionStorage.setItem('tara_step_count', '0');
                sessionStorage.setItem('tara_subgoal_index', '0');
                localStorage.setItem('tara_step_count', '0');
                localStorage.setItem('tara_subgoal_index', '0');
                console.log('🎯 Mission goal tracked:', msg.goal, 'mission_id:', msg.mission_id || 'pending');
            }
            else if (msg.type === 'mission_complete') {
                console.log('✅ Mission complete.');
                sessionStorage.removeItem('tara_active_session');
                sessionStorage.removeItem('tara_is_navigating');
                sessionStorage.removeItem('tara_goal');
                localStorage.removeItem('tara_goal');
                sessionStorage.removeItem('tara_mission_id');
                sessionStorage.removeItem('tara_subgoal_index');
                sessionStorage.removeItem('tara_step_count');
                localStorage.removeItem('tara_mission_id');
                localStorage.removeItem('tara_subgoal_index');
                localStorage.removeItem('tara_step_count');
                window.TARA.Phoenix.clearMissionState();
                widget._currentMissionGoal = null;
                widget.pendingMissionGoal = null;
            }
            else if (msg.type === 'ping') {
                if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                    widget.ws.send(JSON.stringify({ type: 'pong' }));
                }
            }
            else if (msg.type === 'agent_response') {
                UI.appendChatMessage(widget, msg.text, 'ai', true);
            }
            else if (msg.type === 'navigate') {
                console.log(`📍 Navigating to: ${msg.url}`);
                widget.setOrbState('executing');

                try {
                    const targetUrl = new URL(msg.url, window.location.origin);
                    if (targetUrl.origin === window.location.origin) {
                        console.log('🔄 SPA Nav: pushState + popstate');
                        window.history.pushState({}, '', msg.url);
                        window.dispatchEvent(new PopStateEvent('popstate'));

                        setTimeout(() => {
                            if (window.location.href !== msg.url) {
                                console.warn('⚠️ Router blocked — forcing reload');
                                window.location.href = msg.url;
                            }
                        }, 500);
                    } else {
                        console.warn('⚠️ External navigation');
                        if (msg.phoenix) {
                            try {
                                const sessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                                const extUrl = new URL(msg.url, window.location.origin);
                                const phoenixData = {
                                    sid: sessionId || msg.session_id,
                                    goal: widget._currentMissionGoal || msg.mission_goal || ""
                                };
                                extUrl.hash = `tara_phoenix=${btoa(JSON.stringify(phoenixData))}`;
                                window.location.href = extUrl.toString();
                            } catch (err) {
                                console.warn('Phoenix URL format failed', err);
                                window.location.href = msg.url;
                            }
                        } else {
                            window.location.href = msg.url;
                        }
                    }
                } catch (e) {
                    console.error('Navigation error:', e);
                    window.location.href = msg.url;
                }
            }
            else if (msg.type === 'turbo_speech') {
                UI.simulateTyping(widget, msg.text);
            }
            else if (msg.type === 'command') {
                // Extract action details from the command message
                const payload = msg.payload || msg;
                const actionType = (msg.payload ? payload.type : null) || msg.action || payload.action_type || payload.type;
                const target_id = payload.target_id || payload.id || msg.target_id;
                const text = payload.text || msg.text;
                const type = (actionType === 'command') ? 'click' : actionType;

                // Track mission progress from backend
                if (msg.mission_id) {
                    sessionStorage.setItem('tara_mission_id', msg.mission_id);
                    localStorage.setItem('tara_mission_id', msg.mission_id);
                }
                if (msg.subgoal_index !== undefined) {
                    sessionStorage.setItem('tara_subgoal_index', String(msg.subgoal_index));
                    localStorage.setItem('tara_subgoal_index', String(msg.subgoal_index));
                }
                // Increment step count
                const currentStep = parseInt(sessionStorage.getItem('tara_step_count') || '0', 10);
                sessionStorage.setItem('tara_step_count', String(currentStep + 1));
                localStorage.setItem('tara_step_count', String(currentStep + 1));

                console.log(`🤖 Executing: ${type} on ${target_id} (step ${currentStep + 1})`);

                const preActionUrl = window.location.href;
                const preActionHash = Scanner.lastDOMHash;
                const settleStart = Date.now();

                await widget.executor.executeCommand(type, target_id, text);

                // Phoenix Guard
                const willNavigate = sessionStorage.getItem('tara_is_navigating') === 'true';
                if (willNavigate) {
                    console.log('🛡️ Phoenix Guard: Skipping execution_complete — navigating.');
                    widget.waitingForExecution = false;
                    return;
                }

                const settleTime = Date.now() - settleStart;
                const freshDOM = Scanner.scanPageBlueprint(true);
                const urlChanged = window.location.href !== preActionUrl;
                const newElements = freshDOM ? freshDOM.filter(el => el.isNew).length : 0;
                const domChanged = freshDOM !== null || Scanner.lastDOMHash !== preActionHash;

                // 📸 Capture post-action screenshot for Groq Vision (non-blocking)
                let screenshotB64 = null;
                try {
                    screenshotB64 = await Scanner.captureScreenshot();
                } catch (snapErr) {
                    console.warn('📸 Screenshot capture skipped:', snapErr);
                }

                widget.ws.send(JSON.stringify({
                    type: 'execution_complete',
                    status: 'success',
                    mission_id: sessionStorage.getItem('tara_mission_id') || null,
                    subgoal_index: parseInt(sessionStorage.getItem('tara_subgoal_index') || '0', 10),
                    step_count: parseInt(sessionStorage.getItem('tara_step_count') || '0', 10),
                    outcome: {
                        dom_changed: domChanged,
                        url_changed: urlChanged,
                        new_elements_count: newElements,
                        current_url: window.location.href,
                        has_modal: Scanner.detectModal(),
                        settle_time_ms: settleTime,
                        dom_hash: Scanner.lastDOMHash,
                        scroll_y: Math.round(window.scrollY)
                    },
                    dom_context: freshDOM,
                    active_states: Scanner.detectActiveStates(),
                    data_tables: Scanner.extractVisibleTables(),
                    title: document.title,
                    screenshot_b64: screenshotB64,  // 👁️ for Groq Vision in last mile
                    timestamp: Date.now()
                }));

                console.log(`✅ Execution complete (${settleTime}ms, ${newElements} new, url_changed=${urlChanged}, screenshot=${screenshotB64 ? Math.round(screenshotB64.length / 1024) + 'KB' : 'none'})`);
                widget.waitingForExecution = false;
                widget.setOrbState('listening');
            }
            else if (msg.type === 'state_update') {
                if (msg.state) {
                    const s = msg.state;
                    if (s === 'listening') {
                        widget.setOrbState('listening');
                        if (widget.sessionMode === 'interactive') {
                            if (!widget.micStream) {
                                widget.startMicrophoneAndCollection();
                            } else if (widget.micAudioCtx && widget.micAudioCtx.state === 'suspended') {
                                widget.micAudioCtx.resume();
                            }
                        }
                    }
                    if (s === 'thinking') {
                        widget.setOrbState('listening');
                        UI.showTypingIndicator(widget);
                    }
                    if (s === 'speaking') widget.setOrbState('talking');
                }
            }
            else if (msg.type === 'speaker_mute_confirmed') {
                const mode = msg.mode === 'turbo' ? 'TURBO MODE' : 'WALKTHROUGH MODE';
                console.log(`🎛️ Mode confirmed: ${mode}`);

                const statusEl = widget.pillContainer?.querySelector('.tara-pill-status');
                if (statusEl) {
                    const originalText = statusEl.textContent;
                    statusEl.textContent = msg.muted ? '🚀 Turbo Mode' : '🔊 Walkthrough';
                    setTimeout(() => {
                        if (statusEl.textContent.includes('Turbo') || statusEl.textContent.includes('Walkthrough')) {
                            statusEl.textContent = originalText;
                        }
                    }, 2000);
                }
            }
            // ═══════════════════════════════════════════════════════════
            // 👁️ VISION: Backend requests a page screenshot for Groq Vision
            // ═══════════════════════════════════════════════════════════
            else if (msg.type === 'request_screenshot') {
                const reason = msg.reason || 'visual analysis';
                const requestId = msg.request_id || null;
                console.log(`📸 [TARA Vision] Screenshot requested by backend: "${reason}"`);

                widget.setOrbState('thinking');

                try {
                    const b64 = await Scanner.captureScreenshot();

                    if (b64 && widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                        widget.ws.send(JSON.stringify({
                            type: 'screenshot_response',
                            request_id: requestId,
                            image_b64: b64,
                            image_mime: 'image/jpeg',
                            url: window.location.href,
                            timestamp: Date.now()
                        }));
                        console.log(`✅ [TARA Vision] Screenshot sent (${(b64.length / 1024).toFixed(0)}KB)`);
                    } else {
                        // Screenshot failed — tell the backend so it can proceed without vision
                        if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                            widget.ws.send(JSON.stringify({
                                type: 'screenshot_response',
                                request_id: requestId,
                                image_b64: null,
                                error: 'capture_failed',
                                url: window.location.href,
                                timestamp: Date.now()
                            }));
                        }
                        console.warn('⚠️ [TARA Vision] Screenshot capture returned null');
                    }
                } catch (err) {
                    console.error('❌ [TARA Vision] Screenshot error:', err);
                    if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                        widget.ws.send(JSON.stringify({
                            type: 'screenshot_response',
                            request_id: requestId,
                            image_b64: null,
                            error: String(err),
                            url: window.location.href,
                            timestamp: Date.now()
                        }));
                    }
                } finally {
                    widget.setOrbState('listening');
                }
            }
            // ═══════════════════════════════════════════════════════════
            // 🔍 ANALYSE PAGE: Backend finished narrating — reset orb
            // ═══════════════════════════════════════════════════════════
            else if (msg.type === 'analyse_complete') {
                console.log(`✅ [TARA Analyse] Narration complete (mode=${msg.analysis_mode})`);
                widget.orbContainer.classList.remove('executing', 'listening', 'talking');
                widget.orbContainer.classList.add('idle');

                if (widget._analyseMode && msg.narration) {
                    // Show answer in chat bar, keep it open for follow-ups
                    UI.hideTypingIndicator(widget);
                    UI.appendChatMessage(widget, msg.narration, 'ai');
                    widget.chatInput.placeholder = 'Ask another question...';
                    widget.chatInput.focus();
                    const statusEl = widget.pillContainer?.querySelector('.tara-pill-status');
                    if (statusEl) statusEl.textContent = 'Ask about this page';
                } else {
                    const statusEl = widget.pillContainer?.querySelector('.tara-pill-status');
                    if (statusEl) {
                        statusEl.textContent = '✓ Done';
                        setTimeout(() => {
                            if (!widget.isActive) statusEl.textContent = 'Click orb to start';
                        }, 2000);
                    }
                }
            }
            else if (msg.type === 'analyse_error') {
                console.error(`❌ [TARA Analyse] Error:`, msg.error);
                widget.orbContainer.classList.remove('executing', 'listening', 'talking');
                widget.orbContainer.classList.add('idle');
                const statusEl = widget.pillContainer?.querySelector('.tara-pill-status');
                if (statusEl) {
                    statusEl.textContent = '⚠️ Analysis failed';
                    setTimeout(() => {
                        if (!widget.isActive) statusEl.textContent = 'Click orb to start';
                    }, 3000);
                }
            }
        }
    };

    window.TARA.WS = WS;
    console.log('✅ [TARA] WS module loaded');
})();
