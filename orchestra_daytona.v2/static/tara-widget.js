/**
 * TARA Visual Co-Pilot - Overlay Widget v3.0
 * Hetzner Cloud Production Build
 * WebSocket: wss://demo.davinciai.eu:8443/ws
 * 
 * Flow:
 * 1. User clicks small orb (top-right)
 * 2. Send session_config with mode: visual-copilot
 * 3. Backend plays intro audio
 * 4. Widget starts mic and DOM collection
 * 5. User speaks -> DOM sent immediately + audio streamed
 * 6. User stops speaking -> Wait for command
 * 7. Backend sends command -> Widget executes -> Send execution_complete
 * 8. AI unlocked for next command
 */

(function () {
  'use strict';

  // ============================================
  // PARAMETERS & DEFAULTS
  // ============================================
  const DEFAULTS = {
    wsUrl: 'wss://demo.davinciai.eu:8443/ws', // Default Production URL
    orbSize: 30,
    position: 'bottom-right', // 'bottom-right', 'bottom-left'
    colors: {
      core: '#CADCFC',
      accent: '#A0B9D1',
      glow: 'rgba(202, 220, 252, 0.6)',
      highlight: '#FFD700',
      dim: 'rgba(0, 0, 0, 0.65)'
    },
    audio: {
      inputSampleRate: 16000,
      outputSampleRate: 44100, // HD Quality
      bufferSize: 4096 // Larger buffer for smoother mic capture
    },
    vad: {
      energyThreshold: 0.015, // Slightly more sensitive
      silenceThreshold: 0.01,
      minSpeechDuration: 200, // Faster response
      silenceTimeout: 800
    }
  };

  // Merge User Config if available
  const TARA_CONFIG = {
    ...DEFAULTS,
    ...(window.TARA_CONFIG || {})
  };

  // ============================================
  // VOICE ACTIVITY DETECTOR (VAD)
  // ============================================
  class VoiceActivityDetector {
    constructor(onSpeechStart, onSpeechEnd) {
      this.onSpeechStart = onSpeechStart;
      this.onSpeechEnd = onSpeechEnd;
      this.isSpeaking = false;
      this.energyThreshold = TARA_CONFIG.vad.energyThreshold;
      this.silenceThreshold = TARA_CONFIG.vad.silenceThreshold;
      this.minSpeechDuration = TARA_CONFIG.vad.minSpeechDuration;
      this.silenceTimeout = null;
      this.speechStartTime = null;
      this.totalEnergy = 0;
      this.sampleCount = 0;
    }

    processAudioChunk(float32Array) {
      let sum = 0;
      for (let i = 0; i < float32Array.length; i++) {
        sum += float32Array[i] * float32Array[i];
      }
      const rms = Math.sqrt(sum / float32Array.length);

      this.totalEnergy += rms;
      this.sampleCount++;

      if (!this.isSpeaking && rms > this.energyThreshold) {
        this.isSpeaking = true;
        this.speechStartTime = Date.now();
        this.onSpeechStart();
      }

      if (this.isSpeaking && rms < this.silenceThreshold) {
        if (!this.silenceTimeout) {
          this.silenceTimeout = setTimeout(() => {
            const speechDuration = Date.now() - this.speechStartTime;
            if (speechDuration >= this.minSpeechDuration) {
              this.isSpeaking = false;
              this.onSpeechEnd();
            }
            this.silenceTimeout = null;
          }, TARA_CONFIG.vad.silenceTimeout);
        }
      } else if (this.isSpeaking && rms > this.silenceThreshold) {
        if (this.silenceTimeout) {
          clearTimeout(this.silenceTimeout);
          this.silenceTimeout = null;
        }
      }

      return rms;
    }

    reset() {
      this.isSpeaking = false;
      this.totalEnergy = 0;
      this.sampleCount = 0;
      if (this.silenceTimeout) {
        clearTimeout(this.silenceTimeout);
        this.silenceTimeout = null;
      }
    }
  }

  // ============================================
  // GHOST CURSOR
  // ============================================
  class GhostCursor {
    constructor(shadowRoot) {
      this.cursor = document.createElement('div');
      this.cursor.className = 'tara-ghost-cursor';
      this.cursor.innerHTML = `
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
          <path d="M5.5 3.21V20.8c0 .45.54.67.85.35l4.86-4.86a.5.5 0 0 1 .35-.15h6.87c.44 0 .66-.53.35-.85L6.35 2.86a.5.5 0 0 0-.85.35z" 
                fill="white" stroke="#333" stroke-width="1.5"/>
        </svg>
      `;
      this.currentX = window.innerWidth / 2;
      this.currentY = window.innerHeight / 2;

      shadowRoot.appendChild(this.cursor);
      this.hide();
    }

    async moveTo(element, duration = 500) {
      const rect = element.getBoundingClientRect();
      const targetX = rect.left + rect.width / 2 - 12;
      const targetY = rect.top + rect.height / 2 - 12;

      this.show();

      const startX = this.currentX;
      const startY = this.currentY;
      const startTime = performance.now();

      return new Promise((resolve) => {
        const animate = (currentTime) => {
          const elapsed = currentTime - startTime;
          const progress = Math.min(elapsed / duration, 1);
          const easeOut = 1 - Math.pow(1 - progress, 3);

          this.currentX = startX + (targetX - startX) * easeOut;
          this.currentY = startY + (targetY - startY) * easeOut;

          this.cursor.style.transform = `translate(${this.currentX}px, ${this.currentY}px)`;

          if (progress < 1) {
            requestAnimationFrame(animate);
          } else {
            resolve();
          }
        };

        requestAnimationFrame(animate);
      });
    }

    async click() {
      const originalTransform = this.cursor.style.transform;
      this.cursor.style.transform = `${originalTransform} scale(0.8)`;
      await new Promise(r => setTimeout(r, 150));
      this.cursor.style.transform = originalTransform;
    }

    show() {
      this.cursor.style.opacity = '1';
    }

    hide() {
      this.cursor.style.opacity = '0';
    }
  }

  // ============================================
  // AUDIO MANAGER (Robust Version)
  // ============================================
  class AudioManager {
    constructor() {
      this.audioCtx = null;
      this.isInitialized = false;
      this.nextPlayTime = 0;
      this.isPlaying = false;
      this.activeSources = new Set();
      this.sampleRate = 44100;
    }

    async initialize(callbacks = {}) {
      if (this.isInitialized) return;
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      this.onStart = callbacks.onStart;
      this.onEnd = callbacks.onEnd;
      this.isInitialized = true;
      this.nextPlayTime = this.audioCtx.currentTime;
      console.log('🔊 Audio Manager (Robust Mode) Initialized');
    }

    async playChunk(rawBuffer, format = 'pcm_s16le', sampleRate = 44100) {
      if (!this.isInitialized) await this.initialize();
      if (this.audioCtx.state === 'suspended') await this.audioCtx.resume();

      let float32Data;
      try {
        if (format === 'pcm_s16le') {
          const int16Data = new Int16Array(rawBuffer);
          float32Data = new Float32Array(int16Data.length);
          for (let i = 0; i < int16Data.length; i++) {
            float32Data[i] = int16Data[i] / 32768.0;
          }
        } else {
          // Assume pcm_f32le
          float32Data = new Float32Array(rawBuffer);
        }

        const buffer = this.audioCtx.createBuffer(1, float32Data.length, sampleRate || this.sampleRate);
        buffer.getChannelData(0).set(float32Data);

        const source = this.audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(this.audioCtx.destination);

        const now = this.audioCtx.currentTime;
        if (!this.isPlaying) {
          this.isPlaying = true;
          this.nextPlayTime = now + 0.02; // Reduced initial buffer for lower latency
          if (this.onStart) this.onStart();
        }

        // Schedule playback back-to-back (Gapless Stitching like Cartesia Client)
        const startAt = Math.max(now, this.nextPlayTime);
        source.start(startAt);
        this.nextPlayTime = startAt + buffer.duration;

        this.activeSources.add(source);
        source.onended = () => {
          this.activeSources.delete(source);
          if (this.activeSources.size === 0) {
            this.isPlaying = false;
            if (this.onEnd) this.onEnd();
          }
        };
      } catch (err) {
        console.error('❌ Audio play error:', err);
      }
    }

    interrupt() {
      if (!this.audioCtx) return;
      this.activeSources.forEach(s => {
        try { s.stop(); } catch (e) { }
      });
      this.activeSources.clear();
      this.nextPlayTime = 0;
      this.isPlaying = false;
      if (this.onEnd) this.onEnd();
    }

    close() {
      if (this.audioCtx) {
        this.audioCtx.close();
        this.audioCtx = null;
      }
      this.isInitialized = false;
    }
  }

  // ============================================
  // MAIN TARA WIDGET CLASS
  // ============================================
  class TaraWidget {
    constructor(config = {}) {
      this.config = { ...TARA_CONFIG, ...config };
      this.isActive = false;
      this.ws = null;
      this.vad = null;
      this.audioManager = null;
      this.ghostCursor = null;
      this.micStream = null;
      this.micAudioCtx = null;
      this.agentIsSpeaking = false;
      this.audioPlaybackTimer = null;
      this.lastAudioChunkTime = 0;
      this.lastChunkMetadata = null; // Store metadata for correlation
      this.agentState = null;
      this.domSnapshotPending = false;
      this.waitingForExecution = false;
      this.waitingForIntro = false;
      this.audioFormat = 'pcm_f32le'; // Default to Float32
      this.lastDOMHash = null;
      this.binaryQueue = []; // Queue for back-to-back binary chunks
      this.chunksSent = 0;

      this.init();
    }

    init() {
      this.createShadowDOM();
      this.injectStyles();
      this.createOrb();
      this.createOverlay();
      this.createChatUI(); // New Chat UI
      this.createGhostCursor();

      console.log('✨ TARA: Visual Co-Pilot initialized (Hetzner Cloud)');
      console.log('🔗 WebSocket:', this.config.wsUrl);

      // Auto-reconnect if navigated
      const savedSession = sessionStorage.getItem('tara_session_id');
      const savedMode = sessionStorage.getItem('tara_mode');
      if (savedSession && savedMode === 'visual-copilot') {
        console.log('🔄 Restoring Visual Co-Pilot session:', savedSession);
        this.startVisualCopilot(savedSession);
      }
    }

    createShadowDOM() {
      this.host = document.createElement('div');
      this.host.id = 'tara-overlay-root';
      this.host.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        z-index: 999999;
      `;

      this.shadowRoot = this.host.attachShadow({ mode: 'open' });

      this.container = document.createElement('div');
      this.container.id = 'tara-container';
      this.container.style.cssText = `
        position: fixed;
        top: 24px;
        right: 24px;
        pointer-events: auto;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      `;

      this.shadowRoot.appendChild(this.container);
      document.body.appendChild(this.host);
    }

    injectStyles() {
      const styleSheet = new CSSStyleSheet();
      styleSheet.replaceSync(`
        :host { all: initial; }
        #tara-container { isolation: isolate; }
        
        /* === ELEVENLABS STYLE ORB === */
        .tara-orb {
          width: ${this.config.orbSize}px;
          height: ${this.config.orbSize}px;
          border-radius: 50%;
          cursor: pointer;
          position: relative;
          transition: all 0.5s cubic-bezier(0.25, 1, 0.5, 1);
          
          /* Fluid Gradient Base */
          background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.8), rgba(255,255,255,0) 20%),
                      radial-gradient(circle at 70% 70%, ${this.config.colors.accent}, rgba(0,0,0,0) 60%),
                      conic-gradient(from 0deg, ${this.config.colors.core}, ${this.config.colors.accent}, ${this.config.colors.core});
          background-size: 200% 200%;
          border: 1px solid rgba(255,255,255,0.2);
          
          /* Glass/Glow Effect */
          box-shadow: 
            inset 0 0 20px rgba(255,255,255,0.2),
            0 0 15px rgba(0,0,0,0.2),
            0 8px 32px rgba(0,0,0,0.3);
            
          /* Continuous rotation for "Liquid" feel */
          animation: tara-liquid-idle 8s linear infinite;
        }

        .tara-orb::after {
          content: '';
          position: absolute;
          inset: -4px;
          border-radius: 50%;
          background: transparent;
          box-shadow: 0 0 15px ${this.config.colors.glow};
          opacity: 0;
          transition: opacity 0.3s ease;
          pointer-events: none;
        }

        .tara-orb:hover {
          transform: scale(1.05);
          box-shadow: 0 0 25px ${this.config.colors.glow}, 0 12px 40px rgba(0,0,0,0.4);
        }

        /* === STATES === */

        /* IDLE: Dim, slow breathe */
        .tara-orb.idle {
          filter: grayscale(0.5) brightness(0.8);
          animation: tara-liquid-idle 12s linear infinite;
        }

        /* LISTENING: Active, "Microphone" feel, Pulse */
        .tara-orb.listening {
          filter: none;
          box-shadow: 0 0 30px ${this.config.colors.glow}, 0 0 60px ${this.config.colors.accent};
          animation: tara-pulse-listening 1.5s ease-in-out infinite;
        }
        .tara-orb.listening::after {
          opacity: 0.6;
        }

        /* THINKING: Fast spin, processing color */
        .tara-orb.thinking {
          filter: brightness(1.2);
          animation: tara-spin-thinking 1s linear infinite, tara-color-shift 3s ease-in-out infinite alternate;
        }

        /* TALKING: Sound wave vibration simulation */
        .tara-orb.talking {
          filter: none;
          animation: tara-liquid-talking 4s linear infinite; /* Keeps moving */
          /* Note: Scale is handled by JS Audio Analyser for reactivity */
          box-shadow: 
            0 0 40px ${this.config.colors.glow},
            0 0 80px rgba(255,255,255,0.4);
        }

        /* === KEYFRAMES === */

        @keyframes tara-liquid-idle {
          0% { background-position: 0% 50%; transform: rotate(0deg); }
          50% { background-position: 100% 50%; }
          100% { background-position: 0% 50%; transform: rotate(360deg); }
        }

        @keyframes tara-liquid-talking {
          0% { background-position: 0% 50%; transform: rotate(0deg) scale(1.05); }
          50% { background-position: 100% 50%; transform: rotate(180deg) scale(1.15); }
          100% { background-position: 0% 50%; transform: rotate(360deg) scale(1.05); }
        }

        @keyframes tara-pulse-listening {
          0% { transform: scale(1); box-shadow: 0 0 20px ${this.config.colors.glow}; }
          50% { transform: scale(1.15); box-shadow: 0 0 50px ${this.config.colors.glow}, 0 0 10px white; }
          100% { transform: scale(1); box-shadow: 0 0 20px ${this.config.colors.glow}; }
        }

        @keyframes tara-spin-thinking {
          0% { transform: rotate(0deg) scale(0.95); }
          100% { transform: rotate(360deg) scale(0.95); }
        }

        @keyframes tara-color-shift {
          0% { filter: hue-rotate(0deg) brightness(1.2); }
          100% { filter: hue-rotate(90deg) brightness(1.4); }
        }

        .tara-ghost-cursor {
          position: fixed;
          width: 24px;
          height: 24px;
          pointer-events: none;
          z-index: 100000;
          opacity: 0;
          transition: opacity 0.3s ease;
        }
        
        .tara-spotlight {
          position: fixed;
          inset: 0;
          background: ${this.config.colors.dim};
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.5s ease;
          z-index: 99998;
        }
        
        .tara-spotlight.active { opacity: 1; }
        
        .tara-highlight {
          position: fixed;
          border: 3px solid ${this.config.colors.highlight};
          border-radius: 8px;
          box-shadow: 0 0 30px ${this.config.colors.highlight}, inset 0 0 30px rgba(255, 215, 0, 0.3);
          pointer-events: none;
          z-index: 99997;
          animation: tara-highlight-pulse 1s ease-in-out infinite;
        }

        @keyframes tara-highlight-pulse {
          0%, 100% { box-shadow: 0 0 20px ${this.config.colors.highlight}; }
          50% { box-shadow: 0 0 40px ${this.config.colors.highlight}, 0 0 80px rgba(255, 215, 0, 0.6); }
        }

        .tara-chat-toggle {
            position: fixed;
            bottom: 30px;
            right: 120px;
            width: 40px;
            height: 40px;
            background: rgba(30, 30, 30, 0.9);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 50%;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            z-index: 100001;
            font-size: 18px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            transition: all 0.2s ease;
        }
        .tara-chat-toggle:hover {
            transform: scale(1.1);
            background: rgba(40, 40, 40, 0.95);
        }
      `);

      this.shadowRoot.adoptedStyleSheets = [styleSheet];
    }

    createOrb() {
      this.orbContainer = document.createElement('div');
      this.orbContainer.className = 'tara-orb';
      // Tooltip removed per "Orb Only" requirement

      this.container.appendChild(this.orbContainer);

      this.orbContainer.addEventListener('click', async () => {
        if (!this.isActive) {
          await this.startVisualCopilot();
        } else {
          await this.stopVisualCopilot();
        }
      });

      // --- CHAT TOGGLE BUTTON ---
      const chatToggle = document.createElement('div');
      chatToggle.className = 'tara-chat-toggle';
      chatToggle.innerHTML = '💬'; // Or a keyboard icon
      chatToggle.title = "Open Chat / Debug Mode";
      chatToggle.onclick = (e) => {
        e.stopPropagation();
        this.toggleChat();
      };
      // Inject into shadow DOM
      if (this.shadowRoot) {
        this.shadowRoot.appendChild(chatToggle);
      } else {
        document.body.appendChild(chatToggle);
      }
    }

    updateTooltip(text) {
      if (this.tooltip) {
        this.tooltip.textContent = text;
      }
    }

    createOverlay() {
      this.spotlight = document.createElement('div');
      this.spotlight.className = 'tara-spotlight';
      this.shadowRoot.appendChild(this.spotlight);

      this.highlightContainer = document.createElement('div');
      this.highlightContainer.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        z-index: 99997;
      `;
      this.shadowRoot.appendChild(this.highlightContainer);
    }

    createGhostCursor() {
      this.ghostCursor = new GhostCursor(this.shadowRoot);
    }

    // ============================================
    // VISUAL CO-PILOT MODE
    // ============================================
    // --- CHAT UI ---
    createChatUI() {
      const container = document.createElement('div');
      container.className = 'tara-chat-container';
      container.style.cssText = `
          position: fixed;
          bottom: 90px;
          right: 30px;
          width: 350px;
          height: 500px;
          background: rgba(20, 20, 20, 0.95);
          border: 1px solid rgba(255, 255, 255, 0.15);
          border-radius: 12px;
          display: none;
          flex-direction: column;
          z-index: 100002;
          box-shadow: 0 10px 30px rgba(0,0,0,0.5);
          backdrop-filter: blur(10px);
          font-family: 'Inter', sans-serif;
          overflow: hidden;
          transition: opacity 0.2s ease, transform 0.2s ease;
          opacity: 0;
          transform: translateY(10px);
        `;

      // Header
      const header = document.createElement('div');
      header.style.cssText = `
          padding: 12px 16px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.1);
          display: flex;
          justify-content: space-between;\n          align-items: center;
          background: rgba(255, 255, 255, 0.03);
        `;
      header.innerHTML = `<span style="color: #fff; font-weight: 600; font-size: 14px;">TARA Chat / Debug</span>`;
      const closeBtn = document.createElement('button');
      closeBtn.innerHTML = '×';
      closeBtn.style.cssText = `background:none; border:none; color: #aaa; font-size: 20px; cursor: pointer;`;
      closeBtn.onclick = () => this.toggleChat(false);
      header.appendChild(closeBtn);
      container.appendChild(header);

      // Messages Area
      this.chatMessages = document.createElement('div');
      this.chatMessages.className = 'tara-messages';
      this.chatMessages.style.cssText = `
          flex: 1;
          padding: 16px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
          gap: 12px;
          font-size: 13px;
          line-height: 1.5;
        `;
      container.appendChild(this.chatMessages);

      // Input Area
      const inputArea = document.createElement('div');
      inputArea.style.cssText = `
          padding: 12px;
          border-top: 1px solid rgba(255, 255, 255, 0.1);
          display: flex;
          gap: 8px;
          background: rgba(0, 0, 0, 0.2);
        `;

      this.chatInput = document.createElement('input');
      this.chatInput.type = 'text';
      this.chatInput.placeholder = 'Type a command...';
      this.chatInput.style.cssText = `
          flex: 1;
          background: rgba(255, 255, 255, 0.1);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 6px;
          padding: 8px 12px;
          color: white;
          outline: none;
          font-size: 13px;
        `;
      this.chatInput.onkeydown = (e) => {
        if (e.key === 'Enter') this.sendTextCommand();
      };

      const sendBtn = document.createElement('button');
      sendBtn.innerHTML = '➤';
      sendBtn.style.cssText = `
          background: #f25a29;
          color: white;
          border: none;
          border-radius: 6px;
          width: 36px;
          cursor: pointer;
          font-size: 14px;
        `;
      sendBtn.onclick = () => this.sendTextCommand();

      inputArea.appendChild(this.chatInput);
      inputArea.appendChild(sendBtn);
      container.appendChild(inputArea);

      if (this.shadowRoot) {
        this.shadowRoot.appendChild(container);
      } else {
        document.body.appendChild(container);
      }
      this.chatContainer = container;
    }

    toggleChat(force = null) {
      if (!this.chatContainer) return;
      const isHidden = this.chatContainer.style.display === 'none';
      const shouldShow = force !== null ? force : isHidden;

      if (shouldShow) {
        this.chatContainer.style.display = 'flex';
        // Trigger reflow
        this.chatContainer.offsetHeight;
        this.chatContainer.style.opacity = '1';
        this.chatContainer.style.transform = 'translateY(0)';
        this.chatInput.focus();
        // Disable mic if chat is open? Optional. Keeping both allows multimodal.
      } else {
        this.chatContainer.style.opacity = '0';
        this.chatContainer.style.transform = 'translateY(10px)';
        setTimeout(() => {
          this.chatContainer.style.display = 'none';
        }, 200);
      }
    }

    sendTextCommand() {
      const text = this.chatInput.value.trim();
      if (!text) return;

      this.appendChatMessage(text, 'user');
      this.chatInput.value = '';

      // Send to Backend
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          type: 'text_input',
          text: text
        }));
      }
    }

    appendChatMessage(text, sender, isStreaming = false) {
      if (!this.chatMessages) return;

      let msgEl;
      // Basic Streaming Support (Append to last AI message if streaming)
      const lastMsg = this.chatMessages.lastElementChild;
      if (isStreaming && lastMsg && lastMsg.dataset.sender === 'ai' && lastMsg.dataset.streaming === 'true') {
        msgEl = lastMsg;
        msgEl.querySelector('.content').textContent += text;
      } else {
        msgEl = document.createElement('div');
        msgEl.dataset.sender = sender;
        if (isStreaming) msgEl.dataset.streaming = 'true';

        msgEl.style.cssText = `
              align-self: ${sender === 'user' ? 'flex-end' : 'flex-start'};
              max-width: 85%;
              padding: 8px 12px;
              border-radius: 8px;
              background: ${sender === 'user' ? '#f25a29' : 'rgba(255, 255, 255, 0.1)'};
              color: white;
            `;
        msgEl.innerHTML = `<div class="content">${text}</div>`;
        this.chatMessages.appendChild(msgEl);
      }
      this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
    }

    async startVisualCopilot(resumeSessionId = null) {
      try {
        console.log('🎯 ============================================');
        console.log(`🎯 ${resumeSessionId ? 'RESUMING' : 'STARTING'} VISUAL CO-PILOT MODE`);
        console.log('🎯 ============================================');

        // 1. Initialize audio manager
        await this.initializeAudioManager();

        // 2. Start Microphone (Crucial to do this early for user gesture context)
        await this.startMicrophoneAndCollection();

        // 3. Connect WebSocket
        await this.connectWebSocket();

        // 4. Send session_config
        const sessionConfig = {
          type: 'session_config',
          mode: 'visual-copilot',
          timestamp: Date.now(),
          session_id: resumeSessionId,
          current_url: window.location.pathname
        };

        // Persist mode for auto-resume across navigation
        sessionStorage.setItem('tara_mode', 'visual-copilot');

        console.log('📤 Sending session_config:', JSON.stringify(sessionConfig));
        this.ws.send(JSON.stringify(sessionConfig));

        // 5. Send DOM Blueprint
        console.log('🔍 Scanning page blueprint...');
        if (resumeSessionId) await new Promise(r => setTimeout(r, 1000));
        const blueprint = this.scanPageBlueprint(true);
        if (blueprint) {
          this.ws.send(JSON.stringify({
            type: 'dom_update',
            elements: blueprint
          }));
          console.log('✅ dom_update sent successfully');
        }

        this.isActive = true;
        this.setOrbState('listening'); // Default to listening
        this.updateTooltip('Click to end Visual Co-Pilot');

      } catch (err) {
        console.error('❌ Failed to start Visual Co-Pilot:', err);
        // alert('Failed to connect to TARA Orchestrator. Please check your connection.');
      }
    }

    async initializeAudioManager() {
      this.audioManager = new AudioManager();
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
        const MIC_SAMPLE_RATE = 16000; // Must match VAD/STT expectation

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
            console.warn("Microphone blocked by policy. Falling back to Text Mode.");
            // Alert user but DO NOT throw, so we can continue in Text Mode
            alert("Microphone access blocked (Permissions Policy). Falling back to TEXT MODE. use the keyboard to interact.");
            return false;
          }
          // For other errors, we might still want to continue?
          console.warn("Microphone failed (unknown reason). Falling back to Text Mode.");
          return false;
        }

        // VAD
        this.vad = new VoiceActivityDetector(
          () => {
            console.log("🗣️ User started speaking [VAD]");
            // Interrupt AI if speaking
            if (this.agentIsSpeaking && this.audioManager) {
              console.log("🛑 Interrupting AI playback...");
              this.audioManager.interrupt();
            }
            this.onSpeechStart();
          },
          () => this.onSpeechEnd()
        );

        this.micAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
          sampleRate: MIC_SAMPLE_RATE
        });

        if (this.micAudioCtx.state === 'suspended') {
          await this.micAudioCtx.resume();
        }

        const source = this.micAudioCtx.createMediaStreamSource(this.micStream);
        // Worklet or ScriptProcessor (ScriptProcessor is easier single-file)
        const processor = this.micAudioCtx.createScriptProcessor(2048, 1, 1);

        source.connect(processor);
        processor.connect(this.micAudioCtx.destination);

        processor.onaudioprocess = (e) => {
          if (!this.isActive || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;

          const inputData = e.inputBuffer.getChannelData(0);

          // 1. VAD Processing
          this.vad.processAudioChunk(inputData);

          // 2. Volume Visualizer (RMS)
          let sum = 0;
          for (let i = 0; i < inputData.length; i++) {
            sum += inputData[i] * inputData[i];
          }
          const rms = Math.sqrt(sum / inputData.length);
          this.updateOrbVolume(rms * 5); // Boost gain for visual

          // 3. Send Audio (NO GATE - always send to STT for barge-in)
          // Convert Float32 -> Int16 for backend
          const pcmData = new Int16Array(inputData.length);
          for (let i = 0; i < inputData.length; i++) {
            // Clamp and scale
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
        // alert('Please allow microphone access to use Visual Co-Pilot');
      }
    }

    async stopVisualCopilot() {
      console.log('👋 Stopping Visual Co-Pilot...');

      this.isActive = false;
      this.waitingForIntro = false;

      // Clear persistence
      sessionStorage.removeItem('tara_mode');
      sessionStorage.removeItem('tara_session_id');

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

      this.ghostCursor?.hide();
      this.clearHighlights();
      this.spotlight.classList.remove('active');

      // Reset agent speaking state and orb
      this.agentIsSpeaking = false;
      if (this.audioPlaybackTimer) {
        clearTimeout(this.audioPlaybackTimer);
        this.audioPlaybackTimer = null;
      }
      this.setOrbState('idle');
      this.updateTooltip('Click to start Visual Co-Pilot');

      console.log('✅ Visual Co-Pilot stopped');
    }

    connectWebSocket() {
      return new Promise((resolve, reject) => {
        console.log('🔌 Connecting to WebSocket:', this.config.wsUrl);

        this.ws = new WebSocket(this.config.wsUrl);
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
          console.log('✅ WebSocket connected');
          resolve();
        };

        this.ws.onmessage = (e) => {
          if (e.data instanceof ArrayBuffer) {
            // Binary audio frame arrived. Queue it.
            this.binaryQueue.push(e.data);
          } else {
            // JSON message
            let data;
            try {
              data = JSON.parse(e.data);
            } catch (err) {
              console.error('❌ JSON Parse Error:', err, e.data);
              return;
            }

            if (data.type === 'audio_chunk') {
              // 1. Check for binary frame in queue
              if (data.binary_sent && this.binaryQueue.length > 0) {
                const chunk = this.binaryQueue.shift();
                if (this.audioManager) {
                  // Default to f32le for HD audio consistency
                  this.audioManager.playChunk(chunk, data.format || 'pcm_f32le', data.sample_rate || 44100);
                }
              }
              // 2. Fallback: Handle embedded Base64 audio in JSON
              else if (data.data || data.audio) {
                const b64 = data.data || data.audio;
                const binaryString = atob(b64);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                  bytes[i] = binaryString.charCodeAt(i);
                }
                if (this.audioManager) {
                  this.audioManager.playChunk(bytes.buffer, data.format || 'pcm_f32le', data.sample_rate || 44100);
                }
              }
            }

            this.handleBackendMessage(data);
          }
        };

        this.ws.onclose = () => {
          console.log('🔌 WebSocket closed');
          if (this.isActive) this.stopVisualCopilot();
        };

        this.ws.onerror = (err) => {
          console.error('❌ WebSocket error:', err);
          reject(err);
        };
      });
    }

    startAudioProcessing() {
      this.micAudioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: this.config.audio.inputSampleRate
      });

      // Create audio analysis for visual feedback (like AIAssistantPanel)
      const analyser = this.micAudioCtx.createAnalyser();
      analyser.fftSize = 256;

      const source = this.micAudioCtx.createMediaStreamSource(this.micStream);
      source.connect(analyser);

      const processor = this.micAudioCtx.createScriptProcessor(
        this.config.audio.bufferSize, 1, 1
      );

      processor.onaudioprocess = (e) => {
        if (!this.isActive) return;

        const inputData = e.inputBuffer.getChannelData(0);

        // Get volume for visual feedback
        const dataArray = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(dataArray);
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
          sum += dataArray[i];
        }
        const volume = Math.min(1, (sum / dataArray.length) / 30);
        this.updateOrbVolume(volume);

        this.vad?.processAudioChunk(inputData);

        // Gate audio: Do not send if agent is speaking (prevents echo/feedback loop)
        if (this.ws && this.ws.readyState === WebSocket.OPEN && !this.agentIsSpeaking) {
          const pcmData = new Int16Array(inputData.length);
          for (let i = 0; i < inputData.length; i++) {
            pcmData[i] = Math.max(-1, Math.min(1, inputData[i])) * 0x7FFF;
          }
          this.ws.send(pcmData.buffer);
        }
      };

      source.connect(processor);
      processor.connect(this.micAudioCtx.destination);
    }

    updateOrbVolume(volume) {
      // Visual feedback: scale orb based on volume when listening
      if (this.orbContainer && this.orbContainer.classList.contains('listening')) {
        const scale = 1 + (volume * 0.2); // Scale between 1.0 and 1.2
        const glowSize = 20 + (volume * 40); // Glow between 20px and 60px
        this.orbContainer.style.transform = `scale(${scale})`;
        this.orbContainer.style.boxShadow = `
          0 0 ${glowSize}px ${this.config.colors.glow},
          0 2px 10px rgba(0,0,0,0.3)
        `;
      }
    }

    setOrbState(state) {
      // Map legacy 'active' to 'listening' for the new Orb design
      let displayState = state;
      if (state === 'active') displayState = 'listening';
      if (!displayState) displayState = 'idle';

      // Remove all state classes
      this.orbContainer.classList.remove('listening', 'thinking', 'talking', 'idle');

      // Add new state class
      this.orbContainer.classList.add(displayState);

      // Reset transform when not listening (volume scaling)
      if (displayState !== 'listening') {
        this.orbContainer.style.transform = '';
        this.orbContainer.style.boxShadow = '';
      }

      console.log(`🎨 Orb state changed to: ${displayState}`);
    }


    onSpeechStart() {
      if (this.domSnapshotPending || this.waitingForExecution) return;

      this.domSnapshotPending = true;

      // const domData = this.captureDOMSnapshot(); // Original line, now replaced by scanPageBlueprint logic

      if (this.ws && this.ws.readyState === WebSocket.OPEN && !this.waitingForExecution) {

        // --- DIFFERENTIAL UPDATE ---
        const blueprint = this.scanPageBlueprint();

        if (blueprint) {
          console.log('📸 ============================================');
          console.log('📸 SPEECH DETECTED - DOM Changed, Sending Update');
          console.log('📸 ============================================');

          const payload = JSON.stringify({
            type: 'dom_update',
            elements: blueprint
          });

          this.ws.send(payload);

          console.log('📤 WebSocket message sent:', payload.substring(0, 200) + '...');
          console.log('📤 Message size:', payload.length, 'bytes');
        } else {
          console.log('📸 Speech started - DOM Unchanged');
        }
      } else {
        console.error('❌ WebSocket not connected or waiting for execution - cannot send DOM data');
      }

      this.setOrbState('thinking');
    }

    onSpeechEnd() {
      console.log('🤐 Speech ended - waiting for command...');
      this.waitingForExecution = true;
      this.domSnapshotPending = false;

      this.setOrbState('active');
    }

    // ============================================
    // PRODUCTION-GRADE DOM SCANNER (Zero-Touch)
    // ============================================
    scanPageBlueprint(force = false) {
      const elements = [];
      const seenIds = new Set();
      const currentScanIds = new Set(); // Track all IDs in this scan

      // 1. Broaden Scope: Interactive + Context + Text + SHADOW DOM
      // Strategy: Recursive traversal to pierce Shadow DOMs
      const baseSelectorMatches = (el) => {
        return el.matches && el.matches('button, a[href], input, textarea, select, [role="button"], [role="link"], [role="menuitem"], [role="option"], [role="tab"], [tabindex="0"], h1, h2, h3, h4, label, th, nav');
      };

      const collectAllElements = (root) => {
        let elements = [];

        if (!root) return elements;

        const walker = document.createTreeWalker(
          root,
          NodeFilter.SHOW_ELEMENT,
          null,
          false
        );

        let node;
        while (node = walker.nextNode()) {
          elements.push(node);
          if (node.shadowRoot) {
            elements = elements.concat(collectAllElements(node.shadowRoot));
          }
          // 2. Pierce Iframes (Handle blockages gracefully)
          if (node.tagName === 'IFRAME') {
            try {
              if (node.contentDocument && node.contentDocument.body) {
                elements = elements.concat(collectAllElements(node.contentDocument.body));
              }
            } catch (e) {
              // Cross-origin protection blocks this; ignore.
            }
          }
        }
        return elements;
      };

      const allElements = collectAllElements(document.documentElement);

      allElements.forEach(el => {
        // Skip hidden/disabled early
        if (el.disabled || el.type === 'hidden' || el.type === 'password') return;

        // Visibility Check (Expensive, so do last)
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;

        // Filter Logic:
        // 1. Is it a known interactive element?
        // 2. Is it a Leaf Node (no children) with visible text? (Captures div/span text)
        // 3. Is it a pointer cursor?
        const isInteractive = baseSelectorMatches(el) || style.cursor === 'pointer';
        const isTextLeaf = el.children.length === 0 && el.textContent.trim().length > 0;

        if (!isInteractive && !isTextLeaf) return;

        const rect = el.getBoundingClientRect();
        const isInViewport = rect.top < window.innerHeight + 100 &&
          rect.bottom > -100 &&
          rect.left < window.innerWidth &&
          rect.right > 0;

        if (!isInViewport) return;
        if (rect.width < 2 || rect.height < 2) return; // Ignore tiny specks

        // --- Assign Persistent IDs ---
        let finalId = el.id || el.getAttribute('name');
        if (!finalId) {
          if (el.hasAttribute('data-tara-id')) {
            finalId = el.getAttribute('data-tara-id');
          } else {
            finalId = `tara-${Math.random().toString(36).substr(2, 9)}`;
            el.setAttribute('data-tara-id', finalId);
          }
        }

        if (seenIds.has(finalId)) return; // Prevent duplicates across nested scans
        seenIds.add(finalId);
        currentScanIds.add(finalId);

        let type = el.tagName.toLowerCase();
        if (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(type)) type = 'header';

        const isNew = this.previousScanIds && !this.previousScanIds.has(finalId);

        elements.push({
          id: finalId,
          text: this.extractText(el) + (isNew ? ' [NEW]' : ''),
          type: type,
          isNew: isNew, // For sorting
          rect: {
            x: Math.round(rect.left + window.scrollX),
            y: Math.round(rect.top + window.scrollY),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
          }
        });
      });

      // --- DIFFERENTIAL UPDATE CHECK ---
      const newHash = this.generateDOMHash(elements);
      if (!force && this.lastDOMHash === newHash) {
        return null; // No change
      }
      this.lastDOMHash = newHash;
      this.previousScanIds = currentScanIds;

      // SORT: Prioritize [NEW] elements to ensure popups aren't sliced off
      elements.sort((a, b) => (b.isNew === a.isNew) ? 0 : b.isNew ? 1 : -1);

      return elements.slice(0, 300);
    }

    forceScan() {
      // Reset hash to force an update
      this.lastDOMHash = null;
      if (!this.isActive) return;

      console.log('🔄 Forced DOM Scan (Navigation/External Trigger)');
      const blueprint = this.scanPageBlueprint();

      if (blueprint && this.ws && this.ws.readyState === WebSocket.OPEN) {
        const payload = JSON.stringify({
          type: 'dom_update',
          elements: blueprint
        });

        this.ws.send(payload);
        console.log('📤 Forced DOM update sent');
      }
    }

    generateDOMHash(elements) {
      // Simple hash to detect changes
      let str = '';
      for (const el of elements) {
        str += `${el.id}:${el.text}:${el.rect.x}:${el.rect.y}|`;
      }
      // DJB2 hash adaptation
      let hash = 5381;
      for (let i = 0; i < str.length; i++) {
        hash = (hash * 33) ^ str.charCodeAt(i);
      }
      return hash >>> 0; // Ensure unsigned 32-bit
    }

    extractText(el) {
      // 1. Check direct attributes (Accessiblity first)
      let text = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || '';
      if (text) return this.cleanText(text);

      // 2. Check inner text (if visible and meaningful)
      text = el.innerText || el.value || '';
      const cleaned = this.cleanText(text);
      if (cleaned.length > 0) return cleaned;

      // 3. Deep dive for Icons/Images (img alt, svg title)
      const img = el.querySelector('img');
      if (img && img.alt) return this.cleanText(img.alt);

      const svgTitle = el.querySelector('svg title');
      if (svgTitle) return this.cleanText(svgTitle.textContent);

      return '';
    }

    cleanText(str) {
      if (str === null || str === undefined) return '';
      return String(str).replace(/\s+/g, ' ').trim().substring(0, 50);
    }

    async handleBackendMessage(msg) {
      console.log('📨 Backend message:', msg);

      if (msg.type === 'session_created') {
        sessionStorage.setItem('tara_session_id', msg.session_id);
        console.log('💾 Session ID saved:', msg.session_id);
      }
      else if (msg.type === 'ping') {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'pong' }));
        }
      }
      else if (msg.type === 'agent_response') {
        // Append text stream to chat
        const text = msg.text;
        this.appendChatMessage(text, 'ai', true);
      }
      else if (msg.type === 'navigate') {
        console.log(`📍 Navigating to: ${msg.url}`);
        this.setOrbState('executing');

        // Ensure we don't clear session storage on unload
        window.location.href = msg.url;
      }
      else if (msg.type === 'command') {
        const payload = msg.payload || msg;
        const type = payload.type;
        const target_id = payload.target_id || payload.id; // Support both naming styles
        const text = payload.text;

        console.log(`🤖 Executing: ${type} on ${target_id}`);

        await this.executeCommand(type, target_id, text);

        // --- MISSION AGENT HANDSHAKE ---
        // 1. Capture FRESH DOM (wait for settling done in executeCommand)
        const freshDOM = this.scanPageBlueprint(true); // Force scan

        // 2. Send execution_complete WITH DOM
        this.ws.send(JSON.stringify({
          type: 'execution_complete',
          status: 'success',
          dom_context: freshDOM,
          timestamp: Date.now()
        }));

        console.log('✅ Execution complete sent with DOM context - Triggering next step');

        this.waitingForExecution = false;
        this.setOrbState('listening');
      }
      else if (msg.type === 'session_ready' || (msg.type === 'state_update')) {
        if (msg.state) {
          const s = msg.state;
          if (s === 'listening') {
            this.setOrbState('listening');
            // Auto-start microphone if not active (parity with client.html)
            if (!this.micStream) {
              this.startMicrophoneAndCollection();
            } else if (this.micAudioCtx && this.micAudioCtx.state === 'suspended') {
              this.micAudioCtx.resume();
            }
          }
          if (s === 'thinking') this.setOrbState('thinking');
          if (s === 'speaking') this.setOrbState('talking');
        }
      }
    }

    async executeCommand(type, targetId, text) {
      // SET STATE: Executing (Purple)
      this.setOrbState('executing');

      try {
        if (type === 'wait') {
          console.log("⏳ TARA Waiting (as requested)...");
          await new Promise(r => setTimeout(r, 2000));
        }
        else if (type === 'click') {
          const el = this.findElement(targetId, text); // Use text as fallback
          if (el) {
            await this.ghostCursor.moveTo(el);
            await this.ghostCursor.click();

            // --- ROBUST CLICK STRATEGY ---
            const opts = { bubbles: true, cancelable: true, view: window };
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));

            // Native fallback
            if (typeof el.click === 'function') el.click();

            // Handle native focus for inputs
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.focus();
          }
        } else if (type === 'type_text') {
          const el = this.findElement(targetId);
          if (el) {
            await this.ghostCursor.moveTo(el);
            el.focus();

            // Support React/controlled components by bypassing the setter override
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set ||
              Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;

            if (setter) {
              setter.call(el, text);
            } else {
              el.value = text;
            }

            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
        } else if (type === 'scroll_to') {
          const el = this.findElement(targetId, text);
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        } else if (type === 'highlight') {
          this.executeHighlight(targetId, text);
        } else if (type === 'spotlight') {
          this.spotlight.classList.add('active');
          setTimeout(() => this.spotlight.classList.remove('active'), 3000);
        } else if (type === 'clear') {
          this.clearHighlights();
        }

        // --- WAIT FOR DOM SETTLE (Crucial) ---
        // --- WAIT FOR DOM SETTLE (Crucial) ---
        await new Promise(r => setTimeout(r, 2000)); // Increased to 2s for complex React renders

      } catch (err) {
        console.warn("Execution partial error:", err);
      }
    }

    // --- FIX 2: Robust Element Finder ---
    findElement(targetId, fallbackText = null) {
      if (!targetId) return null;

      // Strategy 1: Exact ID match
      let element = document.getElementById(targetId);
      if (element) return element;

      // Strategy 2: data-tara-id match (for our generated IDs)
      element = document.querySelector(`[data-tara-id="${targetId}"]`);
      if (element) return element;

      // Strategy 3: Name or Test ID
      element = document.querySelector(`[name="${targetId}"]`) ||
        document.querySelector(`[data-testid="${targetId}"]`);
      if (element) return element;

      // Strategy 4: Fallback - Text Content Search (if provided)
      if (fallbackText) {
        console.warn(`⚠️ Target ID "${targetId}" not found. Trying provided fallback text: "${fallbackText}"`);
        const allInteractive = document.querySelectorAll('button, a, [role="button"], h1, h2, h3, h4, span, div'); // Broaden search
        for (const el of allInteractive) {
          // Exact-ish match preferable, but loose includes is safer for now
          const elText = this.extractText(el).toLowerCase().trim();
          const targetText = fallbackText.toLowerCase().trim();

          if (elText === targetText || (targetText.length > 5 && elText.includes(targetText))) {
            console.log(`✅ Text fallback found element by content:`, el);
            return el;
          }
        }
      }

      // Strategy 5: Old fallback (searching for ID string in text - rarely works but kept)
      console.warn(`⚠️ Target ID "${targetId}" not found by selector. Trying text fallback...`);
      const allInteractive = document.querySelectorAll('button, a, [role="button"]');
      for (const el of allInteractive) {
        if (this.extractText(el).toLowerCase().includes(targetId.toLowerCase())) {
          console.log(`✅ Text fallback found element:`, el);
          return el;
        }
      }

      return null;
    }

    async executeClick(targetId) {
      const element = this.findElement(targetId);

      if (!element) {
        console.warn(`⚠️ Element NOT found: ${targetId}`);
        return;
      }

      await this.ghostCursor.moveTo(element, 600);
      await new Promise(r => setTimeout(r, 300));
      await this.ghostCursor.click();
      await new Promise(r => setTimeout(r, 100));

      element.click();

      setTimeout(() => this.ghostCursor.hide(), 500);

      console.log(`👆 Clicked: ${targetId}`);
    }

    executeScroll(targetId) {
      const element = this.findElement(targetId);
      if (element) {
        element.scrollIntoView({ behavior: 'smooth', block: 'center' });
        console.log(`📜 Scrolled to: ${targetId}`);
      } else {
        console.warn(`⚠️ Scroll target not found: ${targetId}`);
      }
    }

    executeHighlight(targetId, text) {
      const element = document.getElementById(targetId);
      if (!element) return;

      const rect = element.getBoundingClientRect();

      const highlight = document.createElement('div');
      highlight.className = 'tara-highlight';
      highlight.style.cssText = `
        top: ${rect.top - 4}px;
        left: ${rect.left - 4}px;
        width: ${rect.width + 8}px;
        height: ${rect.height + 8}px;
      `;

      this.highlightContainer.appendChild(highlight);

      setTimeout(() => highlight.remove(), 3000);

      console.log(`✨ Highlighted: ${targetId}`);
    }

    clearHighlights() {
      this.highlightContainer.innerHTML = '';
    }
  }

  window.TaraWidget = TaraWidget;
  window.tara = null;

  function initTara() {
    if (window.tara) return; // Prevent double init
    // Auto-init for Plugin Usage (no specific element required)
    window.tara = new TaraWidget(window.TARA_CONFIG || {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTara);
  } else {
    // If body is ready, init immediately
    if (document.body) {
      initTara();
    } else {
      // Fallback if script runs in head before body exists
      window.addEventListener('DOMContentLoaded', initTara);
    }
  }
})();
