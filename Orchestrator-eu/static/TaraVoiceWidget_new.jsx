import React, { useRef, useState, useEffect, useMemo, Suspense, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';

// ═══════════════════════════════════════════════════════════
// ORB SHADER (Davinci Blue Theme)
// ═══════════════════════════════════════════════════════════

function splitmix32(a) {
    return function () {
        a |= 0; a = (a + 0x9e3779b9) | 0;
        let t = a ^ (a >>> 16); t = Math.imul(t, 0x21f0aaad);
        t = t ^ (t >>> 15); t = Math.imul(t, 0x735a2d97);
        return ((t = t ^ (t >>> 15)) >>> 0) / 4294967296;
    };
}
function clamp01(n) { return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0; }

const vertexShader = `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

const fragmentShader = `
uniform float uTime, uAnimation, uInverted, uInputVolume, uOutputVolume, uOpacity;
uniform float uOffsets[7]; uniform vec3 uColor1, uColor2; varying vec2 vUv;
const float PI = 3.14159265358979323846;
bool drawOval(vec2 pUv, vec2 pC, float a, float b, bool rev, float soft, out vec4 c){vec2 p=pUv-pC;float o=(p.x*p.x)/(a*a)+(p.y*p.y)/(b*b);float e=smoothstep(1.0,1.0-soft,o);if(e>0.0){float g=rev?(1.0-(p.x/a+1.0)/2.0):((p.x/a+1.0)/2.0);g=mix(0.5,g,0.1);c=vec4(vec3(g),0.85*e);return true;}return false;}
vec3 colorRamp(float g,vec3 c1,vec3 c2,vec3 c3,vec3 c4){if(g<0.33)return mix(c1,c2,g*3.0);else if(g<0.66)return mix(c2,c3,(g-0.33)*3.0);else return mix(c3,c4,(g-0.66)*3.0);}
vec2 hash2(vec2 p){return fract(sin(vec2(dot(p,vec2(127.1,311.7)),dot(p,vec2(269.5,183.3))))*43758.5453);}
float noise2D(vec2 p){vec2 i=floor(p);vec2 f=fract(p);vec2 u=f*f*(3.0-2.0*f);float n=mix(mix(dot(hash2(i+vec2(0.0,0.0)),f-vec2(0.0,0.0)),dot(hash2(i+vec2(1.0,0.0)),f-vec2(1.0,0.0)),u.x),mix(dot(hash2(i+vec2(0.0,1.0)),f-vec2(0.0,1.0)),dot(hash2(i+vec2(1.0,1.0)),f-vec2(1.0,1.0)),u.x),u.y);return 0.5+0.5*n;}
float sharpRing(vec3 d,float t){float ns=5.0;float n=mix(noise2D(vec2(d.x,t)*ns),noise2D(vec2(d.y,t)*ns),d.z);return 1.0+(n-0.5)*2.5*0.3*1.5;}
float smoothRing(vec3 d,float t){float ns=6.0;float n=mix(noise2D(vec2(d.x,t)*ns),noise2D(vec2(d.y,t)*ns),d.z);return 0.9+(n-0.5)*5.0*0.2;}
float flow(vec3 d,float t){return mix(noise2D(vec2(t,d.x/2.0)),noise2D(vec2(t,d.y/2.0)),d.z);}
void main(){vec2 uv=vUv*2.0-1.0;float r=length(uv);float th=atan(uv.y,uv.x);if(th<0.0)th+=2.0*PI;vec3 dec=vec3(th/(2.0*PI),mod(th/(2.0*PI)+0.5,1.0)+1.0,abs(th/PI-1.0));float n=flow(dec,r*0.03-uAnimation*0.2)-0.5;th+=n*mix(0.08,0.25,uOutputVolume);vec4 color=vec4(1.0);float oc[7]=float[7](0.0,0.5*PI,PI,1.5*PI,2.0*PI,2.5*PI,3.0*PI);float ct[7];for(int i=0;i<7;i++)ct[i]=oc[i]+0.5*sin(uTime/20.0+uOffsets[i]);float a,b;vec4 ov;for(int i=0;i<7;i++){float nn=noise2D(vec2(mod(ct[i]+uTime*0.05,1.0),0.5));a=0.5+nn*0.3;b=nn*mix(3.5,2.5,uInputVolume);bool rv=(i%2==1);float dt=min(abs(th-ct[i]),min(abs(th+2.0*PI-ct[i]),abs(th-2.0*PI-ct[i])));if(drawOval(vec2(dt,r),vec2(0.0,0.0),a,b,rv,0.6,ov)){color.rgb=mix(color.rgb,ov.rgb,ov.a);color.a=max(color.a,ov.a);}}float rr1=sharpRing(dec,uTime*0.1);float rr2=smoothRing(dec,uTime*0.1);float ir1=r+uInputVolume*0.2;float ir2=r+uInputVolume*0.15;float o1=mix(0.2,0.6,uInputVolume);float o2=mix(0.15,0.45,uInputVolume);float ra1=(ir2>=rr1)?o1:0.0;float ra2=smoothstep(rr2-0.05,rr2+0.05,ir1)*o2;color.rgb=1.0-(1.0-color.rgb)*(1.0-vec3(1.0)*max(ra1,ra2));vec3 c1=vec3(0.0);vec3 c4=vec3(1.0);float l=mix(color.r,1.0-color.r,uInverted);color.rgb=colorRamp(l,c1,uColor1,uColor2,c4);color.a*=uOpacity;gl_FragColor=color;}
`;

function OrbScene({ agentState, userVolume, agentIsSpeaking }) {
    useThree();
    const ref = useRef(null);
    // Davinci Blue Branding: #CADCFC and #A0B9D1
    const colors = ["#CADCFC", "#A0B9D1"];
    const initRef = useRef(colors);
    const tc1 = useRef(new THREE.Color(colors[0]));
    const tc2 = useRef(new THREE.Color(colors[1]));
    const spd = useRef(0.1);
    const agRef = useRef(agentState);
    const cIn = useRef(0), cOut = useRef(0);

    useEffect(() => { agRef.current = agentState; }, [agentState]);

    const rng = useMemo(() => splitmix32(Math.floor(Math.random() * 2 ** 32)), []);
    const off = useMemo(() => new Float32Array(Array.from({ length: 7 }, () => rng() * Math.PI * 2)), [rng]);

    useEffect(() => { if (ref.current) ref.current.material.uniforms.uInverted.value = 1; }, []);

    useFrame((_, dt) => {
        const m = ref.current?.material; if (!m) return;
        const u = m.uniforms;
        u.uTime.value += dt * 0.5;
        if (u.uOpacity.value < 1) u.uOpacity.value = Math.min(1, u.uOpacity.value + dt * 2);
        const t = u.uTime.value * 2;
        let tI = 0, tO = 0.3;
        if (agRef.current === 'listening') { tI = clamp01(userVolume > 0.1 ? userVolume : 0.55 + Math.sin(t * 3.2) * 0.35); tO = 0.45; }
        else if (agRef.current === 'talking') { tI = clamp01(0.65 + Math.sin(t * 4.8) * 0.22); tO = clamp01(0.75 + Math.sin(t * 3.6) * 0.22); }
        else if (agRef.current === 'thinking') { tI = clamp01(0.38 + 0.07 * Math.sin(t * 0.7) + 0.05 * Math.sin(t * 2.1) * Math.sin(t * 0.37 + 1.2)); tO = clamp01(0.48 + 0.12 * Math.sin(t * 1.05 + 0.6)); }
        cIn.current += (tI - cIn.current) * 0.2; cOut.current += (tO - cOut.current) * 0.2;
        spd.current += (0.1 + (1 - Math.pow(cOut.current - 1, 2)) * 0.9 - spd.current) * 0.12;
        u.uAnimation.value += dt * spd.current;
        u.uInputVolume.value = cIn.current; u.uOutputVolume.value = cOut.current;
        u.uColor1.value.lerp(tc1.current, 0.08); u.uColor2.value.lerp(tc2.current, 0.08);
    });

    const uniforms = useMemo(() => ({
        uColor1: new THREE.Uniform(new THREE.Color(initRef.current[0])),
        uColor2: new THREE.Uniform(new THREE.Color(initRef.current[1])),
        uOffsets: { value: off }, uTime: new THREE.Uniform(0), uAnimation: new THREE.Uniform(0.1),
        uInverted: new THREE.Uniform(1), uInputVolume: new THREE.Uniform(0),
        uOutputVolume: new THREE.Uniform(0), uOpacity: new THREE.Uniform(0),
    }), [off]);

    return (<mesh ref={ref}><circleGeometry args={[3.5, 64]} /><shaderMaterial uniforms={uniforms} fragmentShader={fragmentShader} vertexShader={vertexShader} transparent /></mesh>);
}

function OrbRenderer({ agentState, userVolume, agentIsSpeaking }) {
    return (
        <div style={{ width: '100%', height: '100%' }}>
            <Canvas resize={{ debounce: 100 }} gl={{ alpha: true, antialias: true, premultipliedAlpha: true }}>
                <Suspense fallback={null}><OrbScene agentState={agentState} userVolume={userVolume} agentIsSpeaking={agentIsSpeaking} /></Suspense>
            </Canvas>
        </div>
    );
}

// ═══════════════════════════════════════════════════════════
// TARA VOICE WIDGET — Davinci Blue Edition
// ═══════════════════════════════════════════════════════════

const getWsBaseUrl = () => {
    return 'wss://demo.davinciai.eu:8030/ws';
};

const CALL_LIMIT = 300;

const STATE_LABELS = { idle: 'Click to Start', listening: 'Listening...', talking: 'DA VINCI Speaking', thinking: 'Connecting...' };

const TaraVoiceWidget = ({ config: propConfig }) => {
    const config = useMemo(() => {
        const globalConfig = typeof window !== 'undefined' ? window.TaraWidgetConfig : {};
        return {
            tenantId: propConfig?.tenantId || globalConfig?.tenantId || 'davinci',
            agentId: propConfig?.agentId || globalConfig?.agentId || 'davinci',
            agentName: propConfig?.agentName || globalConfig?.agentName || 'DAVINCIAI',
            language: propConfig?.language || globalConfig?.language || 'de',
            accessKey: propConfig?.accessKey || globalConfig?.accessKey || '000000',
            region: propConfig?.region || globalConfig?.region || 'EU',
            ...propConfig
        };
    }, [propConfig]);

    const [isCallActive, setIsCallActive] = useState(false);
    const [agentState, setAgentState] = useState('idle');
    const [callDuration, setCallDuration] = useState(0);
    const [userVolume, setUserVolume] = useState(0);
    const [agentIsSpeaking, setAgentIsSpeaking] = useState(false);
    const [connectionStatus, setConnectionStatus] = useState(null);
    const [micStream, setMicStream] = useState(null);
    const [isMuted, setIsMuted] = useState(false);
    const [showCallSetup, setShowCallSetup] = useState(false);
    const [accessKeyInput, setAccessKeyInput] = useState('');
    const [accessError, setAccessError] = useState('');
    const [isAccessGranted, setIsAccessGranted] = useState(false);
    const [selectedCallMode, setSelectedCallMode] = useState('speaker');
    const [showEmailDialog, setShowEmailDialog] = useState(false);
    const [emailInput, setEmailInput] = useState('');

    const wsRef = useRef(null);
    const audioCtxRef = useRef(null);
    const wsConnectedRef = useRef(false);
    const audioWorkletRef = useRef(null);
    const binaryQueueRef = useRef([]);
    const lastPlaybackTimeRef = useRef(0);
    const playbackStartTimeRef = useRef(null);
    const audioStreamCompleteRef = useRef(false);
    const audioConfigRef = useRef({ format: 'pcm_f32le', sampleRate: 44100 });
    const currentPlaybackTurnIdRef = useRef(null);
    const minAcceptedPlaybackTurnIdRef = useRef(0);
    const activeSourcesRef = useRef(new Set());
    const outputGainRef = useRef(null);
    const telephonyHighpassRef = useRef(null);
    const telephonyLowpassRef = useRef(null);
    const callTimerRef = useRef(null);
    const animationFrameRef = useRef(null);
    const sessionIdRef = useRef(null);

    useEffect(() => {
        if (connectionStatus === 'connected') setAgentState(agentIsSpeaking ? 'talking' : 'listening');
        else if (connectionStatus === 'connecting') setAgentState('thinking');
        else if (!isCallActive) setAgentState('idle');
    }, [agentIsSpeaking, connectionStatus, isCallActive]);

    useEffect(() => {
        if (isCallActive && callDuration >= CALL_LIMIT) endCall();
    }, [callDuration, isCallActive]);

    useEffect(() => {
        if (!micStream) return;
        const ac = new (window.AudioContext || window.webkitAudioContext)();
        const an = ac.createAnalyser(); const src = ac.createMediaStreamSource(micStream);
        src.connect(an); an.fftSize = 256;
        const arr = new Uint8Array(an.frequencyBinCount);
        const up = () => { an.getByteFrequencyData(arr); let s = 0; for (let i = 0; i < arr.length; i++) s += arr[i]; setUserVolume(Math.min(1, s / arr.length / 30)); animationFrameRef.current = requestAnimationFrame(up); };
        up();
        return () => { if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current); ac.close(); };
    }, [micStream]);

    const checkPlaybackComplete = useCallback(() => {
        if (!audioCtxRef.current) return;
        if (audioCtxRef.current.currentTime >= lastPlaybackTimeRef.current - 0.1) {
            setAgentIsSpeaking(false);
            if (audioStreamCompleteRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
                wsRef.current.send(JSON.stringify({ type: 'playback_done', duration_ms: playbackStartTimeRef.current ? Date.now() - playbackStartTimeRef.current : 0, timestamp: Date.now() / 1000 }));
                playbackStartTimeRef.current = null; audioStreamCompleteRef.current = false;
            }
        }
    }, []);

    const ensureOutputChain = useCallback(() => {
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

    const stopPlayback = useCallback(() => {
        if (Number.isFinite(currentPlaybackTurnIdRef.current)) {
            minAcceptedPlaybackTurnIdRef.current = Math.max(minAcceptedPlaybackTurnIdRef.current, currentPlaybackTurnIdRef.current + 1);
        }
        for (const source of activeSourcesRef.current) {
            try { source.onended = null; source.stop(); } catch (_) { }
        }
        activeSourcesRef.current.clear();
        binaryQueueRef.current = [];
        currentPlaybackTurnIdRef.current = null;
        audioStreamCompleteRef.current = false;
        playbackStartTimeRef.current = null;
        if (audioCtxRef.current) lastPlaybackTimeRef.current = audioCtxRef.current.currentTime;
        setAgentIsSpeaking(false);
    }, []);

    const playAudioChunk = useCallback((data, forceInt16 = false) => {
        let f32; const fmt = audioConfigRef.current.format; const sr = audioConfigRef.current.sampleRate;
        if (data instanceof ArrayBuffer) {
            if (fmt === 'pcm_s16le' || forceInt16) { const i16 = new Int16Array(data); f32 = new Float32Array(i16.length); for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0; }
            else f32 = new Float32Array(data);
        } else {
            const bs = atob(data); const by = new Uint8Array(bs.length); for (let i = 0; i < bs.length; i++) by[i] = bs.charCodeAt(i);
            if (fmt === 'pcm_s16le' || forceInt16) { const i16 = new Int16Array(by.buffer); f32 = new Float32Array(i16.length); for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0; }
            else f32 = new Float32Array(by.buffer);
        }
        if (audioCtxRef.current) {
            const buf = audioCtxRef.current.createBuffer(1, f32.length, sr); buf.copyToChannel(f32, 0);
            const s = audioCtxRef.current.createBufferSource(); s.buffer = buf;
            const chain = ensureOutputChain(); if (!chain) return;
            chain.gain.gain.value = selectedCallMode === 'telephony' ? 0.2 : 1.0;
            try { chain.gain.disconnect(); chain.highpass.disconnect(); chain.lowpass.disconnect(); s.disconnect(); } catch (_) { }
            if (selectedCallMode === 'telephony') { s.connect(chain.highpass); chain.highpass.connect(chain.lowpass); chain.lowpass.connect(chain.gain); chain.gain.connect(audioCtxRef.current.destination); }
            else { s.connect(chain.gain); chain.gain.connect(audioCtxRef.current.destination); }
            const now = audioCtxRef.current.currentTime; let at = lastPlaybackTimeRef.current; if (at < now) at = now;
            if (!playbackStartTimeRef.current) playbackStartTimeRef.current = Date.now();
            activeSourcesRef.current.add(s);
            s.onended = () => { activeSourcesRef.current.delete(s); checkPlaybackComplete(); };
            s.start(at); lastPlaybackTimeRef.current = at + buf.duration;
        }
    }, [checkPlaybackComplete, ensureOutputChain, selectedCallMode]);

    const endCall = useCallback(() => {
        if (wsRef.current) {
            if (wsRef.current.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: 'interrupt', timestamp: Date.now() / 1000 }));
            wsRef.current.close(); wsRef.current = null;
        }
        stopPlayback();
        if (audioCtxRef.current) { audioCtxRef.current.close(); audioCtxRef.current = null; }
        outputGainRef.current = null; telephonyHighpassRef.current = null; telephonyLowpassRef.current = null;
        if (callTimerRef.current) clearInterval(callTimerRef.current); callTimerRef.current = null;
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        setIsCallActive(false); setConnectionStatus(null); setAgentIsSpeaking(false);
        wsConnectedRef.current = false; setAgentState('idle'); setMicStream(null); setCallDuration(0);
        setShowEmailDialog(true);
    }, [micStream, stopPlayback]);

    const startCall = async () => {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
            setMicStream(stream); startVoiceCall(stream);
        } catch (err) { console.error("Mic failed:", err); alert("Please enable microphone access"); }
    };

    const handleOrbClick = () => {
        if (isCallActive) { endCall(); return; }
        startCall();
    };

    const submitAccessKey = () => {
        if (accessKeyInput.trim() !== config.accessKey) { setAccessError('Invalid access key'); return; }
        setIsAccessGranted(true); setShowCallSetup(false); setAccessError(''); setAccessKeyInput(''); startCall();
    };

    const sendEmailToServer = async (email) => {
        try {
            const response = await fetch('/api/session/end', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionIdRef.current,
                    email: email,
                    tenant_id: config.tenantId,
                    agent_id: config.agentId,
                    timestamp: Date.now() / 1000
                })
            });
            if (!response.ok) console.warn('Failed to send email to server');
        } catch (err) {
            console.warn('Error sending email:', err);
        }
    };

    const startVoiceCall = (stream) => {
        setConnectionStatus('connecting'); setCallDuration(0);
        const base = getWsBaseUrl();
        const nws = String(base).replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
        const uid = 'user_' + Date.now();
        const wsUrl = `${nws}?tenant_id=${encodeURIComponent(config.tenantId)}&agent_id=${encodeURIComponent(config.agentId)}&session_type=webcall&user_id=${encodeURIComponent(uid)}&agent_name=${encodeURIComponent(config.agentName)}`;
        const ws = new WebSocket(wsUrl); ws.binaryType = "arraybuffer"; wsRef.current = ws;
        ws.onopen = () => {
            wsConnectedRef.current = true;
            sessionIdRef.current = 'session_' + Date.now();
            const sessionConfig = {
                type: 'session_config', tenant_id: config.tenantId, agent_id: config.agentId, agent_name: config.agentName,
                user_id: uid, session_type: 'webcall', language: config.language, interaction_mode: 'interactive',
                stt_mode: 'streaming', tts_mode: 'streaming', metadata: { source: 'davinci_widget_blue', region: 'EU' }
            };
            ws.send(JSON.stringify(sessionConfig));
            ws.send(JSON.stringify({ type: 'start_session', timestamp: Date.now() / 1000 }));
            const ac = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 44100 });
            audioCtxRef.current = ac; lastPlaybackTimeRef.current = ac.currentTime;
            const mic = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            const src = mic.createMediaStreamSource(stream);
            const proc = mic.createScriptProcessor(2048, 1, 1);
            proc.onaudioprocess = (e) => {
                if (ws.readyState === WebSocket.OPEN && wsConnectedRef.current && !isMuted) {
                    const inp = e.inputBuffer.getChannelData(0); const pcm = new Int16Array(inp.length);
                    for (let i = 0; i < inp.length; i++) pcm[i] = Math.max(-1, Math.min(1, inp[i])) * 0x7FFF;
                    ws.send(pcm.buffer);
                }
            };
            src.connect(proc); proc.connect(mic.destination); audioWorkletRef.current = proc;
        };
        ws.onmessage = (e) => {
            if (e.data instanceof ArrayBuffer) { binaryQueueRef.current.push(e.data); return; }
            const d = JSON.parse(e.data);
            if (d.type === 'session_ready' || (d.type === 'state_update' && d.state === 'listening')) {
                wsConnectedRef.current = true;
                if (d.audio_format || d.format) audioConfigRef.current.format = d.audio_format || d.format;
                if (d.sample_rate) audioConfigRef.current.sampleRate = d.sample_rate;
                if (connectionStatus !== 'connected') {
                    setConnectionStatus('connected'); setIsCallActive(true);
                    if (!callTimerRef.current) callTimerRef.current = setInterval(() => setCallDuration(x => x + 1), 1000);
                }
            }

            if (d.type === 'state_update' && (d.state === 'thinking' || d.state === 'interrupt' || d.state === 'listening')) {
                stopPlayback();
            } else if (d.type === 'audio_chunk') {
                const turnId = Number(d.playback_turn_id);
                if (Number.isFinite(turnId)) {
                    if (turnId < minAcceptedPlaybackTurnIdRef.current) {
                        if (d.binary_sent && binaryQueueRef.current.length > 0) binaryQueueRef.current.shift();
                        return;
                    }
                    currentPlaybackTurnIdRef.current = turnId;
                }
                if (d.sample_rate) audioConfigRef.current.sampleRate = d.sample_rate;
                setAgentIsSpeaking(true); audioStreamCompleteRef.current = false;
                if (d.binary_sent && binaryQueueRef.current.length > 0) { const c = binaryQueueRef.current.shift(); if (c) playAudioChunk(c, audioConfigRef.current.format === 'pcm_s16le'); }
                else { const a = d.data || d.audio; if (a) playAudioChunk(a); }
                if (d.is_final) { audioStreamCompleteRef.current = true; checkPlaybackComplete(); }
            } else if (d.type === 'audio_complete' || d.is_final) { audioStreamCompleteRef.current = true; checkPlaybackComplete(); }
            else if (d.type === 'interrupt' || d.type === 'clear' || d.type === 'playback_stop') {
                stopPlayback();
            }
            else if (d.type === 'ping' && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now() / 1000 }));
        };
        ws.onclose = () => endCall(); ws.onerror = () => endCall();
    };

    const fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, '0')}`;
    const remaining = CALL_LIMIT - callDuration;
    const isWarning = remaining <= 30 && isCallActive;

    const handleEmailSubmit = () => {
        if (!emailInput.trim()) {
            setShowEmailDialog(false);
            return;
        }
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(emailInput)) {
            setAccessError('Please enter a valid email address');
            return;
        }
        sendEmailToServer(emailInput);
        setEmailInput('');
        setAccessError('');
        setShowEmailDialog(false);
    };

    return (
        <div style={{ position: 'fixed', bottom: '24px', right: '20px', zIndex: 9999, display: 'flex', alignItems: 'center' }}>
            <AnimatePresence>
                {!isCallActive && (
                    <motion.div
                        initial={{ opacity: 0, x: 20 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 20 }}
                        whileHover={{ scale: 1.05 }}
                        onClick={handleOrbClick}
                        style={{
                            background: 'rgba(10, 10, 10, 0.85)',
                            backdropFilter: 'blur(30px)',
                            border: '1px solid rgba(202, 220, 252, 0.4)',
                            borderRadius: '24px',
                            padding: '10px 20px',
                            marginRight: '12px',
                            color: '#FFFFFF',
                            fontWeight: 700,
                            fontSize: '14px',
                            cursor: 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '10px',
                            boxShadow: '0 15px 40px rgba(0,0,0,0.6)',
                            whiteSpace: 'nowrap'
                        }}
                    >
                        Talk to TARA <span style={{ color: '#CADCFC', fontSize: '16px' }}>✨</span>
                    </motion.div>
                )}
            </AnimatePresence>
            <AnimatePresence>
                {isCallActive && (
                    <motion.div initial={{ width: 0, opacity: 0 }} animate={{ width: 240, opacity: 1 }} exit={{ width: 0, opacity: 0 }}
                        style={{ height: '52px', background: 'rgba(10, 10, 10, 0.8)', backdropFilter: 'blur(20px)', border: '1px solid rgba(202, 220, 252, 0.3)', borderRadius: '26px 0 0 26px', display: 'flex', alignItems: 'center', paddingLeft: '20px', paddingRight: '12px', overflow: 'hidden', borderRight: 'none' }}>
                        <div style={{ flex: 1 }}>
                            <div style={{ fontSize: '12px', fontWeight: 900, color: '#EBE5DF', letterSpacing: '0.02em' }}>DA VINCI AI</div>
                            <div style={{ fontSize: '9px', fontWeight: 600, color: isWarning ? '#ef4444' : '#CADCFC', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{isWarning ? `ENDING IN ${remaining}S` : STATE_LABELS[agentState]}</div>
                        </div>
                        <div style={{ fontSize: '11px', fontFamily: 'monospace', color: 'rgba(235,229,223,0.3)', marginRight: '12px' }}>{fmt(callDuration)}</div>
                    </motion.div>
                )}
            </AnimatePresence>

            <motion.button onClick={handleOrbClick}
                style={{ width: '56px', height: '56px', borderRadius: '50%', background: '#050505', border: isCallActive ? '2px solid #CADCFC' : '1px solid rgba(202, 220, 252, 0.2)', boxShadow: isCallActive ? '0 0 30px rgba(202, 220, 252, 0.4)' : '0 10px 40px rgba(0,0,0,0.5)', cursor: 'pointer', padding: 0, overflow: 'hidden', position: 'relative', zIndex: 10 }}
                whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}>
                <OrbRenderer agentState={isCallActive ? agentState : null} userVolume={userVolume} agentIsSpeaking={agentIsSpeaking} />
                {isCallActive && (
                    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.4)' }}>
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#CADCFC" strokeWidth="3" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                    </div>
                )}
            </motion.button>
            {
                isCallActive && (
                    <a
                        href="https://davinciai.eu"
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                            position: 'absolute',
                            top: '-18px',
                            right: '4px',
                            fontSize: '8px',
                            fontWeight: 700,
                            color: '#FFFFFF',
                            textTransform: 'uppercase',
                            letterSpacing: '0.12em',
                            whiteSpace: 'nowrap',
                            textDecoration: 'none'
                        }}
                    >
                        built by davinciai.eu
                    </a>
                )
            }

            <AnimatePresence>
                {showEmailDialog && (
                    <motion.div
                        initial={{ opacity: 0, scale: 0.9 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.9 }}
                        style={{
                            position: 'fixed',
                            bottom: '100px',
                            right: '20px',
                            width: '320px',
                            background: 'rgba(10, 10, 10, 0.95)',
                            backdropFilter: 'blur(20px)',
                            border: '1px solid rgba(202, 220, 252, 0.4)',
                            borderRadius: '16px',
                            padding: '20px',
                            zIndex: 10000,
                            boxShadow: '0 20px 60px rgba(0,0,0,0.5)'
                        }}
                    >
                        <div style={{ fontSize: '14px', fontWeight: 700, color: '#CADCFC', marginBottom: '8px' }}>
                            Session Ended
                        </div>
                        <div style={{ fontSize: '12px', color: '#A0B9D1', marginBottom: '16px' }}>
                            Would you like to receive a summary of this session?
                        </div>
                        <input
                            type="email"
                            placeholder="Enter your email"
                            value={emailInput}
                            onChange={(e) => { setEmailInput(e.target.value); setAccessError(''); }}
                            onKeyDown={(e) => { if (e.key === 'Enter') handleEmailSubmit(); }}
                            autoFocus
                            style={{
                                width: '100%',
                                padding: '10px 12px',
                                background: 'rgba(255,255,255,0.05)',
                                border: `1px solid ${accessError ? '#ef4444' : 'rgba(202, 220, 252, 0.3)'}`,
                                borderRadius: '8px',
                                color: '#FFFFFF',
                                fontSize: '13px',
                                boxSizing: 'border-box',
                                outline: 'none'
                            }}
                        />
                        {accessError && (
                            <div style={{ fontSize: '11px', color: '#ef4444', marginTop: '4px' }}>{accessError}</div>
                        )}
                        <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                            <button
                                onClick={() => { setShowEmailDialog(false); setEmailInput(''); setAccessError(''); }}
                                style={{
                                    flex: 1,
                                    padding: '8px 12px',
                                    background: 'rgba(255,255,255,0.1)',
                                    border: '1px solid rgba(202, 220, 252, 0.2)',
                                    borderRadius: '6px',
                                    color: '#A0B9D1',
                                    fontSize: '12px',
                                    fontWeight: 600,
                                    cursor: 'pointer'
                                }}
                            >
                                Skip
                            </button>
                            <button
                                onClick={handleEmailSubmit}
                                style={{
                                    flex: 1,
                                    padding: '8px 12px',
                                    background: '#CADCFC',
                                    border: 'none',
                                    borderRadius: '6px',
                                    color: '#050505',
                                    fontSize: '12px',
                                    fontWeight: 700,
                                    cursor: 'pointer'
                                }}
                            >
                                Send
                            </button>
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
};

export default TaraVoiceWidget;
