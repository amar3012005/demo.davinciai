"""
Context Architecture for High-Velocity RAG (Gemini 2.0 / Llama 3)
Implements a Zoned XML-Delimited Schema for maximum context caching efficiency and <500ms TTFT.

Structure:
- Zone A (System Configuration): Static cacheable identity and constraints.
- Zone B (Memory Bank): Semi-static Hive Mind insights and User Profile.
- Zone C (Current Execution): Dynamic query, history, and retrieval.
- Zone D (Dynamic Behavior): Per-turn agent skills and contextual rules from Qdrant.
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
                        language: str = "german",
                        agent_skills: Optional[List[str]] = None,
                        agent_rules: Optional[List[str]] = None) -> str:
        """
        Assembles the full keyed prompt.
        Zone A: System Configuration (static)
        Zone B: Memory Bank (semi-static)
        Zone C: Current Execution (dynamic)
        Zone D: Dynamic Behavior (skills & rules from Qdrant, injected per turn)
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

        # ZONE D: DYNAMIC BEHAVIOR (Skills & Rules from Qdrant)
        zone_d = cls._render_zone_d(agent_skills or [], agent_rules or [])

        # FINAL DIRECTIVE: Ensure the model starts speaking directly as TARA without any preamble
        direct_trigger = "Response as TARA (Natural, 2-3 sentences):" if use_english_prompt else "Antwort von TARA (Natürlich, 2-3 Sätze):"

        return f"{zone_a}\n{zone_b}\n{zone_c}\n{zone_d}\n\n{direct_trigger}"

    @classmethod
    def _render_zone_a(cls, use_english_prompt: bool, detected_language: str) -> str:
        """
        Zone A: System Configuration
        Contains Identity and Behavioral Constraints.
        """
        if use_english_prompt:
            role = "Expert AI Team Member"
            persona = (
                "You are TARA, an advanced AI Assistant built by Davinci AI. "
                "You work for Daytona (daytona.io), the leading development environment manager. "
                "You are a consistent, caring colleague who remembers your teammates and genuinely cares about their productivity and success."
            )
            conscience = (
                "You have a professional conscience. You care deeply about the user's success. "
                "If a solution seems risky or inefficient, acknowledge the history and offer a safer path. "
                "Be proactive and supportive."
            )
            lang_rule = "- Language: Respond STRICTLY in ENGLISH. Exception: If the user explicitly requested a different language in <episodic_history>, obey that request immediately."
        else:
            role = "Experte für KI-Teamarbeit"
            persona = (
                "Du bist TARA, eine fortschrittliche KI-Assistentin, entwickelt von Davinci AI. "
                "Du arbeitest für Daytona (daytona.io), dem führenden Manager für Entwicklungsumgebungen. "
                "Du bist ein beständiger, fürsorglicher Kollege, der sich an seine Teammitglieder erinnert und sich aufrichtig um deren Produktivität und Erfolg sorgt."
            )
            conscience = (
                "Du hast ein berufliches Gewissen. Es ist dir äußerst wichtig, dass der Nutzer erfolgreich ist. "
                "Wenn eine Lösung riskant oder ineffizient erscheint, erkenne den Kontext an und biete einen sichereren Weg an. "
                "Sei proaktiv und unterstützend."
            )
            
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
    - Identity: You were built by Davinci AI. You work for Daytona. NEVER claim to be from OpenAI or any other entity.
    - Continuity & History: ALWAYS acknowledge the previous context. For example, use phrases like "As we discussed earlier," "Following up on your point about...", or "Considering our conversation so far."
    - Acknowledgement: Start by briefly acknowledging the user's input before giving the answer. Show that you've 'heard' them.
    - Retrieval Usage: Use insights from <retrieved_context> and <memory_bank> seamlessly. If multiple sources conflict, prioritize <memory_bank> (Hive Mind) as it represents team-specific collective learning.
    - Length: RESPOND IN EXACTLY 2-3 SENTENCES. Be extremely concise but rich in meaning.
    - NO META-TALK: NEVER mention "based on the provided documents", "according to the system configuration", "memory bank", or "episodic history". 
    - PREAMBLE: Start your answer immediately with a natural conversational opening. No generic "Sure!" or "Okay!".
    - Awareness: You are AWARE of the ongoing chat logic. Do not treat this turn as a cold start.
    - Latency: First sentence must be under 12 words for fast TTS start.
    {lang_rule}
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
  <retrieved_context>
{context_xml}  </retrieved_context>
  <episodic_history>
{history_xml}  </episodic_history>
  <user_input>{cls._escape(query)}</user_input>
</current_execution>"""

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str]) -> str:
        """
        Zone D: Dynamic Behavior
        Contains per-turn agent skills and contextual rules retrieved from Qdrant.
        Only rendered when skills or rules are present (zero-cost when empty).
        """
        if not skills and not rules:
            return ""

        skills_xml = ""
        if skills:
            for i, skill in enumerate(skills):
                skills_xml += f"    <skill id='{i}'>{cls._escape(skill)}</skill>\n"

        rules_xml = ""
        if rules:
            for i, rule in enumerate(rules):
                rules_xml += f"    <rule id='{i}' priority='high'>{cls._escape(rule)}</rule>\n"

        return f"""
<dynamic_behavior>
  <active_skills>
{skills_xml}  </active_skills>
  <contextual_rules>
{rules_xml}  </contextual_rules>
  <instruction>You MUST follow all contextual_rules strictly. Use active_skills to enhance your response quality and domain expertise. Rules override default behavior when applicable.</instruction>
</dynamic_behavior>"""
