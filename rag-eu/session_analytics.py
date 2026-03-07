import json
import time
import logging
import re
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Prompt for generating brief session context/summary
SESSION_CONTEXT_PROMPT = """You are a session summarizer for an AI assistant called TARA.
Your task is to create a brief, objective summary of what happened in this session.

### RULES:
1. **Be concise**: Maximum 2-3 sentences describing the main topic and outcome.
2. **Focus on substance**: What was the user trying to achieve? Was it resolved?
3. **Neutral tone**: Objective description without emotional language.
4. **Key elements**: Mention the main topic, any key actions taken, and the result.
5. **No PII**: Remove any personal identifiers, names, emails, or specific IDs.

### OUTPUT FORMAT:
Provide only the summary text, no JSON, no markdown, no meta-commentary.

### SESSION TRANSCRIPT:
{transcript}

BRIEF CONTEXT SUMMARY:"""

class SessionAnalytics:
    """
    DavinciAI Sentiment & Reasoning Pipeline.
    Implements a 5-phase analysis for post-session business intelligence.
    """

    def __init__(self, llm_provider: Any, model_name: str = "qwen/qwen3-32b"):
        self.llm_provider = llm_provider
        self.model_name = model_name

    @staticmethod
    def _strip_reasoning_artifacts(text: str) -> str:
        """Remove leaked CoT/thinking markup from model output."""
        if not text:
            return ""
        cleaned = text
        # Remove explicit think blocks
        cleaned = re.sub(r"<think>.*?</think>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        # Remove standalone think tags
        cleaned = re.sub(r"</?think>", " ", cleaned, flags=re.IGNORECASE)
        # Trim common reasoning prefaces that leak into final text (broadened)
        cleaned = re.sub(r"^\s*(okay[, ]+(let's|let me|i will|let us).*?:?)\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"^\s*(based on the (transcript|session).*?:?)\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def format_transcript(self, raw_logs: List[Dict[str, Any]]) -> str:
        """
        Phase 1: Data Ingestion & Serialization
        Normalizes raw logs into a clean, context-aware format including latency.
        """
        formatted_transcript = ""
        prev_timestamp = None
        
        for index, log in enumerate(raw_logs):
            curr_ts = log.get('timestamp', time.time())
            latency = 0
            if prev_timestamp is not None:
                latency = round(curr_ts - prev_timestamp, 2)
            
            role = log.get('role', 'unknown').capitalize()
            # Extract content from various possible field names
            content = log.get('content') or log.get('text') or log.get('message') or ""
            
            # Handle dict-style content (common in rich agent responses)
            if isinstance(content, dict):
                content = content.get('answer') or content.get('text') or content.get('content') or str(content)
            
            # Basic cleanup of extra whitespace or reasoning artifact leaks within the content itself
            content = self._strip_reasoning_artifacts(str(content))
            
            formatted_transcript += f"[Turn {index+1}] [{role}] (Latency: {latency}s): {content}\n"
            prev_timestamp = curr_ts
            
        return formatted_transcript

    async def analyze_session(self, raw_logs: List[Dict[str, Any]], session_id: str, brief_context: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes the full analytics pipeline.
        
        Args:
            raw_logs: Raw conversation logs
            session_id: Session identifier
            brief_context: Optional pre-computed brief context (will be generated if not provided)
        """
        start_time = time.time()
        
        # 1. Serialize
        transcript = self.format_transcript(raw_logs)
        
        # 2. Reasoning (LLM)
        reasoning_output = await self._run_reasoning_engine(transcript, session_id)
        
        # 3. Calculate Deterministic Metrics
        davinci_metrics = self._calculate_davinci_metrics(reasoning_output.get('turns', []), raw_logs)
        
        # 4. Classify Business Signals
        business_signals = self._classify_business_signals(reasoning_output.get('session_summary', {}), davinci_metrics, reasoning_output.get('turns', []))
        
        # 5. Generate Brief Context (if not provided)
        if not brief_context:
            brief_context = await self._generate_brief_context(transcript, reasoning_output.get('session_summary', {}))
        brief_context = self._strip_reasoning_artifacts(brief_context)
        
        # 6. Extract Final Report for Orchestrator/Backend
        report = {
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "brief_context": brief_context,
            "metrics": davinci_metrics,
            "business_signals": business_signals,
            "analysis": reasoning_output.get('session_summary', {}),
            "distilled_knowledge": reasoning_output.get('distilled_knowledge', []),
            "analysis_quality": {
                "deterministic_metrics": True,
                "llm_heuristics_present": True
            },
            "processing_time": round(time.time() - start_time, 2)
        }
        
        logger.info(f"Analytics complete for {session_id} in {report['processing_time']}s")
        return report

    async def _generate_brief_context(self, transcript: str, session_summary: Dict[str, Any]) -> str:
        """
        Generate a brief context/summary of what happened in the session.
        Uses LLM to create a concise 2-3 sentence summary.
        """
        try:
            # Use the reasoning model to generate context
            prompt = SESSION_CONTEXT_PROMPT.format(transcript=transcript[:3000])  # Limit transcript length
            
            response = await self.llm_provider.generate_messages(
                messages=[
                    {"role": "system", "content": "You are a session summarizer. Be concise and objective."},
                    {"role": "user", "content": prompt}
                ],
                model=self.model_name,
                max_tokens=150
            )
            
            # Clean up the response
            summary = response.strip() if isinstance(response, str) else response.get('content', '').strip()
            summary = self._strip_reasoning_artifacts(summary)
            
            # Fallback if response is too short or empty
            if not summary or len(summary) < 20:
                # Generate a basic fallback summary from session_summary
                resolution = session_summary.get('resolution_status', 'Unknown')
                sentiment = session_summary.get('overall_sentiment', 0)
                pain_points = session_summary.get('customer_pain_points', [])
                
                if pain_points:
                    main_topic = pain_points[0]
                    summary = f"Session focused on: {main_topic}. Resolution status: {resolution}."
                else:
                    summary = f"Support session completed. Resolution status: {resolution}."
            
            return summary
            
        except Exception as e:
            logger.error(f"Failed to generate brief context: {e}")
            # Return a minimal fallback summary
            resolution = session_summary.get('resolution_status', 'Unknown')
            return f"Support session completed. Resolution status: {resolution}."

    async def _run_reasoning_engine(self, transcript: str, session_id: str) -> Dict[str, Any]:
        """
        Phase 2: Reasoning Engine (LLM Interaction)
        Uses CoT prompting to extract structured sentiment and intent.
        """
        system_prompt = """
You are the DavinciAI Sentiment Engine. Your goal is to analyze the provided [Transcript] using Chain-of-Thought reasoning.

## ANALYSIS STEPS (Perform for EVERY User Turn)
1. **Context Check:** Look at the Agent's *previous* message. Did the Agent answer the question?
2. **Implicit Signal Detection:**
   - Did the user correct the agent? (Signal: Negative)
   - Did the user ignore the Agent's suggestion? (Signal: Disengagement)
   - Did the user thank the Agent specifically for *solving* the problem? (Signal: Resolution)
3. **Aspect Separation:** Distinguish between the User's mood vs. the Agent's performance.
4. **Knowledge Distillation (New):** Identify specific customer questions, objections, preferences, or core requirements that were successfully captured, resolved, or addressed by the Agent. Extract them as standalone knowledge units (Issue/Requirement/Preference vs Solution/Response pairs) for the team's Hive Mind. Use this for ANY domain including creative drafting, branding, and workflows.

## OUTPUT SCHEMA (JSON)
{
  "session_id": "string",
  "turns": [
    {
      "turn_number": int,
      "speaker": "user",
      "thought_trace": "Detailed thought process about sentiment and intent",
      "sentiment_score": float (-1.0 to 1.0),
      "emotional_labels": ["label1", "label2"],
      "intent_type": "Complaint | Inquiry | Purchase | Tech_Support | Interest | Demo_Request"
    }
  ],
  "session_summary": {
    "overall_sentiment": float,
    "resolution_status": "Resolved | Unresolved | Escalated",
    "customer_pain_points": ["point1", "point2"]
  },
  "distilled_knowledge": [
    {
      "issue": "A concise description of the specific problem, objection, or question",
      "solution": "The definitive answer, reframing, or steps provided to resolve it",
      "category": "e.g. technical, sales_objection, pricing, branding_insight",
      "reliability_score": float (0.0 to 1.0)
    }
  ]
}
## KNOWLEDGE DISTILLATION EXAMPLES
Example 1 (Technical): 
Issue: "How do I deploy with Docker?" 
Solution: "Use 'docker-compose up -d' after building."

Example 2 (Creative/Branding):
Issue: "What tone should we use for LinkedIn posts about AI?"
Solution: "A balance of technical efficiency (e.g. 30% speedup) and human impact stories (reclaiming creative time)."

Identify any such valuable insights regardless of industry. Return ONLY valid JSON.
"""
        user_prompt = f"Analyze the following transcript for session {session_id}:\n\n{transcript}"
        
        try:
            # Note: We use the Qwen reasoning model as requested
            response = await self.llm_provider.generate_messages(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                model=self.model_name,
                response_format={"type": "json_object"}
            )
            raw = response if isinstance(response, str) else json.dumps(response)
            raw = self._strip_reasoning_artifacts(raw)
            # Extract JSON object defensively if extra text leaked
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]
            parsed = json.loads(raw)
            # Basic shape hardening
            parsed.setdefault("turns", [])
            parsed.setdefault("session_summary", {"overall_sentiment": 0, "resolution_status": "Unknown", "customer_pain_points": []})
            parsed.setdefault("distilled_knowledge", [])
            return parsed
        except Exception as e:
            logger.error(f"Reasoning engine failed: {e}")
            return {"turns": [], "session_summary": {"overall_sentiment": 0, "resolution_status": "Unknown", "customer_pain_points": []}}

    def _calculate_davinci_metrics(self, analyzed_turns: List[Dict[str, Any]], raw_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Phase 3: Davinci Post-Processing Logic
        Deterministic metrics like Frustration Velocity and Agent IQ.
        """
        # Deterministic, log-derived metrics:
        # - correction_count: based on user utterances indicating correction/rejection
        # - frustration_velocity: based on early vs late user correction intensity
        # - agent_iq: 1 - (corrections / user_turns)
        user_turn_texts = []
        for log in raw_logs:
            role = str(log.get("role", "")).strip().lower()
            if role == "user":
                txt = (log.get("content") or log.get("text") or "").strip().lower()
                if txt:
                    user_turn_texts.append(txt)

        if not user_turn_texts:
            scores = [float(t.get("sentiment_score", 0) or 0) for t in analyzed_turns if isinstance(t.get("sentiment_score", 0), (int, float))]
            return {
                "frustration_velocity": "STABLE",
                "agent_iq": 1.0,
                "avg_sentiment": round(sum(scores) / len(scores), 2) if scores else 0.0,
                "correction_count": 0
            }

        correction_keywords = [
            "no", "not what i meant", "wrong", "listen", "again", "stop",
            "that's not", "not correct", "you misunderstood", "cancel", "interrupt"
        ]

        def has_correction(text: str) -> bool:
            return any(kw in text for kw in correction_keywords)

        correction_flags = [1 if has_correction(t) else 0 for t in user_turn_texts]
        corrections = sum(correction_flags)
        total_user_turns = len(user_turn_texts)

        # Velocity from correction density shift between first and second half.
        mid = max(1, total_user_turns // 2)
        start_rate = sum(correction_flags[:mid]) / len(correction_flags[:mid])
        end_rate = sum(correction_flags[mid:]) / len(correction_flags[mid:]) if correction_flags[mid:] else start_rate
        velocity_val = start_rate - end_rate  # positive means improving
        
        velocity = "STABLE"
        if velocity_val < -0.4:
            velocity = "CRITICAL_DEGRADATION"
        elif velocity_val > 0.4:
            velocity = "SUCCESSFUL_RECOVERY"

        agent_iq = max(0.0, 1.0 - (corrections / total_user_turns))
        scores = [float(t.get("sentiment_score", 0) or 0) for t in analyzed_turns if isinstance(t.get("sentiment_score", 0), (int, float))]
        
        return {
            "frustration_velocity": velocity,
            "agent_iq": round(agent_iq, 2),
            "avg_sentiment": round(sum(scores) / len(scores), 2) if scores else 0,
            "correction_count": corrections
        }

    def _classify_business_signals(self, summary: Dict[str, Any], metrics: Dict[str, Any], analyzed_turns: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Phase 4: Predictive Classification
        Convert math into business value.
        
        is_hot_lead is TRUE when:
          - User shows interest in the service/product (intent: Interest, Demo_Request, Purchase)
          - User engages positively (sentiment > 0.3 and no churn risk)
          - User asks questions about pricing, availability, or next steps
        """
        overall_sentiment = summary.get('overall_sentiment', 0)
        resolution = summary.get('resolution_status', 'Unknown')
        
        is_churn_risk = (
            (overall_sentiment < -0.5 and resolution != "Resolved")
            or (metrics['frustration_velocity'] == "CRITICAL_DEGRADATION")
        )
        
        # Hot lead detection: check intent types from LLM analysis
        hot_intents = {"Purchase", "Interest", "Demo_Request", "Inquiry"}
        detected_hot_intent = False
        if analyzed_turns:
            for turn in analyzed_turns:
                intent = turn.get("intent_type", "")
                if intent in hot_intents:
                    detected_hot_intent = True
                    break
        
        # A user is a hot lead if they showed interest/curiosity AND sentiment is not negative
        is_hot_lead = detected_hot_intent and overall_sentiment > -0.2 and not is_churn_risk
        
        # Even without explicit intent, positive engagement signals interest
        if not is_hot_lead and overall_sentiment > 0.3 and not is_churn_risk:
            is_hot_lead = True
        
        return {
            "is_churn_risk": is_churn_risk,
            "is_hot_lead": is_hot_lead,
            "priority_level": "HIGH" if is_churn_risk else "NORMAL"
        }
