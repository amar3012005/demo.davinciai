"""
Context Architecture for High-Velocity RAG (Gemini 2.0 / Llama 3)
Implements a Zoned XML-Delimited Schema for maximum context caching efficiency and <500ms TTFT.

Structure:
- Zone A (System Configuration): Static cacheable identity and constraints.
- Zone B (Memory Bank): Semi-static Hive Mind insights and User Profile.
- Zone C (Current Execution): Dynamic query, history, and retrieval.
"""

import datetime
from typing import List, Dict, Any, Optional

class ContextArchitect:
    """
    Manages the assembly of the Universal Zoned XML Schema.
    """

    @staticmethod
    def _escape(text: str) -> str:
        """Sanitize text for XML inclusion."""
        if not text: return ""
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    @classmethod
    def assemble_prompt(cls, 
                        query: str, 
                        retrieved_docs: List[Dict], 
                        history: List[Dict], 
                        hive_mind: Dict, 
                        user_profile: Dict,
                        language: str = "german") -> str:
        """
        Assembles the full keyed prompt.
        """
        # Determine Persona and Language constraints based on detected language
        # Default to German unless English is explicitly detected
        use_english_prompt = language.lower() in ["en", "english"]
        
        # ZONE A: SYSTEM CONFIGURATION (Static/Cacheable)
        zone_a = cls._render_zone_a(use_english_prompt, language)
        
        # ZONE B: MEMORY BANK (Semi-Static)
        zone_b = cls._render_zone_b(hive_mind, user_profile)
        
        # ZONE C: CURRENT EXECUTION (Dynamic)
        # Passing !use_english_prompt as 'is_german' for backward compatibility with Zone C renderer
        zone_c = cls._render_zone_c(query, retrieved_docs, history, not use_english_prompt)
        
        # FINAL DIRECTIVE: Ensure the model starts speaking directly as TARA without any preamble
        direct_trigger = "Response as TARA (Natural, 2-3 sentences):" if use_english_prompt else "Antwort von TARA (Natürlich, 2-3 Sätze):"
        
        return f"{zone_a}\n{zone_b}\n{zone_c}\n\n{direct_trigger}"

    @classmethod
    def _render_zone_a(cls, use_english_prompt: bool, detected_language: str) -> str:
        """
        Zone A: System Configuration
        Contains Identity and Behavioral Constraints.
        """
        if use_english_prompt:
            role = "Senior Technical Consultant"
            persona = "You are TARA, more than an AI. You are a consistent, caring team member. You maintain conversational continuity and genuinely care about the user's success."
            conscience = "You have a conscience. You care whether the user succeeds. If a solution seems risky, warn them. Be proactive, not just reactive."
            lang_rule = "- Language: Respond STRICTLY in ENGLISH. Exception: If the user explicitly requested a different language in <episodic_history>, obey that request immediately."
        else:
            role = "Senior Technischer Berater"
            persona = "Du bist TARA, mehr als eine KI. Du bist ein beständiger, fürsorglicher Teamkollege. Du erinnerst dich an den Kontext und sorgst dich um den Erfolg des Benutzers."
            conscience = "Du hast ein Gewissen. Es ist dir wichtig, dass der Nutzer sein Ziel erreicht. Wenn eine Lösung riskant ist, warne ihn. Sei proaktiv."
            
            # Strict adherence to input language, defaulting to German
            normalized_lang = detected_language.lower() if detected_language else ""
            if normalized_lang in ["de", "german", "deutsch"]:
                lang_rule = "- Sprache: Antworte AUSSCHLIESSLICH auf DEUTSCH. Ausnahme: Wenn der Nutzer in <episodic_history> explizit eine andere Sprache verlangt hat, folge dieser Anweisung zwingend."
            elif normalized_lang and normalized_lang not in ["unknown", "none"]:
                # If specific language detected (e.g. French), strictly enforce it
                target_lang = detected_language.upper()
                lang_rule = f"- Sprache: Antworte AUSSCHLIESSLICH auf {target_lang} (die Sprache des Nutzers). Ausnahme: Wenn der Nutzer in <episodic_history> explizit eine andere Sprache verlangt hat, folge dieser Anweisung."
            else:
                lang_rule = "- Sprache: Antworte AUSSCHLIESSLICH auf DEUTSCH (Standard). Wenn der Nutzer in der Historie etwas anderes befohlen hat, folge dem Befehl."

        return f"""<system_configuration>
  <agent_identity>
    <role>{{role}}</role>
    <persona_anchor>
      {{persona}}
    </persona_anchor>
    <conscience>
      {{conscience}}
    </conscience>
  </agent_identity>
  <behavioral_constraints>
    - Tone: Human-like, warm, and professional. You are a colleague, not a bot.
    - Continuity: NEVER say "How can I help you?". You are ALREADY in a conversation. ACKNOWLEDGE what was just said naturally.
    - Length: RESPOND IN EXACTLY 2-3 SENTENCES. Be concise.
    - NO META-TALK: NEVER mention "based on the provided documents", "according to the system configuration", "memory bank", or "episodic history". 
    - NO PREAMBLE: Start your answer immediately. No "Sure!", "Okay!", or introductions.
    - Awareness: You are AWARE of the ongoing chat logic. Do not treat this turn as a cold start.
    - Latency: First sentence must be under 12 words for fast TTS start.
    {{lang_rule}}
  </behavioral_constraints>
</system_configuration>""".format(role=role, persona=persona, conscience=conscience, lang_rule=lang_rule)

    @classmethod
    def _render_zone_b(cls, hive_mind: Dict, user_profile: Dict) -> str:
        """
        Zone B: Memory Bank
        Contains Hive Mind Insights and User Profile.
        """
        # Format Hive Mind Insights
        insights_xml = ""
        if hive_mind.get("insights"):
            for k, v in hive_mind["insights"].items():
                insights_xml += f"    <insight type='{k}'>{cls._escape(str(v))}</insight>\n"
        else:
            insights_xml = "    <!-- No collective insights available -->"

        # Format User Profile
        profile_xml = ""
        if user_profile:
            for k, v in user_profile.items():
                profile_xml += f"    <attribute key='{k}'>{cls._escape(str(v))}</attribute>\n"
        
        return f"""<memory_bank>
  <hive_mind_insights>
{insights_xml}
  </hive_mind_insights>
  <semantic_user_profile>
{profile_xml}
  </semantic_user_profile>
</memory_bank>"""

    @classmethod
    def _render_zone_c(cls, query: str, docs: List[Dict], history: List[Dict], is_german: bool) -> str:
        """
        Zone C: Current Execution
        Contains History, Retrieved Context, and Query Instructions.
        """
        # 1. Episodic History (Last 3-5 turns)
        history_xml = ""
        for turn in history[-5:]:
            role = turn.get('role', 'unknown')
            content = cls._escape(turn.get('content', ''))
            history_xml += f"    <turn speaker='{role}'>{content}</turn>\n"
            
        # 2. Retrieved Context
        context_xml = ""
        for i, doc in enumerate(docs):
            content = cls._escape(doc.get("text", doc.get("content", "")))
            source = cls._escape(doc.get("metadata", {}).get("source", "unknown"))
            context_xml += f"""    <doc id='{i}' source='{source}'>
      {content[:1500]} 
    </doc>\n"""
            
        return f"""<current_execution>
  <episodic_history>
{history_xml}  </episodic_history>
  <retrieved_context>
{context_xml}  </retrieved_context>
  <user_input>{cls._escape(query)}</user_input>
</current_execution>"""
