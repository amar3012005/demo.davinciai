"use client";

import { useEffect, useState, useCallback } from "react";
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

// Derive dynamic credentials and RAG API base URL from tenant subdomain
function getRagCredentials() {
    if (typeof window === "undefined") return { baseUrl: null, tenantId: null, token: null };
    try {
        const tenant = localStorage.getItem("tenant");
        const token = localStorage.getItem("access_token");
        if (!tenant) return { baseUrl: null, tenantId: null, token };

        const parsedTenant = JSON.parse(tenant);
        const { subdomain, tenant_id } = parsedTenant;

        return {
            // Point everything to the Orchestrator EU Proxy (Port 8030)
            baseUrl: `https://demo.davinciai.eu:8030`,
            // Use subdomain as the slug for tenant identification at the backend
            tenantId: subdomain || tenant_id || "davinci",
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

    const getDashboardContext = useCallback((tenantId: string | null) => ({
        surface: "hivemind_dashboard",
        dashboard_mode: "enterprise_insights",
        tenant_id: tenantId || "davinci",
        system_prompt: HIVEMIND_DASHBOARD_SYSTEM_PROMPT,
        selected_agent: selectedAgent?.name || null,
    }), [selectedAgent]);

    // Fetch visualization data from RAG API
    const loadVisualization = useCallback(async () => {
        const { baseUrl: ragBase, tenantId, token } = getRagCredentials();
        if (!ragBase) {
            setConnectionStatus("disconnected");
            return;
        }
        setLoading(true);
        setConnectionStatus("connecting");

        try {
            const url = `${ragBase}/hivemind/visualize?algorithm=tsne&limit=200&tenant_id=${encodeURIComponent(tenantId || "davinci")}`;
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
                throw new Error(`HTTP ${response.status}`);
            }
        } catch (error) {
            console.error("Failed to load HiveMind visualization:", error);
            setConnectionStatus("disconnected");
            // Keep showing the generated neural network
        } finally {
            setLoading(false);
        }
    }, []);

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

        try {
            const { baseUrl: ragBase, tenantId, token } = getRagCredentials();
            if (!ragBase) throw new Error("RAG not configured");
            const response = await fetch(`${ragBase}/hivemind/query?tenant_id=${encodeURIComponent(tenantId || "davinci")}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": token ? `Bearer ${token}` : ""
                },
                body: JSON.stringify({
                    query: chatInput,
                    tenant_id: tenantId || "davinci",
                    agent_name: selectedAgent?.name || undefined,
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
                    content: data.answer,
                    timestamp: new Date(),
                    sources: data.sources,
                    confidence: data.confidence
                };
                setChatMessages(prev => [...prev, aiMessage]);
            } else {
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
        try {
            const { baseUrl: ragBase, tenantId, token } = getRagCredentials();
            if (!ragBase) throw new Error("RAG not configured");

            const payload = {
                text: chatInput,
                type: inputMode === "rule" ? "agent_rule" : inputMode === "knowledge" ? "general_kb" : "agent_skill",
                topic: entryTopic,
                ...(inputMode === "rule" ? { severity: ruleSeverity } : {}),
                tenant_id: tenantId
            };

            const response = await fetch(`${ragBase}/hivemind/skills?tenant_id=${encodeURIComponent(tenantId || "davinci")}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": token ? `Bearer ${token}` : ""
                },
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                const label = inputMode === "rule" ? "rule" : inputMode === "knowledge" ? "knowledge entry" : "skill";
                const successMessage: ChatMessage = {
                    role: "assistant",
                    content: `Saved ${label} to HiveMind for ${(tenantId || "davinci").toUpperCase()}: "${chatInput}"`,
                    timestamp: new Date()
                };
                setChatMessages(prev => [...prev, successMessage]);
                setChatInput("");
                setEntryTopic("general");
                setRuleSeverity("standard");
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
                        TARA
                    </h1>
                    <p style={{
                        fontSize: 14,
                        color: "#666",
                        margin: "4px 0 0 0",
                        fontWeight: 400
                    }}>
                        Enterprise-grade voice agent for Davinci AI
                    </p>
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
            <div style={{ flex: 1, position: "relative" }}>
                <HiveMindRAG
                    points={points}
                    showTooltip={true}
                    showStats={false}
                    autoLoad={true}
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
                {/* Chat Messages */}
                <AnimatePresence>
                    {chatMessages.length > 0 && (
                        <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: "auto" }}
                            exit={{ opacity: 0, height: 0 }}
                            style={{
                                marginBottom: 16,
                                maxHeight: 200,
                                overflowY: "auto",
                                backgroundColor: "rgba(0, 0, 0, 0.9)",
                                borderRadius: 16,
                                border: "1px solid #222",
                                backdropFilter: "blur(20px)"
                            }}
                        >
                            {chatMessages.slice(-4).map((msg, i) => (
                                <div
                                    key={i}
                                    style={{
                                        padding: "14px 18px",
                                        borderBottom: i < chatMessages.length - 1 ? "1px solid #1a1a1a" : "none"
                                    }}
                                >
                                    <div style={{
                                        fontSize: 10,
                                        color: msg.role === "assistant" ? "#22c55e" : "#666",
                                        fontFamily: "JetBrains Mono, monospace",
                                        marginBottom: 6,
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
                                    <div style={{ fontSize: 14, lineHeight: 1.5, color: "#ccc" }}>
                                        {msg.content}
                                    </div>
                                </div>
                            ))}
                            {isQuerying && (
                                <div style={{
                                    padding: "14px 18px",
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 10,
                                    color: "#444"
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
                                border: inputMode === mode.key ? "1px solid #22c55e" : "1px solid #2a2a2a",
                                backgroundColor: inputMode === mode.key ? "rgba(34, 197, 94, 0.12)" : "rgba(18, 18, 18, 0.88)",
                                color: inputMode === mode.key ? "#d1fae5" : "#8b8b8b",
                                fontSize: 12,
                                fontWeight: 600,
                                cursor: "pointer",
                                letterSpacing: "0.02em"
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
                        backgroundColor: "rgba(8, 12, 10, 0.96)",
                        border: "1px solid rgba(34, 197, 94, 0.16)",
                        borderRadius: 18,
                        backdropFilter: "blur(20px)",
                        boxShadow: "0 12px 32px rgba(0,0,0,0.45)",
                        flexWrap: "wrap"
                    }}>
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
                                color: "#6ee7b7",
                                fontWeight: 700
                            }}>
                                Category
                            </span>
                            <input
                                type="text"
                                value={entryTopic}
                                onChange={(e) => setEntryTopic(e.target.value)}
                                placeholder="general"
                                style={{
                                    height: 38,
                                    borderRadius: 12,
                                    border: "1px solid rgba(255,255,255,0.08)",
                                    backgroundColor: "rgba(255,255,255,0.04)",
                                    color: "#d7efe2",
                                    padding: "0 12px",
                                    outline: "none",
                                    fontSize: 13,
                                    fontFamily: "Inter, sans-serif"
                                }}
                            />
                        </div>

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
                                    color: "#fca5a5",
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
                                        border: "1px solid rgba(255,255,255,0.08)",
                                        backgroundColor: "rgba(255,255,255,0.04)",
                                        color: "#d7efe2",
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
                    background:
                        "linear-gradient(180deg, rgba(10, 15, 13, 0.98) 0%, rgba(16, 24, 21, 0.98) 100%)",
                    borderRadius: 28,
                    border: "1px solid rgba(34, 197, 94, 0.18)",
                    backdropFilter: "blur(20px)",
                    boxShadow: "0 18px 48px rgba(0,0,0,0.62)"
                }}>
                    {/* Left Icons */}
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                        <button
                            onClick={() => setIsSkillsModalOpen(true)}
                            title="Upload file"
                            style={{
                                width: 32,
                                height: 32,
                                borderRadius: 8,
                                backgroundColor: "transparent",
                                border: "none",
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                cursor: "pointer",
                                color: "#888",
                                transition: "all 0.2s"
                            }}
                            onMouseEnter={(e) => e.currentTarget.style.color = "#fff"}
                            onMouseLeave={(e) => e.currentTarget.style.color = "#888"}
                        >
                            <Paperclip size={20} />
                        </button>
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
                            color: "#d7efe2",
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
