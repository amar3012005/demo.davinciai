"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import {
    Mic,
    RefreshCw,
    Paperclip,
} from "lucide-react";
import { useTheme } from "@/context/ThemeContext";
import { useAgents } from "@/context/AgentContext";
import HiveMindRAG from "@/components/dashboard/HiveMindRAG";
import AgentSkillsModal from "@/components/dashboard/AgentSkillsModal";

// Derive dynamic credentials and RAG API base URL from tenant
// Uses subdomain (simple name like "davinci", "bundb") NOT tenant_id (UUID)
function getRagCredentials() {
    if (typeof window === "undefined") return { baseUrl: null, tenantId: null, token: null };
    try {
        const tenant = localStorage.getItem("tenant");
        const token = localStorage.getItem("access_token");
        if (!tenant) return { baseUrl: null, tenantId: null, token };

        const parsedTenant = JSON.parse(tenant);
        // Use subdomain for WebSocket/API compatibility (simple name, not UUID)
        const tenantId = parsedTenant?.subdomain || parsedTenant?.tenant_id || "davinci";

        return {
            // Point everything to the Orchestrator EU Proxy (Port 8030)
            baseUrl: `https://demo.davinciai.eu:8030`,
            tenantId,
            token
        };
    } catch { return { baseUrl: null, tenantId: null, token: null }; }
}

interface KnowledgePoint {
    id: string;
    x: number;
    y: number;
    issue: string;
    solution: string;
    issue_type: string;
    customer_segment: string;
}

interface VisualizationData {
    points: KnowledgePoint[];
    collection_name: string;
    total_points: number;
    dimension: number;
    algorithm: string;
}

interface ChatMessage {
    role: "user" | "assistant";
    content: string;
    timestamp: Date;
    sources?: string[];
    confidence?: number;
}

type KnowledgeInputMode = "chat" | "skill" | "rule" | "knowledge";

const HIVEMIND_DASHBOARD_SYSTEM_PROMPT = `
You are the HiveMind enterprise analyst interface for client teams.
Answer using the customer insights TARA collected while speaking to customers.
Prioritize pains, objections, buying intent, brand perception, repeated requests, conversion blockers, and the language customers naturally use.
When evidence is weak, say so clearly.
When useful, structure the answer into customer insights, business implications, and recommended next steps.
If the operator proposes a new skill, rule, or knowledge item, rewrite it cleanly so it can be stored in HiveMind for future TARA conversations.
`.trim();

