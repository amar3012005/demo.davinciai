/**
 * TARA Visual Co-Pilot — UI Components
 * Module: tara-ui.js
 *
 * Owns: Shadow DOM, orb, pill, chat bar, mode selector, overlay,
 *       visual state management, typing indicators, message rendering.
 * Depends on: tara-config.js, tara-styles.js
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    /**
     * Initialize all UI components on a widget instance.
     * @param {Object} widget - The TaraWidget instance
     */
    function initUI(widget) {
        const config = widget.config;
        const Constants = window.TARA.Constants;

        // ─── Shadow DOM ──────────────────────────────────────────
        widget.host = document.createElement('div');
        widget.host.id = 'tara-overlay-root';
        widget.host.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            pointer-events: none; z-index: 999999;
        `;

        widget.shadowRoot = widget.host.attachShadow({ mode: 'open' });

        widget.container = document.createElement('div');
        widget.container.id = 'tara-container';
        widget.container.style.cssText = `
            position: fixed; top: 124px; right: 24px; pointer-events: auto;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        `;

        // ─── Pill Container ──────────────────────────────────────
        widget.pillContainer = document.createElement('div');
        widget.pillContainer.className = 'tara-pill';
        widget.pillContainer.innerHTML = `
            <div class="tara-pill-content">
              <div class="tara-pill-text">
                <div class="tara-pill-title">TARA - Visual Co-pilot</div>
                <div class="tara-pill-status">Click orb to start</div>
              </div>
              <div class="tara-pill-orb-wrapper"></div>
              <button class="tara-pill-speaker" title="Mute Agent Voice">
                <svg class="speaker-on" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                  <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
                </svg>
                <svg class="speaker-off" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none;">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <line x1="23" y1="9" x2="17" y2="15"/>
                  <line x1="17" y1="9" x2="23" y2="15"/>
                </svg>
              </button>
            </div>
        `;

        widget.container.appendChild(widget.pillContainer);
        widget.shadowRoot.appendChild(widget.container);
        document.body.appendChild(widget.host);

        // ─── Inject Styles ───────────────────────────────────────
        const styleSheet = new CSSStyleSheet();
        styleSheet.replaceSync(window.TARA.Styles.getStyles(config));
        widget.shadowRoot.adoptedStyleSheets = [styleSheet];

        // ─── Orb ─────────────────────────────────────────────────
        widget.orbContainer = document.createElement('div');
        widget.orbContainer.className = 'tara-orb idle';

        const orbInner = document.createElement('div');
        orbInner.className = 'tara-orb-inner';

        const orbImg = document.createElement('img');
        const cachedUrl = window.TARA.getCachedOrbUrl();
        orbImg.src = cachedUrl || Constants.ORB_IMAGE_URL;
        orbImg.alt = 'TARA';
        orbImg.draggable = false;
        orbImg.style.cssText = 'pointer-events: none; user-select: none;';
        if (!cachedUrl) {
            orbImg.onerror = () => {
                console.warn('⚠️ Orb image failed to load — will load via WebSocket');
                orbImg.style.opacity = '0.3';
                orbImg.onerror = null;
            };
        }
        widget.orbImg = orbImg;

        orbInner.appendChild(orbImg);
        widget.orbContainer.appendChild(orbInner);

        const orbWrapper = widget.pillContainer.querySelector('.tara-pill-orb-wrapper');
        if (orbWrapper) {
            orbWrapper.appendChild(widget.orbContainer);
        } else {
            widget.container.appendChild(widget.orbContainer);
        }

        // Orb click handler
        widget.orbContainer.addEventListener('click', async () => {
            if (!widget.isActive) {
                const mode = await UI.showModeSelector(widget);
                await widget.startVisualCopilot(null, mode);
            } else {
                await widget.stopVisualCopilot();
            }
        });

        // Speaker mute button
        const speakerBtn = widget.pillContainer.querySelector('.tara-pill-speaker');
        if (speakerBtn) {
            widget.isVoiceMuted = false;
            speakerBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                widget.isVoiceMuted = !widget.isVoiceMuted;
                speakerBtn.classList.toggle('muted', widget.isVoiceMuted);
                speakerBtn.title = widget.isVoiceMuted ? 'Unmute Agent Voice' : 'Mute Agent Voice';

                const speakerOn = speakerBtn.querySelector('.speaker-on');
                const speakerOff = speakerBtn.querySelector('.speaker-off');
                if (speakerOn && speakerOff) {
                    speakerOn.style.display = widget.isVoiceMuted ? 'none' : 'block';
                    speakerOff.style.display = widget.isVoiceMuted ? 'block' : 'none';
                }

                if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                    widget.ws.send(JSON.stringify({
                        type: 'speaker_mute',
                        muted: widget.isVoiceMuted
                    }));
                    console.log(widget.isVoiceMuted ? '🔇 Agent voice muted' : '🔊 Agent voice unmuted');
                }
            });
        }

        // ─── Overlay ─────────────────────────────────────────────
        widget.screenOverlay = document.createElement('div');
        widget.screenOverlay.className = 'tara-screen-overlay';
        widget.shadowRoot.appendChild(widget.screenOverlay);

        widget.spotlight = document.createElement('div');
        widget.spotlight.className = 'tara-spotlight';
        widget.shadowRoot.appendChild(widget.spotlight);

        widget.highlightContainer = document.createElement('div');
        widget.highlightContainer.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            pointer-events: none; z-index: 99997;
        `;
        widget.shadowRoot.appendChild(widget.highlightContainer);

        // ─── Ghost Cursor ────────────────────────────────────────
        widget.ghostCursor = new window.TARA.GhostCursor(widget.shadowRoot);

        // ─── Chat Bar ────────────────────────────────────────────
        const bar = document.createElement('div');
        bar.className = 'tara-chat-bar';

        widget.chatMessages = document.createElement('div');
        widget.chatMessages.className = 'tara-chat-messages-panel';
        bar.appendChild(widget.chatMessages);

        const inputBar = document.createElement('div');
        inputBar.className = 'tara-chat-input-bar';

        widget.chatInput = document.createElement('input');
        widget.chatInput.type = 'text';
        widget.chatInput.placeholder = 'Ask TARA...';
        widget.chatInput.onkeydown = (e) => {
            if (e.key === 'Enter') UI.sendTextCommand(widget);
        };
        widget.chatInput.oninput = () => {
            const hasText = widget.chatInput.value.trim().length > 0;
            widget.sendButton.classList.toggle('has-text', hasText);
        };

        widget.modeBadge = document.createElement('span');
        widget.modeBadge.className = 'tara-mode-badge';
        widget.modeBadge.textContent = '';

        widget.micButton = document.createElement('button');
        widget.micButton.className = 'tara-chat-mic';
        widget.micButton.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`;
        widget.micButton.title = 'Voice input active';

        widget.sendButton = document.createElement('button');
        widget.sendButton.className = 'tara-chat-send-btn';
        widget.sendButton.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`;
        widget.sendButton.onclick = () => UI.sendTextCommand(widget);

        inputBar.appendChild(widget.chatInput);
        inputBar.appendChild(widget.modeBadge);
        inputBar.appendChild(widget.micButton);
        inputBar.appendChild(widget.sendButton);
        bar.appendChild(inputBar);

        widget.shadowRoot.appendChild(bar);
        widget.chatBar = bar;

        // ─── Mode Selector ───────────────────────────────────────
        const overlay = document.createElement('div');
        overlay.className = 'tara-mode-selector';

        const card = document.createElement('div');
        card.className = 'tara-mode-selector-card';

        const title = document.createElement('div');
        title.className = 'tara-mode-selector-title';
        title.textContent = 'Choose Mode';

        const subtitle = document.createElement('div');
        subtitle.className = 'tara-mode-selector-subtitle';
        subtitle.textContent = 'How should TARA assist you this session?';

        const interactiveOpt = document.createElement('div');
        interactiveOpt.className = 'tara-mode-option';
        interactiveOpt.innerHTML = `
            <div class="tara-mode-option-icon interactive">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgba(80,200,120,0.9)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
            </div>
            <div>
              <div class="tara-mode-option-label">Interactive Mode</div>
              <div class="tara-mode-option-desc">Full voice walkthrough with speech & actions</div>
            </div>
        `;

        const turboOpt = document.createElement('div');
        turboOpt.className = 'tara-mode-option';
        turboOpt.innerHTML = `
            <div class="tara-mode-option-icon turbo">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgba(242,90,41,0.9)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            </div>
            <div>
              <div class="tara-mode-option-label">Turbo Mode</div>
              <div class="tara-mode-option-desc">Quick text actions, no voice - maximum speed</div>
            </div>
        `;

        card.appendChild(title);
        card.appendChild(subtitle);
        card.appendChild(interactiveOpt);
        card.appendChild(turboOpt);
        overlay.appendChild(card);

        widget.shadowRoot.appendChild(overlay);
        widget.modeSelector = overlay;
        widget.modeSelectorInteractive = interactiveOpt;
        widget.modeSelectorTurbo = turboOpt;
    }

    // ─── UI Methods ──────────────────────────────────────────────
    const UI = {
        initUI,

        showModeSelector(widget) {
            return new Promise((resolve) => {
                widget.modeSelector.style.display = 'flex';
                requestAnimationFrame(() => {
                    widget.modeSelector.classList.add('visible');
                });

                const handleChoice = (mode) => {
                    widget.modeSelectorInteractive.onclick = null;
                    widget.modeSelectorTurbo.onclick = null;
                    UI.hideModeSelector(widget);
                    resolve(mode);
                };

                widget.modeSelectorInteractive.onclick = () => handleChoice('interactive');
                widget.modeSelectorTurbo.onclick = () => handleChoice('turbo');
            });
        },

        hideModeSelector(widget) {
            widget.modeSelector.classList.remove('visible');
            setTimeout(() => {
                widget.modeSelector.style.display = 'none';
            }, 300);
        },

        showChatBar(widget) {
            if (!widget.chatBar) return;
            widget.chatBar.style.display = 'flex';
            widget.chatBar.style.transform = 'translateX(-50%) translateY(20px)';
            requestAnimationFrame(() => {
                widget.chatBar.classList.add('visible');
            });
        },

        hideChatBar(widget) {
            if (!widget.chatBar) return;
            widget.chatBar.classList.remove('visible');
            setTimeout(() => {
                widget.chatBar.style.display = 'none';
                if (widget.chatMessages) {
                    widget.chatMessages.innerHTML = '';
                    widget.chatMessages.classList.remove('has-messages');
                }
            }, 400);
        },

        showTypingIndicator(widget) {
            if (!widget.chatMessages) return;
            UI.hideTypingIndicator(widget);
            const indicator = document.createElement('div');
            indicator.className = 'tara-typing-indicator';
            indicator.id = 'tara-typing';
            indicator.innerHTML = '<div class="tara-typing-dot"></div><div class="tara-typing-dot"></div><div class="tara-typing-dot"></div>';
            widget.chatMessages.appendChild(indicator);
            widget.chatMessages.scrollTop = widget.chatMessages.scrollHeight;
            widget.chatMessages.classList.add('has-messages');
        },

        hideTypingIndicator(widget) {
            const existing = widget.chatMessages?.querySelector('#tara-typing');
            if (existing) existing.remove();
        },

        sendTextCommand(widget) {
            const text = widget.chatInput.value.trim();
            if (!text) return;

            widget._currentMissionGoal = text;
            UI.appendChatMessage(widget, text, 'user');
            widget.chatInput.value = '';
            widget.sendButton.classList.remove('has-text');
            UI.showTypingIndicator(widget);

            if (widget.ws && widget.ws.readyState === WebSocket.OPEN) {
                widget.ws.send(JSON.stringify({
                    type: 'text_input',
                    text: text,
                    mode: widget.sessionMode
                }));
            }
        },

        async simulateTyping(widget, fullText) {
            if (!fullText) return;

            const lastMsg = widget.chatMessages.lastElementChild;
            if (!lastMsg || lastMsg.dataset.sender !== 'ai' || lastMsg.dataset.streaming !== 'true') {
                UI.appendChatMessage(widget, '', 'ai', true);
            }

            if (widget.sessionMode === 'turbo') {
                UI.appendChatMessage(widget, fullText + ' ', 'ai', true);
                return;
            }

            const chunks = fullText.split(' ');
            for (const chunk of chunks) {
                UI.appendChatMessage(widget, chunk + ' ', 'ai', true);
                await new Promise(r => setTimeout(r, 15 + Math.random() * 25));
            }
        },

        appendChatMessage(widget, text, sender, isStreaming = false) {
            if (!widget.chatMessages) return;

            if (sender === 'ai') UI.hideTypingIndicator(widget);

            let msgEl;
            const lastMsg = widget.chatMessages.lastElementChild;
            if (isStreaming && lastMsg && lastMsg.dataset.sender === 'ai' && lastMsg.dataset.streaming === 'true') {
                msgEl = lastMsg;
                msgEl.querySelector('.content').textContent += text;
            } else {
                msgEl = document.createElement('div');
                msgEl.className = `tara-msg ${sender}`;
                msgEl.dataset.sender = sender;
                if (isStreaming) msgEl.dataset.streaming = 'true';
                msgEl.innerHTML = `<div class="content">${text}</div>`;
                widget.chatMessages.appendChild(msgEl);
            }

            widget.chatMessages.classList.add('has-messages');
            widget.chatMessages.scrollTop = widget.chatMessages.scrollHeight;
        },

        setOrbState(widget, state) {
            let displayState = state;
            if (state === 'active' || state === 'thinking') displayState = 'listening';
            if (!displayState) displayState = 'idle';

            if (widget.sessionMode === 'turbo') {
                if (displayState === 'listening') displayState = 'idle';
                if (displayState === 'talking') return;
            }

            widget.orbContainer.classList.remove('listening', 'talking', 'idle', 'executing');
            widget.orbContainer.classList.add(displayState);

            if (widget.pillContainer) {
                widget.pillContainer.classList.remove('listening', 'talking', 'idle', 'executing');
                widget.pillContainer.classList.add(displayState);

                const statusEl = widget.pillContainer.querySelector('.tara-pill-status');
                if (statusEl) {
                    const statusTexts = widget.sessionMode === 'turbo' ? {
                        'idle': 'Ready',
                        'listening': 'Processing...',
                        'talking': 'Processing...',
                        'executing': 'Executing action...'
                    } : {
                        'idle': 'Click orb to start',
                        'listening': 'Listening...',
                        'talking': 'Speaking...',
                        'executing': 'Executing action...'
                    };
                    statusEl.textContent = statusTexts[displayState] || 'Ready';
                }
            }

            if (widget.micButton && widget.sessionMode === 'interactive') {
                widget.micButton.classList.toggle('active', displayState === 'listening');
            }

            if (displayState !== 'listening') {
                widget.orbContainer.style.transform = '';
            }

            // VAD mic lock
            if (widget.vad) {
                const shouldLock = (displayState !== 'listening' && displayState !== 'talking');
                if (widget.vad.locked !== shouldLock) {
                    widget.vad.locked = shouldLock;
                    if (shouldLock) {
                        widget.vad.reset();
                        console.log(`🔒 Mic LOCKED (State: ${displayState})`);
                    } else {
                        console.log(`🔓 Mic UNLOCKED (State: ${displayState})`);
                    }
                }
            }

            console.log(`🎨 Orb state: ${displayState}`);
        },

        updateOrbVolume(widget, volume) {
            if (widget.orbContainer && widget.orbContainer.classList.contains('listening')) {
                const scale = 1 + (volume * 0.12);
                widget.orbContainer.style.transform = `scale(${scale})`;
            }
        },

        updateTooltip(widget, text) {
            if (widget.tooltip) {
                widget.tooltip.textContent = text;
            }
        }
    };

    window.TARA.UI = UI;
    console.log('✅ [TARA] UI module loaded');
})();
