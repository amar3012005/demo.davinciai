"use client";

import { useRef, useState, useEffect, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Play, PhoneOff, MessageSquare, Clock } from "lucide-react";
import dynamic from "next/dynamic";
import { useTheme } from "@/context/ThemeContext";
import { apiFetch } from "@/lib/api";

// Dynamic import to avoid SSR issues with Three.js
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Orb = dynamic<any>(() => import("@/components/ui/orb").then(mod => mod.Orb), {
    ssr: false,
    loading: () => (
        <div style={{
            width: '100%',
            height: '100%',
            borderRadius: '50%',
            backgroundColor: 'rgba(255,255,255,0.03)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center'
        }} />
    )
});

// ═══════════════════════════════════════════════════════════
// Types
// ═══════════════════════════════════════════════════════════

interface TranscriptMessage {
    id: string;
    role: 'user' | 'agent';
    text: string;
    timestamp: number;
    isFinal: boolean;
}

interface CallMetrics {
    latency: number;
    logprob: number;
    noSpeech: number;
    ratio: number;
    chunks: number;
    ttft: number;
    ttfc: number;
}

interface AudioChainNodes {
    gain: GainNode;
    highpass: BiquadFilterNode;
    lowpass: BiquadFilterNode;
}

type AgentState = 'listening' | 'talking' | 'thinking' | null;

// ═══════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════

const CALL_LIMIT = 300; // 5 minutes
const CALL_WARNING_TIME = 270; // 30s warning

const getWsBaseUrl = (): string => {
    return `wss://demo.davinciai.eu:8030/ws`;
};

// ═══════════════════════════════════════════════════════════
// HUD Components
// ═══════════════════════════════════════════════════════════

function HUDMetric({ label, value, unit = '', delay = 0 }: {
    label: string;
    value: string | number;
    unit?: string;
    color?: string;
    delay?: number;
}): React.ReactElement {
    const { theme } = useTheme();
    const isDark = theme === 'dark';

    return (
        <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay, duration: 0.3 }}
            style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '2px',
                padding: '8px 10px',
                backgroundColor: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.02)',
                border: isDark ? '1px solid rgba(255,255,255,0.06)' : '1px solid rgba(0,0,0,0.04)',
                borderRadius: '6px',
                minWidth: '70px',
            }}
        >
            <span style={{
                fontSize: '9px',
                fontWeight: 500,
                color: isDark ? '#666' : '#888',
                textTransform: 'uppercase',
                letterSpacing: '0.08em',
                fontFamily: 'JetBrains Mono, monospace'
            }}>
                {label}
            </span>
            <div style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: '3px'
            }}>
                <span style={{
                    fontSize: '13px',
                    fontWeight: 600,
                    color: isDark ? '#fff' : '#1a1a1a',
                    fontFamily: 'JetBrains Mono, monospace',
                    fontVariantNumeric: 'tabular-nums'
                }}>
                    {value}
                </span>
                {unit && (
                    <span style={{
                        fontSize: '10px',
                        color: isDark ? '#555' : '#999',
                        fontFamily: 'JetBrains Mono, monospace'
                    }}>
                        {unit}
                    </span>
                )}
            </div>
        </motion.div>
    );
}

function HUDStatus({ state, isDark }: { state: AgentState; isDark: boolean }): React.ReactElement | null {
    if (!state) return null;

    const config: Record<string, { color: string; label: string; sublabel: string }> = {
        listening: { color: '#22c55e', label: 'RX', sublabel: 'RECEIVING' },
        talking: { color: '#3b82f6', label: 'TX', sublabel: 'TRANSMITTING' },
        thinking: { color: '#f59e0b', label: 'PROC', sublabel: 'PROCESSING' }
    };

    const { color, label, sublabel } = config[state];

    return (
        <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                padding: '6px 10px',
                backgroundColor: `${color}08`,
                border: `1px solid ${color}20`,
                borderRadius: '4px',
            }}
        >
            <motion.div
                animate={state === 'listening' ? {
                    opacity: [1, 0.4, 1],
                } : state === 'talking' ? {
                    scale: [1, 1.2, 1],
                } : {
                    rotate: [0, 180, 360]
                }}
                transition={{ duration: state === 'thinking' ? 2 : 1.2, repeat: Infinity, ease: "easeInOut" }}
                style={{
                    width: '6px',
                    height: '6px',
                    borderRadius: '50%',
                    backgroundColor: color,
                    boxShadow: `0 0 8px ${color}`
                }}
            />
            <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
                <span style={{
                    fontSize: '10px',
                    fontWeight: 700,
                    color: color,
                    fontFamily: 'JetBrains Mono, monospace',
                    letterSpacing: '0.05em'
                }}>
                    {label}
                </span>
                <span style={{
                    fontSize: '8px',
                    color: isDark ? '#555' : '#888',
                    fontFamily: 'JetBrains Mono, monospace',
                    letterSpacing: '0.05em'
                }}>
                    {sublabel}
                </span>
            </div>
        </motion.div>
    );
}

// ═══════════════════════════════════════════════════════════
// Main Component
// ═══════════════════════════════════════════════════════════

