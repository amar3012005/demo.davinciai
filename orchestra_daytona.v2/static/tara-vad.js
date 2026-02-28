/**
 * TARA Visual Co-Pilot — Voice Activity Detector
 * Module: tara-vad.js
 *
 * Energy-based VAD with configurable thresholds.
 * Depends on: tara-config.js (window.TARA.Config)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};
    const Config = () => window.TARA.Config;

    class VoiceActivityDetector {
        constructor(onSpeechStart, onSpeechEnd) {
            this.onSpeechStart = onSpeechStart;
            this.onSpeechEnd = onSpeechEnd;
            this.isSpeaking = false;

            const vad = Config().vad || {};
            this.energyThreshold = vad.energyThreshold || 0.018;
            this.silenceThreshold = vad.silenceThreshold || 0.015;
            this.minSpeechDuration = vad.minSpeechDuration || 250;
            this.silenceTimeoutMs = vad.silenceTimeout || 1000;

            this.silenceTimeout = null;
            this.speechStartTime = null;
            this.totalEnergy = 0;
            this.sampleCount = 0;
            this.locked = false;
        }

        processAudioChunk(float32Array) {
            if (this.locked) return 0;

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
                    }, this.silenceTimeoutMs);
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

    window.TARA.VoiceActivityDetector = VoiceActivityDetector;
    console.log('✅ [TARA] VAD module loaded');
})();
