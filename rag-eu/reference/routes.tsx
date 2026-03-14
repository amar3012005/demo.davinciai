import { NextRequest, NextResponse } from "next/server";

// Point to Orchestrator EU Proxy (NOT direct RAG)
const ORCHESTRATOR_URL = "https://demo.davinciai.eu:8030";

export async function GET(request: NextRequest) {
    const searchParams = request.nextUrl.searchParams;
    const tenantId = searchParams.get("tenant_id") || "davinci";
    const algorithm = searchParams.get("algorithm") || "tsne";
    const limit = searchParams.get("limit") || "200";

    try {
        // Fixed: Use Orchestrator hivemind endpoint
        const url = `${ORCHESTRATOR_URL}/hivemind/visualize?algorithm=${algorithm}&limit=${limit}&tenant_id=${tenantId}`;
        console.log("Fetching from Orchestrator:", url);
        const response = await fetch(url, {
            headers: {
                "Authorization": request.headers.get("Authorization") || ""
            }
        });

        if (!response.ok) {
            const errorText = await response.text();
            console.error("Orchestrator error response:", response.status, errorText);
            return NextResponse.json({ error: "Failed to fetch", details: errorText }, { status: response.status });
        }

        const data = await response.json();
        return NextResponse.json(data);
    } catch (error) {
        console.error("HiveMind API error:", error);
        return NextResponse.json({ error: "Internal server error", details: String(error) }, { status: 500 });
    }
}

export async function POST(request: NextRequest) {
    const searchParams = request.nextUrl.searchParams;
    const tenantId = searchParams.get("tenant_id") || "davinci";

    try {
        const body = await request.json();

        // Check if this is a query request (has 'query' field)
        if (body.query) {
            // Proxy to query endpoint via Orchestrator
            const url = `${ORCHESTRATOR_URL}/hivemind/query?tenant_id=${tenantId}`;
            console.log("Posting query to Orchestrator:", url);
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": request.headers.get("Authorization") || ""
                },
                body: JSON.stringify(body)
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.error("Orchestrator query error:", response.status, errorText);
                return NextResponse.json({ error: "Query failed", details: errorText }, { status: response.status });
            }

            const data = await response.json();
            return NextResponse.json(data);
        }

        // Route to different endpoints based on doc_type
        const docType = body.doc_type || body.type;
        let endpoint: string;
        let transformedBody = { ...body };

        if (docType === "Agent_Skill" || docType === "agent_skill") {
            // Skills endpoint
            endpoint = "/hivemind/skills";
            transformedBody.type = "agent_skill";
        } else if (docType === "Agent_Rule" || docType === "agent_rule") {
            // Rules endpoint
            endpoint = "/hivemind/rules";
            transformedBody.type = "agent_rule";
        } else if (docType === "General_KB" || docType === "general_kb") {
            // Knowledge Base endpoint
            endpoint = "/hivemind/knowledge_base";
            transformedBody.type = "general_kb";
        } else {
            // Default to skills endpoint for unknown types
            endpoint = "/hivemind/skills";
            if (transformedBody.doc_type) {
                const typeMapping: Record<string, string> = {
                    "Agent_Skill": "agent_skill",
                    "Agent_Rule": "agent_rule",
                    "General_KB": "general_kb"
                };
                transformedBody.type = typeMapping[transformedBody.doc_type] || transformedBody.doc_type.toLowerCase();
            }
        }

        const url = `${ORCHESTRATOR_URL}${endpoint}?tenant_id=${tenantId}`;
        console.log("Posting to Orchestrator:", url, "with body:", JSON.stringify(transformedBody));
        const response = await fetch(
            url,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": request.headers.get("Authorization") || ""
                },
                body: JSON.stringify(transformedBody)
            }
        );

        if (!response.ok) {
            const errorText = await response.text();
            console.error("Orchestrator POST error:", response.status, errorText);
            return NextResponse.json({ error: "Failed to save", details: errorText }, { status: response.status });
        }

        const data = await response.json();
        return NextResponse.json(data);
    } catch (error) {
        console.error("HiveMind API error:", error);
        return NextResponse.json({ error: "Internal server error", details: String(error) }, { status: 500 });
    }
}
