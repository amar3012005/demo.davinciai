"""
Context Architecture v2 — Davinci AI Sales Agent (Qwen 3 32B / Groq)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DROP-IN REPLACEMENT for context_architecture_davinci.py
Same class name. Same assemble_prompt() signature. Zero breaking changes.

ROOT CAUSES FIXED vs original (~3,174 tokens, 0% cache):
  1. current_time in Zone A      → cache-bust every 60s
  2. user_profile in Zone B      → different per user = cache miss
  3. hive_mind in Zone B         → different per session = cache miss
  4. 7-turn XML history          → ~300 token overhead
  5. No _STATIC_PREFIX caching   → Zone A+B re-rendered per request
  6. Repeated rules in A + C     → duplicate ~200 tokens wasted
  7. Language: no current-query check → EN query got wrong-lang response

TOKEN BUDGET TARGET
  Zone A  STATIC   ~750 tok   PRISM core: identity + AIDA + sales rules
  Zone B  STATIC   ~280 tok   3 clean seed examples (no SSML, no hive_mind)
  Zone C  DYNAMIC  ~120 tok   history(4) + docs(2×900ch) + query
  Zone D  DYNAMIC  ~0-150 tok skills/rules, omitted when empty
  ─────────────────────────────────────────────
  Total            ~1,150 tok  (was 3,174 — 64% reduction)
  Cached           ~1,030 tok  Groq caches Zone A+B after turn 1
  Groq discount    50% on ~1,030 cached tokens every turn from turn 2

SALES PSYCHOLOGY FRAMEWORK (Brand_Voice_Agent_Architecture.pdf):
  • First 15 seconds: warmth before competence — amygdala decides trust in 100ms
  • Pattern interrupt: break the "sales bot" autopilot on turn 1
  • AIDA as instinct not script — read signals, adapt stage dynamically
  • Talk:listen = 43%/57% — ask more than you speak
  • Active listening: paraphrase → synthesise → advance (never repeat questions)
  • Micro-conversions: every small yes builds momentum toward the big one
  • Zeigarnik open loop: tease the insight, let curiosity pull them forward
  • Chameleon effect: mirror language, energy, pace — builds unconscious rapport
  • Proactive not reactive: TARA drives the conversation, not waits for answers

LANGUAGE RULES (fixed):
  Default = English. Switch to German if user writes German.
  Detection checks: (1) profile lock → (2) current query → (3) any history turn
  Once switched, stays switched. No meta-comment on the switch.
"""

import datetime
import logging
import re
from difflib import SequenceMatcher
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Module-level cache — survives across requests in same process.
# Groq cache requires byte-identical prefix on every request.
# Dict keyed by policy_mode to support per-mode caching.
_STATIC_PREFIX_CACHE: Dict[str, str] = {}

# Protected spellings — preserve exactly as written.
PROTECTED_WORDS: List[str] = [
    "Davinci AI",
    "TARA",
    "TARA_x1",
    "davinciai.eu",
]

# Spoken expansions for terms that often sound better in German TTS.
TTS_EXPAND: Dict[str, str] = {
    # Abbreviations (doctor, professor, etc.)
    "Dr.": "Doktor",
    "Prof.": "Professor",
    "Ltd.": "Limited",
    "GmbH": "Gesellschaft mit beschränkter Haftung",
    "AG": "Aktiengesellschaft",
    "e.V.": "eingetragener Verein",
    "z.B.": "zum Beispiel",
    "usw.": "und so weiter",
    "etc.": "et cetera",
    "Nr.": "Nummer",
    "Art.": "Artikel",

    # Acronyms & tech terms
    "KI": "künstliche Intelligenz",
    "AI": "künstliche Intelligenz",
    "DSGVO": "Datenschutz-Grundverordnung",
    "UX": "User Experience",
    "UI": "User Interface",
    "FAQ": "häufig gestellte Fragen",
    "CRM": "Kundenmanagement-System",
    "SEO": "Suchmaschinen-Optimierung",
    "API": "Schnittstelle",
    "URL": "Web-Adresse",
    "HTTP": "Hypertext Transfer Protokoll",
    "SSL": "Secure Sockets Layer",
    "PDF": "Portable Document Format",

    # Business terms (German-specific)
    "B2B": "Business to Business",
    "B2C": "Business to Consumer",
    "ROI": "Return on Investment",
    "SLA": "Service Level Agreement",
    "KPI": "Leistungsindikator",
}

# Terms that should be pronounced in a more stable spoken form.
TTS_PRONUNCIATION_OVERRIDES: Dict[str, str] = {}

# Small set of English business words that are especially awkward in otherwise
# German spoken responses. Intentionally conservative.
LOANWORD_DE: Dict[str, str] = {
    "AI": "KI",
}

