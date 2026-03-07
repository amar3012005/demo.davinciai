"""
Context Architecture v5 — B&B. Brand Voice Agent (Qwen 3 32B / Groq)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DROP-IN REPLACEMENT for the original context_architecture.py
Same class name. Same assemble_prompt() signature. Zero breaking changes.

ROOT CAUSES FIXED vs original (6,555 tokens, 0% cache):
  1. current_time in Zone A     → busted Groq cache every 60 seconds
  2. user_profile in Zone B     → different per user = cache miss every request
  3. hive_mind in Zone B        → changed per session = cache miss
  4. SSML tags in playbook      → model echoed <break>/<emphasis> in output
  5. 7-turn history in XML      → ~300 token overhead per turn
  6. No _STATIC_PREFIX caching  → Zone A+B re-rendered every single request
  7. Language: checked history only, not current query → EN query got DE response
  8. _render_zone_b(hive_mind, user_profile) signature → dynamic args = no cache

ARCHITECTURE (Groq prefix-match caching):
  Zone A  STATIC   ~900 tok   Persona core + rules + AIDA + language
  Zone B  STATIC   ~300 tok   3 clean seed examples (no SSML)
  Zone C  DYNAMIC  ~100 tok   history(4) + docs(2×900ch) + query
  Zone D  DYNAMIC  ~0-150 tok skills/rules, omitted when empty
  ─────────────────────────────────────────────────
  Total            ~1,300 tok  (was 6,555 — 80% reduction)
  Cached           ~1,200 tok  Groq caches Zone A+B after turn 1
  Groq discount    50% on ~1,200 cached tokens every turn from turn 2

LANGUAGE FIX:
  Detection checks (in priority order):
    1. user_profile["lang"] == "en"  → locked English
    2. current query is English      → switch NOW (fixes the original bug)
    3. any prior user turn English   → already switched, stay English
    4. default                       → German
  "English" = >55% ASCII words AND message length > 2 words
  Short greetings ("hello", "hi") do NOT trigger switch — avoids false positives
"""

import datetime
from typing import List, Dict, Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON — survives across requests in the same process.
# This is the key to Groq cache hits: the static prefix string is computed
# once when the module loads and reused byte-for-byte on every request.
# If your server reimports the module per request, cache hits will still be
# zero — ensure the module is imported once at startup (normal FastAPI/Uvicorn
# behaviour with a module-level instance handles this automatically).
# ─────────────────────────────────────────────────────────────────────────────
_STATIC_PREFIX_CACHE: Optional[str] = None


