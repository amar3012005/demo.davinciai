/**
 * TARA Visual Co-Pilot — Core Orchestrator
 * Module: tara-core.js
 *
 * Owns: TaraWidget class — thin coordinator that delegates to all modules.
 * Also owns: mic capture, audio processing, start/stop, bootstrap.
 * Depends on: all other TARA modules
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    const Phoenix = window.TARA.Phoenix;
    const Scanner = window.TARA.Scanner;
    const UI = window.TARA.UI;
    const WS = window.TARA.WS;

    class TaraWidget {
        constructor(config = {}) {
            this.config = { ...window.TARA.Config, ...config };
            this.isActive = false;
            this.ws = null;
            this.vad = null;
            this.audioManager = null;
            this.micStream = null;
            this.micAudioCtx = null;
            this.binaryQueue = [];
            this.audioPreBuffer = [];
            this.audioPreBufferSize = 3;
            this.audioStreamActive = false;
            this.audioWs = null;
            this.waitingForExecution = false;
            this.domSnapshotPending = false;
            this.isNavigating = false;
            this.waitingForIntro = false;
            this.agentIsSpeaking = false;
            this.isVoiceMuted = false;
            this.audioPlaybackTimer = null;
            this.chunksSent = 0;
            this.sessionMode = 'interactive'; // or 'turbo'
            this._currentMissionGoal = null;
            this.pendingMissionGoal = null;

            // Initialize UI (creates shadow DOM, orb, etc.)
            UI.initUI(this);

            // Session recovery (Phoenix Protocol)
            const recovery = Phoenix.recoverSession();
            this.isRestoredSession = recovery.isRestoredSession;
            this.wasNavigating = recovery.wasNavigating;
            if (recovery.pendingMissionGoal) {
                this.pendingMissionGoal = recovery.pendingMissionGoal;
                this._currentMissionGoal = recovery.pendingMissionGoal;
            }
            this._resumeMissionId = recovery.missionId || null;
            this._resumeSubgoalIndex = recovery.subgoalIndex || 0;
            this._resumeStepCount = recovery.stepCount || 0;

            // Fallback: hydrate from persisted mission state if recovery fields are incomplete.
            const savedMission = Phoenix.loadMissionState();
            if (savedMission) {
                if (!this.pendingMissionGoal && savedMission.goal) {
                    this.pendingMissionGoal = savedMission.goal;
                    this._currentMissionGoal = savedMission.goal;
                }
                if (!this._resumeMissionId && savedMission.missionId) {
                    this._resumeMissionId = savedMission.missionId;
                }
                if ((!this._resumeSubgoalIndex || this._resumeSubgoalIndex === 0) && typeof savedMission.subgoalIndex === 'number') {
                    this._resumeSubgoalIndex = savedMission.subgoalIndex;
                }
                if ((!this._resumeStepCount || this._resumeStepCount === 0) && typeof savedMission.stepCount === 'number') {
                    this._resumeStepCount = savedMission.stepCount;
                }
            }

            // Create executor
            this.executor = window.TARA.createExecutor(this);

            // Determine saved mode
            const savedMode = localStorage.getItem('tara_mode') || sessionStorage.getItem('tara_mode');
            const savedInteractionMode = localStorage.getItem('tara_interaction_mode') || sessionStorage.getItem('tara_interaction_mode');

            if (recovery.sessionId && (savedMode === 'visual-copilot' || this.isRestoredSession)) {
                console.log('🔄 Auto-resuming Visual Co-Pilot with session:', recovery.sessionId);
                this.startVisualCopilot(recovery.sessionId, savedInteractionMode || 'interactive');
            }

            // Register beforeunload handler
            Phoenix.registerNavigationPersistence(this);
        }

        // ─── Delegate Proxy Methods ──────────────────────────────

        setOrbState(state) {
            UI.setOrbState(this, state);
        }

        updateOrbVolume(volume) {
            UI.updateOrbVolume(this, volume);
        }

        updateTooltip(text) {
            UI.updateTooltip(this, text);
        }

        showChatBar() {
            UI.showChatBar(this);
        }

        hideChatBar() {
            UI.hideChatBar(this);
        }

        showTypingIndicator() {
            UI.showTypingIndicator(this);
        }

        hideTypingIndicator() {
            UI.hideTypingIndicator(this);
        }

        appendChatMessage(text, sender, isStreaming = false) {
            UI.appendChatMessage(this, text, sender, isStreaming);
        }

        simulateTyping(text) {
            UI.simulateTyping(this, text);
        }

        sendTextCommand() {
            UI.sendTextCommand(this);
        }

        // Scanner proxies
        scanPageBlueprint(force = false) {
            return Scanner.scanPageBlueprint(force);
        }

        scanLiveGraph() {
            return Scanner.scanPageBlueprint(true);
        }

        forceScan() {
            Scanner.forceScan();
            if (this.isActive && this.ws && this.ws.readyState === WebSocket.OPEN) {
                const blueprint = Scanner.scanPageBlueprint();
                if (blueprint) {
                    this.ws.send(JSON.stringify({
                        type: 'dom_update',
                        elements: blueprint,
                        url: window.location.href,
                        title: document.title,
                        active_states: Scanner.detectActiveStates(),
                        data_tables: Scanner.extractVisibleTables()
                    }));
                    console.log('📤 Forced DOM update sent');
                }
            }
        }

        // ─── Audio & Microphone ──────────────────────────────────

        async initializeAudioManager() {
            this.audioManager = new window.TARA.AudioManager();
            await this.audioManager.initialize({
                onStart: () => {
                    this.agentIsSpeaking = true;
                    this.setOrbState('talking');
                    console.log('🎙️ Tara is speaking...');
                },
                onEnd: () => {
                    this.agentIsSpeaking = false;
                    this.setOrbState('listening');
                    console.log('🎙️ Tara finished speaking');
                }
            });
            console.log('🔊 Audio manager initialized');
        }

        async startMicrophoneAndCollection() {
            try {
                const MIC_SAMPLE_RATE = 16000;

                try {
                    this.micStream = await navigator.mediaDevices.getUserMedia({
                        audio: {
                            sampleRate: MIC_SAMPLE_RATE,
                            channelCount: 1,
                            echoCancellation: true,
                            noiseSuppression: true,
                            autoGainControl: true
                        }
                    });
                } catch (micErr) {
                    console.error("Mic Access Error:", micErr);
                    if (micErr.name === 'NotAllowedError' || micErr.message.includes('Permissions policy')) {
                        console.warn("Microphone blocked. Falling back to Text Mode.");
                        alert("Microphone access blocked. Falling back to TEXT MODE.");
                        return false;
                    }
                    console.warn("Microphone failed. Falling back to Text Mode.");
                    return false;
                }

                // VAD: gated — ignore while agent is speaking
                this.vad = new window.TARA.VoiceActivityDetector(
                    () => {
                        if (this.agentIsSpeaking) return;
                        console.log("🗣️ User started speaking [VAD]");
                        this.onSpeechStart();
                    },
                    () => {
                        if (this.agentIsSpeaking) return;
                        this.onSpeechEnd();
                    }
                );

                this.micAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: MIC_SAMPLE_RATE
                });

                if (this.micAudioCtx.state === 'suspended') {
                    await this.micAudioCtx.resume();
                }

                const source = this.micAudioCtx.createMediaStreamSource(this.micStream);
                const processor = this.micAudioCtx.createScriptProcessor(2048, 1, 1);

                source.connect(processor);
                processor.connect(this.micAudioCtx.destination);

                processor.onaudioprocess = (e) => {
                    if (!this.isActive || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;

                    if (this.agentIsSpeaking) {
                        this.updateOrbVolume(0);
                        return;
                    }

                    const inputData = e.inputBuffer.getChannelData(0);

                    // VAD processing
                    this.vad.processAudioChunk(inputData);

                    // Volume visualizer (RMS)
                    let sum = 0;
                    for (let i = 0; i < inputData.length; i++) {
                        sum += inputData[i] * inputData[i];
                    }
                    const rms = Math.sqrt(sum / inputData.length);
                    this.updateOrbVolume(rms * 5);

                    // Send PCM to backend
                    const pcmData = new Int16Array(inputData.length);
                    for (let i = 0; i < inputData.length; i++) {
                        pcmData[i] = Math.max(-1, Math.min(1, inputData[i])) * 0x7FFF;
                    }

                    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                        this.ws.send(pcmData.buffer);
                        this.chunksSent++;
                        if (this.chunksSent % 100 === 1) {
                            console.log(`🎤 Audio streaming: ${this.chunksSent} chunks sent`);
                        }
                    }
                };

                console.log('🎤 Microphone active - Listening for speech...');
                this.setOrbState('listening');

            } catch (err) {
                console.error('❌ Microphone access failed:', err);
            }
        }

        // ─── Speech Events ───────────────────────────────────────

        onSpeechStart() {
            if (this.domSnapshotPending || this.waitingForExecution) return;
            this.domSnapshotPending = true;

            if (this.ws && this.ws.readyState === WebSocket.OPEN && !this.waitingForExecution) {
                const blueprint = Scanner.scanPageBlueprint();
                if (blueprint) {
                    console.log('📸 SPEECH DETECTED — DOM Changed, Sending Update');
                    const payload = JSON.stringify({
                        type: 'dom_update',
                        elements: blueprint,
                        url: window.location.href,
                        title: document.title,
                        active_states: Scanner.detectActiveStates(),
                        data_tables: Scanner.extractVisibleTables()
                    });
                    this.ws.send(payload);

                    // Also send a screenshot snapshot for backend pre-router vision gate.
                    // This runs asynchronously so it does not block VAD flow.
                    (async () => {
                        try {
                            const screenshotB64 = await Scanner.captureScreenshot();
                            if (screenshotB64 && this.ws && this.ws.readyState === WebSocket.OPEN) {
                                this.ws.send(JSON.stringify({
                                    type: 'dom_update',
                                    elements: blueprint,
                                    url: window.location.href,
                                    title: document.title,
                                    active_states: Scanner.detectActiveStates(),
                                    data_tables: Scanner.extractVisibleTables(),
                                    screenshot_b64: screenshotB64
                                }));
                                console.log(`📸 Speech snapshot sent (${Math.round(screenshotB64.length / 1024)}KB)`);
                            }
                        } catch (snapErr) {
                            console.warn('📸 Speech snapshot capture skipped:', snapErr);
                        }
                    })();
                } else {
                    console.log('📸 Speech started — DOM Unchanged');
                }
            }

            this.setOrbState('listening');
        }

        onSpeechEnd() {
            console.log('🤐 Speech ended — waiting for command...');
            this.waitingForExecution = true;
            this.domSnapshotPending = false;
            this.setOrbState('listening');
        }

        // ─── Start / Stop ────────────────────────────────────────

        async startVisualCopilot(resumeSessionId = null, mode = 'interactive') {
            this.sessionMode = mode;
            this.isActive = true;
            this.chatMessages.innerHTML = '';
            this.chatMessages.classList.remove('has-messages');

            // Mode-specific setup
            if (mode === 'interactive') {
                await this.initializeAudioManager();
                this.micButton.style.display = 'flex';
            } else {
                this.micButton.style.display = 'none';
            }

            // Update mode badge
            if (this.modeBadge) {
                this.modeBadge.textContent = mode === 'turbo' ? '⚡ Turbo' : '🎤 Interactive';
                this.modeBadge.className = `tara-mode-badge ${mode}`;
            }

            // Connect WebSocket
            await WS.connectWebSocket(this);

            // Send session_config
            const sessionConfig = {
                type: 'session_config',
                mode: 'visual-copilot',
                interaction_mode: this.sessionMode,
                current_url: window.location.href,
                title: document.title,
                viewport: { width: window.innerWidth, height: window.innerHeight },
                dom_elements: Scanner.scanPageBlueprint(true),
                session_id: resumeSessionId,
                pending_goal: this.pendingMissionGoal || null,
                // Mission resume data (if resuming)
                resume_mission_id: this._resumeMissionId || null,
                resume_subgoal_index: this._resumeSubgoalIndex || 0,
                resume_step_count: this._resumeStepCount || 0
            };

            // 1. Persist for auto-resume
            sessionStorage.setItem('tara_mode', 'visual-copilot');
            sessionStorage.setItem('tara_interaction_mode', this.sessionMode);
            localStorage.setItem('tara_mode', 'visual-copilot');
            localStorage.setItem('tara_interaction_mode', this.sessionMode);

            // 2. Start TaraSensor immediately (if available) - This sends the initial 'full_scan'
            if (typeof window.TaraSensor !== 'undefined') {
                try {
                    this.taraSensor = new window.TaraSensor(this.ws, {
                        sendFullScanOnInit: true,
                        debounceMs: 150,
                        maxBatchSize: 50
                    });
                    this.taraSensor.start();
                    console.log('📡 TaraSensor started for delta streaming');
                } catch (e) {
                    console.warn('TaraSensor init failed:', e);
                }
            }

            // 3. Send session_config (Trigger for mission planning)
            console.log('📤 Sending session_config:', JSON.stringify(sessionConfig));
            this.ws.send(JSON.stringify(sessionConfig));

            // 3a. Force one immediate DOM seed packet on widget-open.
            // This avoids pre-route "0 nodes" races before first user input.
            if (this.ws && this.ws.readyState === WebSocket.OPEN && Array.isArray(sessionConfig.dom_elements) && sessionConfig.dom_elements.length > 0) {
                this.ws.send(JSON.stringify({
                    type: 'dom_update',
                    elements: sessionConfig.dom_elements,
                    url: window.location.href,
                    title: document.title,
                    source: 'widget_click_seed'
                }));
                console.log(`🌱 [TARA] widget_click_seed sent (${sessionConfig.dom_elements.length} nodes)`);
            }

            // 3b. Phoenix: Send resume messages AFTER session_config so backend
            //     has is_mission_active=true before execution_complete arrives
            WS.sendPhoenixResume(this);
            this.pendingMissionGoal = null;

            // 4. Update UI
            this.showChatBar();
            this.setOrbState('listening');

            // 5. Start mic for interactive mode (async)
            if (mode === 'interactive') {
                const micOk = await this.startMicrophoneAndCollection();
                if (micOk === false) {
                    console.log('🔇 Mic failed — continuing in text mode');
                }
            }
        }

        async stopVisualCopilot(keepPhoenixData = false) {
            console.log('👋 Stopping Visual Co-Pilot...', keepPhoenixData ? '(preserving Phoenix data)' : '');
            this.isActive = false;
            this.waitingForIntro = false;

            // Only clear session data if this is a manual/intentional stop,
            // NOT a navigation-triggered WS disconnect
            if (!keepPhoenixData) {
                Phoenix.clearAllSessionData();
                this._currentMissionGoal = null;
                this.pendingMissionGoal = null;
            }

            // Hide chat bar
            this.hideChatBar();

            // Close audio WS
            if (this.audioWs) {
                this.audioWs.close();
                this.audioWs = null;
                this.audioStreamActive = false;
                this.audioPreBuffer = [];
            }

            if (this.vad) {
                this.vad.reset();
                this.vad = null;
            }

            if (this.audioManager) {
                this.audioManager.close();
                this.audioManager = null;
            }

            if (this.micStream) {
                this.micStream.getTracks().forEach(track => track.stop());
                this.micStream = null;
            }

            if (this.micAudioCtx) {
                this.micAudioCtx.close();
                this.micAudioCtx = null;
            }

            if (this.ws) {
                this.ws.close();
                this.ws = null;
            }

            // Cleanup TaraSensor
            if (this.taraSensor) {
                this.taraSensor.stop();
                this.taraSensor = null;
            }

            this.ghostCursor?.hide();
            this.executor.clearHighlights();
            this.spotlight.classList.remove('active');

            if (this.screenOverlay) {
                this.screenOverlay.classList.remove('active', 'listening', 'talking', 'executing');
            }

            this.agentIsSpeaking = false;
            if (this.audioPlaybackTimer) {
                clearTimeout(this.audioPlaybackTimer);
                this.audioPlaybackTimer = null;
            }
            this.setOrbState('idle');
            this.updateTooltip('Click to start Visual Co-Pilot');

            console.log('✅ Visual Co-Pilot stopped');
        }

        // ─── Bridge methods for backward compatibility ───────────

        connectWebSocket() {
            return WS.connectWebSocket(this);
        }

        handleBackendMessage(msg) {
            return WS.handleBackendMessage(this, msg);
        }

        executeCommand(type, targetId, text) {
            return this.executor.executeCommand(type, targetId, text);
        }

        findElement(targetId, fallbackText) {
            return this.executor.findElement(targetId, fallbackText);
        }

        highlightElement(el) {
            return this.executor.highlightElement(el);
        }

        clearHighlights() {
            return this.executor.clearHighlights();
        }

        detectActiveStates() {
            return Scanner.detectActiveStates();
        }

        extractVisibleTables() {
            return Scanner.extractVisibleTables();
        }

        detectModal() {
            return Scanner.detectModal();
        }

        generateStableId(el) {
            return Scanner.generateStableId(el);
        }

        extractText(el) {
            return Scanner.extractText(el);
        }

        cleanText(str) {
            return Scanner.cleanText(str);
        }
    }

    // ─── Bootstrap ───────────────────────────────────────────────

    window.TaraWidget = TaraWidget;
    window.tara = null;

    function initTara() {
        if (window.tara) return;

        // Block injection into iframes
        try {
            if (window.self !== window.top) {
                console.log('🛑 [TARA] Blocked injection in iframe:', window.location.href);
                return;
            }
        } catch (e) {
            console.log('🛑 [TARA] Blocked injection in cross-origin iframe');
            return;
        }

        window.tara = new TaraWidget(window.TARA_CONFIG || {});
        console.log('🚀 [TARA] Widget initialized (modular v5)');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTara);
    } else {
        if (document.body) {
            initTara();
        } else {
            window.addEventListener('DOMContentLoaded', initTara);
        }
    }

    console.log('✅ [TARA] Core module loaded');
})();