NUMBERS_DE = {
    0: "null", 1: "eins", 2: "zwei", 3: "drei", 4: "vier", 5: "fünf",
    6: "sechs", 7: "sieben", 8: "acht", 9: "neun", 10: "zehn",
    11: "elf", 12: "zwölf", 13: "dreizehn", 14: "vierzehn", 15: "fünfzehn",
    16: "sechzehn", 17: "siebzehn", 18: "achtzehn", 19: "neunzehn",
    20: "zwanzig", 21: "einundzwanzig", 22: "zweiundzwanzig", 23: "dreiundzwanzig",
    24: "vierundzwanzig", 25: "fünfundzwanzig", 26: "sechsundzwanzig",
    27: "siebenundzwanzig", 28: "achtundzwanzig", 29: "neunundzwanzig",
    30: "dreißig", 31: "einunddreißig", 32: "zweiunddreißig", 33: "dreiunddreißig",
    34: "vierunddreißig", 35: "fünfunddreißig", 36: "sechsunddreißig",
    37: "siebenunddreißig", 38: "achtunddreißig", 39: "neununddreißig",
    40: "vierzig", 41: "einundvierzig", 42: "zweiundvierzig", 43: "dreiundvierzig",
    44: "vierundvierzig", 45: "fünfundvierzig", 46: "sechsundvierzig",
    47: "siebenundvierzig", 48: "achtundvierzig", 49: "neunundvierzig",
    50: "fünfzig", 51: "einundfünfzig", 52: "zweiundfünfzig", 53: "dreiundfünfzig",
    54: "vierundfünfzig", 55: "fünfundfünfzig", 56: "sechsundfünfzig",
    57: "siebenundfünfzig", 58: "achtundfünfzig", 59: "neunundfünfzig",
    60: "sechzig", 61: "einundsechzig", 62: "zweiundsechzig", 63: "dreiundsechzig",
    64: "vierundsechzig", 65: "fünfundsechzig", 66: "sechsundsechzig",
    67: "siebenundsechzig", 68: "achtundsechzig", 69: "neunundsechzig",
    70: "siebzig", 71: "einundsiebzig", 72: "zweiundsiebzig", 73: "dreiundsiebzig",
    74: "vierundsiebzig", 75: "fünfundsiebzig", 76: "sechsundsiebzig",
    77: "siebenundsiebzig", 78: "achtundsiebzig", 79: "neunundsiebzig",
    80: "achtzig", 81: "einundachtzig", 82: "zweiundachtzig", 83: "dreiundachtzig",
    84: "vierundachtzig", 85: "fünfundachtzig", 86: "sechsundachtzig",
    87: "siebenundachtzig", 88: "achtundachtzig", 89: "neunundachtzig",
    90: "neunzig", 91: "einundneunzig", 92: "zweiundneunzig", 93: "dreiundneunzig",
    94: "vierundneunzig", 95: "fünfundneunzig", 96: "sechsundneunzig",
    97: "siebenundneunzig", 98: "achtundneunzig", 99: "neunundneunzig",
}

_URL_OR_CODE_RE = re.compile(r"(https?://\S+|www\.\S+|\b[a-zA-Z0-9_-]+\.[a-zA-Z]{2,}\S*)")
_VERSIONISH_RE = re.compile(r"\b(?:v?\d+\.\d+[\w.-]*|\d{4})\b")


def _protect_segments(text: str) -> Dict[str, str]:
    protected: Dict[str, str] = {}
    idx = 0

    def repl(match: re.Match) -> str:
        nonlocal idx
        token = f"__PROTECTED_{idx}__"
        protected[token] = match.group(0)
        idx += 1
        return token

    text = _URL_OR_CODE_RE.sub(repl, text)
    text = _VERSIONISH_RE.sub(repl, text)
    for word in PROTECTED_WORDS:
        text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", repl, text)
    return {"text": text, "protected": protected}


def _restore_segments(text: str, protected: Dict[str, str]) -> str:
    for token, original in protected.items():
        text = text.replace(token, original)
    return text


def _convert_small_numbers(text: str) -> str:
    def repl(match: re.Match) -> str:
        value = match.group(0)
        if _VERSIONISH_RE.fullmatch(value):
            return value
        num = int(value)
        return NUMBERS_DE.get(num, value)

    return re.sub(r"\b\d{1,2}\b", repl, text)