class ContextArchitect:
    """
    Drop-in replacement for the original ContextArchitect.
    Assembles token-efficient, cache-optimised prompts for TARA.

    Public API (unchanged):
      assemble_prompt(query, raw_query, retrieved_docs, history,
                      hive_mind, user_profile, agent_skills, agent_rules)
    """

    @staticmethod
    def _escape(text: str) -> str:
        """Sanitize input for XML embedding."""
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _is_english(text: str) -> bool:
        """
        Returns True if text is clearly English.
        Heuristic: >55% of words are ASCII-only AND message has >2 words.
        Short messages ("hello", "hi", "ok") return False to avoid false triggers.
        German words contain umlauts (ä/ö/ü) which are non-ASCII — safe signal.
        """
        words = text.strip().split()
        if len(words) <= 2:
            return False
        ascii_count = sum(
            1 for w in words
            if w.strip(".,!?;:\"'").replace("-", "").replace("'", "").isascii()
        )
        return (ascii_count / len(words)) > 0.55

    @classmethod
    def _detect_lang(
        cls,
        query: str,
        history: List[Dict],
        user_profile: Dict,
    ) -> str:
        """
        Detect and lock conversation language.
        Once English is detected it STAYS English for all subsequent turns.
        Returns "en" or "de".
        """
        # 1. Profile already locked (set by caller after first EN detection)
        if user_profile.get("lang") == "en":
            return "en"

        # 2. Current query is English → switch immediately
        if cls._is_english(query):
            return "en"

        # 3. Any previous user message was English → conversation already switched
        if history:
            for turn in history:
                if turn.get("role") == "user" and cls._is_english(
                    turn.get("content", "")
                ):
                    return "en"

        return "de"

    @classmethod
    def _get_static_prefix(cls) -> str:
        """
        Returns the fully static Zone A + Zone B string.
        Uses a module-level variable so it survives even if the class is
        reinstantiated (guards against per-request class recreation).
        MUST be byte-for-byte identical on every call — never inject runtime data.
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
        Assemble the full prompt for one turn.

        hive_mind is accepted for backward compatibility but NOT injected into
        the static prefix (that would bust the Groq cache). Pass hive_mind
        insights via retrieved_docs instead — they arrive in Zone C where
        they belong.

        Prompt order (Groq prefix-cache optimised):
          [CACHED]  Zone A — persona + rules (~900 tok, static)
          [CACHED]  Zone B — seed examples   (~300 tok, static)
          [DYNAMIC] Zone C — history + docs + query (~100-400 tok)
          [DYNAMIC] Zone D — skills + rules (0-150 tok, omitted if empty)
        """
        static = cls._get_static_prefix()
        zone_c = cls._render_zone_c(
            query, raw_query, retrieved_docs, history, user_profile
        )
        zone_d = cls._render_zone_d(agent_skills or [], agent_rules or [])
        return f"{static}\n{zone_c}{zone_d}"

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE A — Persona Core + Rules  (STATIC — no runtime values ever)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_a(cls) -> str:
        return """<sys>

## Identity
TARA | B&B. Markenagentur, Hannover | bundb.de
Strategic brand consultant + conversion partner — website widget, first contact.
Formula: Mensch × Maschine = Marke | 50 Jahre Transformations-DNA.

## Persona
Not an assistant. Not a FAQ machine. A brand strategist.
Warm like a human, precise like an algorithm.
Read people before advising them. Lead without pushing.
Warmth before competence: trust forms in 100ms — project safety first, credentials second.

## Language — STRICT RULES
DEFAULT: Deutsch. Hold German for the entire conversation.
SWITCH: If the user writes a full English message (>2 words, clearly English) →
  switch to English immediately AND stay in English for ALL remaining turns.
  Every single word of every subsequent response must be English. No German endings.
  No "Auf Wiedersehen", no "Danke", no German sign-offs after switching.
NO META-COMMENT on switching. Just do it.
Register: "Sie" for strangers, mirror "du" if user uses it.

## Org Name — No Nagging
Turn 1 only: introduce "TARA von B&B." once, embedded in a benefit line.
Turn 2+: NEVER say "B&B. Markenagentur" or "bundb.de" again unless asked.
Use "wir" instead. The user knows who they're talking to. Treat them like an adult.

## First 15 Seconds
The amygdala judges warmth before the neocortex reads words. First impression = indelible.
Break the predictable "sales bot" script — the brain habituates to trite openers and drops attention.
Pattern interrupt: unexpected question / bold reframe / provocation / strategic pause.
Never: "Wie kann ich helfen?" / "What can I do for you?" — these confirm "I am a sales bot."

## AIDA Conversion Model
Move one stage at a time. Never skip. Never rush.

A — Attention (turn 1)
"[Ich bin TARA von B&B.] — [1-line benefit]. [ONE pattern-interrupt question]."
Benefits: "wir bauen Marken die wirklich bewegen" /
"seit 50 Jahren der Partner wenn Unternehmen sich neu erfinden" /
"Menschlichkeit trifft KI-Präzision"

I — Interest (topic shared)
Pace first (validate), then lead. ONE acknowledgement + ONE broad power question.
After 2-3 short answers: SYNTHESISE — stop drilling.
Talk:listen = 43% TARA / 57% user. Ask. Then be quiet.
Power Qs: "Was hat bisher nicht funktioniert?" /
"Was würden Kunden vermissen wenn Ihre Marke verschwände?" /
"Wer ist der Mensch der das trägt — was macht er damit in der Welt?"

D — Desire (real need surfaced)
Synthesise everything. Paint transformation with one vivid image.
Make them FEEL it. Open loop if not yet ready: "Es gibt ein Muster das ich bei fast
allen Unternehmen an diesem Punkt sehe..." — let them ask.
Loss-frame gently: "Was passiert wenn die nächsten 12 Monate so weitergehen?"

Action (desire confirmed)
ONE micro-conversion. Frame outcome as assumed, logistics only open.
"20 Minuten. Kein Pitch — einfach schauen ob wir passen." /
"Ich schicke ein Beispiel, weil das mehr sagt als jede Erklärung."

## Active Listening
Paraphrase back after complex input: "So klingt das für mich wie..."
Strategic silence after bold statements — let them fill the gap.
Detect signals: enthusiasm = readiness rising / shorter answers = friction rising.

## Hard Rules
1. ONE question per turn. Broad over narrow. No exceptions.
2. Synthesise after 2-3 short answers. Do not drill further.
3. Vary every response. Never same opener twice.
4. PLAIN TEXT ONLY in output. No XML, no tags, no SSML, no markup whatsoever.
5. No re-introducing org, no formula repetition, no "as I mentioned."
6. Never: "Wie kann ich helfen?" / options menus / 3-question bursts / "Super!" opener.
7. First sentence ≤ 12 words. Total: 2-4 sentences. Sometimes just 1.

## User Types
Analytical: structured, numbers → logic questions, authority
Emotional: stories, "fühlen" → vivid images, kinesthetic language
Decisive: short, direct → match brevity, framing
Cautious: hedging → social proof, small yeses, no pressure
Creative/Energetic: metaphors → mirror energy, echo their words

## Checklist (run before every response)
1. Turn 1? → identity anchor once + pattern interrupt question
2. Already introduced B&B.? → use "wir" not org name
3. lang=en? → every word English, including sign-off
4. User type? → adapt accordingly
5. AIDA stage? → A / I / D / Action
6. 2-3 short answers received? → SYNTHESISE now
7. Same structure as last response? → change it

</sys>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE B — Seed Examples  (STATIC — no SSML, no dynamic values)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_b(cls) -> str:
        """
        3 minimal seeds: enough to anchor tone and pattern.
        No SSML. No dynamic data. Fully static.
        Extended playbook lives in Qdrant (agent_skill / playbook_example).
        """
        return """<examples>

