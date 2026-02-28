/**
 * TARA Visual Co-Pilot — Styles
 * Module: tara-styles.js
 *
 * All CSS for the TARA widget UI (pill, orb, chat bar, mode selector, etc.)
 * Depends on: tara-config.js (window.TARA.Config)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    /**
     * Returns the full CSS string for the TARA widget.
     * @param {Object} config - TARA config with orbSize, colors, etc.
     * @returns {string} CSS text
     */
    function getStyles(config) {
        return `
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
          width: ${config.orbSize}px;
          height: ${config.orbSize}px;
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
          background: ${config.colors.dim};
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.5s ease;
          z-index: 99998;
        }
        
        .tara-spotlight.active { opacity: 1; }
        
        .tara-highlight {
          position: fixed;
          border: 3px solid ${config.colors.highlight};
          border-radius: 8px;
          box-shadow: 0 0 30px ${config.colors.highlight}, inset 0 0 30px rgba(255, 215, 0, 0.3);
          pointer-events: none;
          z-index: 99997;
          animation: tara-highlight-pulse 1s ease-in-out infinite;
        }

        @keyframes tara-highlight-pulse {
          0%, 100% { box-shadow: 0 0 20px ${config.colors.highlight}; }
          50% { box-shadow: 0 0 40px ${config.colors.highlight}, 0 0 80px rgba(255, 215, 0, 0.6); }
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

        /* Messages panel */
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

        /* Input container */
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
      `;
    }

    window.TARA.Styles = { getStyles };
    console.log('✅ [TARA] Styles module loaded');
})();
