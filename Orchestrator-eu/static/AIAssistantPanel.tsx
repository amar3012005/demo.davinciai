"use client";

import { useRef, useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Play, PhoneOff, Activity, Zap, MessageSquare, Mic, Volume2, Clock } from "lucide-react";
import dynamic from "next/dynamic";
import { useTheme } from "@/context/ThemeContext";
import { apiFetch } from "@/lib/api";

// Dynamic import to avoid SSR issues with Three.js
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

interface TranscriptMessage {
    id: string;
    role: 'user' | 'agent';
    text: string;
    timestamp: number;
    isFinal: boolean;
}

// HUD-style Metric Display
function HUDMetric({ label, value, unit = '', color = '#22c55e', delay = 0 }: {
    label: string;
    value: string | number;
    unit?: string;
    color?: string;
    delay?: number;
}) {
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

// HUD Status Indicator
function HUDStatus({ state, isDark }: { state: 'listening' | 'talking' | 'thinking' | null, isDark: boolean }) {
    if (!state) return null;

    const config = {
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

export default function AIAssistantPanel({
    agentId,
    fallbackAgent,
    layoutMode = 'panel'
}: {
    agentId: string;
    fallbackAgent?: any;
    layoutMode?: 'panel' | 'phone';
}) {
    const { theme } = useTheme();
    const isDark = theme === 'dark';
    const [isMounted, setIsMounted] = useState(false);

    useEffect(() => {
        setIsMounted(true);
    }, []);

    // Call State
    const [isCallActive, setIsCallActive] = useState(false);
    const [agentState, setAgentState] = useState<'listening' | 'talking' | 'thinking' | null>(null);
    const [callDuration, setCallDuration] = useState(0);
    const [userVolume, setUserVolume] = useState(0);
    const [metrics, setMetrics] = useState({
        latency: 0,
        logprob: 0,
        noSpeech: 0,
        ratio: 0,
        chunks: 0,
        ttft: 0,
        ttfc: 0
    });
    const [micStream, setMicStream] = useState<MediaStream | null>(null);

    // Transcript State
    const [transcripts, setTranscripts] = useState<TranscriptMessage[]>([]);
    const [currentTranscript, setCurrentTranscript] = useState<string>('');
    const transcriptContainerRef = useRef<HTMLDivElement>(null);

    // Voice Agent WebSocket
    const wsRef = useRef<WebSocket | null>(null);
    const audioWsRef = useRef<WebSocket | null>(null);
    const audioCtxRef = useRef<AudioContext | null>(null);
    const wsConnectedRef = useRef(false);
    const audioStreamActiveRef = useRef(false);
    const sessionIdRef = useRef<string | null>(null);
    const audioWorkletRef = useRef<ScriptProcessorNode | null>(null);
    const binaryQueueRef = useRef<ArrayBuffer[]>([]);
    const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | null>(null);
    const [agentIsSpeaking, setAgentIsSpeaking] = useState(false);
    const lastPlaybackTimeRef = useRef(0);
    const playbackStartTimeRef = useRef<number | null>(null);
    const audioStreamCompleteRef = useRef(false);
    // Match server audio format: pcm_s16le at 16000 Hz
    const audioConfigRef = useRef({ format: 'pcm_s16le', sampleRate: 16000 });
    const pingIntervalRef = useRef<NodeJS.Timeout | null>(null);

    // Audio Enhancements Refs
    const activeSourcesRef = useRef(new Set<AudioBufferSourceNode>());
    const outputGainRef = useRef<GainNode | null>(null);
    const telephonyHighpassRef = useRef<BiquadFilterNode | null>(null);
    const telephonyLowpassRef = useRef<BiquadFilterNode | null>(null);
    const minAcceptedPlaybackTurnIdRef = useRef(0);
    const currentPlaybackTurnIdRef = useRef<number | null>(null);

    const callTimerRef = useRef<NodeJS.Timeout | null>(null);
    const animationFrameRef = useRef<number | null>(null);

    const [agentData, setAgentData] = useState<any>(null);
    const [isLoadingAgent, setIsLoadingAgent] = useState(true);
    const isPhoneLayout = layoutMode === 'phone';

    // Turn Timing Refs
    const turnStartTimeRef = useRef<number | null>(null);
    const hasTtftForTurnRef = useRef(false);
    const hasTtfcForTurnRef = useRef(false);
    const agentResponseBufferRef = useRef<string>("");
    const currentAgentMessageRef = useRef<string>("");

    // Audio Chain Initialization
    const ensureOutputChain = () => {
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
    };

    const stopPlayback = () => {
        if (Number.isFinite(currentPlaybackTurnIdRef.current)) {
            minAcceptedPlaybackTurnIdRef.current = Math.max(minAcceptedPlaybackTurnIdRef.current, (currentPlaybackTurnIdRef.current || 0) + 1);
        }
        for (const source of activeSourcesRef.current) {
            try { 
                source.onended = null; 
                source.stop(); 
            } catch (_) { }
        }
        activeSourcesRef.current.clear();
        binaryQueueRef.current = [];
        currentPlaybackTurnIdRef.current = null;
        audioStreamCompleteRef.current = false;
        playbackStartTimeRef.current = null;
        if (audioCtxRef.current) {
            lastPlaybackTimeRef.current = audioCtxRef.current.currentTime;
        }
        setAgentIsSpeaking(false);
    };

    // Login Mode & Demo Call Limit
    const [loginMode, setLoginMode] = useState<string>('demo');
    const DEMO_CALL_LIMIT = 300;
    const DEMO_WARNING_TIME = 270;

    // Auto-scroll transcript
    useEffect(() => {
        if (transcriptContainerRef.current) {
            transcriptContainerRef.current.scrollTop = transcriptContainerRef.current.scrollHeight;
        }
    }, [transcripts, currentTranscript]);

    // Helper to mark turn start
    const markTurnStart = () => {
        turnStartTimeRef.current = performance.now();
        hasTtftForTurnRef.current = false;
        hasTtfcForTurnRef.current = false;
        agentResponseBufferRef.current = "";
    };

    const VOICE_API_KEY = process.env.NEXT_PUBLIC_VOICE_API_KEY || "sk_car_ChbYsPTQzZjruzRRPLy2zK";

    // Fetch Agent Data
    useEffect(() => {
        if (!agentId) return;

        const fetchAgent = async () => {
            if (agentId === "agent-demo-001") {
                setAgentData(fallbackAgent);
                setIsLoadingAgent(false);
                return;
            }

            try {
                const response = await apiFetch(`/api/agents/${agentId}`);
                if (!response.ok) {
                    if (fallbackAgent?.websocket_url) {
                        setAgentData(fallbackAgent);
                    }
                    return;
                }
                const data = await response.json();
                setAgentData(data);
            } catch (err) {
                if (fallbackAgent?.websocket_url) {
                    setAgentData(fallbackAgent);
                }
            } finally {
                setIsLoadingAgent(false);
            }
        };

        fetchAgent();
    }, [agentId, fallbackAgent]);

    // Retrieve login mode
    useEffect(() => {
        const storedLoginMode = localStorage.getItem('login_mode') || 'demo';
        setLoginMode(storedLoginMode);
    }, []);

    // Demo Mode Call Limit Enforcement
    useEffect(() => {
        if (loginMode === 'demo' && isCallActive) {
            if (callDuration === DEMO_WARNING_TIME) {
                console.warn('Demo call will end in 30 seconds');
            }
            if (callDuration >= DEMO_CALL_LIMIT) {
                endCall();
                console.warn('Demo call limit reached');
            }
        }
    }, [callDuration, loginMode, isCallActive]);

    // Volume Analyzer for Responsive Orb
    useEffect(() => {
        if (!micStream) return;

        const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
        const analyser = audioContext.createAnalyser();
        const source = audioContext.createMediaStreamSource(micStream);
        source.connect(analyser);
        analyser.fftSize = 256;

        const dataArray = new Uint8Array(analyser.frequencyBinCount);
        const updateVolume = () => {
            analyser.getByteFrequencyData(dataArray);
            let sum = 0;
            for (let i = 0; i < dataArray.length; i++) {
                sum += dataArray[i];
            }
            const average = sum / dataArray.length;
            setUserVolume(Math.min(1, average / 30));
            animationFrameRef.current = requestAnimationFrame(updateVolume);
        };

        updateVolume();

        return () => {
            if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
            audioContext.close();
        };
    }, [micStream]);

    // Sync agent state
    useEffect(() => {
        if (connectionStatus === 'connected') {
            setAgentState(agentIsSpeaking ? 'talking' : 'listening');
        } else if (connectionStatus === 'connecting') {
            setAgentState('thinking');
        } else {
            setAgentState(null);
        }
    }, [agentIsSpeaking, connectionStatus]);

    // Connect to dedicated audio WebSocket
    const connectAudioWebSocket = (sessionId: string, baseWsUrl: string, resolvedTenantId?: string) => {
        if (!sessionId || !baseWsUrl) {
            console.warn('⚠️ Cannot connect audio WebSocket: missing sessionId or baseWsUrl');
            return;
        }

        const baseUrl = baseWsUrl.replace(/\/ws\/?(\?.*)?$/, '');
        // Use the explicitly passed tenant ID, or try to parse from URL, or read from localStorage
        let tenantId: string = resolvedTenantId || '';
        if (!tenantId) {
            try { tenantId = new URL(baseWsUrl).searchParams.get('tenant_id') || ''; } catch { /* ignore */ }
        }
        if (!tenantId) {
            const stored = typeof window !== 'undefined' ? localStorage.getItem('tenant') : null;
            const info = stored ? JSON.parse(stored) : null;
            tenantId = info?.subdomain || info?.tenant_id || 'davinci';
        }
        const audioUrl = `${baseUrl}/stream?session_id=${encodeURIComponent(sessionId)}&tenant_id=${encodeURIComponent(tenantId)}`;

        console.log('🔊 Connecting dedicated audio WebSocket:', audioUrl);

        const audioWs = new WebSocket(audioUrl);
        audioWs.binaryType = 'arraybuffer';
        audioWsRef.current = audioWs;

        audioWs.onopen = () => {
            console.log('✅ Dedicated audio stream connected');
            audioStreamActiveRef.current = true;
        };

        audioWs.onmessage = (e) => {
            if (e.data instanceof ArrayBuffer) {
                if (!playbackStartTimeRef.current) playbackStartTimeRef.current = Date.now();
                setAgentIsSpeaking(true);
                audioStreamCompleteRef.current = false;

                if (turnStartTimeRef.current && !hasTtfcForTurnRef.current) {
                    const ttfc = Math.round(performance.now() - turnStartTimeRef.current);
                    setMetrics(prev => ({ ...prev, ttfc }));
                    hasTtfcForTurnRef.current = true;
                }

                playAudioChunk(e.data);
            } else {
                try {
                    const data = JSON.parse(e.data);
                    if (data.type === 'stream_complete') {
                        console.log('🎵 Audio stream marked complete');
                        audioStreamCompleteRef.current = true;
                        // For dedicated audio streams, we don't clear agentIsSpeaking here, 
                        // we let playAudioChunk's onended do it via checkPlaybackComplete
                    }
                } catch (err) {
                    console.warn('⚠️ Non-JSON message on audio stream:', e.data);
                }
            }
        };

        audioWs.onclose = (event) => {
            console.log(`🔌 Audio WebSocket closed: ${event.code}`);
            audioStreamActiveRef.current = false;
            audioWsRef.current = null;
        };

        audioWs.onerror = (event) => {
            console.error('❌ Audio WebSocket error:', event);
            audioStreamActiveRef.current = false;
        };
    };

    const startCall = async () => {
        if (!agentData) {
            console.warn("Agent data not loaded yet");
            return;
        }
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
            console.error("❌ Microphone access failed:", err);
            alert("Please enable microphone access to use voice chat");
        }
    };

    const startVoiceCall = async (stream: MediaStream) => {
        setConnectionStatus('connecting');
        setAgentState('thinking');
        setCallDuration(0);
        setTranscripts([]);
        setCurrentTranscript('');

        const customWsUrl = agentData.websocket_url;
        const cartesiaId = agentData.cartesia_agent_id;
        const selectedVoice = agentData.voice || agentData.voice_name || agentData.voice_id;
        const isCustomProtocol = !!customWsUrl && customWsUrl !== "not_set";
        const normalizedCustomWsUrl = isCustomProtocol
            ? String(customWsUrl).replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://')
            : customWsUrl;
        const baseUrl = normalizedCustomWsUrl ? String(normalizedCustomWsUrl).replace(/\/ws\/?(\?.*)?$/, '') : '';
        const wsUrlTemp = isCustomProtocol
            ? `${baseUrl}/ws`
            : `wss://api.cartesia.ai/agents/stream/${cartesiaId}?api_key=${VOICE_API_KEY}&cartesia-version=2025-04-16`;

        const userId = typeof window !== 'undefined' ? (localStorage.getItem('user_id') || localStorage.getItem('access_token') || "anonymous_user") : "anonymous_user";
        const storedTenant = typeof window !== 'undefined' ? localStorage.getItem('tenant') : null;
        const tenantInfo = storedTenant ? JSON.parse(storedTenant) : null;
        // Use subdomain for WebSocket/API compatibility (simple name, not UUID)
        const tenantId = tenantInfo?.subdomain || tenantInfo?.tenant_id || "davinci";
        const agentIdToUse = agentData?.agent_id || agentId || "tara";
        const agentName = agentData?.agent_name || "Tara AI";
        let defaultSessionId = crypto.randomUUID();

        const wsUrl = isCustomProtocol
            ? `${baseUrl}/ws?tenant_id=${encodeURIComponent(tenantId)}&agent_id=${encodeURIComponent(agentIdToUse)}&session_type=webcall&user_id=${encodeURIComponent(userId)}&agent_name=${encodeURIComponent(agentName)}&session_id=${encodeURIComponent(defaultSessionId)}`
            : wsUrlTemp;

        const ws = new WebSocket(wsUrl);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;

        ws.onopen = () => {
            wsConnectedRef.current = true;
            const sessionId = crypto.randomUUID();
            sessionIdRef.current = sessionId;

            if (isCustomProtocol) {
                console.log(`🚀 Handshake for ${agentName} (Tenant: ${tenantId})`);
                ws.send(JSON.stringify({
                    type: 'session_config',
                    config: {
                        mode: 'voice',
                        tenant_id: tenantId,
                        agent_id: agentIdToUse,
                        agent_name: agentName,
                        user_id: userId,
                        session_type: 'webcall',
                        stt_mode: 'audio',
                        tts_mode: 'audio',
                        language: agentData.language_primary || 'de',
                    }
                }));

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
                // NOTE: Audio streams through main /ws, not separate /stream endpoint
                // connectAudioWebSocket(sessionId, wsUrlTemp, tenantId);
            } else {
                ws.send(JSON.stringify({
                    event: "start",
                    config: { input_format: "pcm_44100" }
                }));
            }

            // Use 16000 sample rate for both playback and recording (matches server config)
            const audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 16000 });
            audioCtxRef.current = audioCtx;
            lastPlaybackTimeRef.current = audioCtx.currentTime;

            // Audio capture - MUST match working implementation exactly
            const mic = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 16000 });
            const src = mic.createMediaStreamSource(stream);
            const proc = mic.createScriptProcessor(2048, 1, 1);
            proc.onaudioprocess = (e) => {
                // Send audio only if WebSocket is connected
                if (ws.readyState === WebSocket.OPEN && wsConnectedRef.current) {
                    const inp = e.inputBuffer.getChannelData(0);
                    const pcm = new Int16Array(inp.length);
                    for (let i = 0; i < inp.length; i++) {
                        pcm[i] = Math.max(-1, Math.min(1, inp[i])) * 0x7FFF;
                    }
                    // Send raw binary audio buffer directly
                    ws.send(pcm.buffer);
                }
            };
            src.connect(proc);
            proc.connect(mic.destination);
            audioWorkletRef.current = proc;

            if (!isCustomProtocol) {
                pingIntervalRef.current = setInterval(() => {
                    if (ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: "ping" }));
                    }
                }, 20000);
            }
        };

        ws.onmessage = async (e) => {
            if (e.data instanceof ArrayBuffer) {
                binaryQueueRef.current.push(e.data);
                return;
            }

            const data = JSON.parse(e.data);

            if (isCustomProtocol) {
                if (data.type === 'session_ready' || (data.type === 'state_update' && data.state === 'listening')) {
                    wsConnectedRef.current = true;

                    if (data.audio_format || data.format) {
                        audioConfigRef.current.format = data.audio_format || data.format;
                    }
                    if (data.sample_rate) {
                        audioConfigRef.current.sampleRate = data.sample_rate;
                    }

                    if (connectionStatus !== 'connected') {
                        setConnectionStatus('connected');
                        setIsCallActive(true);
                        setAgentState('listening');
                        if (!callTimerRef.current) {
                            callTimerRef.current = setInterval(() => setCallDuration(d => d + 1), 1000);
                        }
                    }
                } else if (data.type === 'transcript') {
                    if (data.is_final && data.text && data.text.trim()) {
                        markTurnStart();
                        setTranscripts(prev => [...prev, {
                            id: crypto.randomUUID(),
                            role: 'user',
                            text: data.text,
                            timestamp: Date.now(),
                            isFinal: true
                        }]);
                        setCurrentTranscript('');
                    } else if (data.text && !data.is_final) {
                        setCurrentTranscript(data.text);
                    }
                    setMetrics(prev => ({
                        ...prev,
                        latency: data.latency_ms || prev.latency,
                        logprob: data.avg_logprob || prev.logprob,
                        noSpeech: data.no_speech_prob || prev.noSpeech,
                        ratio: data.compression_ratio || prev.ratio,
                        chunks: prev.chunks + 1
                    }));
                } else if (data.type === 'agent_response') {
                    if (data.text && data.text.trim()) {
                        if (data.is_streaming) {
                            if (turnStartTimeRef.current && !hasTtftForTurnRef.current) {
                                const ttft = Math.round(performance.now() - turnStartTimeRef.current);
                                setMetrics(prev => ({ ...prev, ttft }));
                                hasTtftForTurnRef.current = true;
                            }
                            // One turn at a time: if it's a new response start, or we're streaming
                            currentAgentMessageRef.current = data.text; // Update with latest chunk for current turn
                            setCurrentTranscript(currentAgentMessageRef.current);
                        } else {
                            const newAgentMsg = {
                                id: crypto.randomUUID(),
                                role: 'agent' as const,
                                text: data.text,
                                timestamp: Date.now(),
                                isFinal: true
                            };
                            setTranscripts(prev => [...prev, newAgentMsg]);
                            currentAgentMessageRef.current = '';
                            setCurrentTranscript('');
                        }
                    }
                } else if (data.type === 'audio_chunk') {
                    const turnId = Number(data.playback_turn_id);
                    if (Number.isFinite(turnId)) {
                        if (turnId < minAcceptedPlaybackTurnIdRef.current) {
                            if (data.binary_sent && binaryQueueRef.current.length > 0) binaryQueueRef.current.shift();
                            return;
                        }
                        currentPlaybackTurnIdRef.current = turnId;
                    }

                    if (data.sample_rate) {
                        audioConfigRef.current.sampleRate = data.sample_rate;
                    }
                    if (data.format || data.audio_format) {
                        audioConfigRef.current.format = data.format || data.audio_format;
                    }

                    if (!playbackStartTimeRef.current) playbackStartTimeRef.current = Date.now();
                    setAgentIsSpeaking(true);
                    audioStreamCompleteRef.current = false;

                    if (turnStartTimeRef.current && !hasTtfcForTurnRef.current) {
                        const ttfc = Math.round(performance.now() - turnStartTimeRef.current);
                        setMetrics(prev => ({ ...prev, ttfc }));
                        hasTtfcForTurnRef.current = true;
                    }

                    if (isCustomProtocol && data.binary_sent && binaryQueueRef.current.length > 0) {
                        const binChunk = binaryQueueRef.current.shift();
                        if (binChunk) {
                            playAudioChunk(binChunk, audioConfigRef.current.format === 'pcm_s16le');
                        }
                    } else {
                        const audioB64 = data.data || data.audio;
                        if (audioB64) {
                            playAudioChunk(audioB64);
                        }
                    }

                    if (data.is_final) {
                        audioStreamCompleteRef.current = true;
                        checkPlaybackComplete();
                    }
                } else if (data.type === 'audio_complete' || data.is_final) {
                    audioStreamCompleteRef.current = true;
                    checkPlaybackComplete();
                } else if (data.type === 'interrupt' || data.type === 'clear' || data.type === 'playback_stop' || (data.type === 'state_update' && (data.state === 'listening' || data.state === 'thinking'))) {
                    stopPlayback();
                } else if (data.type === 'ping') {
                    if (ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now() / 1000 }));
                    }
                }
            } else {
                if (data.event === 'ack') {
                    wsConnectedRef.current = true;
                    setConnectionStatus('connected');
                    setIsCallActive(true);
                    setAgentState('listening');
                    if (!callTimerRef.current) {
                        callTimerRef.current = setInterval(() => setCallDuration(d => d + 1), 1000);
                    }
                } else if (data.event === 'media_output') {
                    setAgentIsSpeaking(true);
                    if (data.media?.payload) {
                        if (turnStartTimeRef.current && !hasTtfcForTurnRef.current) {
                            const ttfc = Math.round(performance.now() - turnStartTimeRef.current);
                            setMetrics(prev => ({ ...prev, ttfc }));
                            hasTtfcForTurnRef.current = true;
                        }
                        playAudioChunk(data.media.payload, true);
                    }
                } else if (data.type === "transcript") {
                    if (data.is_final && data.text && data.text.trim()) {
                        markTurnStart();
                        setTranscripts(prev => [...prev, {
                            id: crypto.randomUUID(),
                            role: 'user',
                            text: data.text,
                            timestamp: Date.now(),
                            isFinal: true
                        }]);
                        setCurrentTranscript('');
                    } else if (data.text && !data.is_final) {
                        setCurrentTranscript(data.text);
                    }
                } else if (data.event === 'clear') {
                    setAgentIsSpeaking(false);
                    lastPlaybackTimeRef.current = audioCtxRef.current?.currentTime || 0;
                }
            }
        };

        ws.onclose = (event) => {
            console.log(`🔌 WebSocket closed: ${event.code}`);
            endCall();
        };

        ws.onerror = (event) => {
            console.error('❌ WebSocket error occurred:', event);
            console.error("Connection error: voice orchestrator may be offline");
            endCall();
        };
    };

    const playAudioChunk = (data: string | ArrayBuffer, forceInt16 = false) => {
        let float32: Float32Array;
        const format = audioConfigRef.current.format;
        const sampleRate = audioConfigRef.current.sampleRate;

        if (data instanceof ArrayBuffer) {
            if (format === 'pcm_s16le' || forceInt16) {
                const int16 = new Int16Array(data);
                float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) {
                    float32[i] = int16[i] / 32768.0;
                }
            } else {
                float32 = new Float32Array(data);
            }
        } else {
            const binaryString = atob(data);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            if (format === 'pcm_s16le' || forceInt16) {
                const int16 = new Int16Array(bytes.buffer);
                float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) {
                    float32[i] = int16[i] / 32768.0;
                }
            } else {
                float32 = new Float32Array(bytes.buffer);
            }
        }

        if (audioCtxRef.current) {
            const buffer = audioCtxRef.current.createBuffer(1, float32.length, sampleRate);
            buffer.copyToChannel(float32 as any, 0);

            const source = audioCtxRef.current.createBufferSource();
            source.buffer = buffer;
            
            const chain = ensureOutputChain();
            if (!chain) return;

            // Apply "Telephony" style audio enhancement by default for the dashboard panel
            source.connect(chain.highpass);
            chain.highpass.connect(chain.lowpass);
            chain.lowpass.connect(chain.gain);
            chain.gain.connect(audioCtxRef.current.destination);

            const now = audioCtxRef.current.currentTime;
            let startAt = lastPlaybackTimeRef.current;
            
            // Drift correction & initial buffer
            if (!playbackStartTimeRef.current) {
                playbackStartTimeRef.current = Date.now();
                startAt = now + 0.05;
            }
            if (startAt < now) {
                startAt = now;
            }

            activeSourcesRef.current.add(source);
            source.onended = () => {
                activeSourcesRef.current.delete(source);
                checkPlaybackComplete();
            };
            
            source.start(startAt);
            lastPlaybackTimeRef.current = startAt + buffer.duration;
        }
    };

    const checkPlaybackComplete = () => {
        if (!audioCtxRef.current) return;
        if (audioCtxRef.current.currentTime >= lastPlaybackTimeRef.current - 0.1) {
            setAgentIsSpeaking(false);
            if (audioStreamCompleteRef.current && wsRef.current?.readyState === WebSocket.OPEN && agentData?.websocket_url && agentData.websocket_url !== "not_set") {
                const duration = playbackStartTimeRef.current ? Date.now() - playbackStartTimeRef.current : 0;
                wsRef.current.send(JSON.stringify({
                    type: 'playback_done',
                    duration_ms: duration,
                    playback_turn_id: currentPlaybackTurnIdRef.current,
                    timestamp: Date.now() / 1000
                }));
                playbackStartTimeRef.current = null;
                audioStreamCompleteRef.current = false;
                currentPlaybackTurnIdRef.current = null;
            }
        }
    };

    const endCall = async () => {
        const finalDuration = callDuration;
        const finalMetrics = { ...metrics };
        const finalAgentId = agentId;

        if (wsRef.current) {
            if (agentData?.websocket_url && agentData.websocket_url !== "not_set" && wsRef.current.readyState === WebSocket.OPEN) {
                wsRef.current.send(JSON.stringify({ type: 'interrupt', timestamp: Date.now() / 1000 }));
            }
            wsRef.current.close();
            wsRef.current = null;
        }

        if (audioWsRef.current) {
            audioWsRef.current.close();
            audioWsRef.current = null;
            audioStreamActiveRef.current = false;
        }

        stopPlayback();

        if (finalDuration > 0) {
            try {
                const response = await fetch(`/api/metrics`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        call_id: sessionIdRef.current,
                        agent_id: finalAgentId,
                        duration_seconds: finalDuration,
                        status: 'completed',
                        ttft_ms: finalMetrics.ttft,
                        ttfc_ms: finalMetrics.ttfc,
                        compression_ratio: finalMetrics.ratio,
                        cost_tokens: Math.ceil(finalDuration / 60) * 100
                    })
                });
                if (response.ok) {
                    console.log("AIAssistantPanel: Call metrics synced successfully");
                }
            } catch (err) {
                console.error("AIAssistantPanel: Failed to sync call metrics:", err);
            }
        }

        if (audioCtxRef.current) {
            audioCtxRef.current.close();
            audioCtxRef.current = null;
        }
        
        // Clean up audio enhancement nodes
        outputGainRef.current = null;
        telephonyHighpassRef.current = null;
        telephonyLowpassRef.current = null;

        if (pingIntervalRef.current) {
            clearInterval(pingIntervalRef.current);
            pingIntervalRef.current = null;
        }
        if (callTimerRef.current) {
            clearInterval(callTimerRef.current);
            callTimerRef.current = null;
        }
        if (micStream) {
            micStream.getTracks().forEach(track => track.stop());
        }

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
        setMetrics({
            latency: 0,
            logprob: 0,
            noSpeech: 0,
            ratio: 0,
            chunks: 0,
            ttft: 0,
            ttfc: 0
        });
    };

    const formatDuration = (seconds: number) => {
        const mins = Math.floor(seconds / 60);
        const secs = seconds % 60;
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

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
                            {agentData?.agent_name || "TARA"}
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
                                animate={loginMode === 'demo' && callDuration >= DEMO_WARNING_TIME ? {
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

            {/* Main Content - Compact Layout */}
            <div style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '12px',
                position: 'relative',
                zIndex: 1
            }}>
                {/* Orb Container - 180px as requested */}
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

                {/* Transcript Area - Compact */}
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
                                    ▊
                                </motion.span>
                            </motion.div>
                        )}
                    </div>
                </div>
            </div>

            {/* Call Button - Compact */}
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