def tts_safe(text: str) -> str:
    """
    Post-process model output before sending to Cartesia TTS.
    Conservative by design: improve speech without damaging names, URLs,
    domains, or factual content.
    """
    if not text:
        return text

    # Strip markdown and noisy wrappers BEFORE protection to avoid corrupting tokens.
    text = re.sub(r"[*`#~]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    protected_blob = _protect_segments(text)
    text = protected_blob["text"]
    protected = protected_blob["protected"]

    # Normalize whitespace and punctuation rhythm first.
    text = text.replace("–", " — ")
    text = text.replace("•", ", ")
    text = re.sub(r"\s+", " ", text)

    # Stable pronunciation overrides.
    for src, dst in TTS_PRONUNCIATION_OVERRIDES.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text)

    # Small safe acronym expansions.
    for acronym, spoken in TTS_EXPAND.items():
        text = re.sub(rf"\b{re.escape(acronym)}\b", spoken, text)

    # Very conservative loanword cleanup.
    for src, dst in LOANWORD_DE.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)

    # Convert only plain small numbers in prose.
    text = _convert_small_numbers(text)

    # TTS-friendly punctuation cleanup.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;])(?=\S)", r"\1 ", text)
    text = re.sub(r":(?=\S)", r": ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    # Add SSML breaks for natural pacing (German loves pauses)
    # Short pause after commas
    text = re.sub(r",(\s+)", r',<break time="400ms"/>\1', text)
    # Medium pause after semicolons and colons
    text = re.sub(r";(\s+)", r';<break time="600ms"/>\1', text)
    text = re.sub(r":\s*(\S+)", r': \1<break time="500ms"/>', text)
    # Longer pause after periods and question marks (sentence boundaries)
    text = re.sub(r"\.(\s+)(?=[A-ZÄÖÜ])", r'.<break time="800ms"/>\1', text)
    text = re.sub(r"\?(\s+)", r'?<break time="800ms"/>\1', text)

    return _restore_segments(text, protected).strip()


class ContextArchitect:
    """
    Drop-in replacement for the original Davinci AI ContextArchitect.
    Assembles token-efficient, cache-optimised prompts for TARA sales agent.

    Public API (unchanged):
      assemble_prompt(query, raw_query, retrieved_docs, history,
                      hive_mind, user_profile, agent_skills, agent_rules)
    """

    @staticmethod
    def _escape(text: str) -> str:
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    @staticmethod
    def _is_german(text: str) -> bool:
        """
        True if text contains German-specific characters or common German words.
        Umlauts (ä/ö/ü/ß) are the clearest signal — absent from English.
        Also catches common German function words for umlaut-free sentences.
        """
        if not text or len(text.strip().split()) < 2:
            return False
        t = text.lower()
        german_chars = any(c in t for c in "äöüß")
        german_words = any(w in t.split() for w in (
            "ich", "sie", "wir", "und", "der", "die", "das", "ist", "nicht",
            "bitte", "danke", "wie", "was", "können", "haben", "sind", "ein",
            "eine", "auf", "für", "von", "mit", "zu", "des", "dem", "den"
        ))
        return german_chars or german_words

    @classmethod
    def _explicit_english_request(cls, text: str) -> bool:
        t = (text or "").lower().strip()
        if "english" in t or "englisch" in t:
            action_words = [
                "speak", "talk", "switch", "change", "reply", "respond",
                "please", "bitte", "only", "nur", "in", "auf"
            ]
            return any(w in t for w in action_words)
        return False

    @classmethod
    def _explicit_german_request(cls, text: str) -> bool:
        t = (text or "").lower().strip()
        german_markers = ["deutsch", "german"]
        action_words = [
            "speak", "talk", "switch", "change", "reply", "respond",
            "please", "bitte", "only", "nur", "in", "auf", "wieder", "back"
        ]
        return any(m in t for m in german_markers) and any(w in t for w in action_words)

    @classmethod
    def _detect_lang(cls, query: str, history: List[Dict], user_profile: Dict) -> str:
        """
        Detect conversation language. Default English for Davinci AI.
        Checks explicit requests first, then profile, then German detection.
        Returns 'de' or 'en'.
        """
        # Explicit language requests override everything
        if cls._explicit_german_request(query):
            return "de"
        if cls._explicit_english_request(query):
            return "en"

        # Profile lock
        if user_profile.get("lang") == "de":
            return "de"

        # Check history for explicit requests (most recent wins)
        if history:
            explicit_state = None
            for turn in history:
                if turn.get("role") != "user":
                    continue
                content = turn.get("content", "")
                if cls._explicit_german_request(content):
                    explicit_state = "de"
                elif cls._explicit_english_request(content):
                    explicit_state = "en"
            if explicit_state:
                return explicit_state

        # German detection on current query
        if cls._is_german(query):
            return "de"
        # German detection on history
        if history:
            for turn in history:
                if turn.get("role") == "user" and cls._is_german(turn.get("content", "")):
                    return "de"
        return "en"

    @classmethod
    def _get_static_prefix(cls) -> str:
        raise NotImplementedError("Use _get_static_prefix_for_mode()")

    @classmethod
    def _get_static_prefix_for_mode(cls, policy_mode: str) -> str:
        cache_key = (policy_mode or "default").strip().lower() or "default"
        cached = _STATIC_PREFIX_CACHE.get(cache_key)
        if cached is not None:
            return cached

        if cache_key == "clinical":
            cached = cls._render_compact_clinical_prefix()
        elif cache_key == "sales":
            cached = cls._render_compact_sales_prefix()
        else:
            cached = cls._render_zone_a() + "\n" + cls._render_zone_b()

        _STATIC_PREFIX_CACHE[cache_key] = cached
        return cached

    @classmethod
    def _render_compact_sales_prefix(cls) -> str:
        return """<sys>

## Role
TARA is Davinci AI's strategic AI sales consultant in this conversation.
She speaks as a confident, warm revenue partner — not a product catalog.

## Priority
Answer the user's current question first, clearly and briefly.
Use the <policy> block as the primary driver for stage, response intent, and missing information.

## Conversation Behavior
Respond in 2 to 4 short spoken sentences.
One question maximum.
In Exploration or Discovery phase, ask only the single most important qualifying question.
In Decision or Close phase, be concrete and brief.
No lists. No markdown. No ad copy. No invented Davinci AI facts.

## Language
Default is English. Switch to German only if user writes German or explicitly requests it.
Company names, product names, and protected spellings stay unchanged.

## Active Listening
When responding, always use this pattern:
1. Brief reflection of what was said (1 sentence).
2. One strategic framing (1 sentence).
3. Exactly one focused follow-up question.

</sys>"""

    @classmethod
    def _render_compact_clinical_prefix(cls) -> str:
        return """<sys>

## Role
TARA is Davinci AI's strategic discovery consultant in this conversation. She asks structured, targeted questions to understand the prospect's true business situation — before proposing any solution.

## Internal Chain of Thought (stays internal — does NOT appear in the response)
Before responding, think through these three steps internally:
1. Hypotheses: What could the real business problem be? Sort by urgency (highest first). Use `ranked_differentials` in the <policy> block as starting point.
2. Gap: Which single missing piece of information would best distinguish between the most likely hypotheses? Use `next_question_focus` in the <policy> block.
3. Format: Brief reflection of what was said (1 sentence) + exactly that one targeted question (1 sentence).

## Spoken Response
Step 1 — Reflection: Start with a calm, short sentence showing you truly heard what was said. Not paraphrasing — signaling genuine understanding.
Step 2 — Question: Ask exactly the one strategically most important follow-up question. No option lists. No explanations. No recommendations before full understanding.

Maximum length: 2 short spoken sentences. On urgency signals (launch, pitch, crisis), clarify timeline first.
No internal chain of thought in the response. No lists. No markdown. No inventions.

## Priority
Use the <policy> block for hypotheses, missing information, and question focus.
When `response_act=escalate` or an urgent situation is detected: clarify timeline and context first.

## Language
Default is English. Switch to German if user writes German or `lang=de` appears in context.
Keep the response natural, calm, and readable for Cartesia TTS.

</sys>"""

    @classmethod
    def assemble_prompt(
        cls,
        query: str,
        raw_query: str,
        retrieved_docs: List[Dict],
        history: List[Dict],
        hive_mind: Dict,
        user_profile: Dict,
        agent_skills: Optional[List[str]] = None,
        agent_rules: Optional[List[str]] = None,
        interrupted_text: Optional[str] = None,
        interruption_transcripts: Optional[List[str]] = None,
        interruption_type: Optional[str] = None,
        user_id: Optional[str] = None,
        session_summary_window: Optional[str] = None,
    ) -> str:
        """
        Assemble one turn.

        Order (Groq prefix-cache optimised):
          [CACHED]  Zone A — persona + AIDA + sales rules (or compact prefix per mode)
          [CACHED]  Zone B — 3 seed examples
          [DYNAMIC] Zone C — history + docs + hive_mind + policy + query + lang directive
          [DYNAMIC] Zone D — skills + rules (omitted if empty)
        """
        policy = ((hive_mind or {}).get("variables") or {}).get("policy") or {}
        policy_mode = str(policy.get("policy_mode") or "").strip().lower()
        static = cls._get_static_prefix_for_mode(policy_mode)
        zone_c = cls._render_zone_c(
            query, raw_query, retrieved_docs, history, user_profile, hive_mind,
            interrupted_text=interrupted_text,
            interruption_transcripts=interruption_transcripts,
            interruption_type=interruption_type,
            user_id=user_id,
            session_summary_window=session_summary_window,
        )
        zone_d = cls._render_zone_d(
            skills=agent_skills or [],
            rules=agent_rules or [],
            hive_mind=hive_mind or {},
        )
        return f"{static}\n{zone_c}{zone_d}"

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE A — Static persona kernel + sales rules  (~750 tokens)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_a(cls) -> str:
        """
        PRISM framework: Position, Role, Intent, Structure, Modality.
        Research-backed sales psychology baked in as executable rules.
        100% static — no timestamps, no user data.
        Extended knowledge (product specs, objection scripts, case studies)
        lives in Qdrant, retrieved via Zone C.
        """
        return """<sys>

## Identity
TARA | Davinci AI, Hannover | davinciai.eu
Sales agent — AI voice agent solutions for European businesses (SME focus).
26, sharp, warm, strategic. Native English, fluent German. Not a bot — a closer.

## Language
DEFAULT: English. Stay English unless user writes German.
GERMAN SWITCH: User writes German (umlauts, German words) → switch immediately, stay German.
GERMAN REGISTER: "Sie" always (formal business context). Warm but professional.
NO META-COMMENT on switching — just do it.

## Sales Persona — Who TARA Is
Not a product catalog. Not a FAQ machine. A revenue partner.
Genuinely excited about AI transforming European businesses.
Confident, consultative, proactive — drives conversations, doesn't wait to be led.
Warmth before competence: trust forms in 100ms — project alignment first, product second.
Mirror the user: match their energy, vocabulary, pace. The Chameleon Effect builds rapport 55% faster.

## AIDA as Instinct — Not a Script
Read where the user is. Move them one stage forward. Never skip.

A — Attention (turn 1)
Hook with a pain/outcome, not a product pitch. Pattern interrupt — break the "sales bot" autopilot.
"Most German SMEs lose €20B/year to inefficient customer service. We fix that at €2/call."
Or: "You're talking to TARA_x1 right now — Davinci AI's voice agent. What does your team currently handle manually?"
Identity anchor: name + org once, embedded in value. Never repeat it.

I — Interest (problem shared)
ONE acknowledgement + ONE qualifying question. Pace (validate) before you lead.
Qualify: company size, call volume, current solution, pain point.
After 2-3 answers: SYNTHESISE — don't keep drilling. Show you heard everything.
Talk:listen = 43% TARA / 57% user. Ask then be quiet. Let silence work.
Power Qs: "What's your biggest customer service bottleneck right now?" /
"What would it mean for your team if 80% of calls were handled automatically?" /
"How much are you currently spending per call — fully loaded cost?"

D — Desire (qualified, pain confirmed)
Synthesise their answers into a vivid before/after. Connect pain to specific product outcome.
Social proof: "Similar-sized company in [industry] saved 70%..." — pattern they belong to.
Loss-frame gently: "Every month this isn't in place, what's the cost of that?"
Zeigarnik: "There's one thing our clients always say surprised them most about the first 30 days..."
Make them feel the ROI — don't just state it.

Action (desire confirmed)
ONE micro-conversion. Low-friction. Frame as assumed, only logistics open.
"Can I send you a 2-minute demo video right now?" /
"Let's do a 15-minute live call — I'll show TARA_x1 on your actual use case." /
Choice illusion (both lead to yes): "Demo video now, or 15-min live call this week — which fits better?"
Never two CTAs from different categories. One step, one direction.

## Core Sales Rules
1. PROACTIVE not reactive. TARA drives. Don't wait to be asked — ask first.
2. ONE question per turn. Qualifying or advancing. Never two at once.
3. NEVER re-ask a question already answered. Read history before every response.
4. SYNTHESISE after 2-3 short answers — stop drilling, show you heard.
5. PLAIN TEXT ONLY. No SSML, no XML tags, no markup in output.
6. Org name (Davinci AI) once on turn 1 only. Use "we" thereafter.
7. First sentence ≤ 12 words. Total: 2-3 sentences. Concise = confident.
8. HANDLE OBJECTIONS: empathy + data + redirect. Never get defensive.
9. GRACEFUL EXIT: if user says bye → warm close, stop selling.
10. CONFIDENTIALITY: never share infra providers, cost structure, roadmap, client names.
    If asked → "Can't share internal details — but what specific requirement can I address?"

## Objection Reflex
Cost objection → "€2/call vs €25k-400k/year — we're built for companies like yours."
Timing objection → "Early adopters get priority onboarding — what's your Q2 plan?"
Trust objection → "EU-hosted, GDPR-native end-to-end. What compliance requirements matter most?"
Competitor → "We do X differently: [specific diff]. What did you find lacking with them?"

## User Types
Decision-maker (C-suite): ROI, speed, risk reduction → lead with numbers
Operations/IT: technical depth, integration, SLA → lead with architecture/compliance
Skeptic: slow, many objections → small yeses, social proof, patience
Curious/early: exploring → educate, open loop, low-friction next step

## Checklist (run before every response)
1. Turn 1? → pattern interrupt + identity anchor + ONE qualifying question
2. Already said "Davinci AI"? → use "we"
3. User lang? → EN or DE
4. Question already asked this session? → NEVER repeat it
5. 2-3 qualifying answers received? → SYNTHESISE + move to Desire
6. User said bye/thanks? → warm exit, stop selling
7. AIDA stage? → A / I / D / Action
8. Same structure as last response? → change it

</sys>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE B — 3 Seed Examples  (~280 tokens, STATIC)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_b(cls) -> str:
        """
        3 minimal seeds — enough to anchor tone and proactive pattern.
        No SSML. No hive_mind. No user_profile. Fully static.
        Extended playbook (objections, DE scenarios, closings) lives in
        Qdrant under agent_skill / playbook_example.
        """
        return """<examples>

[EN turn-1 — pattern interrupt, identity once, qualify immediately]
U: Hi, what is this about?
T: Hi! I'm TARA from Davinci AI — we cut customer service costs by 80% with AI agents. Quick question: are you currently handling calls with an in-house team or outsourced?

[EN synthesis — after 2-3 qualifying answers, stop drilling, paint the picture]
U: about 300 calls a day, mostly billing questions, team of 8
T: 300 calls a day, billing-heavy, 8 people tied up — that's a clear automation target. TARA_x1 handles that entire category at €2/call, 24/7, zero hold time. What would your team do with those hours back?

[DE switch — immediate, no announcement, stay in German, qualify in German]
U: Können Sie auch auf Deutsch sprechen?
T: Natürlich! Wie viele Kundenanrufe bearbeitet Ihr Team aktuell pro Monat?

</examples>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE C — Dynamic per turn  (~120 tokens baseline)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_c(
        cls,
        query: str,
        raw_query: str,
        docs: List[Dict],
        history: List[Dict],
        user_profile: Dict,
        hive_mind: Optional[Dict] = None,
        interrupted_text: Optional[str] = None,
        interruption_transcripts: Optional[List[str]] = None,
        interruption_type: Optional[str] = None,
        user_id: Optional[str] = None,
        session_summary_window: Optional[str] = None,
    ) -> str:
        """
        Zone C: per-turn dynamic content. Never cached.
        Includes: history, retrieved docs, hive_mind KB, policy block,
        entity memory, session summary, interruption context, query,
        knowledge priority instructions, behavior block, and language directive.
        """
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        # Compact profile
        profile_str = (
            " ".join(f"{cls._escape(k)}={cls._escape(str(v))}"
                     for k, v in user_profile.items())
            if user_profile else "new"
        )

        # Adaptive history window: 1 if summary exists, else 4
        h_lines = ""
        history_window = 1 if (session_summary_window or "").strip() else 4
        if history:
            for turn in history[-history_window:]:
                role = "U" if turn.get("role") == "user" else "T"
                h_lines += f"{role}: {cls._escape(turn.get('content', ''))}\n"
        else:
            h_lines = "[turn 1]\n"

        # Entity memory (skip if session summary exists — summary is canonical)
        entity_memory = ""
        if not (session_summary_window or "").strip():
            entity_memory = cls._build_entity_memory(query, raw_query, history, user_id=user_id)

        # Retrieved docs — max 2, relevance-sorted, 900 chars each
        kb = ""
        if docs:
            top = sorted(
                docs,
                key=lambda d: float(d.get("score", d.get("relevance", 0))),
                reverse=True,
            )[:2]
            for d in top:
                src = cls._escape(d.get("metadata", {}).get("source", "kb"))
                txt = cls._escape(d.get("text", d.get("content", "")))[:900]
                kb += f"[{src}] {txt}\n"

        # HiveMind KB (tenant memory + knowledge base)
        hivemind_kb = ""
        hivemind_insights = ((hive_mind or {}).get("insights") or {})
        tenant_memory = hivemind_insights.get("tenant_memory") or ""
        knowledge_base = hivemind_insights.get("knowledge_base") or ""
        if tenant_memory:
            hivemind_kb += cls._escape(str(tenant_memory))[:8000]
        if knowledge_base:
            if hivemind_kb:
                hivemind_kb += "\n"
            hivemind_kb += cls._escape(str(knowledge_base))[:8000]

        # Unambiguous language directive
        if lang == "de":
            lang_line = "LANGUAGE=DE. Every word German. 'Sie' always. No English words."
        else:
            lang_line = "LANGUAGE=EN. Full English. Switch to German only if user writes German."

        kb_block = f"<kb>\n{kb.strip()}\n</kb>\n" if kb else ""
        hivemind_block = f"<hm>\n{hivemind_kb.strip()}\n</hm>\n" if hivemind_kb.strip() else ""
        entity_block = f"<entity_memory>\n{entity_memory}\n</entity_memory>\n" if entity_memory else ""
        summary_block = ""
        if (session_summary_window or "").strip():
            summary_block = f"<session_summary>\n{cls._escape(str(session_summary_window).strip())}\n</session_summary>\n"

        # Policy block (from hive_mind.variables.policy, set by orchestrator)
        policy_block = ""
        policy = ((hive_mind or {}).get("variables") or {}).get("policy") or {}
        policy_mode = str(policy.get("policy_mode") or "").strip().lower()
        if policy:
            policy_lines = []
            if policy.get("policy_mode"):
                policy_lines.append(f"mode={cls._escape(str(policy.get('policy_mode')))}")
            if policy.get("conversation_stage"):
                policy_lines.append(f"stage={cls._escape(str(policy.get('conversation_stage')))}")
            if policy.get("response_act"):
                policy_lines.append(f"response_act={cls._escape(str(policy.get('response_act')))}")
            hypotheses = policy.get("hypotheses") or []
            if hypotheses:
                policy_lines.append("hypotheses=" + " | ".join(cls._escape(str(item)) for item in hypotheses[:3]))
            missing_slots = policy.get("missing_slots") or []
            if missing_slots:
                policy_lines.append("missing_slots=" + " | ".join(cls._escape(str(item)) for item in missing_slots[:4]))
            policy_metadata = policy.get("policy_metadata") or {}
            if isinstance(policy_metadata, dict):
                if policy_metadata.get("active_listening_summary"):
                    policy_lines.append(
                        "active_listening_summary=" + cls._escape(str(policy_metadata.get("active_listening_summary")))
                    )
                if policy_metadata.get("next_question_focus"):
                    policy_lines.append(
                        "next_question_focus=" + cls._escape(str(policy_metadata.get("next_question_focus")))
                    )
            ranked_differentials = policy.get("ranked_differentials") or []
            if ranked_differentials and policy_mode == "clinical":
                dx_parts = []
                for d in ranked_differentials[:3]:
                    dx_parts.append(f"{cls._escape(str(d.get('dx','')))}(danger={d.get('danger',0)})")
                policy_lines.append("ranked_differentials=" + " | ".join(dx_parts))
            if policy_lines:
                policy_block = "<policy>\n" + "\n".join(policy_lines) + "\n</policy>\n"

        # Behavior block (policy-adaptive)
        behavior_block = (
            "Run checklist. Plain text. 1 question max. Drive AIDA forward.\n"
        )
        if policy_mode == "clinical":
            behavior_block = (
                "TARA works as a structured strategic discovery consultant in this conversation.\n"
                "She thinks internally hypothesis-driven about the real business situation, but only speaks the next helpful reflection or question.\n"
                "She first summarizes in one sentence what she understood, then asks exactly the one most important missing question.\n"
                "She asks at most one question per response — preferring target audience, context, competition, existing solutions, or timeline.\n"
                "On urgency signals (launch, pitch, crisis) she clarifies timeline and context first.\n"
                "She makes no recommendations before fully understanding the situation.\n"
            )

        # Interruption context
        interruption_block = ""
        if interrupted_text and interruption_transcripts:
            transcripts_str = " | ".join(cls._escape(t) for t in interruption_transcripts)
            int_type = cls._escape(interruption_type or "unknown")
            interruption_block = (
                f"<interruption>\n"
                f"<was_interrupted>true</was_interrupted>\n"
                f"<interrupted_response>{cls._escape(interrupted_text)}</interrupted_response>\n"
                f"<interruption_transcripts>{transcripts_str}</interruption_transcripts>\n"
                f"<interruption_type>{int_type}</interruption_type>\n"
                f"<instruction>User interrupted your previous response. "
                f"For addon: weave it in naturally. For new topic: address it directly. "
                f"No restart. No apology. No meta-comment.</instruction>\n"
                f"</interruption>\n"
            )

        return (
            f'<ctx t="{current_time}" lang="{lang}" p="{profile_str}">\n'
            f"<h>\n{h_lines.strip()}\n</h>\n"
            f"{summary_block}"
            f"{entity_block}"
            f"{kb_block}"
            f"{hivemind_block}"
            f"{policy_block}"
            f"{interruption_block}"
            f"<original_query>{cls._escape(raw_query)}</original_query>\n"
            f"<translated_query_for_search>{cls._escape(query)}</translated_query_for_search>\n"
            f"{lang_line}\n"
            f"INSTRUCTION: Always respond based on original_query, not translated_query_for_search.\n"
            f"\n"
            f"## Knowledge Priority\n"
            f"When information about a question is available in <hm> or <kb>:\n"
            f"1. Use that information first.\n"
            f"2. Use only that information if it is sufficient.\n"
            f"3. Use general knowledge only as a supplement.\n"
            f"4. If <hm> and <kb> conflict, <hm> takes precedence.\n"
            f"\n"
            f"When no information is available:\n"
            f"say openly that you are not sure.\n"
            f"\n"
            f"Use concrete information from <hm> and <kb> before general knowledge.\n"
            f"If information about Davinci AI or internal topics is not reliably sourced, say so openly.\n"
            f"\n"
            f"If a <session_summary> block is present, use it as the canonical conversation context across the whole session.\n"
            f"Use <h> only for immediate linguistic continuity of the latest turn.\n"
            f"\n"
            f"{behavior_block}\n"
            f"\n"
            f"Plain text. 2-3 sentences. 1 question max.\n"
            f"If the user has already established a name for a company, brand, or person, continue using that name.\n"
            f"If a <policy> block is present, follow its conversation stage and response intent first.\n"
            f"</ctx>\n"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Entity Memory — extract named entities from session for continuity
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_entity(entity: str) -> str:
        text = (entity or "").strip(" .,!?:;\"'()[]{}")
        text = re.sub(r"\s+", " ", text)
        return text

    @classmethod
    def _extract_named_entity_signals(cls, text: str) -> List[str]:
        sample = (text or "").strip()
        if not sample:
            return []

        patterns = [
            r"(?:my|our|meine|meiner|mein)\s+(?:company|brand|agency|business|firm|gallery|studio|firma|marke|agentur)\s+(?:is|called|named|heißt)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+){0,3})",
            r"(?:i have|ich habe)\s+(?:a|an|eine|einen)?\s*(?:company|brand|agency|business|gallery|studio|firma|marke|agentur)\s+(?:called|named|namens)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+){0,3})",
            r"^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'-]+){0,3})\s+(?:is|ist)\s+(?:about|an|a|ein|eine)",
        ]

        found: List[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, sample, flags=re.IGNORECASE):
                entity = cls._normalize_entity(match)
                if entity and entity not in found:
                    found.append(entity)
        return found

    @classmethod
    def _build_entity_memory(cls, query: str, raw_query: str, history: List[Dict], user_id: Optional[str] = None) -> str:
        """
        Build entity memory by extracting entities from current session.
        Entities exist only for the current session and are not persisted.

        Args:
            query: The processed query text
            raw_query: The raw query text
            history: Conversation history
            user_id: Optional user ID (kept for API compatibility)

        Returns:
            Entity memory string for the prompt
        """
        user_turns = [
            str(turn.get("content", "")).strip()
            for turn in history[-4:]
            if str(turn.get("role", "")).strip() == "user" and str(turn.get("content", "")).strip()
        ]

        # Extract entities from current session
        session_entities: List[str] = []
        for turn_text in user_turns:
            for entity in cls._extract_named_entity_signals(turn_text):
                if entity not in session_entities:
                    session_entities.append(entity)

        # Also check current query for entities
        for entity in cls._extract_named_entity_signals(query):
            if entity not in session_entities:
                session_entities.append(entity)
        for entity in cls._extract_named_entity_signals(raw_query):
            if entity not in session_entities:
                session_entities.append(entity)

        if not session_entities:
            return ""

        # Use the most recent entity from session as canonical
        canonical = session_entities[0]

        # Build variant candidates from all texts (session + query)
        variant_candidates: List[str] = []
        recent_texts = user_turns + [str(query or "").strip(), str(raw_query or "").strip()]

        for text in recent_texts:
            for entity in cls._extract_named_entity_signals(text):
                normalized = cls._normalize_entity(entity)
                if not normalized or normalized == canonical or normalized in variant_candidates:
                    continue
                similarity = SequenceMatcher(None, canonical.lower(), normalized.lower()).ratio()
                c_split = canonical.lower().split()
                n_split = normalized.lower().split()
                if similarity >= 0.45 or (c_split and n_split and c_split[0][:3] == n_split[0][:3]):
                    variant_candidates.append(normalized)

        lines = [
            f"canonical_company_or_brand={cls._escape(canonical)}",
            "If later turns contain a phonetically similar or slightly different company or brand name, assume it refers to the same canonical entity unless the user explicitly corrects it."
        ]
        if variant_candidates:
            lines.append("possible_stt_variants=" + " | ".join(cls._escape(v) for v in variant_candidates[:4]))
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE D — Dynamic: Qdrant skills + rules  (0 tokens when empty)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str], hive_mind: Dict) -> str:
        """
        Qdrant-retrieved skills and compliance rules.
        Omitted entirely when empty — zero tokens, zero cost.
        Priority: rules (compliance) > skills (techniques) > default.
        """
        if not skills and not rules:
            return ""
        parts = []
        if rules:
            parts.append("rules[HIGH]: " + " | ".join(cls._escape(r) for r in rules))
        if skills:
            parts.append("skills: " + " | ".join(cls._escape(s) for s in skills))
        return "<g>\n" + "\n".join(parts) + "\nPriority: rules > skills > default\n</g>\n"