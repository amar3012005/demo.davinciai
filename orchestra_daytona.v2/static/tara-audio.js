/**
 * TARA Visual Co-Pilot — Audio Manager
 * Module: tara-audio.js
 *
 * Gapless PCM audio playback with debounced end-of-playback detection.
 * Depends on: nothing (standalone)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    class AudioManager {
        constructor() {
            this.audioCtx = null;
            this.isInitialized = false;
            this.nextPlayTime = 0;
            this.isPlaying = false;
            this.activeSources = new Set();
            this.sampleRate = 44100;
            this._endDebounce = null;
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
                    this.nextPlayTime = now + 0.02;
                    if (this.onStart) this.onStart();
                }

                // Schedule playback back-to-back (Gapless Stitching)
                const startAt = Math.max(now, this.nextPlayTime);
                source.start(startAt);
                this.nextPlayTime = startAt + buffer.duration;

                this.activeSources.add(source);
                source.onended = () => {
                    this.activeSources.delete(source);
                    if (this.activeSources.size === 0) {
                        // Debounce: wait 500ms before declaring playback ended
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

    window.TARA.AudioManager = AudioManager;
    console.log('✅ [TARA] AudioManager module loaded');
})();
