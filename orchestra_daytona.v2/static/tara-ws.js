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
         * 
         * DEPRECATED: Use sendBackendResume() for backend-driven recovery.
         * This method is kept for backward compatibility with legacy clients.
         */
        sendPhoenixResume(widget) {
            if (!widget._phoenixResumeNeeded) return;
            widget._phoenixResumeNeeded = false;

            const sessionId = sessionStorage.getItem('tara_session_id') ||
                localStorage.getItem('tara_session_id');

            console.log('📡 Phoenix: Sending resume messages after session_config...', sessionId);

            let savedActionHistory = [];
            try {
                savedActionHistory = JSON.parse(
                    sessionStorage.getItem('tara_action_history') ||
                    localStorage.getItem('tara_action_history') ||
                    '[]'
                );
                if (!Array.isArray(savedActionHistory)) savedActionHistory = [];
            } catch (e) {
                savedActionHistory = [];
            }

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
                    action_history: savedActionHistory.slice(-5),
                    outcome: {
                        current_url: window.location.href
                    },
                    dom_context: window.TARA.Scanner.scanPageBlueprint(true) || []
                }));
            }

            widget.wasNavigating = false;
            widget.isRestoredSession = false;
        },

        /**
         * Send Backend Resume request (backend-driven recovery).
         * Called by startVisualCopilot to request authoritative mission state from backend.
         * 
         * This is the NEW recovery protocol that makes the backend the canonical owner
         * of mission state, page position, and action history.
         */
        sendBackendResume(widget) {
            const sessionId = sessionStorage.getItem('tara_session_id') ||
                localStorage.getItem('tara_session_id');

            if (!sessionId) {
                console.log('📡 Backend Resume: No session ID found, skipping');
                return;
            }

            // Load frontend storage hints (for reference only, backend is authoritative)
            let frontendContext = {
                step_count: parseInt(sessionStorage.getItem('tara_step_count') || localStorage.getItem('tara_step_count') || '0', 10),
                subgoal_index: parseInt(sessionStorage.getItem('tara_subgoal_index') || localStorage.getItem('tara_subgoal_index') || '0', 10),
                action_history: []
            };

            try {
                frontendContext.action_history = JSON.parse(
                    sessionStorage.getItem('tara_action_history') ||
                    localStorage.getItem('tara_action_history') ||
                    '[]'
                );
                if (!Array.isArray(frontendContext.action_history)) {
                    frontendContext.action_history = [];
                }
            } catch (e) {
                frontendContext.action_history = [];
            }

            // Send resume_session request to backend
            if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                console.log('📡 Backend Resume: Requesting mission state from backend...', sessionId);

                widget.ws.send(JSON.stringify({
                    type: 'resume_session',
                    session_id: sessionId,
                    current_url: window.location.href,
                    page_title: document.title,
                    goal_hint: widget.pendingMissionGoal || widget._currentMissionGoal || '',
                    client_context: frontendContext,
                    request_full_history: true  // Request authoritative history from backend
                }));
            }
        },

        /**
         * Handle resume_state response from backend.
         * Updates widget state with authoritative backend mission state.
         */
        handleResumeState(widget, msg) {
            console.log('📥 Backend Resume State:', msg);

            if (!msg.found) {
                console.log('📡 Backend Resume: No active mission found');
                // Clear frontend storage
                sessionStorage.removeItem('tara_mission_id');
                sessionStorage.removeItem('tara_step_count');
                sessionStorage.removeItem('tara_subgoal_index');
                localStorage.removeItem('tara_mission_id');
                localStorage.removeItem('tara_step_count');
                localStorage.removeItem('tara_subgoal_index');
                return;
            }

            // Update widget state with backend authority
            if (msg.mission_id) {
                sessionStorage.setItem('tara_mission_id', msg.mission_id);
                localStorage.setItem('tara_mission_id', msg.mission_id);
            }

            if (msg.goal) {
                widget._currentMissionGoal = msg.goal;
                widget.pendingMissionGoal = msg.goal;
                sessionStorage.setItem('tara_goal', msg.goal);
                localStorage.setItem('tara_goal', msg.goal);
            }

            // Update step count and subgoal index from backend
            if (typeof msg.step_count === 'number') {
                sessionStorage.setItem('tara_step_count', msg.step_count.toString());
                localStorage.setItem('tara_step_count', msg.step_count.toString());
            }

            if (typeof msg.subgoal_index === 'number') {
                sessionStorage.setItem('tara_subgoal_index', msg.subgoal_index.toString());
                localStorage.setItem('tara_subgoal_index', msg.subgoal_index.toString());
            }

            // Log recent actions for debugging
            if (msg.recent_actions && msg.recent_actions.length > 0) {
                console.log('📋 Recent actions from backend:', msg.recent_actions);
                sessionStorage.setItem('tara_action_history', JSON.stringify(msg.recent_actions));
                localStorage.setItem('tara_action_history', JSON.stringify(msg.recent_actions));
            }

            // Handle pending pipeline (multi-action resumption)
            if (msg.has_pending_pipeline) {
                console.log('📋 Backend has pending pipeline for resumption');
                // Pipeline will be sent separately via backend action dispatch
            }

            if (
                msg.resume_instruction === 'request_dom_refresh' &&
                widget.ws &&
                widget.ws.readyState === WebSocket.OPEN
            ) {
                const Scanner = window.TARA.Scanner;
                const freshDOM = Scanner.scanPageBlueprint(true) || [];
                widget.ws.send(JSON.stringify({
                    type: 'dom_update',
                    elements: freshDOM,
                    url: window.location.href,
                    title: document.title,
                    active_states: Scanner.detectActiveStates(),
                    data_tables: Scanner.extractVisibleTables(),
                    source: 'backend_resume_refresh'
                }));
                widget.ws.send(JSON.stringify({
                    type: 'execution_complete',
                    status: 'resume_sync',
                    mission_id: msg.mission_id || null,
                    subgoal_index: typeof msg.subgoal_index === 'number' ? msg.subgoal_index : 0,
                    step_count: typeof msg.step_count === 'number' ? msg.step_count : 0,
                    action_history: [],
                    outcome: {
                        current_url: window.location.href,
                        url_changed: false,
                        dom_changed: false
                    },
                    dom_context: freshDOM,
                    active_states: Scanner.detectActiveStates(),
                    data_tables: Scanner.extractVisibleTables(),
                    title: document.title
                }));
            }

            console.log(
                `✅ Backend Resume Complete | mission=${msg.mission_id} | ` +
                `phase=${msg.phase} | step=${msg.step_count} | subgoal=${msg.subgoal_index}`
            );
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
            else if (msg.type === 'resume_state') {
                // Backend-driven recovery: handle resume state response
                WS.handleResumeState(widget, msg);
            }
            else if (msg.type === 'mission_started') {
                widget._currentMissionGoal = msg.goal;
                // Save mission context for persistence
                sessionStorage.setItem('tara_goal', msg.goal || '');
                localStorage.setItem('tara_goal', msg.goal || '');
                if (msg.mission_id) {
                    sessionStorage.setItem('tara_mission_id', msg.mission_id);
                    localStorage.setItem('tara_mission_id', msg.mission_id);
                }
                sessionStorage.setItem('tara_step_count', '0');
                sessionStorage.setItem('tara_subgoal_index', '0');
                sessionStorage.setItem('tara_action_history', '[]');
                localStorage.setItem('tara_step_count', '0');
                localStorage.setItem('tara_subgoal_index', '0');
                localStorage.setItem('tara_action_history', '[]');
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
                sessionStorage.removeItem('tara_action_history');
                localStorage.removeItem('tara_mission_id');
                localStorage.removeItem('tara_subgoal_index');
                localStorage.removeItem('tara_step_count');
                localStorage.removeItem('tara_action_history');
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
                // ═══════════════════════════════════════════════════════════
                // 🚀 MULTI-ACTION PIPELINE — Handles bundled action arrays
                //
                // The backend now returns either:
                //   msg.action = { type, target_id, ... }   (single action, legacy)
                //   msg.action = [ {...}, {...}, {...} ]     (bundled pipeline)
                //
                // For bundled arrays, we execute each action sequentially with
                // a micro-delay between steps and only send ONE execution_complete
                // at the very end. This eliminates the 4-5s per-step latency tax.
                // ═══════════════════════════════════════════════════════════

                // Normalise payload — backend may send msg.action (array or object)
                // or the legacy msg.payload wrapper.
                const rawAction = msg.action || msg.payload || msg;
                const actionList = Array.isArray(rawAction) ? rawAction : [rawAction];

                // Track pipeline_id for action acknowledgements (Backend Recovery)
                if (msg.pipeline_id) {
                    window.TARA._lastPipelineId = msg.pipeline_id;
                    console.log(`📋 Pipeline ID: ${msg.pipeline_id}`);
                }

                // Track mission progress from backend (once, before the sequence)
                if (msg.mission_id) {
                    sessionStorage.setItem('tara_mission_id', msg.mission_id);
                    localStorage.setItem('tara_mission_id', msg.mission_id);
                }
                if (msg.subgoal_index !== undefined) {
                    sessionStorage.setItem('tara_subgoal_index', String(msg.subgoal_index));
                    localStorage.setItem('tara_subgoal_index', String(msg.subgoal_index));
                }
                const currentStep = parseInt(sessionStorage.getItem('tara_step_count') || '0', 10);
                sessionStorage.setItem('tara_step_count', String(currentStep + actionList.length));
                localStorage.setItem('tara_step_count', String(currentStep + actionList.length));

                const preActionUrl = window.location.href;
                const preActionHash = Scanner.lastDOMHash;
                const settleStart = Date.now();
                let abortedForNavigation = false;
                let actionableSteps = 0;
                let executedActionable = 0;
                const actionErrors = [];

                console.log(`🤖 Pipeline: executing ${actionList.length} action(s) (step ${currentStep + 1})`);

                // ── Execute each action in sequence ──
                for (let i = 0; i < actionList.length; i++) {
                    const step = actionList[i];
                    if (!step || typeof step !== 'object') continue;

                    const rawType = step.type || step.action_type || step.action || '';
                    const type = rawType === 'command' ? 'click' : rawType;
                    const target_id = step.target_id || step.id || '';
                    const text = step.text || step.target_label || step.label || step.speech || '';
                    const force_click = step.force_click || false;
                    // Bundled waits may carry explicit wait_ms (from search injection)
                    const wait_ms = step.wait_ms || null;

                    console.log(`  ↳ [${i + 1}/${actionList.length}] ${type} ${target_id || ''}`);

                    // Persist compact action history for Phoenix resume
                    try {
                        const actionSig = `${type}:${target_id || 'none'}`;
                        let hist = JSON.parse(
                            sessionStorage.getItem('tara_action_history') ||
                            localStorage.getItem('tara_action_history') ||
                            '[]'
                        );
                        if (!Array.isArray(hist)) hist = [];
                        hist.push(actionSig);
                        hist = hist.slice(-5);
                        sessionStorage.setItem('tara_action_history', JSON.stringify(hist));
                        localStorage.setItem('tara_action_history', JSON.stringify(hist));
                    } catch (e) {
                        console.warn('⚠️ Failed to persist action history:', e);
                    }

                    // Execute the action
                    if (type === 'wait') {
                        const delay = wait_ms || ((step.seconds || 2) * 1000);
                        console.log(`  ⏳ Wait ${delay}ms`);
                        await new Promise(r => setTimeout(r, delay));
                    } else {
                        actionableSteps += 1;
                        const execResult = await widget.executor.executeCommand(type, target_id, text, force_click);
                        if (execResult === undefined || (execResult && execResult.executed)) {
                            executedActionable += 1;
                        } else {
                            actionErrors.push({
                                step: i + 1,
                                type,
                                target_id,
                                reason: (execResult && execResult.reason) || 'unknown'
                            });
                        }
                    }

                    // Phoenix Guard: if navigation triggered mid-sequence, abort and skip execution_complete
                    if (sessionStorage.getItem('tara_is_navigating') === 'true') {
                        console.log('🛡️ Phoenix Guard: Navigation detected mid-pipeline — aborting sequence.');
                        abortedForNavigation = true;
                        break;
                    }

                    // Micro-delay between sequential actions (not after last one)
                    if (i < actionList.length - 1 && type !== 'wait') {
                        await new Promise(r => setTimeout(r, 800));
                    }
                }

                if (abortedForNavigation) {
                    widget.waitingForExecution = false;
                    return;
                }

                // ── Post-sequence: gather state and report back ──
                const settleTime = Date.now() - settleStart;
                const freshDOM = Scanner.scanPageBlueprint(true);
                const urlChanged = window.location.href !== preActionUrl;
                const newElements = freshDOM ? freshDOM.filter(el => el.isNew).length : 0;
                const domChanged = freshDOM !== null || Scanner.lastDOMHash !== preActionHash;

                // 📸 Capture post-sequence screenshot for Groq Vision (non-blocking)
                let screenshotB64 = null;
                try {
                    screenshotB64 = await Scanner.captureScreenshot();
                } catch (snapErr) {
                    console.warn('📸 Screenshot capture skipped:', snapErr);
                }

                const execStatus = executedActionable > 0 || actionableSteps === 0 ? 'success' : 'no_action';
                if (execStatus !== 'success') {
                    console.warn(`⚠️ Pipeline executed no actionable UI steps (attempted=${actionableSteps})`, actionErrors);
                }

                widget.ws.send(JSON.stringify({
                    type: 'execution_complete',
                    status: execStatus,
                    mission_id: sessionStorage.getItem('tara_mission_id') || null,
                    subgoal_index: parseInt(sessionStorage.getItem('tara_subgoal_index') || '0', 10),
                    step_count: parseInt(sessionStorage.getItem('tara_step_count') || '0', 10),
                    action_history: (() => {
                        try {
                            const hist = JSON.parse(
                                sessionStorage.getItem('tara_action_history') ||
                                localStorage.getItem('tara_action_history') ||
                                '[]'
                            );
                            return Array.isArray(hist) ? hist.slice(-5) : [];
                        } catch {
                            return [];
                        }
                    })(),
                    pipeline_length: actionList.length,
                    // Backend Recovery: Include action acknowledgements
                    action_acknowledgements: (() => {
                        // Track which backend actions were executed
                        // This allows the backend to update the action ledger status
                        const acks = [];
                        if (window.TARA._lastPipelineId) {
                            // Acknowledge all actions in the pipeline
                            for (let i = 0; i < actionList.length; i++) {
                                acks.push({
                                    pipeline_id: window.TARA._lastPipelineId,
                                    action_index: i,
                                    action_type: actionList[i].type,
                                    target_id: actionList[i].target_id,
                                    executed: true,
                                    url_after: window.location.href
                                });
                            }
                        }
                        return acks;
                    })(),
                    outcome: {
                        action_attempted_count: actionableSteps,
                        action_executed_count: executedActionable,
                        action_errors: actionErrors,
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
                    screenshot_b64: screenshotB64,
                    timestamp: Date.now()
                }));

                console.log(`✅ Pipeline complete: ${actionList.length} actions in ${settleTime}ms (url_changed=${urlChanged}, screenshot=${screenshotB64 ? Math.round(screenshotB64.length / 1024) + 'KB' : 'none'})`);
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
