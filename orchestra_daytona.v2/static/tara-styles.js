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

        /* === DARK GLASSMORPHIC PILL === */
        .tara-pill {
          background: rgba(17, 17, 24, 0.85);
          backdrop-filter: blur(24px) saturate(1.8);
          -webkit-backdrop-filter: blur(24px) saturate(1.8);
          border-radius: 16px;
          padding: 14px 18px 14px 20px;
          box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.4),
            0 2px 8px rgba(0, 0, 0, 0.3),
            inset 0 1px 1px rgba(255, 255, 255, 0.05),
            inset 0 -1px 1px rgba(0, 0, 0, 0.2);
          border: 1px solid rgba(255, 255, 255, 0.08);
          transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .tara-pill:hover {
          background: rgba(22, 22, 32, 0.9);
          border-color: rgba(255, 255, 255, 0.12);
          box-shadow:
            0 12px 48px rgba(0, 0, 0, 0.5),
            0 4px 12px rgba(0, 0, 0, 0.35),
            inset 0 1px 1px rgba(255, 255, 255, 0.08),
            inset 0 -1px 1px rgba(0, 0, 0, 0.25);
        }

        /* Active states with blue glow */
        .tara-pill.listening,
        .tara-pill.talking,
        .tara-pill.executing {
          border-color: rgba(59, 130, 246, 0.4);
          box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.4),
            0 0 40px rgba(59, 130, 246, 0.15),
            inset 0 1px 1px rgba(255, 255, 255, 0.08),
            inset 0 -1px 1px rgba(0, 0, 0, 0.2);
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
          text-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
        }

        .tara-pill-status {
          font-size: 13px;
          font-weight: 400;
          color: rgba(255, 255, 255, 0.5);
          transition: color 0.3s ease;
        }

        /* Status colors based on state - electric blue accent */
        .tara-pill.listening .tara-pill-status { color: rgba(96, 165, 250, 0.95); }
        .tara-pill.talking .tara-pill-status { color: rgba(147, 197, 253, 0.95); }
        .tara-pill.executing .tara-pill-status { color: rgba(251, 191, 36, 0.95); }

        .tara-pill-orb-wrapper {
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .tara-pill-speaker {
          width: 36px;
          height: 36px;
          border-radius: 10px;
          border: 1px solid rgba(255, 255, 255, 0.08);
          background: rgba(255, 255, 255, 0.04);
          color: rgba(255, 255, 255, 0.6);
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .tara-pill-speaker:hover {
          background: rgba(255, 255, 255, 0.08);
          border-color: rgba(255, 255, 255, 0.15);
          color: rgba(255, 255, 255, 0.9);
        }

        .tara-pill-speaker.muted {
          color: rgba(248, 113, 113, 0.9);
          background: rgba(248, 113, 113, 0.12);
          border-color: rgba(248, 113, 113, 0.25);
        }

        /* === ORB - DARK THEME === */
        .tara-orb {
          width: ${config.orbSize}px;
          height: ${config.orbSize}px;
          border-radius: 50%;
          cursor: pointer;
          position: relative;
          transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
          background: transparent;
          overflow: visible;
          border: none;
          box-shadow: none;
        }

        /* Orb glow ring - subtle in idle, enhanced when active */
        .tara-orb::before {
          content: '';
          position: absolute;
          inset: -4px;
          border-radius: 50%;
          background: conic-gradient(from 0deg, transparent, rgba(59, 130, 246, 0.1), transparent);
          opacity: 0;
          transition: opacity 0.4s ease;
          filter: blur(8px);
        }

        .tara-orb.listening::before,
        .tara-orb.talking::before,
        .tara-orb.executing::before {
          opacity: 1;
          animation: tara-orb-glow-rotate 3s linear infinite;
        }

        @keyframes tara-orb-glow-rotate {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }

        .tara-orb-inner {
          position: absolute;
          inset: 0;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
          box-shadow:
            0 4px 20px rgba(0, 0, 0, 0.4),
            inset 0 2px 4px rgba(255, 255, 255, 0.1),
            inset 0 -2px 4px rgba(0, 0, 0, 0.2);
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

        /* === ORB STATES === */
        .tara-orb.idle .tara-orb-inner img {
          filter: brightness(0.85) saturate(0.85);
        }

        .tara-orb.listening .tara-orb-inner {
          box-shadow:
            0 4px 24px rgba(59, 130, 246, 0.3),
            0 0 60px rgba(59, 130, 246, 0.15),
            inset 0 2px 4px rgba(255, 255, 255, 0.15),
            inset 0 -2px 4px rgba(0, 0, 0, 0.2);
        }

        .tara-orb.listening .tara-orb-inner img {
          filter: brightness(1.1) saturate(1.15);
          animation: tara-orb-pulse 2s ease-in-out infinite;
        }

        .tara-orb.talking .tara-orb-inner {
          box-shadow:
            0 4px 24px rgba(147, 197, 253, 0.35),
            0 0 60px rgba(147, 197, 253, 0.2),
            inset 0 2px 4px rgba(255, 255, 255, 0.2),
            inset 0 -2px 4px rgba(0, 0, 0, 0.2);
        }

        .tara-orb.talking .tara-orb-inner img {
          filter: brightness(1.2) saturate(1.25);
          animation: tara-orb-speak 1s ease-in-out infinite;
        }

        .tara-orb.executing .tara-orb-inner {
          box-shadow:
            0 4px 24px rgba(251, 191, 36, 0.25),
            0 0 60px rgba(251, 191, 36, 0.12),
            inset 0 2px 4px rgba(255, 255, 255, 0.15),
            inset 0 -2px 4px rgba(0, 0, 0, 0.2);
        }

        .tara-orb.executing .tara-orb-inner img {
          filter: brightness(1.15) saturate(1.15);
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

        /* === SCREEN-WIDE BLUE GLOW OVERLAY (Active States) === */
        .tara-screen-overlay {
          position: fixed;
          inset: 0;
          pointer-events: none;
          z-index: 999998;
          opacity: 0;
          transition: opacity 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .tara-screen-overlay.active {
          opacity: 1;
        }

        /* Ambient blue glow around screen edges */
        .tara-screen-overlay::before {
          content: '';
          position: fixed;
          inset: 0;
          border-radius: 0;
          box-shadow:
            inset 0 0 120px rgba(59, 130, 246, 0.08),
            inset 0 0 60px rgba(59, 130, 246, 0.05);
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .tara-screen-overlay.active::before {
          opacity: 1;
        }

        /* Animated border glow effect */
        .tara-screen-overlay::after {
          content: '';
          position: fixed;
          inset: 0;
          border: 2px solid transparent;
          border-image: linear-gradient(135deg, 
            rgba(59, 130, 246, 0) 0%,
            rgba(59, 130, 246, 0.3) 25%,
            rgba(147, 197, 253, 0.4) 50%,
            rgba(59, 130, 246, 0.3) 75%,
            rgba(59, 130, 246, 0) 100%
          ) 1;
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.6s cubic-bezier(0.16, 1, 0.3, 1);
          animation: tara-screen-glow-pulse 3s ease-in-out infinite;
        }

        .tara-screen-overlay.active::after {
          opacity: 1;
        }

        @keyframes tara-screen-glow-pulse {
          0%, 100% { 
            border-image: linear-gradient(135deg, 
              rgba(59, 130, 246, 0) 0%,
              rgba(59, 130, 246, 0.2) 25%,
              rgba(147, 197, 253, 0.3) 50%,
              rgba(59, 130, 246, 0.2) 75%,
              rgba(59, 130, 246, 0) 100%
            ) 1;
          }
          50% { 
            border-image: linear-gradient(225deg, 
              rgba(59, 130, 246, 0) 0%,
              rgba(59, 130, 246, 0.35) 25%,
              rgba(147, 197, 253, 0.45) 50%,
              rgba(59, 130, 246, 0.35) 75%,
              rgba(59, 130, 246, 0) 100%
            ) 1;
          }
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

        /* === DARK CHAT BAR === */
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
            transition: opacity 0.4s cubic-bezier(0.16, 1, 0.3, 1),
                        transform 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .tara-chat-bar.visible {
            opacity: 1;
            transform: translateX(-50%) translateY(0);
        }

        /* Messages panel - dark glassmorphism */
        .tara-chat-messages-panel {
            max-height: 380px;
            overflow-y: auto;
            display: none;
            flex-direction: column;
            gap: 12px;
            padding: 16px 20px;
            background: rgba(10, 10, 15, 0.92);
            backdrop-filter: blur(40px) saturate(1.6);
            -webkit-backdrop-filter: blur(40px) saturate(1.6);
            border: 1px solid rgba(255,255,255,0.06);
            border-bottom: none;
            border-radius: 20px 20px 0 0;
            box-shadow: 0 -20px 60px rgba(0,0,0,0.4);
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,0.08) transparent;
        }
        .tara-chat-messages-panel::-webkit-scrollbar { width: 4px; }
        .tara-chat-messages-panel::-webkit-scrollbar-track { background: transparent; }
        .tara-chat-messages-panel::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
        .tara-chat-messages-panel.has-messages {
            display: flex;
        }

        /* Input container - dark theme */
        .tara-chat-input-bar {
            background: rgba(17, 17, 24, 0.95);
            backdrop-filter: blur(40px) saturate(1.6);
            -webkit-backdrop-filter: blur(40px) saturate(1.6);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 24px;
            padding: 6px 8px 6px 20px;
            display: flex;
            align-items: center;
            gap: 8px;
            box-shadow:
                0 16px 60px rgba(0,0,0,0.5),
                0 4px 20px rgba(0,0,0,0.25),
                inset 0 1px 1px rgba(255,255,255,0.04);
            transition: border-color 0.3s ease, box-shadow 0.3s ease;
        }
        .tara-chat-messages-panel.has-messages + .tara-chat-input-bar {
            border-radius: 0 0 24px 24px;
            border-top: 1px solid rgba(255,255,255,0.04);
        }
        .tara-chat-input-bar:focus-within {
            border-color: rgba(59, 130, 246, 0.4);
            box-shadow:
                0 16px 60px rgba(0,0,0,0.5),
                0 0 0 3px rgba(59, 130, 246, 0.08),
                inset 0 1px 1px rgba(255,255,255,0.04);
        }
        .tara-chat-input-bar input {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            color: rgba(255,255,255,0.95);
            font-size: 15px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            padding: 10px 0;
        }
        .tara-chat-input-bar input::placeholder {
            color: rgba(255,255,255,0.35);
        }

        /* Mic button - dark theme */
        .tara-chat-mic {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: rgba(255,255,255,0.05);
            border: none;
            color: rgba(255,255,255,0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            flex-shrink: 0;
        }
        .tara-chat-mic:hover {
            background: rgba(255,255,255,0.1);
            color: rgba(255,255,255,0.85);
        }
        .tara-chat-mic.active {
            background: rgba(59, 130, 246, 0.2);
            color: #60a5fa;
            animation: tara-mic-pulse 2s ease-in-out infinite;
        }
        @keyframes tara-mic-pulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0.3); }
            50% { box-shadow: 0 0 0 8px rgba(59,130,246,0); }
        }

        /* Send button - blue accent */
        .tara-chat-send-btn {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            border: none;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            flex-shrink: 0;
            opacity: 0.4;
        }
        .tara-chat-send-btn.has-text { opacity: 1; }
        .tara-chat-send-btn.has-text:hover {
            transform: scale(1.08);
            box-shadow: 0 4px 16px rgba(59,130,246,0.35);
        }

        /* Mode badge - dark theme */
        .tara-mode-badge {
            font-size: 11px;
            padding: 3px 10px;
            border-radius: 12px;
            background: rgba(255,255,255,0.05);
            color: rgba(255,255,255,0.45);
            white-space: nowrap;
            flex-shrink: 0;
        }
        .tara-mode-badge.interactive { color: rgba(96,165,250,0.85); background: rgba(59,130,246,0.12); }
        .tara-mode-badge.turbo { color: rgba(251,191,36,0.85); background: rgba(251,191,36,0.12); }

        /* Message bubbles - dark theme */
        .tara-msg {
            max-width: 85%;
            padding: 10px 14px;
            border-radius: 14px;
            font-size: 13.5px;
            line-height: 1.55;
            color: rgba(255,255,255,0.95);
            animation: tara-msg-appear 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .tara-msg.user {
            align-self: flex-end;
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white;
            border-bottom-right-radius: 4px;
        }
        .tara-msg.ai {
            align-self: flex-start;
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.04);
            border-bottom-left-radius: 4px;
        }
        @keyframes tara-msg-appear {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Typing indicator - dark theme */
        .tara-typing-indicator {
            align-self: flex-start;
            padding: 12px 18px;
            background: rgba(255,255,255,0.04);
            border-radius: 14px;
            display: flex;
            gap: 5px;
            align-items: center;
        }
        .tara-typing-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            background: rgba(255,255,255,0.35);
            animation: tara-typing-bounce 1.4s ease-in-out infinite;
        }
        .tara-typing-dot:nth-child(2) { animation-delay: 0.2s; }
        .tara-typing-dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes tara-typing-bounce {
            0%, 60%, 100% { transform: translateY(0); opacity: 0.35; }
            30% { transform: translateY(-6px); opacity: 0.9; }
        }

        /* === DARK MODE SELECTOR DIALOG === */
        .tara-mode-selector {
            position: fixed;
            inset: 0;
            z-index: 100003;
            display: none;
            align-items: center;
            justify-content: center;
            background: rgba(0,0,0,0.6);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            opacity: 0;
            transition: opacity 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            pointer-events: auto;
        }
        .tara-mode-selector.visible { opacity: 1; }
        .tara-mode-selector-card {
            background: rgba(13, 13, 20, 0.95);
            backdrop-filter: blur(40px) saturate(1.6);
            -webkit-backdrop-filter: blur(40px) saturate(1.6);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 24px;
            padding: 32px;
            width: 380px;
            box-shadow:
                0 24px 80px rgba(0,0,0,0.6),
                0 0 0 1px rgba(255,255,255,0.02);
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
            border: 1px solid rgba(255,255,255,0.06);
            background: rgba(255,255,255,0.03);
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            margin-bottom: 10px;
            text-align: left;
        }
        .tara-mode-option:hover {
            background: rgba(255,255,255,0.06);
            border-color: rgba(59, 130, 246, 0.3);
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(59, 130, 246, 0.1);
        }
        .tara-mode-option-icon {
            width: 44px; height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .tara-mode-option-icon.interactive { background: rgba(59, 130, 246, 0.15); }
        .tara-mode-option-icon.turbo { background: rgba(251, 191, 36, 0.15); }
        .tara-mode-option-label {
            color: rgba(255,255,255,0.95);
            font-size: 15px;
            font-weight: 500;
        }
        .tara-mode-option-desc {
            color: rgba(255,255,255,0.4);
            font-size: 12px;
            margin-top: 2px;
        }

        /* === DARK ANALYSE PAGE STRIP === */
        .tara-analyse-strip {
            display: none;
            flex-direction: column;
            gap: 0;
            width: 240px;
            background: rgba(13, 13, 20, 0.95);
            backdrop-filter: blur(40px) saturate(1.8);
            -webkit-backdrop-filter: blur(40px) saturate(1.8);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 18px;
            padding: 16px;
            box-shadow:
                0 20px 60px rgba(0, 0, 0, 0.6),
                0 4px 24px rgba(0, 0, 0, 0.4),
                inset 0 1px 0 rgba(255, 255, 255, 0.04);
            opacity: 0;
            transform: translateY(-8px) scale(0.97);
            transition:
                opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1),
                transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
            pointer-events: auto;
        }
        .tara-analyse-strip.visible {
            opacity: 1;
            transform: translateY(0) scale(1);
        }

        /* Header row */
        .tara-analyse-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 10px;
        }
        .tara-analyse-eyebrow {
            display: flex;
            align-items: center;
            gap: 5px;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: rgba(96, 165, 250, 0.9);
        }
        .tara-analyse-close {
            width: 22px;
            height: 22px;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.06);
            color: rgba(255, 255, 255, 0.4);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            padding: 0;
        }
        .tara-analyse-close:hover {
            background: rgba(255, 255, 255, 0.08);
            color: rgba(255, 255, 255, 0.8);
        }

        /* Label */
        .tara-analyse-label {
            font-size: 12.5px;
            color: rgba(255, 255, 255, 0.5);
            margin: 0 0 14px 0;
            line-height: 1.4;
        }

        /* Option buttons row */
        .tara-analyse-options {
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin-bottom: 14px;
        }
        .tara-analyse-btn {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 11px 12px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.03);
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            text-align: left;
            position: relative;
            overflow: hidden;
        }
        .tara-analyse-btn::before {
            content: '';
            position: absolute;
            inset: 0;
            opacity: 0;
            transition: opacity 0.2s ease;
            border-radius: 12px;
        }
        .tara-analyse-btn.dom::before {
            background: linear-gradient(135deg, rgba(59, 130, 246, 0.1), rgba(37, 99, 235, 0.06));
        }
        .tara-analyse-btn.vision::before {
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.1), rgba(124, 58, 237, 0.06));
        }
        .tara-analyse-btn:hover::before { opacity: 1; }
        .tara-analyse-btn:hover {
            border-color: rgba(255, 255, 255, 0.1);
            transform: translateX(2px);
        }
        .tara-analyse-btn.dom:hover {
            border-color: rgba(59, 130, 246, 0.3);
            box-shadow: 0 4px 16px rgba(59, 130, 246, 0.12);
        }
        .tara-analyse-btn.vision:hover {
            border-color: rgba(139, 92, 246, 0.35);
            box-shadow: 0 4px 16px rgba(139, 92, 246, 0.15);
        }

        /* Icon badge */
        .tara-analyse-btn-icon {
            width: 34px;
            height: 34px;
            border-radius: 9px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: transform 0.2s ease;
        }
        .tara-analyse-btn:hover .tara-analyse-btn-icon {
            transform: scale(1.08);
        }
        .tara-analyse-btn-icon.dom {
            background: rgba(59, 130, 246, 0.12);
            color: rgba(96, 165, 250, 0.95);
        }
        .tara-analyse-btn-icon.vision {
            background: rgba(139, 92, 246, 0.14);
            color: rgba(167, 139, 250, 0.95);
        }

        /* Text block */
        .tara-analyse-btn-text {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 1px;
        }
        .tara-analyse-btn-title {
            font-size: 13px;
            font-weight: 600;
            color: rgba(255, 255, 255, 0.95);
            letter-spacing: -0.1px;
        }
        .tara-analyse-btn-desc {
            font-size: 10.5px;
            color: rgba(255, 255, 255, 0.4);
        }

        /* Arrow indicator */
        .tara-analyse-btn-arrow {
            font-size: 14px;
            color: rgba(255, 255, 255, 0.2);
            transition: all 0.2s ease;
            flex-shrink: 0;
        }
        .tara-analyse-btn:hover .tara-analyse-btn-arrow {
            color: rgba(255, 255, 255, 0.6);
            transform: translateX(2px);
        }

        /* Divider */
        .tara-analyse-divider {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 10px;
        }
        .tara-analyse-divider-line {
            flex: 1;
            height: 1px;
            background: rgba(255, 255, 255, 0.06);
        }
        .tara-analyse-divider-text {
            font-size: 10px;
            color: rgba(255, 255, 255, 0.3);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        /* "Start full session" button - blue accent */
        .tara-analyse-start-session {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 7px;
            width: 100%;
            padding: 10px 14px;
            border-radius: 11px;
            border: 1px solid rgba(59, 130, 246, 0.25);
            background: rgba(59, 130, 246, 0.08);
            color: rgba(96, 165, 250, 0.95);
            font-size: 12.5px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            letter-spacing: 0.01em;
        }
        .tara-analyse-start-session:hover {
            background: rgba(59, 130, 246, 0.16);
            border-color: rgba(59, 130, 246, 0.45);
            color: rgba(147, 197, 253, 1);
            box-shadow: 0 4px 20px rgba(59, 130, 246, 0.2);
        }
      `;
    }

    window.TARA.Styles = { getStyles };
    console.log('✅ [TARA] Styles module loaded');
})();