export default function AIAssistantPanel({
    agentId,
    fallbackAgent,
}: {
    agentId: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    fallbackAgent?: any;
}): React.ReactElement {
    const { theme } = useTheme();
    const isDark = theme === 'dark';
    const [isMounted, setIsMounted] = useState(false);

    useEffect(() => {
        setIsMounted(true);
    }, []);

    // ── Call State ──────────────────────────────────────────
    const [isCallActive, setIsCallActive] = useState(false);
    const [agentState, setAgentState] = useState<AgentState>(null);
    const [callDuration, setCallDuration] = useState(0);
    const [userVolume, setUserVolume] = useState(0);
    const [agentIsSpeaking, setAgentIsSpeaking] = useState(false);
    const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | null>(null);
    const [micStream, setMicStream] = useState<MediaStream | null>(null);
    const [metrics, setMetrics] = useState<CallMetrics>({
        latency: 0, logprob: 0, noSpeech: 0, ratio: 0, chunks: 0, ttft: 0, ttfc: 0
    });

    // ── Transcript State ───────────────────────────────────
    const [transcripts, setTranscripts] = useState<TranscriptMessage[]>([]);
    const [currentTranscript, setCurrentTranscript] = useState<string>('');
    const transcriptContainerRef = useRef<HTMLDivElement>(null);

    // ── WebSocket & Audio Refs ──────────────────────────────
    const wsRef = useRef<WebSocket | null>(null);
    const audioCtxRef = useRef<AudioContext | null>(null);
    const wsConnectedRef = useRef(false);
    const audioWorkletRef = useRef<ScriptProcessorNode | null>(null);
    const binaryQueueRef = useRef<ArrayBuffer[]>([]);
    const lastPlaybackTimeRef = useRef(0);
    const playbackStartTimeRef = useRef<number | null>(null);
    const audioStreamCompleteRef = useRef(false);
    const playedAudioThisTurnRef = useRef(false);
    const audioConfigRef = useRef({ format: 'pcm_s16le', sampleRate: 16000 });
    const currentPlaybackTurnIdRef = useRef<number | null>(null);
    const minAcceptedPlaybackTurnIdRef = useRef(0);
    const activeSourcesRef = useRef(new Set<AudioBufferSourceNode>());
    const outputGainRef = useRef<GainNode | null>(null);
    const telephonyHighpassRef = useRef<BiquadFilterNode | null>(null);
    const telephonyLowpassRef = useRef<BiquadFilterNode | null>(null);
    const callTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const animationFrameRef = useRef<number | null>(null);
    const sessionIdRef = useRef<string | null>(null);

    // ── Turn Timing Refs ───────────────────────────────────
    const turnStartTimeRef = useRef<number | null>(null);
    const hasTtftForTurnRef = useRef(false);
    const hasTtfcForTurnRef = useRef(false);
    const currentAgentMessageRef = useRef<string>("");

    // ── Agent Data ─────────────────────────────────────────
    const [agentData, setAgentData] = useState<Record<string, unknown> | null>(null);
    const [isLoadingAgent, setIsLoadingAgent] = useState(true);

    // ── Demo Mode ──────────────────────────────────────────
    const [loginMode, setLoginMode] = useState<string>('demo');

    // ── Resolved config (mirrors TaraVoiceWidget config) ───
    const config = useMemo(() => {
        let tenantId = 'davinci';
        if (typeof window !== 'undefined') {
            try {
                const stored = localStorage.getItem('tenant');
                if (stored) {
                    const info = JSON.parse(stored);
                    tenantId = info?.subdomain || info?.tenant_id || 'davinci';
                }
            } catch {
                // ignore parse errors
            }
        }

        const agent = agentData || fallbackAgent || {};
        return {
            tenantId,
            agentId: (agent as any).agent_id as string || agentId || 'davinci',
            agentName: (agent as any).agent_name as string || 'DAVINCIAI',
            language: (agent as any).language_primary as string || 'de',
        };
    }, [agentData, fallbackAgent, agentId]);

    // ── Fetch Agent Data ───────────────────────────────────
    useEffect(() => {
        if (!agentId) return;

        const fetchAgent = async (): Promise<void> => {
            if (agentId === "agent-demo-001") {
                setAgentData(fallbackAgent as any || null);
                setIsLoadingAgent(false);
                return;
            }

            try {
                const response = await apiFetch(`/api/agents/${agentId}`);
                if (!response.ok) {
                    if (fallbackAgent) setAgentData(fallbackAgent as any);
                    return;
                }
                const data = await response.json();
                setAgentData(data);
            } catch {
                if (fallbackAgent) setAgentData(fallbackAgent as any);
            } finally {
                setIsLoadingAgent(false);
            }
        };

        fetchAgent();
    }, [agentId, fallbackAgent]);

    // ── Retrieve login mode ────────────────────────────────
    useEffect(() => {
        const storedLoginMode = localStorage.getItem('login_mode') || 'demo';
        setLoginMode(storedLoginMode);
    }, []);

    // ── Auto-scroll transcript ─────────────────────────────
    useEffect(() => {
        if (transcriptContainerRef.current) {
            transcriptContainerRef.current.scrollTop = transcriptContainerRef.current.scrollHeight;
        }
    }, [transcripts, currentTranscript]);

    // ── Sync agent state from connectionStatus + speaking ──
    useEffect(() => {
        if (connectionStatus === 'connected') {
            setAgentState(agentIsSpeaking ? 'talking' : 'listening');
        } else if (connectionStatus === 'connecting') {
            setAgentState('thinking');
        } else if (!isCallActive) {
            setAgentState(null);
        }
    }, [agentIsSpeaking, connectionStatus, isCallActive]);

    // ── Demo call limit enforcement ────────────────────────
    useEffect(() => {
        if (isCallActive && callDuration >= CALL_LIMIT) {
            endCall();
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [callDuration, isCallActive]);

    // ── Volume Analyzer for Responsive Orb ─────────────────
    useEffect(() => {
        if (!micStream) return;

        const ac = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
        const an = ac.createAnalyser();
        const src = ac.createMediaStreamSource(micStream);
        src.connect(an);
        an.fftSize = 256;

        const arr = new Uint8Array(an.frequencyBinCount);
        const up = (): void => {
            an.getByteFrequencyData(arr);
            let s = 0;
            for (let i = 0; i < arr.length; i++) s += arr[i];
            setUserVolume(Math.min(1, s / arr.length / 30));
            animationFrameRef.current = requestAnimationFrame(up);
        };
        up();

        return () => {
            if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
            ac.close();
        };
    }, [micStream]);

    // ═══════════════════════════════════════════════════════
    // VOICE LOGIC — Direct port from TaraVoiceWidget.jsx
    // ═══════════════════════════════════════════════════════

    const checkPlaybackComplete = useCallback((): void => {
        if (!audioCtxRef.current) return;
        if (audioCtxRef.current.currentTime >= lastPlaybackTimeRef.current - 0.1) {
            setAgentIsSpeaking(false);
            if (
                audioStreamCompleteRef.current &&
                playedAudioThisTurnRef.current &&
                wsRef.current?.readyState === WebSocket.OPEN
            ) {
                const duration = playbackStartTimeRef.current ? Date.now() - playbackStartTimeRef.current : 0;
                wsRef.current.send(JSON.stringify({
                    type: 'playback_done',
                    duration_ms: duration,
                    playback_turn_id: currentPlaybackTurnIdRef.current,
                    timestamp: Date.now() / 1000
                }));
                playbackStartTimeRef.current = null;
                audioStreamCompleteRef.current = false;
                playedAudioThisTurnRef.current = false;
                currentPlaybackTurnIdRef.current = null;
            }
        }
    }, []);

    const ensureOutputChain = useCallback((): AudioChainNodes | null => {
        if (!audioCtxRef.current) return null;
        const ctx = audioCtxRef.current;
        if (!outputGainRef.current) outputGainRef.current = ctx.createGain();
        if (!telephonyHighpassRef.current) {
            telephonyHighpassRef.current = ctx.createBiquadFilter();
            telephonyHighpassRef.current.type = 'highpass';
            telephonyHighpassRef.current.frequency.value = 250;
        }
        if (!telephonyLowpassRef.current) {
            telephonyLowpassRef.current = ctx.createBiquadFilter();
            telephonyLowpassRef.current.type = 'lowpass';
            telephonyLowpassRef.current.frequency.value = 3400;
        }
        return { gain: outputGainRef.current, highpass: telephonyHighpassRef.current, lowpass: telephonyLowpassRef.current };
    }, []);

    const stopPlayback = useCallback((): void => {
        if (Number.isFinite(currentPlaybackTurnIdRef.current)) {
            minAcceptedPlaybackTurnIdRef.current = Math.max(
                minAcceptedPlaybackTurnIdRef.current,
                (currentPlaybackTurnIdRef.current || 0) + 1
            );
        }
        for (const source of activeSourcesRef.current) {
            try { source.onended = null; source.stop(); } catch (_) { /* noop */ }
        }
        activeSourcesRef.current.clear();
        binaryQueueRef.current = [];
        currentPlaybackTurnIdRef.current = null;
        audioStreamCompleteRef.current = false;
        playedAudioThisTurnRef.current = false;
        playbackStartTimeRef.current = null;
        if (audioCtxRef.current) lastPlaybackTimeRef.current = audioCtxRef.current.currentTime;
        setAgentIsSpeaking(false);
    }, []);

    const playAudioChunk = useCallback((data: ArrayBuffer | string, forceInt16 = false): void => {
        let f32: Float32Array;
        const fmt = audioConfigRef.current.format;
        const sr = audioConfigRef.current.sampleRate;

        if (data instanceof ArrayBuffer) {
            if (fmt === 'pcm_s16le' || forceInt16) {
                const i16 = new Int16Array(data);
                f32 = new Float32Array(i16.length);
                for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;
            } else {
                f32 = new Float32Array(data);
            }
        } else {
            const bs = atob(data);
            const by = new Uint8Array(bs.length);
            for (let i = 0; i < bs.length; i++) by[i] = bs.charCodeAt(i);
            if (fmt === 'pcm_s16le' || forceInt16) {
                const i16 = new Int16Array(by.buffer);
                f32 = new Float32Array(i16.length);
                for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;
            } else {
                f32 = new Float32Array(by.buffer);
            }
        }

        if (audioCtxRef.current) {
            playedAudioThisTurnRef.current = true;
            const buf = audioCtxRef.current.createBuffer(1, f32.length, sr);
            buf.copyToChannel(f32 as Float32Array<ArrayBuffer>, 0);
            const s = audioCtxRef.current.createBufferSource();
            s.buffer = buf;

            const chain = ensureOutputChain();
            if (!chain) return;

            chain.gain.gain.value = 1.0;
            try { chain.gain.disconnect(); chain.highpass.disconnect(); chain.lowpass.disconnect(); s.disconnect(); } catch (_) { /* noop */ }

            s.connect(chain.gain);
            chain.gain.connect(audioCtxRef.current.destination);

            const now = audioCtxRef.current.currentTime;
            let at = lastPlaybackTimeRef.current;
            // Initial buffer offset (50ms) for first chunk
            if (!playbackStartTimeRef.current) {
                playbackStartTimeRef.current = Date.now();
                at = now + 0.05;
            }
            // Drift correction: jump to current time if falling behind
            if (at < now) at = now;

            activeSourcesRef.current.add(s);
            s.onended = () => { activeSourcesRef.current.delete(s); checkPlaybackComplete(); };
            s.start(at);
            lastPlaybackTimeRef.current = at + buf.duration;
        }
    }, [checkPlaybackComplete, ensureOutputChain]);

    const endCall = useCallback((): void => {
        if (wsRef.current) {
            if (wsRef.current.readyState === WebSocket.OPEN) {
                wsRef.current.send(JSON.stringify({ type: 'interrupt', timestamp: Date.now() / 1000 }));
            }
            wsRef.current.close();
            wsRef.current = null;
        }

        stopPlayback();

        if (audioCtxRef.current) {
            audioCtxRef.current.close();
            audioCtxRef.current = null;
        }
        outputGainRef.current = null;
        telephonyHighpassRef.current = null;
        telephonyLowpassRef.current = null;

        if (callTimerRef.current) { clearInterval(callTimerRef.current); callTimerRef.current = null; }
        if (micStream) micStream.getTracks().forEach(t => t.stop());

        setIsCallActive(false);
        setConnectionStatus(null);
        setAgentIsSpeaking(false);
        wsConnectedRef.current = false;
        setAgentState(null);
        setMicStream(null);
        setCallDuration(0);
        setTranscripts([]);
        setCurrentTranscript('');
        currentAgentMessageRef.current = '';
        setMetrics({ latency: 0, logprob: 0, noSpeech: 0, ratio: 0, chunks: 0, ttft: 0, ttfc: 0 });
    }, [micStream, stopPlayback]);

    // ── Helper to mark turn start ──────────────────────────
    const markTurnStart = useCallback((): void => {
        turnStartTimeRef.current = performance.now();
        hasTtftForTurnRef.current = false;
        hasTtfcForTurnRef.current = false;
    }, []);

    // ── Start Voice Call (direct port from TaraVoiceWidget) ─
    const startVoiceCall = useCallback((stream: MediaStream): void => {
        setConnectionStatus('connecting');
        setCallDuration(0);
        setTranscripts([]);
        setCurrentTranscript('');

        const base = getWsBaseUrl();
        const nws = String(base).replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
        const uid = 'user_' + Date.now();
        const wsUrl = `${nws}?tenant_id=${encodeURIComponent(config.tenantId)}&agent_id=${encodeURIComponent(config.agentId)}&session_type=webcall&user_id=${encodeURIComponent(uid)}&agent_name=${encodeURIComponent(config.agentName)}`;

        const ws = new WebSocket(wsUrl);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;

        ws.onopen = (): void => {
            wsConnectedRef.current = true;
            sessionIdRef.current = 'session_' + Date.now();

            const sessionConfig = {
                type: 'session_config',
                config: {
                    mode: 'voice',
                    tenant_id: config.tenantId,
                    agent_id: config.agentId,
                    agent_name: config.agentName,
                    user_id: uid,
                    stt_mode: 'audio',
                    tts_mode: 'audio',
                    language: config.language
                }
            };
            ws.send(JSON.stringify(sessionConfig));

            ws.send(JSON.stringify({
                type: 'start_session',
                flow_config: {
                    policy_mode: 'sales',
                    conversation_policy: 'sales',
                    policy_flags: {
                        enable_strategic_policy: true,
                        enable_stage_aware_retrieval: true,
                        enable_micro_reasoning: true
                    }
                },
                timestamp: Date.now() / 1000
            }));

            // Playback AudioContext at 16000 sampleRate
            const ac = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)({ sampleRate: 16000 });
            audioCtxRef.current = ac;
            lastPlaybackTimeRef.current = ac.currentTime;

            // Mic capture AudioContext at 16000 sampleRate
            const mic = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)({ sampleRate: 16000 });
            const src = mic.createMediaStreamSource(stream);
            const proc = mic.createScriptProcessor(2048, 1, 1);
            proc.onaudioprocess = (e: AudioProcessingEvent): void => {
                if (ws.readyState === WebSocket.OPEN && wsConnectedRef.current) {
                    const inp = e.inputBuffer.getChannelData(0);
                    const pcm = new Int16Array(inp.length);
                    for (let i = 0; i < inp.length; i++) {
                        pcm[i] = Math.max(-1, Math.min(1, inp[i])) * 0x7FFF;
                    }
                    ws.send(pcm.buffer);
                }
            };
            src.connect(proc);
            proc.connect(mic.destination);
            audioWorkletRef.current = proc;
        };

        ws.onmessage = (e: MessageEvent): void => {
            // Binary audio data → queue for later dequeue on audio_chunk
            if (e.data instanceof ArrayBuffer) {
                binaryQueueRef.current.push(e.data);
                return;
            }

            const d = JSON.parse(e.data as string);

            // ── session_ready / state_update listening → mark connected ──
            if (d.type === 'session_ready' || (d.type === 'state_update' && d.state === 'listening')) {
                wsConnectedRef.current = true;
                if (d.audio_format || d.format) audioConfigRef.current.format = d.audio_format || d.format;
                if (d.sample_rate) audioConfigRef.current.sampleRate = d.sample_rate;

                if (connectionStatus !== 'connected') {
                    setConnectionStatus('connected');
                    setIsCallActive(true);
                    if (!callTimerRef.current) {
                        callTimerRef.current = setInterval(() => setCallDuration(x => x + 1), 1000);
                    }
                }
            }

            // ── state_update thinking/interrupt/listening → stopPlayback ──
            if (d.type === 'state_update' && (d.state === 'thinking' || d.state === 'interrupt' || d.state === 'listening')) {
                stopPlayback();
            }

            // ── transcript handling (enterprise feature) ──
            if (d.type === 'transcript') {
                if (d.is_final && d.text && d.text.trim()) {
                    markTurnStart();
                    setTranscripts(prev => [...prev, {
                        id: crypto.randomUUID(),
                        role: 'user',
                        text: d.text,
                        timestamp: Date.now(),
                        isFinal: true
                    }]);
                    setCurrentTranscript('');
                } else if (d.text && !d.is_final) {
                    setCurrentTranscript(d.text);
                }
                setMetrics(prev => ({
                    ...prev,
                    latency: d.latency_ms || prev.latency,
                    logprob: d.avg_logprob || prev.logprob,
                    noSpeech: d.no_speech_prob || prev.noSpeech,
                    ratio: d.compression_ratio || prev.ratio,
                    chunks: prev.chunks + 1
                }));
            }

            // ── agent_response handling (enterprise feature) ──
            if (d.type === 'agent_response') {
                if (d.text && d.text.trim()) {
                    if (d.is_streaming) {
                        if (turnStartTimeRef.current && !hasTtftForTurnRef.current) {
                            const ttft = Math.round(performance.now() - turnStartTimeRef.current);
                            setMetrics(prev => ({ ...prev, ttft }));
                            hasTtftForTurnRef.current = true;
                        }
                        currentAgentMessageRef.current = d.text;
                        setCurrentTranscript(currentAgentMessageRef.current);
                    } else {
                        setTranscripts(prev => [...prev, {
                            id: crypto.randomUUID(),
                            role: 'agent' as const,
                            text: d.text,
                            timestamp: Date.now(),
                            isFinal: true
                        }]);
                        currentAgentMessageRef.current = '';
                        setCurrentTranscript('');
                    }
                }
            }

            // ── audio_chunk → dequeue binary or play base64 ──
            if (d.type === 'audio_chunk') {
                const turnId = Number(d.playback_turn_id);
                if (Number.isFinite(turnId)) {
                    if (turnId < minAcceptedPlaybackTurnIdRef.current) {
                        if (d.binary_sent && binaryQueueRef.current.length > 0) binaryQueueRef.current.shift();
                        return;
                    }
                    currentPlaybackTurnIdRef.current = turnId;
                }
                if (d.sample_rate) audioConfigRef.current.sampleRate = d.sample_rate;

                const hasBinaryAudio = Boolean(d.binary_sent && binaryQueueRef.current.length > 0);
                const hasInlineAudio = Boolean(d.data || d.audio);
                const hasAudioPayload = hasBinaryAudio || hasInlineAudio;

                if (hasAudioPayload) {
                    setAgentIsSpeaking(true);
                    audioStreamCompleteRef.current = false;
                }

                // TTFC metric
                if (hasAudioPayload && turnStartTimeRef.current && !hasTtfcForTurnRef.current) {
                    const ttfc = Math.round(performance.now() - turnStartTimeRef.current);
                    setMetrics(prev => ({ ...prev, ttfc }));
                    hasTtfcForTurnRef.current = true;
                }

                if (hasBinaryAudio) {
                    const c = binaryQueueRef.current.shift();
                    if (c) playAudioChunk(c, audioConfigRef.current.format === 'pcm_s16le');
                } else if (hasInlineAudio) {
                    const a = d.data || d.audio;
                    if (a) playAudioChunk(a);
                }

                if (d.is_final) {
                    audioStreamCompleteRef.current = true;
                    checkPlaybackComplete();
                }
            }

            // ── explicit audio_complete only ──
            if (d.type === 'audio_complete') {
                audioStreamCompleteRef.current = true;
                checkPlaybackComplete();
            }

            // ── interrupt / clear / playback_stop ──
            if (d.type === 'interrupt' || d.type === 'clear' || d.type === 'playback_stop') {
                stopPlayback();
            }

            // ── ping → pong ──
            if (d.type === 'ping' && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now() / 1000 }));
            }
        };

        ws.onclose = (): void => { endCall(); };
        ws.onerror = (): void => { endCall(); };
    }, [config, connectionStatus, checkPlaybackComplete, playAudioChunk, stopPlayback, endCall, markTurnStart]);

    // ── Start Call (get mic then start WS) ─────────────────
    const startCall = useCallback(async (): Promise<void> => {
        if (!agentData) return;
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 16000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });
            setMicStream(stream);
            startVoiceCall(stream);
        } catch (err) {
            if (typeof window !== 'undefined') {
                window.alert("Please enable microphone access to use voice chat");
            }
        }
    }, [agentData, startVoiceCall]);

    // ═══════════════════════════════════════════════════════
    // Formatting helpers
    // ═══════════════════════════════════════════════════════

    const formatDuration = (seconds: number): string => {
        const mins = Math.floor(seconds / 60);
        const secs = seconds % 60;
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    const remaining = CALL_LIMIT - callDuration;
    const isWarning = remaining <= 30 && isCallActive;

    // ═══════════════════════════════════════════════════════
    // Loading State
    // ═══════════════════════════════════════════════════════

    if (!isMounted || isLoadingAgent || !agentData) {
        return (
            <div style={{
                backgroundColor: isDark ? '#111' : '#fff',
                borderRadius: '12px',
                padding: '20px',
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                border: isDark ? '1px solid #222' : '1px solid #e5e5e5',
            }}>
                <motion.div
                    animate={{ opacity: [0.4, 0.8, 0.4] }}
                    transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
                    style={{
                        width: '120px',
                        height: '120px',
                        borderRadius: '50%',
                        backgroundColor: isDark ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.03)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: '12px',
                        fontWeight: 500,
                        color: isDark ? '#444' : '#999',
                        fontFamily: 'JetBrains Mono, monospace'
                    }}
                >
                    {!isMounted || isLoadingAgent ? "INITIALIZING..." : "ERROR"}
                </motion.div>
            </div>
        );
    }

    // ═══════════════════════════════════════════════════════
    // RENDER
    // ═══════════════════════════════════════════════════════

    return (
        <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.3 }}
            style={{
                backgroundColor: isDark ? '#0a0a0a' : '#fff',
                borderRadius: '12px',
                padding: '16px',
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                border: isDark ? '1px solid #111' : '1px solid #e5e5e5',
                position: 'relative',
                overflow: 'hidden'
            }}
        >
            {/* Header - Compact HUD Style */}
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '12px',
                paddingBottom: '12px',
                borderBottom: isDark ? '1px solid #222' : '1px solid #e5e5e5',
                position: 'relative',
                zIndex: 1
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <div style={{
                        padding: '4px 8px',
                        backgroundColor: isDark ? '#0a0a0a' : '#f5f5f5',
                        border: isDark ? '1px solid #333' : '1px solid #ddd',
                        borderRadius: '4px',
                        fontSize: '10px',
                        fontWeight: 700,
                        color: isDark ? '#666' : '#888',
                        fontFamily: 'JetBrains Mono, monospace',
                        letterSpacing: '0.1em'
                    }}>
                        SYS.AI
                    </div>
                    <div>
                        <h3 style={{
                            fontSize: '14px',
                            fontWeight: 600,
                            color: isDark ? '#fff' : '#1a1a1a',
                            margin: 0,
                            letterSpacing: '0.02em'
                        }}>
                            {(agentData as any)?.agent_name as string || "TARA"}
                        </h3>
                    </div>
                </div>

                <AnimatePresence mode="wait">
                    {isCallActive ? (
                        <motion.div
                            key="active"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '6px',
                                padding: '4px 10px',
                                backgroundColor: isDark ? 'rgba(34, 197, 94, 0.08)' : 'rgba(34, 197, 94, 0.05)',
                                border: `1px solid ${isDark ? 'rgba(34, 197, 94, 0.2)' : 'rgba(34, 197, 94, 0.15)'}`,
                                borderRadius: '4px',
                                color: '#22c55e'
                            }}
                        >
                            <Clock size={12} />
                            <motion.span
                                animate={loginMode === 'demo' && callDuration >= CALL_WARNING_TIME ? {
                                    color: ['#ef4444', '#f87171', '#ef4444']
                                } : {}}
                                transition={{ duration: 1, repeat: Infinity }}
                                style={{
                                    fontSize: '12px',
                                    fontWeight: 600,
                                    fontFamily: 'JetBrains Mono, monospace',
                                    fontVariantNumeric: 'tabular-nums'
                                }}
                            >
                                {formatDuration(callDuration)}
                            </motion.span>
                            {loginMode === 'demo' && (
                                <span style={{ fontSize: '9px', opacity: 0.6, fontFamily: 'JetBrains Mono, monospace' }}>/5:00</span>
                            )}
                            {isWarning && (
                                <span style={{ fontSize: '9px', color: '#ef4444', fontWeight: 700, fontFamily: 'JetBrains Mono, monospace' }}>
                                    {remaining}s
                                </span>
                            )}
                        </motion.div>
                    ) : (
                        <motion.div
                            key="inactive"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '6px',
                                padding: '4px 10px',
                                backgroundColor: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.02)',
                                border: isDark ? '1px solid #333' : '1px solid #ddd',
                                borderRadius: '4px',
                                color: isDark ? '#666' : '#888'
                            }}
                        >
                            <div style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: '#666' }} />
                            <span style={{ fontSize: '11px', fontWeight: 500, fontFamily: 'JetBrains Mono, monospace' }}>STANDBY</span>
                        </motion.div>
                    )}
                </AnimatePresence>
            </div>

            {/* Main Content */}
            <div style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '12px',
                position: 'relative',
                zIndex: 1
            }}>
                {/* Orb Container - 180px */}
                <div style={{
                    width: '180px',
                    height: '180px',
                    position: 'relative',
                    borderRadius: '50%',
                    backgroundColor: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.015)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    border: isDark ? '1px solid rgba(255,255,255,0.05)' : '1px solid rgba(0,0,0,0.05)',
                }}>
                    {/* HUD Ring Decoration */}
                    <div style={{
                        position: 'absolute',
                        inset: '-4px',
                        borderRadius: '50%',
                        border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                        pointerEvents: 'none'
                    }} />
                    <div style={{
                        position: 'absolute',
                        inset: '-8px',
                        borderRadius: '50%',
                        border: `1px dashed ${isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)'}`,
                        pointerEvents: 'none'
                    }} />

                    <motion.div
                        animate={agentState === 'talking' ? {
                            scale: [1, 1.02, 1],
                        } : {}}
                        transition={{ duration: 0.8, repeat: Infinity, ease: "easeInOut" }}
                        style={{ width: '160px', height: '160px' }}
                    >
                        <Orb
                            agentState={agentState}
                            volumeMode="manual"
                            manualInput={userVolume}
                            manualOutput={agentIsSpeaking ? (0.6 + Math.random() * 0.4) : 0}
                            colors={["#A63E1B", "#EBE5DF"]}
                        />
                    </motion.div>
                </div>

                {/* Status Row */}
                <div style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    flexWrap: 'wrap',
                    justifyContent: 'center'
                }}>
                    <HUDStatus state={agentState} isDark={isDark} />
                </div>

                {/* HUD Metrics Row */}
                {isCallActive && (
                    <motion.div
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        style={{
                            display: 'flex',
                            gap: '6px',
                            flexWrap: 'wrap',
                            justifyContent: 'center',
                            width: '100%'
                        }}
                    >
                        <HUDMetric label="TTFC" value={metrics.ttfc} unit="ms" delay={0} />
                        <HUDMetric label="Latency" value={metrics.latency} unit="ms" delay={0.05} />
                        <HUDMetric label="Duration" value={formatDuration(callDuration)} delay={0.1} />
                        <HUDMetric label="Chunks" value={metrics.chunks} delay={0.15} />
                    </motion.div>
                )}

                {/* Transcript Area */}
                <div style={{
                    flex: 1,
                    width: '100%',
                    backgroundColor: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.02)',
                    borderRadius: '8px',
                    padding: '10px',
                    border: isDark ? '1px solid #1a1a1a' : '1px solid #e5e5e5',
                    overflow: 'hidden',
                    display: 'flex',
                    flexDirection: 'column'
                }}>
                    <div style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        marginBottom: '8px',
                        paddingBottom: '8px',
                        borderBottom: isDark ? '1px solid #1a1a1a' : '1px solid #e5e5e5'
                    }}>
                        <MessageSquare size={12} color={isDark ? '#444' : '#888'} />
                        <span style={{
                            fontSize: '10px',
                            fontWeight: 600,
                            textTransform: 'uppercase',
                            letterSpacing: '0.08em',
                            color: isDark ? '#666' : '#888',
                            fontFamily: 'JetBrains Mono, monospace'
                        }}>
                            LOG
                        </span>
                    </div>

                    <div
                        ref={transcriptContainerRef}
                        style={{
                            flex: 1,
                            overflowY: 'auto',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '6px'
                        }}
                    >
                        {transcripts.length === 0 && !currentTranscript && !isCallActive && (
                            <div style={{
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                height: '100%',
                                color: isDark ? '#444' : '#aaa',
                                fontSize: '12px',
                                textAlign: 'center',
                                fontFamily: 'JetBrains Mono, monospace'
                            }}>
                                AWAITING INPUT...
                            </div>
                        )}

                        {/* Show only the last transcript (current turn) */}
                        {transcripts.length > 0 && (
                            <motion.div
                                key={transcripts[transcripts.length - 1].id}
                                initial={{ opacity: 0, x: transcripts[transcripts.length - 1].role === 'user' ? -10 : 10 }}
                                animate={{ opacity: 1, x: 0 }}
                                transition={{ type: "spring", stiffness: 300, damping: 25 }}
                                style={{
                                    alignSelf: transcripts[transcripts.length - 1].role === 'user' ? 'flex-start' : 'flex-end',
                                    maxWidth: '90%',
                                    padding: '8px 10px',
                                    borderRadius: transcripts[transcripts.length - 1].role === 'user' ? '6px 6px 6px 2px' : '6px 6px 2px 6px',
                                    backgroundColor: transcripts[transcripts.length - 1].role === 'user'
                                        ? (isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)')
                                        : (isDark ? 'rgba(59, 130, 246, 0.12)' : 'rgba(59, 130, 246, 0.08)'),
                                    border: transcripts[transcripts.length - 1].role === 'user'
                                        ? (isDark ? '1px solid rgba(255,255,255,0.04)' : '1px solid rgba(0,0,0,0.04)')
                                        : '1px solid rgba(59, 130, 246, 0.15)',
                                    color: isDark ? '#fff' : '#1a1a1a',
                                    fontSize: '12px',
                                    lineHeight: 1.4,
                                    wordBreak: 'break-word'
                                }}
                            >
                                <span style={{
                                    fontSize: '9px',
                                    color: isDark ? '#555' : '#888',
                                    fontFamily: 'JetBrains Mono, monospace',
                                    marginRight: '6px',
                                    textTransform: 'uppercase'
                                }}>
                                    {transcripts[transcripts.length - 1].role === 'user' ? '>' : '<'}
                                </span>
                                {transcripts[transcripts.length - 1].text}
                            </motion.div>
                        )}

                        {currentTranscript && (
                            <motion.div
                                initial={{ opacity: 0, y: 5 }}
                                animate={{ opacity: 1, y: 0 }}
                                style={{
                                    alignSelf: agentIsSpeaking ? 'flex-end' : 'flex-start',
                                    maxWidth: '90%',
                                    padding: '8px 10px',
                                    borderRadius: agentIsSpeaking ? '6px 6px 2px 6px' : '6px 6px 6px 2px',
                                    backgroundColor: agentIsSpeaking
                                        ? (isDark ? 'rgba(59, 130, 246, 0.08)' : 'rgba(59, 130, 246, 0.05)')
                                        : (isDark ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)'),
                                    border: agentIsSpeaking
                                        ? '1px dashed rgba(59, 130, 246, 0.2)'
                                        : (isDark ? '1px dashed rgba(255,255,255,0.06)' : '1px dashed rgba(0,0,0,0.06)'),
                                    color: isDark ? '#aaa' : '#666',
                                    fontSize: '12px',
                                    lineHeight: 1.4,
                                    wordBreak: 'break-word'
                                }}
                            >
                                <span style={{
                                    fontSize: '9px',
                                    color: isDark ? '#555' : '#888',
                                    fontFamily: 'JetBrains Mono, monospace',
                                    marginRight: '6px'
                                }}>
                                    {agentIsSpeaking ? '<' : '>'}
                                </span>
                                {currentTranscript}
                                <motion.span
                                    animate={{ opacity: [0.4, 1, 0.4] }}
                                    transition={{ duration: 1, repeat: Infinity }}
                                    style={{ marginLeft: '4px' }}
                                >
                                    |
                                </motion.span>
                            </motion.div>
                        )}
                    </div>
                </div>
            </div>

            {/* Call Button */}
            <div style={{
                marginTop: '12px',
                paddingTop: '12px',
                borderTop: isDark ? '1px solid #222' : '1px solid #e5e5e5',
                position: 'relative',
                zIndex: 1
            }}>
                <AnimatePresence mode="wait">
                    {!isCallActive ? (
                        <motion.button
                            key="start"
                            onClick={startCall}
                            whileHover={{ scale: 1.01 }}
                            whileTap={{ scale: 0.98 }}
                            transition={{ type: "spring", stiffness: 400, damping: 25 }}
                            style={{
                                width: '100%',
                                padding: '12px 20px',
                                backgroundColor: isDark ? '#fff' : '#0a0a0a',
                                color: isDark ? '#0a0a0a' : '#fff',
                                fontSize: '12px',
                                fontWeight: 600,
                                textTransform: 'uppercase',
                                letterSpacing: '0.1em',
                                border: 'none',
                                borderRadius: '6px',
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: '8px',
                                fontFamily: 'JetBrains Mono, monospace'
                            }}
                        >
                            <Play size={14} fill="currentColor" />
                            INITIATE
                        </motion.button>
                    ) : (
                        <motion.button
                            key="end"
                            onClick={endCall}
                            whileHover={{ scale: 1.01 }}
                            whileTap={{ scale: 0.98 }}
                            transition={{ type: "spring", stiffness: 400, damping: 25 }}
                            style={{
                                width: '100%',
                                padding: '12px 20px',
                                backgroundColor: '#ef4444',
                                color: '#fff',
                                fontSize: '12px',
                                fontWeight: 600,
                                textTransform: 'uppercase',
                                letterSpacing: '0.1em',
                                border: 'none',
                                borderRadius: '6px',
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: '8px',
                                fontFamily: 'JetBrains Mono, monospace'
                            }}
                        >
                            <PhoneOff size={14} />
                            TERMINATE
                        </motion.button>
                    )}
                </AnimatePresence>
            </div>
        </motion.div>
    );
}