export default function EnterpriseDashboardHiveMindPage() {
    const router = useRouter();
    useTheme();
    const { selectedAgent, loading: agentLoading } = useAgents();

    const [points, setPoints] = useState<KnowledgePoint[]>([]);
    const [vizData, setVizData] = useState<VisualizationData | null>(null);
    const [, setLoading] = useState(false);
    const [chatInput, setChatInput] = useState("");
    const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
    const [isQuerying, setIsQuerying] = useState(false);
    const [connectionStatus, setConnectionStatus] = useState<"connected" | "disconnected" | "connecting">("disconnected");
    const [timeRange, setTimeRange] = useState<"1D" | "1W" | "1M">("1D");
    const [uptime, setUptime] = useState("~1 days");
    const [isSkillsModalOpen, setIsSkillsModalOpen] = useState(false);
    const [inputMode, setInputMode] = useState<KnowledgeInputMode>("chat");
    const [entryTopic, setEntryTopic] = useState("general");
    const [ruleSeverity, setRuleSeverity] = useState<"standard" | "critical">("standard");
    const [tenantId, setTenantId] = useState<string | null>(null);
    const [isChatMinimized, setIsChatMinimized] = useState(false);

    // Knowledge Base rich context fields
    const [kbCategory, setKbCategory] = useState("");
    const [kbDocType, setKbDocType] = useState("General");
    const [kbFilename, setKbFilename] = useState("");
    const [kbChunkIndex, setKbChunkIndex] = useState(0);
    const [kbDocId, setKbDocId] = useState("");

    // File upload state
    const [isProcessingFile, setIsProcessingFile] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const getDashboardContext = useCallback((tenantId: string | null) => ({
        surface: "hivemind_dashboard",
        dashboard_mode: "enterprise_insights",
        tenant_id: tenantId || "davinci",
        system_prompt: HIVEMIND_DASHBOARD_SYSTEM_PROMPT,
        selected_agent: selectedAgent?.agent_name || null,
    }), [selectedAgent]);

    // Fetch visualization data from RAG API
    const loadVisualization = useCallback(async () => {
        const { token, tenantId: credsTenantId } = getRagCredentials();
        setLoading(true);
        setConnectionStatus("connecting");

        try {
            // Use Next.js API route as proxy to avoid CORS issues
            // Use tenantId from state or fall back to credentials from localStorage
            const effectiveTenantId = tenantId || credsTenantId || "davinci";
            const url = `/enterprise/dashboard/hivemind/api/hivemind?algorithm=tsne&limit=200&tenant_id=${effectiveTenantId}`;
            console.log("Loading visualization for tenant:", effectiveTenantId);
            const response = await fetch(url, {
                headers: {
                    "Authorization": token ? `Bearer ${token}` : ""
                }
            });

            if (response.ok) {
                const data: VisualizationData = await response.json();
                setVizData(data);
                setPoints(data.points);
                setConnectionStatus("connected");
            } else {
                const errorData = await response.json();
                console.error("Visualization error:", response.status, errorData);
                throw new Error(`HTTP ${response.status}: ${JSON.stringify(errorData)}`);
            }
        } catch (error) {
            console.error("Failed to load HiveMind visualization:", error);
            setConnectionStatus("disconnected");
        } finally {
            setLoading(false);
        }
    }, [tenantId]);

    // Query the RAG system
    const handleQuery = async () => {
        if (!chatInput.trim()) return;

        const userMessage: ChatMessage = {
            role: "user",
            content: chatInput,
            timestamp: new Date()
        };

        setChatMessages(prev => [...prev, userMessage]);
        setChatInput("");
        setIsQuerying(true);
        setIsChatMinimized(false);

        try {
            const { token } = getRagCredentials();
            const response = await fetch(`/enterprise/dashboard/hivemind/api/hivemind?tenant_id=${tenantId || "davinci"}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": token ? `Bearer ${token}` : ""
                },
                body: JSON.stringify({
                    query: chatInput,
                    tenant_id: tenantId || "davinci",
                    agent_name: selectedAgent?.agent_name || undefined,
                    context: getDashboardContext(tenantId),
                    history_context: chatMessages.slice(-6).map(m => ({
                        role: m.role,
                        content: m.content
                    }))
                })
            });

            if (response.ok) {
                const data = await response.json();
                const aiMessage: ChatMessage = {
                    role: "assistant",
                    content: data.answer || data.text || "No response available",
                    timestamp: new Date(),
                    sources: data.sources,
                    confidence: data.confidence
                };
                setChatMessages(prev => [...prev, aiMessage]);
            } else {
                const errorData = await response.json();
                console.error("Query error:", errorData);
                throw new Error("Query failed");
            }
        } catch (error) {
            const errorMessage: ChatMessage = {
                role: "assistant",
                content: "Unable to connect to HiveMind. The collective intelligence is temporarily unavailable.",
                timestamp: new Date()
            };
            setChatMessages(prev => [...prev, errorMessage]);
        } finally {
            setIsQuerying(false);
        }
    };

    const handleKnowledgeSubmit = async () => {
        if (!chatInput.trim()) return;

        setIsQuerying(true);
        setIsChatMinimized(false);
        try {
            const { tenantId, token } = getRagCredentials();

            // Build base payload with all universal fields
            const basePayload: Record<string, unknown> = {
                tenant_id: tenantId || "davinci",
                agent_id: selectedAgent?.agent_name || "unknown",
                session_type: "hivemind_dashboard",
            };

            let payload: Record<string, unknown>;

            if (inputMode === "knowledge") {
                // Rich context payload for Knowledge Base (General_KB)
                payload = {
                    ...basePayload,
                    doc_type: "General_KB",
                    text: chatInput,
                    summary: chatInput.slice(0, 200),
                    // Type-specific fields for General_KB
                    filename: kbFilename || "manual_entry.txt",
                    original_filename: kbFilename || "manual_entry.txt",
                    doc_type_detail: kbDocType || "General",
                    chunk_index: kbChunkIndex,
                    doc_id: kbDocId || undefined,
                    topics: kbCategory || entryTopic || "general",
                    // Backward compat
                    data_type: "Internal_KB",
                };
            } else if (inputMode === "rule") {
                // Agent Rule payload
                payload = {
                    ...basePayload,
                    doc_type: "Agent_Rule",
                    text: chatInput,
                    summary: chatInput,
                    topic: entryTopic,
                    severity: ruleSeverity,
                    // Backward compat
                    type: "agent_rule",
                };
            } else if (inputMode === "skill") {
                // Agent Skill payload
                payload = {
                    ...basePayload,
                    doc_type: "Agent_Skill",
                    text: chatInput,
                    summary: chatInput,
                    topic: entryTopic,
                    // Backward compat
                    type: "agent_skill",
                };
            } else {
                payload = basePayload;
            }

            const response = await fetch(`/enterprise/dashboard/hivemind/api/hivemind?tenant_id=${encodeURIComponent(tenantId || "davinci")}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": token ? `Bearer ${token}` : ""
                },
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                const label = inputMode === "rule" ? "rule" : inputMode === "knowledge" ? "knowledge entry" : "skill";
                const details = inputMode === "knowledge"
                    ? ` (${kbDocType}${kbCategory ? ` • ${kbCategory}` : ""}${kbChunkIndex > 0 ? ` • chunk ${kbChunkIndex}` : ""})`
                    : "";
                const successMessage: ChatMessage = {
                    role: "assistant",
                    content: `Saved ${label} to HiveMind for ${(tenantId || "davinci").toUpperCase()}: "${chatInput.slice(0, 60)}${chatInput.length > 60 ? "..." : ""}"${details}`,
                    timestamp: new Date()
                };
                setChatMessages(prev => [...prev, successMessage]);
                setChatInput("");
                setEntryTopic("general");
                setRuleSeverity("standard");
                // Reset KB fields
                setKbCategory("");
                setKbDocType("General");
                setKbFilename("");
                setKbChunkIndex(0);
                setKbDocId("");
            } else {
                throw new Error("Failed to add entry");
            }
        } catch (error) {
            const errorMessage: ChatMessage = {
                role: "assistant",
                content: "Failed to save the HiveMind entry. Please try again.",
                timestamp: new Date()
            };
            setChatMessages(prev => [...prev, errorMessage]);
        } finally {
            setIsQuerying(false);
        }
    };

    /**
     * Handle file upload - extract text and chunk using Groq LLM
     */
    const handleFileUpload = async (file: File) => {
        setIsProcessingFile(true);
        try {
            const { tenantId, token } = getRagCredentials();

            // Add status message
            setChatMessages(prev => [...prev, {
                role: "assistant",
                content: `Processing "${file.name}"...`,
                timestamp: new Date()
            }]);

            // Extract text from PDF
            const pdfjsLib = await import("pdfjs-dist");
            pdfjsLib.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.js";

            const arrayBuffer = await file.arrayBuffer();
            const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;

            let fullText = "";
            for (let i = 1; i <= pdf.numPages; i++) {
                const page = await pdf.getPage(i);
                const textContent = await page.getTextContent();
                const pageText = textContent.items.map((item: any) => item.str).join(" ");
                fullText += `\n--- Page ${i} ---\n${pageText}`;
            }

            if (!fullText.trim()) {
                throw new Error("No text extracted from PDF");
            }

            // Chunk text using Groq LLM
            const GROQ_API_KEY = process.env.NEXT_PUBLIC_GROQ_API_KEY || "";
            if (!GROQ_API_KEY) {
                throw new Error("Groq API key not configured");
            }

            const prompt = `You are a text chunking assistant. Break down the document into semantic chunks (200-500 words each).
Each chunk should be self-contained. Return ONLY valid JSON:
{
  "chunks": [
    {"text": "chunk content", "topic": "topic name", "summary": "brief summary"}
  ]
}

DOCUMENT:
${fullText.slice(0, 15000)}`;

            const response = await fetch("https://api.groq.com/openai/v1/chat/completions", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${GROQ_API_KEY}`
                },
                body: JSON.stringify({
                    model: "llama-3.1-70b-versatile",
                    messages: [
                        { role: "system", content: "Respond with valid JSON only." },
                        { role: "user", content: prompt }
                    ],
                    temperature: 0.1,
                    max_tokens: 4000
                })
            });

            if (!response.ok) {
                throw new Error(`Groq API error: ${response.status}`);
            }

            const data = await response.json();
            const content = data.choices[0]?.message?.content || "";
            const jsonMatch = content.match(/\{[\s\S]*\}/);

            if (!jsonMatch) {
                throw new Error("Invalid JSON from Groq");
            }

            const parsed = JSON.parse(jsonMatch[0]);
            const chunks = parsed.chunks || [];

            if (chunks.length === 0) {
                throw new Error("No chunks created from document");
            }

            // Generate a doc_id for this file
            const docId = `file_${file.name.replace(/[^a-z0-9]/gi, "_")}_${Date.now()}`;

            // Submit each chunk using the same handleKnowledgeSubmit flow
            setChatMessages(prev => [...prev, {
                role: "assistant",
                content: `Uploading ${chunks.length} chunks to HiveMind...`,
                timestamp: new Date()
            }]);

            let successCount = 0;
            for (let i = 0; i < chunks.length; i++) {
                const chunk = chunks[i];

                const payload = {
                    tenant_id: tenantId || "davinci",
                    agent_id: selectedAgent?.agent_name || "unknown",
                    session_type: "hivemind_dashboard",
                    doc_type: "General_KB",
                    text: chunk.text,
                    summary: chunk.summary,
                    filename: file.name,
                    original_filename: file.name,
                    doc_type_detail: "General",
                    chunk_index: i,
                    doc_id: docId,
                    topics: chunk.topic || "general",
                    data_type: "Internal_KB",
                };

                try {
                    const uploadResponse = await fetch(`/enterprise/dashboard/hivemind/api/hivemind?tenant_id=${encodeURIComponent(tenantId || "davinci")}`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "Authorization": token ? `Bearer ${token}` : ""
                        },
                        body: JSON.stringify(payload)
                    });

                    if (uploadResponse.ok) {
                        successCount++;
                    }
                } catch (chunkError) {
                    console.error(`Chunk ${i} upload failed:`, chunkError);
                }

                // Small delay between uploads
                await new Promise(resolve => setTimeout(resolve, 200));
            }

            // Final status message
            setChatMessages(prev => [...prev, {
                role: "assistant",
                content: `✅ Successfully uploaded ${successCount}/${chunks.length} chunks from "${file.name}" to HiveMind`,
                timestamp: new Date()
            }]);

            // Reset KB fields
            setKbFilename("");
            setKbDocId("");
            setKbChunkIndex(0);

        } catch (error) {
            console.error("File upload error:", error);
            setChatMessages(prev => [...prev, {
                role: "assistant",
                content: `Failed to process file: ${error instanceof Error ? error.message : String(error)}`,
                timestamp: new Date()
            }]);
        } finally {
            setIsProcessingFile(false);
        }
    };

    const onFileSelected = (event: React.ChangeEvent<HTMLInputElement>) => {
        const files = event.target.files;
        if (files && files.length > 0) {
            handleFileUpload(files[0]);
            // Reset input so same file can be selected again
            if (fileInputRef.current) {
                fileInputRef.current.value = "";
            }
        }
    };

    const inputPlaceholder =
        inputMode === "chat"
            ? "Ask HiveMind for customer insights, objections, demand signals, or trends..."
            : inputMode === "skill"
                ? "Add a new TARA skill the voice agent should use..."
                : inputMode === "rule"
                    ? "Add a new rule or guardrail for TARA..."
                    : "Add new reusable knowledge for HiveMind and future conversations...";

    const showEntryMeta = inputMode !== "chat";


    useEffect(() => {
        const storedTenant = localStorage.getItem("tenant");
        if (!storedTenant) {
            router.push("/login");
            return;
        }

        // Extract tenant identifier from localStorage
        // Uses subdomain (simple name) for WebSocket/API compatibility
        try {
            const parsedTenant = JSON.parse(storedTenant);
            const extractedTenantId = parsedTenant?.subdomain || parsedTenant?.tenant_id || "davinci";
            setTenantId(extractedTenantId);
        } catch {
            setTenantId("davinci");
        }

        // Load visualization on mount
        loadVisualization();
    }, [router, loadVisualization]);



    if (agentLoading) {
        return (
            <div style={{
                minHeight: "100vh",
                backgroundColor: "#000",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
            }}>
                <motion.div
                    animate={{ rotate: 360 }}
                    transition={{ duration: 1, repeat: Infinity, ease: "linear" }}
                >
                    <RefreshCw size={32} color="#333" />
                </motion.div>
            </div>
        );
    }

    return (
        <div style={{
            height: "100%",
            width: "100%",
            backgroundColor: "#000",
            fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
            color: "#fff",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden"
        }}>
            {/* Header Bar */}
            <div style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                padding: "32px 48px",
                position: "absolute",
                top: 0,
                left: 0,
                right: 0,
                zIndex: 10,
                pointerEvents: "none"
            }}>
                {/* Left - Brand */}
                <div style={{ pointerEvents: "auto" }}>
                    <h1 style={{
                        fontSize: 48,
                        fontWeight: 800,
                        margin: 0,
                        letterSpacing: "-0.03em",
                        color: "#fff"
                    }}>
                        HIVEMIND
                    </h1>
                    <p style={{
                        fontSize: 14,
                        color: "#666",
                        margin: "4px 0 0 0",
                        fontWeight: 400
                    }}>
                        Enterprise-grade voice agent for Davinci AI
                    </p>
                    <div style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 12,
                        marginTop: 12,
                        fontSize: 12,
                        fontFamily: "JetBrains Mono, monospace"
                    }}>
                        {tenantId && (
                            <span style={{
                                padding: "4px 10px",
                                backgroundColor: "rgba(34, 197, 94, 0.15)",
                                color: "#22c55e",
                                borderRadius: 6,
                                border: "1px solid rgba(34, 197, 94, 0.25)"
                            }}>
                                TENANT: {tenantId.toUpperCase()}
                            </span>
                        )}
                        {selectedAgent?.agent_name && (
                            <span style={{
                                padding: "4px 10px",
                                backgroundColor: "rgba(100, 100, 100, 0.15)",
                                color: "#aaa",
                                borderRadius: 6,
                                border: "1px solid rgba(100, 100, 100, 0.25)"
                            }}>
                                AGENT: {selectedAgent.agent_name.toUpperCase()}
                            </span>
                        )}
                    </div>
                </div>

                {/* Right - Status */}
                <div style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    pointerEvents: "auto"
                }}>
                    <motion.div
                        animate={{
                            scale: connectionStatus === "connected" ? [1, 1.2, 1] : 1,
                            opacity: connectionStatus === "connecting" ? [1, 0.5, 1] : 1
                        }}
                        transition={{ duration: 2, repeat: Infinity }}
                        style={{
                            width: 10,
                            height: 10,
                            borderRadius: "50%",
                            backgroundColor: connectionStatus === "connected" ? "#22c55e" :
                                connectionStatus === "connecting" ? "#f59e0b" : "#666",
                            boxShadow: connectionStatus === "connected" ? "0 0 12px #22c55e" : "none"
                        }}
                    />
                    <span style={{
                        color: connectionStatus === "connected" ? "#22c55e" : "#666",
                        fontSize: 14,
                        fontWeight: 500
                    }}>
                        LIVE
                    </span>
                    <span style={{ color: "#444", fontSize: 14 }}>
                        ({uptime})
                    </span>
                </div>
            </div>

            {/* Main Visualization Area */}
            <div style={{ flex: 1, position: "relative", minHeight: "400px" }}>
                <HiveMindRAG
                    points={points}
                    showTooltip={true}
                    showStats={false}
                    autoLoad={true}
                    width="100%"
                    height="100%"
                />
            </div>

            {/* Bottom Controls */}
            <div style={{
                position: "absolute",
                bottom: 32,
                left: 48,
                right: 48,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-end",
                zIndex: 10
            }}>
                {/* Time Range Selector */}
                <div style={{
                    display: "flex",
                    backgroundColor: "rgba(30, 30, 30, 0.8)",
                    borderRadius: 12,
                    padding: 4,
                    backdropFilter: "blur(10px)"
                }}>
                    {(["1D", "1W", "1M"] as const).map((range) => (
                        <button
                            key={range}
                            onClick={() => setTimeRange(range)}
                            style={{
                                padding: "10px 20px",
                                backgroundColor: timeRange === range ? "#fff" : "transparent",
                                color: timeRange === range ? "#000" : "#666",
                                border: "none",
                                borderRadius: 8,
                                fontSize: 13,
                                fontWeight: 600,
                                cursor: "pointer",
                                transition: "all 0.2s"
                            }}
                        >
                            {range}
                        </button>
                    ))}
                </div>

                {/* Stats */}
                {vizData && (
                    <div style={{
                        display: "flex",
                        gap: 32,
                        color: "#444",
                        fontSize: 12,
                        fontFamily: "JetBrains Mono, monospace"
                    }}>
                        <div>
                            <span style={{ color: "#666" }}>{vizData.total_points}</span>
                            <span style={{ marginLeft: 6 }}>NODES</span>
                        </div>
                        <div>
                            <span style={{ color: "#666" }}>{vizData.dimension}</span>
                            <span style={{ marginLeft: 6 }}>DIMENSIONS</span>
                        </div>
                        <div>
                            <span style={{ color: "#666" }}>{vizData.algorithm?.toUpperCase()}</span>
                            <span style={{ marginLeft: 6 }}>ALGORITHM</span>
                        </div>
                    </div>
                )}
            </div>

            {/* Chat Interface - Bottom Center */}
            <div style={{
                position: "absolute",
                bottom: 56,
                left: "50%",
                transform: "translateX(-50%)",
                width: "100%",
                maxWidth: 600,
                padding: "0 24px",
                zIndex: 20
            }}>
                {/* Chat Messages - Only show last message with minimize button */}
                <AnimatePresence>
                    {chatMessages.length > 0 && !isChatMinimized && (
                        <motion.div
                            initial={{ opacity: 0, y: 50 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -100, transition: { duration: 0.5, ease: "easeInOut" } }}
                            transition={{ type: "spring", stiffness: 300, damping: 30 }}
                            style={{
                                marginBottom: 16,
                                maxHeight: 400,
                                overflowY: "auto",
                                backgroundColor: "#0a0a0a",
                                borderRadius: 16,
                                border: "1px solid #222",
                                backdropFilter: "blur(20px)"
                            }}
                        >
                            {/* Minimize button */}
                            <div style={{
                                display: "flex",
                                justifyContent: "flex-end",
                                padding: "8px 12px 0",
                                borderBottom: "1px solid #1a1a1a"
                            }}>
                                <button
                                    onClick={() => setIsChatMinimized(true)}
                                    style={{
                                        display: "flex",
                                        alignItems: "center",
                                        gap: 4,
                                        padding: "4px 8px",
                                        backgroundColor: "transparent",
                                        border: "none",
                                        color: "#666",
                                        fontSize: 11,
                                        fontFamily: "JetBrains Mono, monospace",
                                        cursor: "pointer",
                                        transition: "color 0.2s"
                                    }}
                                    onMouseEnter={(e) => e.currentTarget.style.color = "#fff"}
                                    onMouseLeave={(e) => e.currentTarget.style.color = "#666"}
                                >
                                    <span>HIDE</span>
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <path d="M6 9l6 6 6-6"/>
                                    </svg>
                                </button>
                            </div>

                            {/* Only show the last message */}
                            {(() => {
                                const msg = chatMessages[chatMessages.length - 1];
                                return (
                                    <div
                                        key={msg.timestamp.getTime()}
                                        style={{
                                            padding: "16px 24px 20px"
                                        }}
                                    >
                                        <div style={{
                                            fontSize: 10,
                                            color: msg.role === "assistant" ? "#fff" : "#666",
                                            fontFamily: "JetBrains Mono, monospace",
                                            marginBottom: 12,
                                            textTransform: "uppercase",
                                            letterSpacing: "0.05em"
                                        }}>
                                            {msg.role === "user" ? "You" : "HiveMind"}
                                            {msg.confidence && (
                                                <span style={{ marginLeft: 12, color: "#444" }}>
                                                    {(msg.confidence * 100).toFixed(0)}% confidence
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: 16, lineHeight: 1.6, color: "#fff" }}>
                                            {msg.content}
                                        </div>
                                    </div>
                                );
                            })()}
                            {isQuerying && (
                                <div style={{
                                    padding: "20px 24px",
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 10,
                                    color: "#666"
                                }}>
                                    <motion.div
                                        animate={{ rotate: 360 }}
                                        transition={{ duration: 1, repeat: Infinity, ease: "linear" }}
                                    >
                                        <RefreshCw size={14} />
                                    </motion.div>
                                    <span style={{ fontSize: 13 }}>Querying collective intelligence...</span>
                                </div>
                            )}
                        </motion.div>
                    )}
                </AnimatePresence>

                {/* Show button when minimized */}
                {isChatMinimized && chatMessages.length > 0 && (
                    <motion.button
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        onClick={() => setIsChatMinimized(false)}
                        style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            gap: 6,
                            marginBottom: 16,
                            padding: "8px 16px",
                            backgroundColor: "#0a0a0a",
                            border: "1px solid #333",
                            borderRadius: 999,
                            color: "#fff",
                            fontSize: 12,
                            fontFamily: "JetBrains Mono, monospace",
                            cursor: "pointer",
                            alignSelf: "center"
                        }}
                    >
                        <span>SHOW RESPONSE</span>
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M18 15l-6-6-6 6"/>
                        </svg>
                    </motion.button>
                )}

                <div style={{
                    display: "flex",
                    gap: 8,
                    marginBottom: 12,
                    justifyContent: "center",
                    flexWrap: "wrap"
                }}>
                    {([
                        { key: "chat", label: "Insights" },
                        { key: "skill", label: "New Skill" },
                        { key: "rule", label: "New Rule" },
                        { key: "knowledge", label: "Knowledge Base" },
                    ] as const).map((mode) => (
                        <button
                            key={mode.key}
                            onClick={() => setInputMode(mode.key)}
                            style={{
                                padding: "8px 14px",
                                borderRadius: 999,
                                border: inputMode === mode.key ? "1px solid #fff" : "1px solid #333",
                                backgroundColor: inputMode === mode.key ? "rgba(255, 255, 255, 0.1)" : "transparent",
                                color: inputMode === mode.key ? "#fff" : "#666",
                                fontSize: 12,
                                fontWeight: 600,
                                cursor: "pointer",
                                letterSpacing: "0.02em",
                                transition: "all 0.2s"
                            }}
                        >
                            {mode.label}
                        </button>
                    ))}
                </div>

                {showEntryMeta && (
                    <div style={{
                        display: "flex",
                        gap: 10,
                        marginBottom: 12,
                        padding: "10px 12px",
                        backgroundColor: "#0a0a0a",
                        border: "1px solid #222",
                        borderRadius: 18,
                        backdropFilter: "blur(20px)",
                        boxShadow: "0 12px 32px rgba(0,0,0,0.45)",
                        flexWrap: "wrap"
                    }}>
                        {/* Category/Topic - shown for all non-chat modes */}
                        <div style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 6,
                            minWidth: 180,
                            flex: 1
                        }}>
                            <span style={{
                                fontSize: 10,
                                letterSpacing: "0.08em",
                                textTransform: "uppercase",
                                color: "#666",
                                fontWeight: 700
                            }}>
                                {inputMode === "knowledge" ? "Topics/Category" : "Topic"}
                            </span>
                            <input
                                type="text"
                                value={inputMode === "knowledge" ? kbCategory : entryTopic}
                                onChange={(e) => inputMode === "knowledge" ? setKbCategory(e.target.value) : setEntryTopic(e.target.value)}
                                placeholder={inputMode === "knowledge" ? "e.g. billing, api_docs" : "general"}
                                style={{
                                    height: 38,
                                    borderRadius: 12,
                                    border: "1px solid #333",
                                    backgroundColor: "#111",
                                    color: "#fff",
                                    padding: "0 12px",
                                    outline: "none",
                                    fontSize: 13,
                                    fontFamily: "Inter, sans-serif"
                                }}
                            />
                        </div>

                        {/* Knowledge Base specific fields */}
                        {inputMode === "knowledge" && (
                            <>
                                <div style={{
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: 6,
                                    minWidth: 150,
                                    flex: 1
                                }}>
                                    <span style={{
                                        fontSize: 10,
                                        letterSpacing: "0.08em",
                                        textTransform: "uppercase",
                                        color: "#666",
                                        fontWeight: 700
                                    }}>
                                        Doc Type
                                    </span>
                                    <select
                                        value={kbDocType}
                                        onChange={(e) => setKbDocType(e.target.value)}
                                        style={{
                                            height: 38,
                                            borderRadius: 12,
                                            border: "1px solid #333",
                                            backgroundColor: "#111",
                                            color: "#fff",
                                            padding: "0 12px",
                                            outline: "none",
                                            fontSize: 13,
                                            fontFamily: "Inter, sans-serif"
                                        }}
                                    >
                                        <option value="General">General</option>
                                        <option value="Documentation">Documentation</option>
                                        <option value="FAQ">FAQ</option>
                                        <option value="API_Docs">API Docs</option>
                                        <option value="Tutorial">Tutorial</option>
                                        <option value="Policy">Policy</option>
                                        <option value="Product_Info">Product Info</option>
                                        <option value="Troubleshooting">Troubleshooting</option>
                                    </select>
                                </div>

                                <div style={{
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: 6,
                                    minWidth: 180,
                                    flex: 1
                                }}>
                                    <span style={{
                                        fontSize: 10,
                                        letterSpacing: "0.08em",
                                        textTransform: "uppercase",
                                        color: "#666",
                                        fontWeight: 700
                                    }}>
                                        Filename (optional)
                                    </span>
                                    <input
                                        type="text"
                                        value={kbFilename}
                                        onChange={(e) => setKbFilename(e.target.value)}
                                        placeholder="e.g. manual_entry.md"
                                        style={{
                                            height: 38,
                                            borderRadius: 12,
                                            border: "1px solid #333",
                                            backgroundColor: "#111",
                                            color: "#fff",
                                            padding: "0 12px",
                                            outline: "none",
                                            fontSize: 13,
                                            fontFamily: "Inter, sans-serif"
                                        }}
                                    />
                                </div>

                                <div style={{
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: 6,
                                    minWidth: 100,
                                    flex: 1
                                }}>
                                    <span style={{
                                        fontSize: 10,
                                        letterSpacing: "0.08em",
                                        textTransform: "uppercase",
                                        color: "#666",
                                        fontWeight: 700
                                    }}>
                                        Chunk Index
                                    </span>
                                    <input
                                        type="number"
                                        value={kbChunkIndex}
                                        onChange={(e) => setKbChunkIndex(parseInt(e.target.value) || 0)}
                                        min={0}
                                        style={{
                                            height: 38,
                                            borderRadius: 12,
                                            border: "1px solid #333",
                                            backgroundColor: "#111",
                                            color: "#fff",
                                            padding: "0 12px",
                                            outline: "none",
                                            fontSize: 13,
                                            fontFamily: "Inter, sans-serif"
                                        }}
                                    />
                                </div>
                            </>
                        )}

                        {inputMode === "rule" && (
                            <div style={{
                                display: "flex",
                                flexDirection: "column",
                                gap: 6,
                                minWidth: 150
                            }}>
                                <span style={{
                                    fontSize: 10,
                                    letterSpacing: "0.08em",
                                    textTransform: "uppercase",
                                    color: "#666",
                                    fontWeight: 700
                                }}>
                                    Severity
                                </span>
                                <select
                                    value={ruleSeverity}
                                    onChange={(e) => setRuleSeverity(e.target.value as "standard" | "critical")}
                                    style={{
                                        height: 38,
                                        borderRadius: 12,
                                        border: "1px solid #333",
                                        backgroundColor: "#111",
                                        color: "#fff",
                                        padding: "0 12px",
                                        outline: "none",
                                        fontSize: 13,
                                        fontFamily: "Inter, sans-serif"
                                    }}
                                >
                                    <option value="standard">standard</option>
                                    <option value="critical">critical</option>
                                </select>
                            </div>
                        )}
                    </div>
                )}

                {/* Chat Input */}
                <div style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "16px 24px",
                    background: "#0a0a0a",
                    borderRadius: 28,
                    border: "1px solid #222",
                    backdropFilter: "blur(20px)",
                    boxShadow: "0 18px 48px rgba(0,0,0,0.62)"
                }}>
                    {/* Left Icons */}
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                        <button
                            onClick={() => fileInputRef.current?.click()}
                            disabled={isProcessingFile}
                            title="Upload PDF file"
                            style={{
                                width: 32,
                                height: 32,
                                borderRadius: 8,
                                backgroundColor: "transparent",
                                border: "none",
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                cursor: isProcessingFile ? "not-allowed" : "pointer",
                                color: isProcessingFile ? "#444" : "#888",
                                transition: "all 0.2s"
                            }}
                            onMouseEnter={(e) => {
                                if (!isProcessingFile) e.currentTarget.style.color = "#fff";
                            }}
                            onMouseLeave={(e) => {
                                if (!isProcessingFile) e.currentTarget.style.color = "#888";
                            }}
                        >
                            <Paperclip size={20} />
                        </button>
                        <input
                            ref={fileInputRef}
                            type="file"
                            accept=".pdf,.docx,.txt,.md"
                            onChange={onFileSelected}
                            style={{ display: "none" }}
                        />
                    </div>

                    <input
                        type="text"
                        placeholder={inputPlaceholder}
                        value={chatInput}
                        onChange={(e) => setChatInput(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter" && !e.shiftKey) {
                                if (inputMode === "chat") {
                                    handleQuery();
                                } else {
                                    handleKnowledgeSubmit();
                                }
                            }
                        }}
                        disabled={isQuerying}
                        style={{
                            flex: 1,
                            padding: "10px 0",
                            backgroundColor: "transparent",
                            border: "none",
                            outline: "none",
                            color: "#fff",
                            fontSize: 15,
                            fontFamily: "Inter, sans-serif"
                        }}
                    />

                    {/* Right Microphone Button */}
                    <button
                        style={{
                            width: 44,
                            height: 44,
                            borderRadius: "50%",
                            backgroundColor: "#fff",
                            border: "none",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            cursor: "pointer",
                            color: "#000",
                            transition: "all 0.2s",
                            boxShadow: "0 2px 8px rgba(255,255,255,0.2)"
                        }}
                        onMouseEnter={(e) => {
                            e.currentTarget.style.transform = "scale(1.05)";
                        }}
                        onMouseLeave={(e) => {
                            e.currentTarget.style.transform = "scale(1)";
                        }}
                    >
                        <Mic size={20} />
                    </button>
                </div>
            </div>

            {/* Agent Skills Modal */}
            <AgentSkillsModal
                isOpen={isSkillsModalOpen}
                onClose={() => setIsSkillsModalOpen(false)}
                isDark={true}
            />
        </div>
    );
}
