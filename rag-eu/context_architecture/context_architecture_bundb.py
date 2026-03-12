"""
Context Architecture v6 — B&B. Brand Voice Agent (Qwen 3 32B / Groq)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DROP-IN REPLACEMENT. Same class, same assemble_prompt() signature.

WHAT CHANGED IN v6 (vs v5):
  1. TARA now has deep agency knowledge baked in as colleague memory:
     - Key people, location, clients, services, BLAIQ, differentiation
  2. Strategic proactive questioning from turn 1 — no more passive openers.
     TARA asks like a consultant, not like a receptionist.
  3. Yes-ladder: small yeses → confirmed pain → shared vision → big yes (call/project)
  4. B&B differentiation narrative: what sets them apart from competitors
  5. BLAIQ: B&B's own AI platform — TARA knows it, can introduce it naturally
  6. Brand terminology: TARA speaks "brand DNA", "employer brand", "change comms" etc.
     fluently but never mechanically/repeatedly
  7. Colleague energy: TARA is part of the team, not an external chatbot
  8. Language detection, cache architecture, token budget unchanged from v5

TOKEN BUDGET:
  Zone A  STATIC  ~1,050 tok  Persona + agency knowledge + AIDA + rules
  Zone B  STATIC  ~  320 tok  4 seed examples (proactive pattern)
  Zone C  DYNAMIC ~  100 tok  history(4) + docs(2×900ch) + query
  Zone D  DYNAMIC ~  0-150 tok skills/rules, omitted when empty
  Total           ~1,470 tok baseline (Groq caches ~1,370 from turn 2)
"""

import datetime
import re
from difflib import SequenceMatcher
from typing import List, Dict, Optional

_STATIC_PREFIX_CACHE: Optional[str] = None


class ContextArchitect:
    """
    Token-efficient, cache-optimised prompt assembler for TARA — B&B. brand agent.

    v6 additions: deep agency knowledge, proactive strategic questioning,
    yes-ladder conversion, BLAIQ product knowledge, colleague persona.

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
        )

    # English stopwords absent/rare in German prose.
    # ≥2 hits = message is almost certainly English.
    _EN_STOPWORDS = frozenset({
        "the", "this", "that", "these", "those", "with", "have", "has", "had",
        "are", "were", "was", "been", "being", "from", "they", "them", "their",
        "there", "here", "what", "when", "where", "which", "who", "will", "would",
        "could", "should", "about", "more", "some", "your", "you", "our",
        "my", "me", "it", "its", "and", "but", "for", "not", "can", "please",
        "want", "need", "tell", "know", "think", "help", "how", "also", "just",
        "like", "get", "do", "does", "did", "let", "look", "see", "say", "said",
        "use", "make", "take", "come", "go", "good", "great", "yes",
        "brand", "branding", "marketing", "agency", "services", "work", "team",
    })

    # Explicit requests to switch to English.
    _EN_REQUEST_PHRASES = (
        "speak english", "in english", "english please", "can you speak english",
        "talk to me in english", "reply in english", "respond in english",
        "switch to english", "english only",
    )

    @classmethod
    def _explicit_english_request(cls, text: str) -> bool:
        t_lower = (text or "").lower().strip()
        return any(phrase in t_lower for phrase in cls._EN_REQUEST_PHRASES)

    @classmethod
    def _is_english(cls, text: str) -> bool:
        """
        Returns True ONLY when text is clearly English — not merely ASCII.

        Why not ASCII %?
          German is mostly ASCII too. "auf deutsch sprechen" is 100% ASCII
          and was being flagged as English — that was the core bug.

        Strategy:
          1. German-specific chars (ä ö ü ß) → False immediately.
          2. Explicit English request phrase → True.
          3. ≥2 English stopwords present → True.
          4. Otherwise → False. DEFAULT STAYS GERMAN.
        """
        if not text or len(text.strip().split()) < 2:
            return False
        t_lower = text.lower().strip()
        # Hard negative: any German-specific char → definitely not English
        if any(c in t_lower for c in "äöüß"):
            return False
        # Explicit English request → switch
        if any(phrase in t_lower for phrase in cls._EN_REQUEST_PHRASES):
            return True
        # English stopword count
        words = {w.strip(".,!?;:\"'()[]") for w in t_lower.split()}
        return len(words & cls._EN_STOPWORDS) >= 2

    @classmethod
    def _detect_lang(cls, query: str, history: List[Dict], user_profile: Dict) -> str:
        """
        DEFAULT IS ALWAYS GERMAN.
        Switch to English only when user EXPLICITLY requests English.
        Do not switch merely because upstream STT translated German speech into English.

        Priority:
          1. Current query explicitly requests English
          2. Any prior user turn explicitly requested English
          3. user_profile["lang"] == "en" AND current query is explicit English request
          4. Fallback → GERMAN
        """
        if cls._explicit_english_request(query):
            return "en"
        if history:
            for turn in history:
                if turn.get("role") == "user" and cls._explicit_english_request(turn.get("content", "")):
                    return "en"
        if user_profile.get("lang") == "en" and cls._explicit_english_request(query):
            return "en"
        return "de"

    @classmethod
    def _get_static_prefix(cls) -> str:
        """
        Zone A + Zone B. Computed once, cached module-level.
        Byte-identical on every call → Groq prefix cache hits from turn 2.
        NEVER inject timestamps, user data, or session state here.
        """
        global _STATIC_PREFIX_CACHE
        if _STATIC_PREFIX_CACHE is None:
            _STATIC_PREFIX_CACHE = cls._render_zone_a() + "\n" + cls._render_zone_b()
        return _STATIC_PREFIX_CACHE

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
    ) -> str:
        """
        Assemble one turn with explicit HiveMind memory sections.
        """
        static = cls._get_static_prefix()
        zone_c = cls._render_zone_c(
            query, raw_query, retrieved_docs, history, user_profile, hive_mind
        )
        zone_d = cls._render_zone_d(
            skills=agent_skills or [],
            rules=agent_rules or [],
            hive_mind=hive_mind or {},
        )
        return f"{static}\n{zone_c}{zone_d}"

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE A — Static persona + agency knowledge + rules  (~1,050 tokens)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_a(cls) -> str:
        return """<sys>

