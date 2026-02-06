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
    orbSize: 48, // Clean orb size for glass container
    position: 'bottom-right', // 'bottom-right', 'bottom-left'
    colors: {
      core: '#1a1a1a', // Black core
      accent: '#333333', // Dark gray accent
      glow: 'rgba(255, 255, 255, 0.3)', // White glow
      highlight: '#ffffff', // White highlight
      dim: 'rgba(0, 0, 0, 0.75)' // Darker dim overlay
    },
    audio: {
      inputSampleRate: 16000,
      outputSampleRate: 44100, // HD Quality
      bufferSize: 4096 // Larger buffer for smoother mic capture
    },
    vad: {
      energyThreshold: 0.018, // Matches Backend 600 threshold (600/32768)
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

  // External orb image - absolute URL for cross-origin usage
  const ORB_IMAGE_URL = 'https://demo.davinciai.eu/static/tara-orb.svg';

  // LocalStorage key for cached orb SVG (delivered via WebSocket)
  const ORB_CACHE_KEY = 'tara_orb_svg_cache';

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
      this.locked = false; // New lock flag
    }

    processAudioChunk(float32Array) {
      if (this.locked) return 0; // Completely ignore audio if locked

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
      this._endDebounce = null; // Debounce timer for onEnd to prevent inter-chunk flicker
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

        // Cancel any pending end-of-playback debounce (new chunk arrived)
        if (this._endDebounce) {
          clearTimeout(this._endDebounce);
          this._endDebounce = null;
        }

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
            // Debounce: wait 500ms before declaring playback ended
            // Prevents speaking→listening flicker between audio chunks
            this._endDebounce = setTimeout(() => {
              if (this.activeSources.size === 0) {
                this.isPlaying = false;
                if (this.onEnd) this.onEnd();
              }
              this._endDebounce = null;
            }, 500);
          }
        };
      } catch (err) {
        console.error('❌ Audio play error:', err);
      }
    }

    interrupt() {
      if (!this.audioCtx) return;
      if (this._endDebounce) {
        clearTimeout(this._endDebounce);
        this._endDebounce = null;
      }
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
      this.isVoiceMuted = false; // Agent voice mute state
      this.sessionMode = 'interactive'; // 'interactive' | 'turbo' - set per session

      // Audio WS (dedicated stream for TTS)
      this.audioWs = null;
      this.audioPreBuffer = [];
      this.audioPreBufferSize = 3; // Pre-buffer 3 chunks (~300ms) before playback
      this.audioStreamActive = false;

      this.init();
    }

    init() {
      this.createShadowDOM();
      this.injectStyles();
      this.createOrb();
      this.createOverlay();
      this.createChatUI();
      this.createModeSelector();
      this.createGhostCursor();

      console.log('✨ TARA: Visual Co-Pilot initialized (Hetzner Cloud)');
      console.log('🔗 WebSocket:', this.config.wsUrl);

      // Auto-reconnect if navigated (preserving mode)
      const savedSession = sessionStorage.getItem('tara_session_id');
      const savedMode = sessionStorage.getItem('tara_mode');
      const savedInteractionMode = sessionStorage.getItem('tara_interaction_mode');
      if (savedSession && savedMode === 'visual-copilot') {
        console.log('🔄 Restoring Visual Co-Pilot session:', savedSession);
        this.startVisualCopilot(savedSession, savedInteractionMode || 'interactive');
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

      // Create the pill container
      this.pillContainer = document.createElement('div');
      this.pillContainer.className = 'tara-pill';
      this.pillContainer.innerHTML = `
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

      this.container.appendChild(this.pillContainer);
      this.shadowRoot.appendChild(this.container);
      document.body.appendChild(this.host);
    }

    injectStyles() {
      const styleSheet = new CSSStyleSheet();
      styleSheet.replaceSync(`
        :host { all: initial; }
        #tara-container { isolation: isolate; }

        /* === GLASS CONTAINER === */
        .tara-pill {
          background: rgba(255, 255, 255, 0.12);
          backdrop-filter: blur(24px);
          -webkit-backdrop-filter: blur(24px);
          border-radius: 16px;
          padding: 14px 18px 14px 20px;
          box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.12),
            inset 0 1px 1px rgba(255, 255, 255, 0.2),
            inset 0 -1px 1px rgba(0, 0, 0, 0.05);
          border: 1px solid rgba(255, 255, 255, 0.18);
          transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .tara-pill:hover {
          background: rgba(255, 255, 255, 0.16);
          border-color: rgba(255, 255, 255, 0.25);
          box-shadow:
            0 12px 40px rgba(0, 0, 0, 0.15),
            inset 0 1px 1px rgba(255, 255, 255, 0.25),
            inset 0 -1px 1px rgba(0, 0, 0, 0.05);
        }

        .tara-pill-content {
          display: flex;
          align-items: center;
          gap: 16px;
        }

        .tara-pill-text {
          display: flex;
          flex-direction: column;
          gap: 3px;
          min-width: 160px;
        }

        .tara-pill-title {
          font-size: 14px;
          font-weight: 600;
          color: rgba(255, 255, 255, 0.95);
          letter-spacing: -0.2px;
          text-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }

        .tara-pill-status {
          font-size: 13px;
          font-weight: 400;
          color: rgba(255, 255, 255, 0.6);
          transition: color 0.3s ease;
        }

        /* Status colors based on state */
        .tara-pill.listening .tara-pill-status { color: rgba(180, 160, 255, 0.9); }
        .tara-pill.talking .tara-pill-status { color: rgba(200, 160, 255, 0.9); }
        .tara-pill.executing .tara-pill-status { color: rgba(255, 200, 140, 0.9); }

        .tara-pill-orb-wrapper {
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .tara-pill-speaker {
          width: 36px;
          height: 36px;
          border-radius: 10px;
          border: 1px solid rgba(255, 255, 255, 0.12);
          background: rgba(255, 255, 255, 0.08);
          color: rgba(255, 255, 255, 0.7);
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          transition: all 0.2s ease;
        }

        .tara-pill-speaker:hover {
          background: rgba(255, 255, 255, 0.15);
          border-color: rgba(255, 255, 255, 0.2);
          color: rgba(255, 255, 255, 0.9);
        }

        .tara-pill-speaker.muted {
          color: rgba(255, 100, 100, 0.9);
          background: rgba(255, 100, 100, 0.15);
          border-color: rgba(255, 100, 100, 0.2);
        }

        /* === ORB - PURE SVG (NO BORDER) === */
        .tara-orb {
          width: ${this.config.orbSize}px;
          height: ${this.config.orbSize}px;
          border-radius: 50%;
          cursor: pointer;
          position: relative;
          transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
          background: transparent;
          overflow: visible;
          border: none;
          box-shadow: none;
        }

        .tara-orb-inner {
          position: absolute;
          inset: 0;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
        }

        .tara-orb-inner img {
          width: 100%;
          height: 100%;
          object-fit: cover;
          border-radius: 50%;
          transition: filter 0.3s ease, transform 0.3s ease;
        }

        .tara-orb:hover .tara-orb-inner img {
          transform: scale(1.05);
        }

        /* === STATES (filter effects on SVG) === */

        .tara-orb.idle .tara-orb-inner img {
          filter: brightness(0.9) saturate(0.8);
        }

        .tara-orb.listening .tara-orb-inner img {
          filter: brightness(1.05) saturate(1.1);
          animation: tara-orb-pulse 2s ease-in-out infinite;
        }

        .tara-orb.talking .tara-orb-inner img {
          filter: brightness(1.15) saturate(1.2);
          animation: tara-orb-speak 1s ease-in-out infinite;
        }

        .tara-orb.executing .tara-orb-inner img {
          filter: brightness(1.1) saturate(1.1) hue-rotate(20deg);
          animation: tara-orb-pulse 1.2s ease-in-out infinite;
        }

        /* === KEYFRAMES === */

        @keyframes tara-orb-pulse {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.06); }
        }

        @keyframes tara-orb-speak {
          0%, 100% { transform: scale(1.02); }
          50% { transform: scale(1.1); }
        }

        /* === BLUE SCREEN FILTER (Agent in Control) === */
        .tara-screen-overlay {
          position: fixed;
          inset: 0;
          background: rgba(10, 50, 140, 0.08);
          pointer-events: none;
          z-index: 999998;
          opacity: 0;
          transition: opacity 0.8s cubic-bezier(0.4, 0, 0.2, 1);
          mix-blend-mode: multiply;
        }

        .tara-screen-overlay.active {
          opacity: 1;
        }

        /* Subtle blue vignette - no blur, just color */
        .tara-screen-overlay::before {
          content: '';
          position: absolute;
          inset: 0;
          background: radial-gradient(ellipse at center, transparent 40%, rgba(5, 30, 100, 0.12) 100%);
          pointer-events: none;
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

        /* === GEMINI-STYLE CHAT BAR === */
        .tara-chat-bar {
            position: fixed;
            bottom: 28px;
            left: 50%;
            transform: translateX(-50%);
            width: min(720px, 65vw);
            z-index: 100002;
            pointer-events: auto;
            display: none;
            flex-direction: column;
            gap: 0;
            opacity: 0;
            transition: opacity 0.4s cubic-bezier(0.4,0,0.2,1),
                        transform 0.4s cubic-bezier(0.4,0,0.2,1);
        }
        .tara-chat-bar.visible {
            opacity: 1;
            transform: translateX(-50%) translateY(0);
        }

        /* Messages panel - expands above input */
        .tara-chat-messages-panel {
            max-height: 380px;
            overflow-y: auto;
            display: none;
            flex-direction: column;
            gap: 12px;
            padding: 16px 20px;
            background: rgba(15, 15, 25, 0.80);
            backdrop-filter: blur(40px) saturate(1.4);
            -webkit-backdrop-filter: blur(40px) saturate(1.4);
            border: 1px solid rgba(255,255,255,0.10);
            border-bottom: none;
            border-radius: 20px 20px 0 0;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.1) transparent;
        }
        .tara-chat-messages-panel::-webkit-scrollbar { width: 4px; }
        .tara-chat-messages-panel::-webkit-scrollbar-track { background: transparent; }
        .tara-chat-messages-panel::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
        .tara-chat-messages-panel.has-messages {
            display: flex;
        }

        /* Input container - the main bar */
        .tara-chat-input-bar {
            background: rgba(30, 30, 40, 0.85);
            backdrop-filter: blur(40px) saturate(1.4);
            -webkit-backdrop-filter: blur(40px) saturate(1.4);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 24px;
            padding: 6px 8px 6px 20px;
            display: flex;
            align-items: center;
            gap: 8px;
            box-shadow:
                0 16px 60px rgba(0,0,0,0.35),
                0 4px 20px rgba(0,0,0,0.15),
                inset 0 1px 1px rgba(255,255,255,0.06);
            transition: border-color 0.3s ease, box-shadow 0.3s ease;
        }
        .tara-chat-messages-panel.has-messages + .tara-chat-input-bar {
            border-radius: 0 0 24px 24px;
            border-top: 1px solid rgba(255,255,255,0.06);
        }
        .tara-chat-input-bar:focus-within {
            border-color: rgba(242, 90, 41, 0.35);
            box-shadow:
                0 16px 60px rgba(0,0,0,0.35),
                0 0 0 3px rgba(242, 90, 41, 0.08);
        }
        .tara-chat-input-bar input {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            color: rgba(255,255,255,0.92);
            font-size: 15px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            padding: 10px 0;
        }
        .tara-chat-input-bar input::placeholder {
            color: rgba(255,255,255,0.30);
        }

        /* Mic button */
        .tara-chat-mic {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: rgba(255,255,255,0.08);
            border: none;
            color: rgba(255,255,255,0.6);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s ease;
            flex-shrink: 0;
        }
        .tara-chat-mic:hover {
            background: rgba(255,255,255,0.14);
            color: rgba(255,255,255,0.9);
        }
        .tara-chat-mic.active {
            background: rgba(242, 90, 41, 0.2);
            color: #f25a29;
            animation: tara-mic-pulse 2s ease-in-out infinite;
        }
        @keyframes tara-mic-pulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(242,90,41,0.3); }
            50% { box-shadow: 0 0 0 8px rgba(242,90,41,0); }
        }

        /* Send button */
        .tara-chat-send-btn {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, #f25a29, #e04820);
            border: none;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s ease;
            flex-shrink: 0;
            opacity: 0.4;
        }
        .tara-chat-send-btn.has-text { opacity: 1; }
        .tara-chat-send-btn.has-text:hover {
            transform: scale(1.08);
            box-shadow: 0 4px 16px rgba(242,90,41,0.3);
        }

        /* Mode badge */
        .tara-mode-badge {
            font-size: 11px;
            padding: 3px 10px;
            border-radius: 12px;
            background: rgba(255,255,255,0.08);
            color: rgba(255,255,255,0.5);
            white-space: nowrap;
            flex-shrink: 0;
        }
        .tara-mode-badge.interactive { color: rgba(80,200,120,0.8); background: rgba(80,200,120,0.1); }
        .tara-mode-badge.turbo { color: rgba(242,90,41,0.8); background: rgba(242,90,41,0.1); }

        /* Message bubbles */
        .tara-msg {
            max-width: 85%;
            padding: 10px 14px;
            border-radius: 14px;
            font-size: 13.5px;
            line-height: 1.55;
            color: rgba(255,255,255,0.92);
            animation: tara-msg-appear 0.3s cubic-bezier(0.4,0,0.2,1);
        }
        .tara-msg.user {
            align-self: flex-end;
            background: linear-gradient(135deg, #f25a29, #e04820);
            color: white;
            border-bottom-right-radius: 4px;
        }
        .tara-msg.ai {
            align-self: flex-start;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.06);
            border-bottom-left-radius: 4px;
        }
        @keyframes tara-msg-appear {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Typing indicator */
        .tara-typing-indicator {
            align-self: flex-start;
            padding: 12px 18px;
            background: rgba(255,255,255,0.06);
            border-radius: 14px;
            display: flex;
            gap: 5px;
            align-items: center;
        }
        .tara-typing-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            background: rgba(255,255,255,0.4);
            animation: tara-typing-bounce 1.4s ease-in-out infinite;
        }
        .tara-typing-dot:nth-child(2) { animation-delay: 0.2s; }
        .tara-typing-dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes tara-typing-bounce {
            0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
            30% { transform: translateY(-6px); opacity: 1; }
        }

        /* === MODE SELECTOR DIALOG === */
        .tara-mode-selector {
            position: fixed;
            inset: 0;
            z-index: 100003;
            display: none;
            align-items: center;
            justify-content: center;
            background: rgba(0,0,0,0.5);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            opacity: 0;
            transition: opacity 0.3s ease;
            pointer-events: auto;
        }
        .tara-mode-selector.visible { opacity: 1; }
        .tara-mode-selector-card {
            background: rgba(25, 25, 35, 0.92);
            backdrop-filter: blur(40px);
            -webkit-backdrop-filter: blur(40px);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 24px;
            padding: 32px;
            width: 380px;
            box-shadow: 0 24px 80px rgba(0,0,0,0.5);
            text-align: center;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        .tara-mode-selector-title {
            color: rgba(255,255,255,0.95);
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .tara-mode-selector-subtitle {
            color: rgba(255,255,255,0.4);
            font-size: 13px;
            margin-bottom: 24px;
        }
        .tara-mode-option {
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 16px;
            border-radius: 16px;
            border: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.04);
            cursor: pointer;
            transition: all 0.2s ease;
            margin-bottom: 10px;
            text-align: left;
        }
        .tara-mode-option:hover {
            background: rgba(255,255,255,0.08);
            border-color: rgba(255,255,255,0.15);
            transform: translateY(-1px);
        }
        .tara-mode-option-icon {
            width: 44px; height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .tara-mode-option-icon.interactive { background: rgba(80,200,120,0.15); }
        .tara-mode-option-icon.turbo { background: rgba(242,90,41,0.15); }
        .tara-mode-option-label {
            color: rgba(255,255,255,0.92);
            font-size: 15px;
            font-weight: 500;
        }
        .tara-mode-option-desc {
            color: rgba(255,255,255,0.4);
            font-size: 12px;
            margin-top: 2px;
        }
      `);

      this.shadowRoot.adoptedStyleSheets = [styleSheet];
    }

    createOrb() {
      this.orbContainer = document.createElement('div');
      this.orbContainer.className = 'tara-orb idle';

      // Add inner container with external SVG image
      const orbInner = document.createElement('div');
      orbInner.className = 'tara-orb-inner';

      // Use cached SVG (from WebSocket) or fall back to external URL
      const orbImg = document.createElement('img');
      const cachedUrl = getCachedOrbUrl();
      orbImg.src = cachedUrl || this.getOrbImageUrl();
      orbImg.alt = 'TARA';
      orbImg.draggable = false;
      orbImg.style.cssText = 'pointer-events: none; user-select: none;';
      if (!cachedUrl) {
        // External URL may fail due to cert - will be resolved when WS delivers the asset
        orbImg.onerror = () => {
          console.warn('⚠️ Orb image failed to load (cert issue) - will load via WebSocket');
          orbImg.style.opacity = '0.3'; // Dim until WS delivers
          orbImg.onerror = null;
        };
      }
      this.orbImg = orbImg; // Store reference for WS update

      orbInner.appendChild(orbImg);
      this.orbContainer.appendChild(orbInner);

      // Append orb to the pill wrapper instead of container directly
      const orbWrapper = this.pillContainer.querySelector('.tara-pill-orb-wrapper');
      if (orbWrapper) {
        orbWrapper.appendChild(this.orbContainer);
      } else {
        this.container.appendChild(this.orbContainer);
      }

      // Click on orb or pill to start/stop (with mode selection)
      this.orbContainer.addEventListener('click', async () => {
        if (!this.isActive) {
          const mode = await this.showModeSelector();
          await this.startVisualCopilot(null, mode);
        } else {
          await this.stopVisualCopilot();
        }
      });

      // Speaker mute button - mutes agent voice output (not mic)
      const speakerBtn = this.pillContainer.querySelector('.tara-pill-speaker');
      if (speakerBtn) {
        this.isVoiceMuted = false;
        speakerBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.isVoiceMuted = !this.isVoiceMuted;
          speakerBtn.classList.toggle('muted', this.isVoiceMuted);
          speakerBtn.title = this.isVoiceMuted ? 'Unmute Agent Voice' : 'Mute Agent Voice';

          // Toggle speaker icons
          const speakerOn = speakerBtn.querySelector('.speaker-on');
          const speakerOff = speakerBtn.querySelector('.speaker-off');
          if (speakerOn && speakerOff) {
            speakerOn.style.display = this.isVoiceMuted ? 'none' : 'block';
            speakerOff.style.display = this.isVoiceMuted ? 'block' : 'none';
          }

          // Notify backend about mute state for turbo mode toggle
          if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
              type: 'speaker_mute',
              muted: this.isVoiceMuted
            }));
            console.log(this.isVoiceMuted ? '🔇 Agent voice muted (TURBO MODE enabled)' : '🔊 Agent voice unmuted (WALKTHROUGH MODE)');
          } else {
            console.log(this.isVoiceMuted ? '🔇 Agent voice muted' : '🔊 Agent voice unmuted');
          }
        });
      }

      // Chat toggle removed - replaced by Gemini-style bottom bar (Step 2)
    }

    getOrbImageUrl() {
      // Use absolute URL for cross-origin compatibility
      return ORB_IMAGE_URL;
    }

    updateTooltip(text) {
      if (this.tooltip) {
        this.tooltip.textContent = text;
      }
    }

    createOverlay() {
      // Glass screen overlay - shows when agent is in control
      this.screenOverlay = document.createElement('div');
      this.screenOverlay.className = 'tara-screen-overlay';
      this.shadowRoot.appendChild(this.screenOverlay);

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
    // --- GEMINI-STYLE CHAT BAR ---
    createChatUI() {
      // Main bar wrapper (hidden until session starts)
      const bar = document.createElement('div');
      bar.className = 'tara-chat-bar';

      // Messages panel (hidden until messages exist)
      this.chatMessages = document.createElement('div');
      this.chatMessages.className = 'tara-chat-messages-panel';
      bar.appendChild(this.chatMessages);

      // Input bar
      const inputBar = document.createElement('div');
      inputBar.className = 'tara-chat-input-bar';

      // Text input
      this.chatInput = document.createElement('input');
      this.chatInput.type = 'text';
      this.chatInput.placeholder = 'Ask TARA...';
      this.chatInput.onkeydown = (e) => {
        if (e.key === 'Enter') this.sendTextCommand();
      };
      this.chatInput.oninput = () => {
        const hasText = this.chatInput.value.trim().length > 0;
        this.sendButton.classList.toggle('has-text', hasText);
      };

      // Mode badge
      this.modeBadge = document.createElement('span');
      this.modeBadge.className = 'tara-mode-badge';
      this.modeBadge.textContent = '';

      // Mic button (SVG mic icon, hidden in turbo mode)
      this.micButton = document.createElement('button');
      this.micButton.className = 'tara-chat-mic';
      this.micButton.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`;
      this.micButton.title = 'Voice input active';

      // Send button (SVG arrow icon)
      this.sendButton = document.createElement('button');
      this.sendButton.className = 'tara-chat-send-btn';
      this.sendButton.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`;
      this.sendButton.onclick = () => this.sendTextCommand();

      inputBar.appendChild(this.chatInput);
      inputBar.appendChild(this.modeBadge);
      inputBar.appendChild(this.micButton);
      inputBar.appendChild(this.sendButton);
      bar.appendChild(inputBar);

      this.shadowRoot.appendChild(bar);
      this.chatBar = bar;
    }

    // --- MODE SELECTOR DIALOG ---
    createModeSelector() {
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

      // Interactive option
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

      // Turbo option
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

      this.shadowRoot.appendChild(overlay);
      this.modeSelector = overlay;
      this.modeSelectorInteractive = interactiveOpt;
      this.modeSelectorTurbo = turboOpt;
    }

    showModeSelector() {
      return new Promise((resolve) => {
        this.modeSelector.style.display = 'flex';
        requestAnimationFrame(() => {
          this.modeSelector.classList.add('visible');
        });

        const handleChoice = (mode) => {
          this.modeSelectorInteractive.onclick = null;
          this.modeSelectorTurbo.onclick = null;
          this.hideModeSelector();
          resolve(mode);
        };

        this.modeSelectorInteractive.onclick = () => handleChoice('interactive');
        this.modeSelectorTurbo.onclick = () => handleChoice('turbo');
      });
    }

    hideModeSelector() {
      this.modeSelector.classList.remove('visible');
      setTimeout(() => {
        this.modeSelector.style.display = 'none';
      }, 300);
    }

    showChatBar() {
      if (!this.chatBar) return;
      this.chatBar.style.display = 'flex';
      // Set initial transform for animation
      this.chatBar.style.transform = 'translateX(-50%) translateY(20px)';
      requestAnimationFrame(() => {
        this.chatBar.classList.add('visible');
      });
    }

    hideChatBar() {
      if (!this.chatBar) return;
      this.chatBar.classList.remove('visible');
      setTimeout(() => {
        this.chatBar.style.display = 'none';
        // Clear messages
        if (this.chatMessages) {
          this.chatMessages.innerHTML = '';
          this.chatMessages.classList.remove('has-messages');
        }
      }, 400);
    }

    showTypingIndicator() {
      if (!this.chatMessages) return;
      this.hideTypingIndicator();
      const indicator = document.createElement('div');
      indicator.className = 'tara-typing-indicator';
      indicator.id = 'tara-typing';
      indicator.innerHTML = '<div class="tara-typing-dot"></div><div class="tara-typing-dot"></div><div class="tara-typing-dot"></div>';
      this.chatMessages.appendChild(indicator);
      this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
      // Show messages panel if not visible
      this.chatMessages.classList.add('has-messages');
    }

    hideTypingIndicator() {
      const existing = this.chatMessages?.querySelector('#tara-typing');
      if (existing) existing.remove();
    }

    sendTextCommand() {
      const text = this.chatInput.value.trim();
      if (!text) return;

      this.appendChatMessage(text, 'user');
      this.chatInput.value = '';
      this.sendButton.classList.remove('has-text');
      this.showTypingIndicator();

      // Send to Backend with mode info
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          type: 'text_input',
          text: text,
          mode: this.sessionMode
        }));
      }
    }

    appendChatMessage(text, sender, isStreaming = false) {
      if (!this.chatMessages) return;

      // Hide typing indicator when AI message arrives
      if (sender === 'ai') this.hideTypingIndicator();

      let msgEl;
      // Streaming: append to last AI message
      const lastMsg = this.chatMessages.lastElementChild;
      if (isStreaming && lastMsg && lastMsg.dataset.sender === 'ai' && lastMsg.dataset.streaming === 'true') {
        msgEl = lastMsg;
        msgEl.querySelector('.content').textContent += text;
      } else {
        msgEl = document.createElement('div');
        msgEl.className = `tara-msg ${sender}`;
        msgEl.dataset.sender = sender;
        if (isStreaming) msgEl.dataset.streaming = 'true';
        msgEl.innerHTML = `<div class="content">${text}</div>`;
        this.chatMessages.appendChild(msgEl);
      }

      // Show messages panel if not visible
      this.chatMessages.classList.add('has-messages');
      this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
    }

    async startVisualCopilot(resumeSessionId = null, mode = 'interactive') {
      try {
        this.sessionMode = mode;
        console.log('🎯 ============================================');
        console.log(`🎯 ${resumeSessionId ? 'RESUMING' : 'STARTING'} VISUAL CO-PILOT MODE [${mode.toUpperCase()}]`);
        console.log('🎯 ============================================');

        // Configure based on mode
        if (mode === 'interactive') {
          // 1. Initialize audio manager + mic for voice mode
          await this.initializeAudioManager();
          await this.startMicrophoneAndCollection();
        } else {
          // Turbo mode: no audio, no mic
          this.isVoiceMuted = true;
          console.log('⚡ Turbo Mode: Skipping audio & mic initialization');
        }

        // 2. Connect WebSocket
        await this.connectWebSocket();

        // 3. Send session_config with interaction mode
        const sessionConfig = {
          type: 'session_config',
          mode: 'visual-copilot',
          interaction_mode: this.sessionMode,
          timestamp: Date.now(),
          session_id: resumeSessionId,
          current_url: window.location.pathname
        };

        // Persist for auto-resume across navigation
        sessionStorage.setItem('tara_mode', 'visual-copilot');
        sessionStorage.setItem('tara_interaction_mode', this.sessionMode);

        console.log('📤 Sending session_config:', JSON.stringify(sessionConfig));
        this.ws.send(JSON.stringify(sessionConfig));

        // 4. Send DOM Blueprint
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
        this.setOrbState('listening');
        this.updateTooltip('Click to end Visual Co-Pilot');

        // 5. Show Gemini-style chat bar + configure for mode
        this.showChatBar();
        this.micButton.style.display = mode === 'turbo' ? 'none' : 'flex';
        this.modeBadge.textContent = mode === 'turbo' ? 'Turbo' : 'Interactive';
        this.modeBadge.className = `tara-mode-badge ${mode}`;

        // Update mic button state based on listening
        if (mode === 'interactive') {
          this.micButton.classList.add('active');
        }

      } catch (err) {
        console.error('❌ Failed to start Visual Co-Pilot:', err);
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

        // VAD - gated: ignore entirely while agent is speaking (prevents echo feedback loop)
        this.vad = new VoiceActivityDetector(
          () => {
            if (this.agentIsSpeaking) return; // Agent's own voice - ignore
            console.log("🗣️ User started speaking [VAD]");
            this.onSpeechStart();
          },
          () => {
            if (this.agentIsSpeaking) return; // Agent's own voice - ignore
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
        // Worklet or ScriptProcessor (ScriptProcessor is easier single-file)
        const processor = this.micAudioCtx.createScriptProcessor(2048, 1, 1);

        source.connect(processor);
        processor.connect(this.micAudioCtx.destination);

        processor.onaudioprocess = (e) => {
          if (!this.isActive || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;

          // GATE: Skip all mic processing while agent is speaking (prevents echo feedback)
          if (this.agentIsSpeaking) {
            this.updateOrbVolume(0);
            return;
          }

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

          // 3. Send Audio to backend STT
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
      sessionStorage.removeItem('tara_interaction_mode');

      // Hide chat bar
      this.hideChatBar();

      // Close dedicated audio WebSocket
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

      this.ghostCursor?.hide();
      this.clearHighlights();
      this.spotlight.classList.remove('active');

      // Remove screen overlay
      if (this.screenOverlay) {
        this.screenOverlay.classList.remove('active', 'listening', 'talking', 'executing');
      }

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

          // Activate blue screen overlay - TARA is active
          if (this.screenOverlay) this.screenOverlay.classList.add('active');

          // Request orb SVG via WebSocket if not cached locally
          if (!getCachedOrbUrl()) {
            this.ws.send(JSON.stringify({ type: 'request_asset', asset: 'tara-orb.svg' }));
            console.log('📦 Requesting orb SVG via WebSocket...');
          }

          resolve();
        };

        this.ws.onmessage = (e) => {
          if (e.data instanceof ArrayBuffer) {
            // Binary audio frame arrived - only queue if dedicated audio WS is not active
            if (!this.audioStreamActive) {
              this.binaryQueue.push(e.data);
            }
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
              // Skip audio in turbo mode
              if (this.sessionMode === 'turbo') {
                if (data.binary_sent && this.binaryQueue.length > 0) this.binaryQueue.shift();
                // Don't return - still process handleBackendMessage for metadata
              }
              // Skip if dedicated audio stream is active (audio comes via /stream WS)
              else if (this.audioStreamActive) {
                // Audio handled by dedicated WS, just drain any stale binary queue
                if (data.binary_sent && this.binaryQueue.length > 0) this.binaryQueue.shift();
              }
              // Normal mode: play via control WS (fallback)
              else {
                // 1. Check for binary frame in queue
                if (data.binary_sent && this.binaryQueue.length > 0) {
                  const chunk = this.binaryQueue.shift();
                  if (this.audioManager && !this.isVoiceMuted) {
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
                  if (this.audioManager && !this.isVoiceMuted) {
                    this.audioManager.playChunk(bytes.buffer, data.format || 'pcm_f32le', data.sample_rate || 44100);
                  }
                }
              }
            }

            this.handleBackendMessage(data);
          }
        };

        this.ws.onclose = () => {
          console.log('🔌 WebSocket closed');
          // Remove blue screen overlay - TARA is inactive
          if (this.screenOverlay) this.screenOverlay.classList.remove('active');
          if (this.isActive) this.stopVisualCopilot();
        };

        this.ws.onerror = (err) => {
          console.error('❌ WebSocket error:', err);
          reject(err);
        };
      });
    }

    // --- DEDICATED AUDIO WEBSOCKET (Step 4) ---
    connectAudioWebSocket(sessionId) {
      if (!sessionId || this.sessionMode === 'turbo') return;

      const audioUrl = this.config.wsUrl.replace('/ws', '/stream') +
        '?session_id=' + encodeURIComponent(sessionId);

      console.log('🔊 Connecting audio WebSocket:', audioUrl);
      this.audioWs = new WebSocket(audioUrl);
      this.audioWs.binaryType = 'arraybuffer';

      this.audioWs.onopen = () => {
        console.log('✅ Audio WebSocket connected');
        this.audioStreamActive = true;
      };

      this.audioWs.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) {
          // Binary audio frame from dedicated stream
          this.handleAudioStreamChunk(e.data);
        } else {
          try {
            const data = JSON.parse(e.data);
            if (data.type === 'audio_stream_ready') {
              console.log('🔊 Audio stream ready:', data);
            } else if (data.type === 'audio_stream_end') {
              this.flushAudioPreBuffer();
            }
          } catch (err) {
            console.error('Audio WS JSON error:', err);
          }
        }
      };

      this.audioWs.onclose = () => {
        console.log('🔊 Audio WebSocket closed');
        this.audioStreamActive = false;
      };

      this.audioWs.onerror = (err) => {
        console.warn('⚠️ Audio WebSocket error (falling back to control WS):', err);
        this.audioStreamActive = false;
      };
    }

    handleAudioStreamChunk(buffer) {
      // Skip if turbo mode or voice muted
      if (this.sessionMode === 'turbo' || this.isVoiceMuted) return;

      // Pre-buffer strategy: collect N chunks before starting playback
      this.audioPreBuffer.push(buffer);
      if (this.audioPreBuffer.length >= this.audioPreBufferSize) {
        this.flushAudioPreBuffer();
      }
    }

    flushAudioPreBuffer() {
      if (!this.audioManager || this.audioPreBuffer.length === 0) return;
      for (const chunk of this.audioPreBuffer) {
        this.audioManager.playChunk(chunk, 'pcm_f32le', 44100);
      }
      this.audioPreBuffer = [];
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
      // Uses transform only - glow is handled by CSS ::before pseudo-element
      if (this.orbContainer && this.orbContainer.classList.contains('listening')) {
        const scale = 1 + (volume * 0.12); // Subtle scale: 1.0 to 1.12
        this.orbContainer.style.transform = `scale(${scale})`;
      }
    }

    setOrbState(state) {
      // Map legacy states - 'thinking'/'processing' maps to 'listening' (no separate processing UI)
      let displayState = state;
      if (state === 'active' || state === 'thinking') displayState = 'listening';
      if (!displayState) displayState = 'idle';

      // MODE FILTER: Turbo mode only allows thinking + executing visual states
      if (this.sessionMode === 'turbo') {
        if (displayState === 'listening') displayState = 'idle';
        if (displayState === 'talking') return; // Skip speaking state entirely
      }

      // Remove all state classes from orb
      this.orbContainer.classList.remove('listening', 'talking', 'idle', 'executing');
      this.orbContainer.classList.add(displayState);

      // Update pill container state class
      if (this.pillContainer) {
        this.pillContainer.classList.remove('listening', 'talking', 'idle', 'executing');
        this.pillContainer.classList.add(displayState);

        // Update status text (mode-aware)
        const statusEl = this.pillContainer.querySelector('.tara-pill-status');
        if (statusEl) {
          const statusTexts = this.sessionMode === 'turbo' ? {
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

      // Blue screen overlay is controlled by WS connect/disconnect only (Step 1)

      // Update mic button state on chat bar
      if (this.micButton && this.sessionMode === 'interactive') {
        this.micButton.classList.toggle('active', displayState === 'listening');
      }

      // Reset transform when not listening (volume scaling)
      if (displayState !== 'listening') {
        this.orbContainer.style.transform = '';
      }

      // STRICT MIC LOCK: Only allow VAD/Transmission when 'listening' or 'talking' (barge-in support)
      if (this.vad) {
        // We allow 'talking' so user can interrupt (barge-in)
        const shouldLock = (displayState !== 'listening' && displayState !== 'talking');
        if (this.vad.locked !== shouldLock) {
          this.vad.locked = shouldLock;
          if (shouldLock) {
            this.vad.reset();
            console.log(`🔒 Mic LOCKED (State: ${displayState})`);
          } else {
            console.log(`🔓 Mic UNLOCKED (State: ${displayState})`);
          }
        }
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

      this.setOrbState('listening');
    }

    onSpeechEnd() {
      console.log('🤐 Speech ended - waiting for command...');
      this.waitingForExecution = true;
      this.domSnapshotPending = false;

      this.setOrbState('listening');
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

      if (msg.type === 'asset_data') {
        // Orb SVG delivered via WebSocket - cache in localStorage and apply
        if (msg.asset === 'tara-orb.svg' && msg.data) {
          const dataUrl = cacheOrbSvg(msg.data);
          if (this.orbImg) {
            this.orbImg.src = dataUrl;
            this.orbImg.style.opacity = '1';
          }
          console.log('📦 Orb SVG received via WebSocket and cached in localStorage');
        }
        return;
      }
      else if (msg.type === 'session_created') {
        sessionStorage.setItem('tara_session_id', msg.session_id);
        console.log('💾 Session ID saved:', msg.session_id);
        // Connect dedicated audio WebSocket (interactive mode only)
        if (this.sessionMode === 'interactive') {
          this.connectAudioWebSocket(msg.session_id);
        }
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

        // Robust SPA Navigation for modern frameworks (React/Next.js/Vue)
        try {
          const targetUrl = new URL(msg.url, window.location.origin);
          if (targetUrl.origin === window.location.origin) {
            console.log('🔄 SPA Nav: history.pushState + popstate dispatch');
            window.history.pushState({}, '', msg.url);
            window.dispatchEvent(new PopStateEvent('popstate'));

            // Fallback: If URL doesn't change after 100ms (router ignored popstate), force reload
            setTimeout(() => {
              if (window.location.href !== msg.url) {
                console.warn('⚠️ Router blocked pushState - Forcing reload');
                window.location.href = msg.url;
              }
            }, 500);
          } else {
            console.warn('⚠️ External navigation detected - Page WILL reload');
            window.location.href = msg.url;
          }
        } catch (e) {
          console.error('Navigation error:', e);
          window.location.href = msg.url;
        }
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
            // Auto-start microphone if not active (interactive mode only)
            if (this.sessionMode === 'interactive') {
              if (!this.micStream) {
                this.startMicrophoneAndCollection();
              } else if (this.micAudioCtx && this.micAudioCtx.state === 'suspended') {
                this.micAudioCtx.resume();
              }
            }
          }
          if (s === 'thinking') {
            this.setOrbState('listening');
            this.showTypingIndicator();
          }
          if (s === 'speaking') this.setOrbState('talking');
        }
      }
      else if (msg.type === 'speaker_mute_confirmed') {
        // Backend confirmed the mute state change
        const mode = msg.mode === 'turbo' ? 'TURBO MODE (fast execution)' : 'WALKTHROUGH MODE (synchronized)';
        console.log(`🎛️ Mode confirmed: ${mode}`);

        // Optional: Show a brief notification to user
        const statusEl = this.pillContainer?.querySelector('.tara-pill-status');
        if (statusEl) {
          const originalText = statusEl.textContent;
          statusEl.textContent = msg.muted ? '🚀 Turbo Mode' : '🔊 Walkthrough';
          setTimeout(() => {
            // Restore original status after 2 seconds
            if (statusEl.textContent.includes('Turbo') || statusEl.textContent.includes('Walkthrough')) {
              statusEl.textContent = originalText;
            }
          }, 2000);
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
        } else if (type === 'scroll') {
          // Blind scroll for exploration
          window.scrollBy({ top: window.innerHeight * 0.7, behavior: 'smooth' });
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