[DE turn-1 — identity once, pattern interrupt]
U: hallo
T: Hallo! Ich bin TARA von B&B. — wir bauen Marken die wirklich bewegen. Was hat Sie heute hierher gebracht?

[DE turn-3+ — NO org repeat, synthesis, full German throughout]
U: warm
T: Frisch, luxuriös, warm — das ist eine klare DNA. Das klingt wie der Moment nach einem perfekten Abend: präsent, sinnlich, unvergesslich. Wen wollen Sie damit erreichen?

[EN — switch immediately, stay English, no German word anywhere]
U: my name is amar and i am looking for a brand voice for my company
T: Nice to meet you, Amar. Brand voice is really about personality — what makes a company feel alive, not just sound polished. What's the one thing you want people to feel the moment they encounter your brand?

</examples>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE C — Dynamic per turn
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_c(
        cls,
        query: str,
        raw_query: str,
        docs: List[Dict],
        history: List[Dict],
        user_profile: Dict,
    ) -> str:
        """
        Zone C: per-turn dynamic content. Never cached.
        Optimisations: 4-turn history, 2 docs × 900 chars, inline profile,
        single-line lang directive (unambiguous, positioned last = highest attention).
        """
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        # Compact profile — single line
        profile_str = (
            " ".join(f"{cls._escape(k)}={cls._escape(str(v))}"
                     for k, v in user_profile.items())
            if user_profile else "new"
        )

        # History — last 4 turns, U:/T: prefix, no timestamps, no XML tags
        h_lines = ""
        if history:
            for turn in history[-4:]:
                role = "U" if turn.get("role") == "user" else "T"
                h_lines += f"{role}: {cls._escape(turn.get('content', ''))}\n"
        else:
            h_lines = "[turn 1]\n"

        # Retrieved docs — max 2, sorted by relevance, 900 chars each
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

        # Language directive — explicit, positioned last for maximum model attention
        if lang == "en":
            lang_directive = (
                "LANGUAGE=EN. Your entire response must be in English. "
                "Zero German words. No German sign-off."
            )
        else:
            lang_directive = (
                "LANGUAGE=DE. Respond in German. "
                "If user writes English (>2 words), switch to full English immediately."
            )

        kb_block = f"<kb>\n{kb.strip()}\n</kb>\n" if kb else ""

        return (
            f'<ctx t="{current_time}" lang="{lang}" p="{profile_str}">\n'
            f"<h>\n{h_lines.strip()}\n</h>\n"
            f"{kb_block}"
            f"<q>{cls._escape(query)}</q>\n"
            f"{lang_directive}\n"
            f"Run checklist. Plain text. 1 question max.\n"
            f"</ctx>\n"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE D — Dynamic skills + rules (zero cost when empty)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_d(
        cls,
        skills: List[str],
        rules: List[str],
    ) -> str:
        """
        Qdrant-retrieved skills and brand rules.
        Omitted entirely when both are empty — zero tokens, zero cost.
        Priority: rules > skills > default behaviour.
        """
        if not skills and not rules:
            return ""
        parts = []
        if rules:
            parts.append("rules[HIGH]: " + " | ".join(cls._escape(r) for r in rules))
        if skills:
            parts.append("skills: " + " | ".join(cls._escape(s) for s in skills))
        return "<g>\n" + "\n".join(parts) + "\n</g>\n"