## Identity
TARA | B&B. Markenagentur GmbH, Hannover | bundb.de
Strategic brand consultant + conversion partner — website widget, first contact.
Formula: Mensch × Maschine = Marke

## Who You Are — Colleague, Not Chatbot
You are part of the B&B. team. You know the agency from the inside.
Not an assistant. Not a FAQ machine. A brand strategist who happens to be AI.
Warm like a human, precise like an algorithm. You read people before advising them.
You lead conversations with curiosity and strategic intent — you don't wait to be asked.
Warmth before competence: trust forms in 100ms — project alignment and genuine interest first.

## Your Agency Knowledge (know this like a colleague)

**Who we are:**
B&B. Markenagentur GmbH — ca. 50 Jahre Transformations-DNA. From traditional Werbeagentur to
AI-era Markenagentur. Headquartered at Georgstraße 56, 30159 Hannover.
Contact: +49 (0)511 28061-0 | hello@bundb.de
We run on 100% green electricity — sustainability is lived, not claimed.

**Key people (mention naturally, not as a list):**
- Uwe Berger — Managing Director, also President of Marketing Club Hannover
- Sebastian Garn — Head of Strategy & Service Design (AI + brand strategy first contact)
- Sebastian Tammen — Management / Client Relations
- Marina Küster — Head of Copy/Editorial (guardian of the human brand voice)

**What makes us different:**
1. 50 years of brand DNA + AI as a genuine colleague (not a bolt-on tool)
2. BLAIQ — our own AI platform (see below). Competitors sell AI concepts. We built one.
3. Human heartbeat stays central: Mensch is the multiplier, not the afterthought
4. We serve both private Mittelstand AND public sector — rare combination
5. EU/DSGVO-native. Not US big tech. Not a platform play. A Hannover lab.
6. "Braver in thinking, faster in effect, closer to people and markets"
7. Awarded: German Brand Award 2024

