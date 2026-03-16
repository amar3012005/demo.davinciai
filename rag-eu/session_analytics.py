import json
import time
import logging
import re
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Prompt for generating brief session context/summary
SESSION_CONTEXT_PROMPT = """You are a senior analyst for DavinciAI. Summarize the following session.

### RULES:
1. **Focus on Substance**: Distinguish between meaningful business interaction and generic small talk. 
2. **Main Achievement**: What did the user actually accomplish or ask about in terms of branding, AI, or agency services?
3. **Outcome**: Was a lead generated? Was a technical question answered? Did the user leave satisfied or frustrated?
4. **Professionalism**: Use professional, clear, and objective language.
5. **No Noise**: Do not include 'The user started by...', 'Then they asked...'. Just the facts.
6. **PiI**: Strictly ensure no personal names or emails are included.

### OUTPUT FORMAT:
A maximum of 3 sentences. No JSON. No markdown.

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
        """Remove leaked CoT/thinking markup and conversational filler from model output."""
        if not text:
            return ""
        cleaned = text
        # Remove explicit think blocks
        cleaned = re.sub(r"<think>.*?</think>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        # Remove standalone think tags
        cleaned = re.sub(r"</?think>", " ", cleaned, flags=re.IGNORECASE)
        # Trim common reasoning prefaces that leak into final text
        prefaces = [
            r"^\s*(okay[, ]+(let's|let me|i will|let us).*?:?)\s*",
            r"^\s*(based on the (transcript|session).*?:?)\s*",
            r"^\s*(here is (a|the) (brief )?summary:?)\s*",
            r"^\s*(i need to (make|create|generate) (a|the) (concise )?summary:?)\s*",
            r"^\s*(the session (can be summarized as|summarizes to):?)\s*",
            r"^\s*(see\.\s+)",
            r"^\s*(summary:?)\s*"
        ]
        for p in prefaces:
            cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # If the model left "I need to..." at the end
        cleaned = re.sub(r"(i need to make a concise summary\.?)$", "", cleaned, flags=re.IGNORECASE).strip()
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
        business_signals = self._classify_business_signals(
            reasoning_output.get('session_summary', {}), 
            davinci_metrics, 
            reasoning_output.get('turns', []),
            raw_logs=raw_logs
        )
        
        # 5. Generate Brief Context (if not provided)
        if not brief_context:
            brief_context = await self._generate_brief_context(transcript, reasoning_output.get('session_summary', {}))
        brief_context = self._strip_reasoning_artifacts(brief_context)
        
        # 6. Fallback Knowledge Distillation (if primary reasoning found 0 units)
        distilled_knowledge = reasoning_output.get('distilled_knowledge', [])
        if not distilled_knowledge and transcript:
            try:
                from distillprompt_hivemind_savecase import CaseDistiller
                logger.info(f"🔄 Primary reasoning found 0 units. running fallback distillation for {session_id}...")
                fallback_prompt = CaseDistiller.get_prompt(transcript)
                fallback_res = await self.llm_provider.generate_messages(
                    messages=[
                        {"role": "system", "content": "You are a professional knowledge extractor. Output JSON list ONLY."},
                        {"role": "user", "content": fallback_prompt}
                    ],
                    model=self.model_name
                )
                fallback_units = CaseDistiller.clean_json_response(str(fallback_res))
                if fallback_units:
                    logger.info(f"✅ Fallback distillation extracted {len(fallback_units)} units")
                    distilled_knowledge = fallback_units
            except Exception as fe:
                logger.error(f"Fallback distillation failed: {fe}")

        # 7. Extract Final Report for Orchestrator/Backend
        report = {
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "brief_context": brief_context,
            "metrics": davinci_metrics,
            "business_signals": business_signals,
            "analysis": reasoning_output.get('session_summary', {}),
            "distilled_knowledge": distilled_knowledge,
            "analysis_quality": {
                "deterministic_metrics": True,
                "llm_heuristics_present": True,
                "fallback_used": (not reasoning_output.get('distilled_knowledge'))
            },
            "processing_time": round(time.time() - start_time, 2)
        }
        
        logger.info(f"Analytics complete for {session_id} in {report['processing_time']}s")
        if report.get("distilled_knowledge"):
            logger.info(f"🧠 Total extracted {len(report['distilled_knowledge'])} knowledge units")
        else:
            logger.warning(f"⚠️ All extraction passes found 0 knowledge units for {session_id}")
            
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
4. **Knowledge Distillation (AGGRESSIVE):** Identify ANY specific customer questions, technical inquiries, branding objections, feature requests, or core requirements that were successfully captured, resolved, or addressed by the Agent. 
   - Extract them as standalone knowledge units (Issue/Requirement/Preference vs Solution/Response pairs).
   - EVEN IF the question is general (e.g. "What is your philosophy?"), if the Agent provided a clear, definitive answer, EXTRACT IT. 
   - This knowledge will seed the Hive Mind to help future agents answer similar questions.
   - Look for: Inquiries about product features, branding philosophy (e.g. "Brand DNA", "Markenstimme"), team members (e.g. Amar), or technical setup.
   - IMPORTANT: If the assistant helped the user form a branding strategy, extract that strategy as a knowledge unit.

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
      "issue": "A concise description of the specific problem, objection, or question (e.g. 'What is the Blake AI platform?')",
      "solution": "The definitive answer or steps provided to resolve it (e.g. 'Blake (BLAIQ) is an EU-hosted AI platform for brand-aligned content.')",
      "category": "technical | branding | product_info | pricing | person_info",
      "reliability_score": 0.95
    }
  ]
}
## KNOWLEDGE DISTILLATION EXAMPLES
Example 1 (Technical): 
Issue: "How do I deploy with Docker?" 
Solution: "Use 'docker-compose up -d' after building."

Example 2 (General Inquiry):
Issue: "What is Blake?"
Solution: "Blake is the BLAIQ AI platform for brand-aligned content generation, hosted on ISO-27001 servers in the EU."

Identify any such valuable insights. Return ONLY valid JSON.
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
            logger.info(f"DEBUG: Reasoning raw response (first 500 chars): {raw[:500]}...")
            
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
            "that's not", "not correct", "you misunderstood", "cancel", "interrupt",
            "fail", "error", "didn't work"
        ]
        
        frustration_keywords = [
            "slow", "wait", "taking long", "taking so long", "hurry", "loading",
            "annoying", "boring", "useless", "stupid", "bad", "terrible", "waste of time",
            "frustrated", "don't like", "disappointing"
        ]

        def has_keyword(text: str, kws: List[str]) -> bool:
            return any(k in text for k in kws)

        corrections = sum(1 for t in user_turn_texts if has_keyword(t, correction_keywords))
        frustrations = sum(1 for t in user_turn_texts if has_keyword(t, frustration_keywords))
        
        # Sentiment-based penalties (turns with negative sentiment)
        neg_sentiment_turns = sum(1 for t in analyzed_turns if float(t.get("sentiment_score", 0)) < -0.3)

        total_user_turns = len(user_turn_texts)
        
        # Multi-factor IQ: Base 1.0, penalties for corrections, frustrations, and sustained negativity
        # Penalty weights: correction=15% per, frustration=10% per, neg_sentiment=5% per
        penalty = (corrections * 0.15) + (frustrations * 0.10) + (neg_sentiment_turns * 0.05)
        
        # Incorporate average sentiment into IQ: if avg sentiment is negative, it directly weighs down the score
        scores = [float(t.get("sentiment_score", 0) or 0) for t in analyzed_turns if isinstance(t.get("sentiment_score", 0), (int, float))]
        avg_sentiment = sum(scores) / len(scores) if scores else 0.0
        
        # Sentiment-based penalty: up to 30% penalty for deep dissatisfaction
        sentiment_penalty = max(0.0, -avg_sentiment * 0.3)
        agent_iq = max(0.0, 1.0 - (penalty + sentiment_penalty))

        # Velocity from correction density shift between first and second half.
        correction_flags = [1 if has_keyword(t, correction_keywords) else 0 for t in user_turn_texts]
        mid = max(1, total_user_turns // 2)
        start_rate = sum(correction_flags[:mid]) / len(correction_flags[:mid])
        end_rate = sum(correction_flags[mid:]) / len(correction_flags[mid:]) if correction_flags[mid:] else start_rate
        velocity_val = start_rate - end_rate  # positive means improving
        
        velocity = "STABLE"
        if velocity_val < -0.3:
            velocity = "CRITICAL_DEGRADATION"
        elif velocity_val > 0.3:
            velocity = "SUCCESSFUL_RECOVERY"

        scores = [float(t.get("sentiment_score", 0) or 0) for t in analyzed_turns if isinstance(t.get("sentiment_score", 0), (int, float))]
        
        return {
            "frustration_velocity": velocity,
            "agent_iq": round(agent_iq, 2),
            "avg_sentiment": round(sum(scores) / len(scores), 2) if scores else 0,
            "correction_count": corrections,
            "frustration_count": frustrations
        }

    def _classify_business_signals(self, summary: Dict[str, Any], metrics: Dict[str, Any], analyzed_turns: List[Dict[str, Any]] = None, raw_logs: List[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        high_value_intents = {"Purchase", "Demo_Request"}
        moderate_value_intents = {"Interest", "Inquiry"}
        
        detected_high_intent = False
        detected_moderate_intent = False
        
        # Substance check: Ignore short/trivial inquiries (like personal questions)
        substance_keywords = [
            "hire", "project", "brand", "agency", "cost", "pricing", "demo",
            "service", "help", "consult", "ai", "solution", "capability", "davinci"
        ]

        if analyzed_turns and raw_logs:
            user_texts = " ".join([str(l.get('content','')).lower() for l in raw_logs if l.get('role') == 'user'])
            has_substance = any(sk in user_texts for sk in substance_keywords)

            for turn in analyzed_turns:
                intent = turn.get("intent_type", "")
                if intent in high_value_intents:
                    detected_high_intent = True
                elif intent in moderate_value_intents:
                    detected_moderate_intent = True
            
            # Stricter Hot Lead Logic:
            # 1. High intent is always a lead if substance is present
            if detected_high_intent and has_substance:
                is_hot_lead = True
            # 2. Moderate intent requires clear positive sentiment and business substance
            elif detected_moderate_intent and has_substance and overall_sentiment > 0.2:
                is_hot_lead = True
            # 3. Exceptionally high sentiment in a long session (>3 user turns)
            elif overall_sentiment > 0.5 and len([l for l in raw_logs if l.get('role') == 'user']) > 3:
                is_hot_lead = True
            else:
                is_hot_lead = False
        else:
            is_hot_lead = False
        
        # Force false for churn risks or highly negative sessions
        if is_churn_risk or overall_sentiment < -0.3:
            is_hot_lead = False
        
        return {
            "is_churn_risk": is_churn_risk,
            "is_hot_lead": is_hot_lead,
            "priority_level": "HIGH" if is_churn_risk else ("HIGH" if is_hot_lead else "NORMAL")
        }