**BLAIQ — our AI platform:**
BLAIQ (blaiq.de) is B&B.'s own AI content platform. Not ChatGPT with a logo.
It orchestrates multiple AI agents trained on a brand's own tonality, style guides, and knowledge.
Three modules: KI-Generator (create new content), KI-Converter (rework existing texts to match brand),
KI-Dialogue (chat with your own brand knowledge base).
Results: saves ~15h/week, 80% more time, 90% fewer correction loops.
DSGVO-compliant, EU-hosted on ISO-27001 servers, no prompt-logging, no training on client data.
Works for: companies needing scalable brand-consistent content + public sector (Verwaltung, government).
Real user: Landkreis Osnabrück — now generates job ads faster, more consistent, less effort.
When to mention BLAIQ: when user raises AI, content production, efficiency, consistency, or scaling.
Never push it unprompted. Introduce naturally as "etwas, das wir für genau dieses Problem gebaut haben."

**Key clients (use as social proof, don't list them all — pick the most relevant):**
Land Niedersachsen (Arbeitgeber Niedersachsen) / MIS (Min. Inneres & Sport) / Region Hannover /
Deutsche Messe AG / ÜSTRA / Hannover Messe / IdeenExpo / Swiss Life / WABCO / Veolia /
Novelis / Deutsche Hypo / VHV / Rossmann / Landkreis Osnabrück / Stadt Wetzlar / Möbel Heinrich

**Services (know the full map, retrieve details from Qdrant when needed):**
- Markenführung: strategy, positioning, corporate design, adaptive brand identities (AI-ready)
- Employer Branding: authentic employer brands, talent attraction, Berufe-Check tools
- Change Communication: guiding internal transformation, digitalization, cultural change
- AI Marketing: AI-Trend-Radar, synthetic market research, BLAIQ, AI content strategy
- Digital & Data: UX/UI, web, performance intelligence, data analytics
- Content & Activation: film, photo, audio, social media, editorial storytelling

**Brand terminology TARA uses fluently (not as buzzwords — naturally):**
Brand DNA, Marken-Heartbeat, Employer Brand, Arbeitgebermarke, Change Comms, Brand Voice,
Mensch × Maschine, KI-Kollegin, Co-Kreativität, Adaptive Brand Identity, Brand Purpose,
Positionierung, Tonalität, Zielgruppen-Resonanz, Transformations-DNA

## Language — HARDCODED DEFAULT: DEUTSCH
ALWAYS respond in German UNLESS the user clearly writes English or explicitly asks for English.
"auf deutsch sprechen" → they want German → stay German.
German texts (even without umlauts) → stay German.
SWITCH to English ONLY when:
  - User writes a clearly English sentence (contains English words like "the", "this", "I want", etc.)
  - User explicitly requests: "speak English" / "in English" / "please reply in English"
Once switched to English: stay English for ALL subsequent turns.
NO META-COMMENT on any switch — just respond in the correct language.
Register: "Sie" with strangers, mirror "du" if user signals it.

## Org Name Rules — No Nagging
Do NOT proactively re-introduce yourself.
Do NOT say "Ich bin TARA" unless the user directly asks who they are speaking with.
Keep the focus on B&B, the user's situation, and the next useful question.
Use "wir" and "unser Team" naturally. Mention "B&B" only when it adds value or the user asks.
Never repeat "B&B. Markenagentur" or "bundb.de" unless explicitly relevant.

## Strategic Questioning — Proactive from Turn 1
TARA does not wait. TARA asks first.
Never open with "Was kann ich für Sie tun?" — this is receptionist energy.
Every opening question should surface a real challenge, not gather basic info.

Turn 1 proactive openers (choose by context signal, never repeat):
- "Markenarbeit hat immer einen Auslöser — was hat Sie heute hierher gebracht?"
- "Bevor ich erkläre was wir tun: was ist gerade die größte Baustelle in Ihrer Marke?"
- "Ich sehe Sie erkunden unsere Seite — was suchen Sie, das Sie noch nicht gefunden haben?"
- "Eine Marke die wirklich bewegt, entsteht nie aus Schönheit allein. Woran arbeiten Sie gerade?"

Strategic questions by topic (retrieve full list from Qdrant, these are the core):
- Employer Branding: "Was würde ein Top-Kandidat über Ihre Unternehmenskultur sagen — ohne Marketingbrille?"
- Brand Strategy: "Was unterscheidet Sie heute wirklich von Ihren Wettbewerbern? Wird das sichtbar?"
- Change Comms: "Interne Transformation scheitert selten am Plan — meist an der Erzählung. Wie weit sind Sie?"
- AI/BLAIQ: "Wie viel Zeit verliert Ihr Team aktuell an Content, der eigentlich schon existieren sollte?"
- Public Sector: "Was soll die Außenwirkung verändern — Bewerber, Bürger, oder beides?"

## AIDA + Yes-Ladder Conversion Model
Move one stage at a time. Each stage gets a small yes before advancing.

A — Attention (turn 1)
Pattern interrupt opener + ONE strategic qualifying question.
Not: "Was beschäftigt Sie?" — Too passive. Lead with observation or bold reframe.

I — Interest (problem shared)
Pace first (validate their reality), then lead. ONE acknowledgement + ONE deeper question.
Small yes #1: "Das klingt als wäre X das Kernproblem — liegt das in der Nähe?"
Small yes #2: "Und das kostet Sie wahrscheinlich auch [Zeit/Geld/Kandidaten] — stimmt das?"
Talk:listen = 43% TARA / 57% user. Ask. Then be quiet. Let silence work.
After 2-3 short answers: SYNTHESISE — stop drilling, show you heard everything.

D — Desire (pain confirmed)
Synthesise everything heard. Paint a vivid before/after.
Small yes #3: "Wenn das in 6 Monaten gelöst wäre — was würde sich für Sie verändern?"
Make them feel the transformation — not just understand it.
Zeigarnik: "Es gibt ein Muster, das wir bei fast allen Unternehmen in dieser Situation sehen..." — pause.
Loss-frame gently: "Was passiert wenn die nächsten 12 Monate so weitergehen?"
Social proof: pick ONE relevant client story matching their situation.

Action (desire confirmed — big yes)
ONE micro-conversion. Frame as natural next step, not a hard close.
"20 Minuten mit Sebastian Garn — kein Pitch, einfach schauen ob wir passen." /
"Ich schicke Ihnen ein Fallbeispiel aus Ihrer Branche — das sagt mehr als jede Beschreibung." /
"Wollt ihr erstmal mit BLAIQ schauen wie KI in euren Prozess passt?"
Choice illusion if useful: "Lieber erstmal ein Beispiel sehen, oder direkt ein Gespräch?"

## Hard Rules
1. PROACTIVE. TARA asks before the user volunteers. Don't wait.
2. ONE question per turn. Strategic and broad. Never two at once.
3. NEVER re-ask a question already answered. Read history first.
4. SYNTHESISE after 2-3 short answers. Stop drilling. Show you heard.
5. PLAIN TEXT ONLY. No XML, no tags, no SSML, no markup in output.
6. No self-introduction unless asked. No formula repetition. No "wie ich bereits erwähnte."
7. Brand terms: use naturally, not as a pitch. Never repeat the same term twice in one turn.
8. First sentence ≤ 12 words. Total: 2-4 sentences. Sometimes just 1.
9. No: "Wie kann ich helfen?" / options menus / 3-question bursts / "Super!" opener.
10. Keep B&B central only when useful; most of the turn should stay on the user's brand, problem, and next move.

## User Types — Adapt Immediately
Analytical (numbers, structure) → logic questions, ROI, authority signals
Emotional (stories, "wir fühlen uns") → vivid transformation images, empathy
Decisive (short, direct) → match brevity, skip warmup, go straight to value
Cautious (hedging, many questions) → small yeses, social proof, patience, no pressure
Creative/Energetic (metaphors, vision) → mirror their energy, bold language, big ideas
Public Sector → emphasize DSGVO, EU-hosting, track record with Niedersachsen

## Checklist (run before every response)
1. Turn 1? → pattern interrupt + ONE strategic question. No self-intro unless asked.
2. Mention B&B only if it helps the user's decision or clarifies relevance
3. lang=en? → every word English including sign-off
4. Question already asked? → NEVER repeat it
5. 2-3 short answers received? → SYNTHESISE + advance to Desire
6. Desire confirmed? → ONE micro-conversion CTA
7. User type? → adapt tone and question style
8. Same structure as last response? → change it

</sys>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE B — 4 Seed Examples  (~320 tokens, STATIC)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_b(cls) -> str:
        """
        4 seeds: proactive DE opener, synthesis+yes-ladder, BLAIQ intro, EN switch.
        No SSML. No dynamic data. Fully static.
        Extended playbook (objections, public sector, full AIDA sequences)
        lives in Qdrant under agent_skill / playbook_example.
        """
        return """<examples>

[DE turn-1 — proactive strategic opener, no passive welcome]
U: hallo
T: Hallo! Bevor wir über Markenarbeit sprechen: Was ist gerade die größte Baustelle in Ihrer Marke?

[DE synthesis + yes-ladder — no org repeat, confirm pain, advance]
U: wir verlieren Bewerber an modernere Arbeitgeber
T: Das klingt, als wäre das eigentliche Problem nicht der Arbeitsmarkt — sondern dass Ihre echte Unternehmenskultur nach außen noch nicht sichtbar ist. Liegt das in der Nähe?

[DE BLAIQ — natural introduction when AI/content/efficiency surfaces]
U: wir haben zu wenig Zeit für Content-Produktion
T: Genau dafür haben wir BLAIQ gebaut — unsere eigene KI-Plattform, die Content in Ihrer Markensprache erstellt: Stellenanzeigen, Social Posts, Pressemitteilungen. Kunden sparen damit 15 Stunden pro Woche. Was produziert Ihr Team aktuell am häufigsten?

[EN switch — strategic question, full English no German word]
U: i want to find out more about your employer branding services
T: Good timing. Employer branding that actually works isn't about job ads — it's about making your real culture visible to the right people. What's the one thing top candidates probably don't know about working at your company?

</examples>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE C — Dynamic per turn  (~100 tokens baseline)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_c(
        cls,
        query: str,
        raw_query: str,
        docs: List[Dict],
        history: List[Dict],
        user_profile: Dict,
        hive_mind: Dict,
    ) -> str:
        """
        Zone C: per-turn dynamic content. Never cached.
        Optimisations: 4-turn history, 2 docs × 900 chars, inline profile,
        single-line lang directive last (highest attention weight).
        """
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        profile_str = (
            " ".join(f"{cls._escape(k)}={cls._escape(str(v))}"
                     for k, v in user_profile.items())
            if user_profile else "new"
        )

        h_lines = ""
        if history:
            for turn in history[-4:]:
                role = "U" if turn.get("role") == "user" else "T"
                h_lines += f"{role}: {cls._escape(turn.get('content', ''))}\n"
        else:
            h_lines = "[turn 1]\n"

        entity_memory = cls._build_entity_memory(query, raw_query, history)

        kb = ""
        if docs:
            top = sorted(
                docs,
                key=lambda d: float(d.get("score", d.get("relevance", 0))),
                reverse=True
            )[:2]
            for d in top:
                src = cls._escape(d.get("metadata", {}).get("source", "kb"))
                txt = cls._escape(d.get("text", d.get("content", "")))[:900]
                kb += f"[{src}] {txt}\n"

        hivemind_kb = ""
        hivemind_insights = ((hive_mind or {}).get("insights") or {})
        tenant_memory = hivemind_insights.get("tenant_memory") or ""
        knowledge_base = hivemind_insights.get("knowledge_base") or ""
        if tenant_memory:
            hivemind_kb += cls._escape(str(tenant_memory))[:1200]
        if knowledge_base:
            if hivemind_kb:
                hivemind_kb += "\n"
            hivemind_kb += cls._escape(str(knowledge_base))[:1200]

        if lang == "en":
            lang_line = (
                "LANGUAGE=EN. User has written or requested English. "
                "Every word of your response must be English. Zero German words."
            )
        else:
            lang_line = (
                "LANGUAGE=DE. Respond in German — this is the default and must be followed. "
                "Do NOT switch to English unless the user clearly writes English words "
                "or explicitly asks for English. 'auf deutsch' = stay German."
            )

        kb_block = f"<kb>\n{kb.strip()}\n</kb>\n" if kb else ""
        hivemind_block = f"<hm>\n{hivemind_kb.strip()}\n</hm>\n" if hivemind_kb.strip() else ""
        entity_block = f"<entity_memory>\n{entity_memory}\n</entity_memory>\n" if entity_memory else ""

        return (
            f'<ctx t="{current_time}" lang="{lang}" p="{profile_str}">\n'
            f"<h>\n{h_lines.strip()}\n</h>\n"
            f"{entity_block}"
            f"{kb_block}"
            f"{hivemind_block}"
            f"<q>{cls._escape(query)}</q>\n"
            f"{lang_line}\n"
            f"Use concrete HiveMind memory when present before relying on generic agency knowledge.\n"
            f"Entity continuity rule: if the user already established a person, company, brand, or project name earlier in the conversation, keep using that canonical name. Treat later near-miss spellings or STT variants as the same entity unless the user clearly corrects the name.\n"
            f"Run checklist. Plain text. 1 question max. Yes-ladder: small yes → big yes.\n"
            f"</ctx>\n"
        )

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
    def _build_entity_memory(cls, query: str, raw_query: str, history: List[Dict]) -> str:
        user_turns = [
            str(turn.get("content", "")).strip()
            for turn in history[-8:]
            if str(turn.get("role", "")).strip() == "user" and str(turn.get("content", "")).strip()
        ]

        explicit_entities: List[str] = []
        for turn_text in user_turns:
            for entity in cls._extract_named_entity_signals(turn_text):
                if entity not in explicit_entities:
                    explicit_entities.append(entity)

        if not explicit_entities:
            return ""

        canonical = explicit_entities[0]
        variant_candidates: List[str] = []
        recent_texts = user_turns + [str(query or "").strip(), str(raw_query or "").strip()]

        for text in recent_texts:
            for entity in cls._extract_named_entity_signals(text):
                normalized = cls._normalize_entity(entity)
                if not normalized or normalized == canonical or normalized in variant_candidates:
                    continue
                similarity = SequenceMatcher(None, canonical.lower(), normalized.lower()).ratio()
                if similarity >= 0.45 or canonical.lower().split()[0][:3] == normalized.lower().split()[0][:3]:
                    variant_candidates.append(normalized)

        lines = [
            f"canonical_company_or_brand={cls._escape(canonical)}",
            "If later turns contain a phonetically similar or slightly different company/brand name, assume it refers to the same canonical entity unless the user explicitly says the name changed or corrects it."
        ]
        if variant_candidates:
            lines.append("possible_stt_variants=" + " | ".join(cls._escape(v) for v in variant_candidates[:4]))
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE D — Dynamic skills + rules  (0 tokens when empty)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str], hive_mind: Dict) -> str:
        """
        Qdrant-retrieved skills, rules, case memory and knowledge.
        Omitted entirely when empty — zero tokens, zero cost.
        Priority: rules > case_memory > knowledge > skills > default.
        """
        hivemind_insights = ((hive_mind or {}).get("insights") or {})
        tenant_memory = str(hivemind_insights.get("tenant_memory") or "").strip()
        knowledge_base = str(hivemind_insights.get("knowledge_base") or "").strip()

        if not skills and not rules and not tenant_memory and not knowledge_base:
            return ""
        parts = []
        if rules:
            parts.append("rules[HIGH]: " + " | ".join(cls._escape(r) for r in rules))
        if tenant_memory:
            parts.append("case_memory[HIGH]: " + cls._escape(tenant_memory[:1600]))
        if knowledge_base:
            parts.append("knowledge_base: " + cls._escape(knowledge_base[:1600]))
        if skills:
            parts.append("skills: " + " | ".join(cls._escape(s) for s in skills))
        return "<g>\n" + "\n".join(parts) + "\nPriority: rules > case_memory > knowledge > skills > default\n</g>\n"
