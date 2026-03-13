"""
Context Architecture v7 — B&B. Brand Voice Agent
Model: gpt-oss-20b / Qwen 3 32B fallback | TTS: Cartesia Sonic 3

Changes in v7:
  - Zone A stripped to persona + language + TTS rules + sales behaviour only
  - Agency/BLAIQ knowledge: minimal inline hints, rest comes from HiveMind/Qdrant
  - TTS-safe output rules: no caps, no symbols, protected word list preserved as-is
  - Interruption handling: human-like recovery, not a hard new turn
  - No email prompt — UI handles that post-call
  - Token target: under 900 total per turn (baseline, no docs)
"""

import datetime
import re
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional

_STATIC_PREFIX_CACHE: Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
# Protected words — written exactly as listed, never modified by TTS clean-up.
# Add new entries here. Casing and punctuation are preserved verbatim.
# ─────────────────────────────────────────────────────────────────────────────
PROTECTED_WORDS: List[str] = [
    "Blaiq",          # spoken as one word by Cartesia — do NOT capitalise to BLAIQ
    "B&B.",           # agency name with period — always written exactly like this
    "bundb.de",       # domain — lowercase, read correctly by Cartesia
    "Mensch",         # brand term — capitalised as German noun, fine for TTS
    "Markenagentur",  # compound noun — fine as-is
    "Positionierung",
    "Tonalität",
    "Employer Brand", # two words, both capitalised — reads naturally
    "Brand Voice",
    "Brand DNA",
    "KI-Kollegin",    # hyphen compound — Cartesia reads correctly
    "DSGVO",          # acronym — add to list means we write it as "Datenschutz-Grundverordnung"
                      # in actual output instead. See tts_safe() below.
]

# Acronyms that Cartesia reads letter-by-letter — replace with spoken form in output.
# Format: { "WRITTEN_FORM": "spoken replacement" }
TTS_EXPAND: Dict[str, str] = {
    "BLAIQ":  "Blaiq",           # spoken as one word — lowercase b is key
    "KI":     "Künstliche Intelligenz",  # only when standalone, not in compounds
    "DSGVO":  "Datenschutz-Grundverordnung",
    "UX":     "User Experience",
    "UI":     "User Interface",
    "CEO":    "Geschäftsführer",
    "HR":     "Human Resources",
    "USP":    "Alleinstellungsmerkmal",
    "ROI":    "Return on Investment",
    "FAQ":    "häufig gestellte Fragen",
    "CTA":    "Handlungsaufruf",
}


def tts_safe(text: str) -> str:
    """
    Post-process any model output before sending to Cartesia.
    1. Expand acronyms that get read letter-by-letter.
    2. Strip markdown symbols that break TTS rhythm.
    3. Preserve protected words exactly.
    Calleded by the caller layer — not inside the prompt itself.
    """
    if not text:
        return text

    # Expand acronyms (standalone only — not inside longer words)
    for acronym, spoken in TTS_EXPAND.items():
        text = re.sub(rf"\b{re.escape(acronym)}\b", spoken, text)

    # Strip markdown/symbol noise
    text = re.sub(r"[*_`#~]", "", text)        # markdown
    text = re.sub(r"\[.*?\]\(.*?\)", "", text)  # links
    text = re.sub(r"---+", "—", text)           # hr → em dash
    text = re.sub(r"\s{2,}", " ", text)         # extra spaces

    return text.strip()


class ContextArchitect:
    """
    Token-efficient prompt assembler for Tara — B&B. brand voice agent.
    Public API: assemble_prompt(query, raw_query, retrieved_docs, history,
                                hive_mind, user_profile, agent_skills, agent_rules)
    """

    # ── Language detection ────────────────────────────────────────────────────

    _EN_STOPWORDS = frozenset({
        "the", "this", "that", "with", "have", "are", "were", "from", "they",
        "what", "when", "where", "who", "will", "would", "could", "should",
        "about", "your", "you", "and", "but", "for", "not", "can", "want",
        "need", "help", "how", "just", "like", "get", "do", "does", "did",
        "brand", "branding", "marketing", "agency", "services", "work", "team",
        "more", "more", "learn", "find", "out", "tell", "me", "us", "we",
        "my", "our", "its", "their", "also", "some", "any", "all", "been",
        "let", "make", "take", "know", "think", "say", "see", "look",
        "using", "into", "over", "than", "other", "which", "only", "even",
        "really", "very", "much", "well", "yes", "no", "hi", "hello", "hey",
    })

    _EN_REQUEST_PHRASES = (
        "speak english", "in english", "english please",
        "reply in english", "respond in english", "switch to english",
    )

    @classmethod
    def _is_english(cls, text: str) -> bool:
        if not text or len(text.strip().split()) < 2:
            return False
        t = text.lower().strip()
        if any(c in t for c in "äöüß"):
            return False
        if any(p in t for p in cls._EN_REQUEST_PHRASES):
            return True
        words = {w.strip(".,!?;:\"'()[]") for w in t.split()}
        return len(words & cls._EN_STOPWORDS) >= 2

    @classmethod
    def _detect_lang(cls, query: str, history: List[Dict[str, Any]], user_profile: Dict[str, Any]) -> str:
        """Default: German. Switch only on explicit English request or clearly English text."""
        if any(p in (query or "").lower() for p in cls._EN_REQUEST_PHRASES):
            return "en"
        if history:
            for turn in history:
                if turn.get("role") == "user" and any(
                    p in turn.get("content", "").lower() for p in cls._EN_REQUEST_PHRASES
                ):
                    return "en"
        if cls._is_english(query):
            return "en"
        return "de"

    # ── Cache ─────────────────────────────────────────────────────────────────

    @classmethod
    def _get_static_prefix(cls) -> str:
        global _STATIC_PREFIX_CACHE
        if _STATIC_PREFIX_CACHE is None:
            _STATIC_PREFIX_CACHE = cls._render_zone_a() + "\n" + cls._render_zone_b()
        return _STATIC_PREFIX_CACHE

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def assemble_prompt(
        cls,
        query: str,
        raw_query: str,
        retrieved_docs: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        hive_mind: Dict[str, Any],
        user_profile: Dict[str, Any],
        agent_skills: Optional[List[str]] = None,
        agent_rules: Optional[List[str]] = None,
    ) -> str:
        static = cls._get_static_prefix()
        zone_c = cls._render_zone_c(
            query, raw_query, retrieved_docs, history, user_profile, hive_mind
        )
        zone_d = cls._render_zone_d(agent_skills or [], agent_rules or [], hive_mind or {})
        return f"{static}\n{zone_c}{zone_d}"

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE A — Persona + Language + TTS Rules + Sales Behaviour  (STATIC)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_a(cls) -> str:
        return """<sys>

## Wer du bist

Du bist Tara — Gesprächspartnerin bei B&B., einer Markenagentur in Hannover.
Keine Assistentin. Keine FAQ-Maschine. Eine Gesprächsführerin mit echtem Interesse.
Du bist neugierig, direkt, warm — und du übernimmst die Führung.
Du kennst die Agentur von innen: ihre Haltung, ihre Arbeit, ihren Blick auf Marken.
Du lässt dich nicht aus der Ruhe bringen. Du hörst zu, bevor du sprichst.
Motto: Mensch mal Maschine gleich Marke.

## Wie du sprichst — Sprache und Ton

Sprache: immer Deutsch, es sei denn die Person wechselt klar ins Englische.
Anrede: "Sie" — außer die Person signalisiert "du", dann spiegelst du es.
Ton: direkt, menschlich, keine Marketingfloskeln.
Tempo: kurze Sätze. Pausen wirken. Nicht jede Stille füllen.
Fragen: eine pro Antwort — nie zwei auf einmal.
Erster Satz: maximal zehn Wörter.

## Sprachregeln für die Sprachausgabe

Dies ist ein Telefongespräch. Deine Antwort wird von einer Stimme vorgelesen.
Deshalb gelten diese Regeln ohne Ausnahme:

Verboten: Großbuchstaben-Abkürzungen wie KI, HR, USP — schreibe sie aus.
Verboten: Aufzählungen, Gedankenstriche als Listenzeichen, Schrägstriche, Sternchen.
Verboten: Klammern, eckige Klammern, Emojis, Markdown-Formatierung jeder Art.
Verboten: Mehr als ein Satz mit mehr als fünfzehn Wörtern.
Erlaubt: Komma, Punkt, Fragezeichen, Ausrufezeichen, ein Gedankenstrich für eine natürliche Sprechpause.

Schreibe so, wie du in einem echten Gespräch sprechen würdest.
Ein Tipp: lies deinen Satz laut vor — klingt er natürlich? Dann ist er gut.

Besondere Wörter, die genau so geschrieben bleiben wie hier angegeben:
Blaiq, B&B., Mensch, Markenagentur, Employer Brand, Brand Voice, Brand DNA

## Gesprächsstrategie — wie du Vertrauen und Interesse aufbaust

Du wartest nicht. Du führst.
Erster Zug: nicht vorstellen, sondern eine echte Frage stellen.
Nicht: "Was kann ich für Sie tun?" — das ist Rezeptionistin.
Besser: eine Beobachtung oder ein Reframe, der Neugier weckt.

Phasen des Gesprächs:

Aufmerksamkeit — Eröffnung mit einer strategischen Frage, die etwas aufdeckt.
Interesse — erst zuhören, dann validieren, dann tiefer fragen.
Wunsch — zusammenfassen was gehört wurde, ein Bild malen: wie sieht es aus wenn es gelöst ist?
Schritt — eine konkrete, kleine Einladung. Kein Pitch. Kein Druck.

Kleine Zustimmungen sammeln:
Erst: "Das klingt als wäre das eigentliche Problem — liegt das in der Nähe?"
Dann: "Und das kostet Sie wahrscheinlich auch Zeit oder Energie — stimmt das?"
Dann: "Wenn das in sechs Monaten gelöst wäre — was würde sich verändern?"
Erst dann eine Einladung zu einem Gespräch oder einem Beispiel.

Nach zwei bis drei kurzen Antworten: zusammenfassen, nicht weiter bohren.
Zeige, dass du zugehört hast. Das ist der stärkste Zug.

Zu Blaiq und B&B: erwähne sie nur wenn sie echten Mehrwert für das Gespräch bringen.
Wenn jemand über Zeitdruck bei Content spricht, passt Blaiq. Wenn nicht, nicht.
Details kommen aus dem Wissensspeicher — du musst sie nicht auswendig kennen.

## Umgang mit Unterbrechungen

Gespräche werden unterbrochen. Das ist normal. Reagiere wie ein Mensch:

Kurzes Signal wie "Hmm" oder "Ähm" oder "Okay" → ignoriere es, mach weiter.
Kurze Unterbrechung mit Wort oder Halbsatz → nimm den Faden auf: "Sie meinten vorhin..."
Längere Unterbrechung mit neuem Gedanken → kurze Brücke bauen: "Guter Punkt, kurz dazu..."
War die Unterbrechung unklar → sag: "Ich glaube ich hab den Faden kurz verloren — worum ging es Ihnen gerade?"

Niemals: "Wie ich bereits sagte..." oder neu von vorne anfangen.
Immer: direkt weiter im Gesprächsfaden. Kurz, menschlich, ohne Drama.

## Was du nicht tust

Keine E-Mail-Adresse erfragen — das übernimmt das System nach dem Gespräch.
Nicht mehrere Fragen auf einmal stellen.
Nicht dieselbe Frage zweimal stellen.
Nicht "Super!", "Absolut!", "Natürlich!" als Gesprächseinstieg.
Nicht mit "Ich bin Tara" oder "Ich bin eine KI" einsteigen.
Nicht über B&B. sprechen wenn die Person etwas anderes braucht.

</sys>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE B — 3 Pattern Seeds  (STATIC)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_b(cls) -> str:
        return """<examples>

[Erstkontakt — Führung übernehmen, keine Vorstellung]
U: hallo
T: Hallo, schön dass Sie da sind. Markenarbeit hat immer einen Auslöser — was hat Sie heute hierher gebracht?

[Zustimmungsleiter — validieren, nicht verkaufen]
U: wir verlieren Bewerber an modernere Arbeitgeber
T: Das klingt, als wäre das eigentliche Problem nicht der Markt — sondern dass Ihre echte Kultur nach außen noch nicht sichtbar ist. Liegt das in der Nähe?

[Blaiq — nur wenn Zeitdruck oder Content-Thema auftaucht]
U: wir haben kaum Zeit für Content
T: Genau dafür haben wir Blaiq gebaut — eine Plattform, die Content in Ihrer eigenen Markensprache erstellt. Kunden sparen damit bis zu fünfzehn Stunden pro Woche. Was produziert Ihr Team aktuell am meisten?

</examples>"""

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE C — Dynamic per turn  (never cached)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_c(
        cls,
        query: str,
        raw_query: str,
        docs: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        user_profile: Dict[str, Any],
        hive_mind: Dict[str, Any],
    ) -> str:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        # Compact history — 4 turns, U/T prefix only
        h_lines = ""
        if history:
            for turn in history[-4:]:  # pyre-ignore[67]
                role = "U" if turn.get("role") == "user" else "T"
                h_lines += f"{role}: {cls._escape(turn.get('content', ''))}\n"
        else:
            h_lines = "[erstes Gespräch]\n"

        # Interruption detection
        interruption_hint = cls._detect_interruption(raw_query, history)

        # Entity memory (canonical brand/company name)
        entity_memory = cls._build_entity_memory(query, raw_query, history)

        # Retrieved docs — top 2, 800 chars each
        kb = ""
        if docs:
            top = sorted(docs, key=lambda d: float(d.get("score", d.get("relevance", 0))), reverse=True)[:2]  # pyre-ignore[67]
            for d in top:
                src = cls._escape(d.get("metadata", {}).get("source", "kb"))
                txt = cls._escape(d.get("text", d.get("content", "")))[:800]  # pyre-ignore[6]
                kb += f"[{src}] {txt}\n"

        # HiveMind knowledge
        hm_insights = ((hive_mind or {}).get("insights") or {})  # pyre-ignore[6]
        hm_text = ""
        for key in ("tenant_memory", "knowledge_base"):  # pyre-ignore[6]
            val = str(hm_insights.get(key) or "").strip()
            if val:
                hm_text += val[:1000] + "\n"  # pyre-ignore[6]

        # Language directive
        if lang == "en":
            lang_line = "Sprache: Englisch. Jedes Wort Englisch. Kein deutsches Wort."
        else:
            lang_line = "Sprache: Deutsch. Kein Englisch außer wenn die Person klar wechselt."

        # Assemble
        parts = [f'<ctx t="{current_time}" lang="{lang}">']
        parts.append(f"<h>\n{h_lines.strip()}\n</h>")  # pyre-ignore[6]
        if interruption_hint:
            parts.append(f"<interruption>{cls._escape(interruption_hint)}</interruption>")
        if entity_memory:
            parts.append(f"<entity>{cls._escape(entity_memory)}</entity>")
        if kb:
            parts.append(f"<kb>\n{kb.strip()}\n</kb>")  # pyre-ignore[6]
        if hm_text.strip():  # pyre-ignore[6]
            parts.append(f"<hm>\n{cls._escape(hm_text.strip())}\n</hm>")
        parts.append(f"<q>{cls._escape(query)}</q>")
        parts.append(lang_line)
        parts.append(
            "Regeln: kein Markdown, keine Listen, keine Abkürzungen in Großbuchstaben, "
            "maximal vier Sätze, erster Satz maximal zehn Wörter, eine Frage pro Antwort. "
            "Schreib so dass eine deutsche Stimme es flüssig vorlesen kann. Jetzt antworten."
        )
        parts.append("</ctx>")
        return "\n".join(parts) + "\n"

    # ── Interruption detection ────────────────────────────────────────────────

    # Filler signals — too short or too vague to be a real new turn
    _FILLER_SIGNALS = frozenset({
        "hmm", "hm", "ähm", "äh", "uh", "uhm", "okay", "ok", "ja", "nein",
        "gut", "ah", "oh", "ach so", "alles klar", "verstehe", "genau",
        "moment", "kurz", "warte", "warten sie", "entschuldigung",
    })

    @classmethod
    def _detect_interruption(cls, raw_query: str, history: List[Dict[str, Any]]) -> str:
        """
        Returns a hint string when the current query looks like an interruption.
        Empty string means: treat as a normal new turn.

        Three tiers:
          filler   — ignore and continue (Hmm, Ähm, Ok...)
          soft     — short interjection, weave into thread
          hard     — new thought mid-sentence, build a bridge
        """
        if not raw_query or not history:
            return ""

        q = raw_query.strip().lower().rstrip(".,!?")
        words = q.split()

        # Check last assistant turn ended mid-thought (no ending punctuation = was cut off)
        last_tara = ""
        for turn in reversed(history[-6:]):  # pyre-ignore[67]
            if turn.get("role") == "assistant":
                last_tara = turn.get("content", "").strip()
                break

        was_cut_off = bool(last_tara) and not last_tara[-1] in ".!?)"

        if q in cls._FILLER_SIGNALS or (len(words) == 1 and q in cls._FILLER_SIGNALS):
            return "filler: ignore this input, continue your previous thought naturally without saying 'wie ich sagte'."

        if len(words) <= 3:
            if was_cut_off:
                return (
                    f"soft_interruption: the person said '{raw_query}' while you were mid-sentence. "
                    "Acknowledge briefly in one word and continue your thought from where you left off."
                )
            return ""  # short but not a clear interruption — treat normally

        # Longer input while Tara was cut off = hard interruption
        if was_cut_off:
            return (
                f"hard_interruption: you were cut off mid-sentence, then the person said: '{raw_query}'. "
                "Build a short bridge: pick up their new point, then finish what you were saying if it still matters. "
                "Sound like a human who got briefly interrupted, not like a system that restarted."
            )

        return ""

    # ── Entity memory ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_entity(entity: str) -> str:
        return re.sub(r"\s+", " ", (entity or "").strip(" .,!?:;\"'()[]{}"))

    @classmethod
    def _extract_named_entity_signals(cls, text: str) -> List[str]:
        if not text:
            return []
        patterns = [
            r"(?:my|our|meine?r?|unser[e]?)\s+(?:company|brand|agency|firma|marke|agentur)\s+(?:is|called|heißt|ist)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'\-]+){0,3})",
            r"(?:i have|ich habe)\s+(?:a|an|eine[n]?)?\s*(?:company|brand|firma|marke)\s+(?:called|namens)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9&.'\-]+){0,3})",
        ]
        found: List[str] = []
        for pat in patterns:
            for m in re.findall(pat, text, re.IGNORECASE):
                e = cls._normalize_entity(m)
                if e and e not in found:
                    found.append(e)
        return found

    @classmethod
    def _build_entity_memory(cls, query: str, raw_query: str, history: List[Dict[str, Any]]) -> str:
        user_turns = [
            str(t.get("content", "")).strip()
            for t in history[-8:]  # pyre-ignore[67]
            if t.get("role") == "user" and t.get("content", "").strip()
        ]
        entities: List[str] = []
        for txt in user_turns:
            for e in cls._extract_named_entity_signals(txt):
                if e not in entities:
                    entities.append(e)
        if not entities:
            return ""
        canonical = entities[0]
        variants: List[str] = []
        for txt in user_turns + [str(query), str(raw_query)]:
            for e in cls._extract_named_entity_signals(txt):
                n = cls._normalize_entity(e)
                if not n or n == canonical or n in variants:
                    continue
                if SequenceMatcher(None, canonical.lower(), n.lower()).ratio() >= 0.45:
                    variants.append(n)
        result = f"canonical_name={canonical}"
        if variants:
            result += " | stt_variants=" + " | ".join(variants[:3])  # pyre-ignore[67]
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE D — Qdrant skills + rules + HiveMind  (0 tokens when empty)
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str], hive_mind: Dict[str, Any]) -> str:
        hm = ((hive_mind or {}).get("insights") or {})
        tenant = str(hm.get("tenant_memory") or "").strip()
        kb     = str(hm.get("knowledge_base") or "").strip()
        if not skills and not rules and not tenant and not kb:
            return ""
        parts = []
        if rules:
            parts.append("rules[HIGH]: " + " | ".join(cls._escape(r) for r in rules))
        if tenant:
            parts.append("memory: " + cls._escape(tenant[:1400]))  # pyre-ignore[6]
        if kb:
            parts.append("kb: " + cls._escape(kb[:1400]))  # pyre-ignore[6]
        if skills:
            parts.append("skills: " + " | ".join(cls._escape(s) for s in skills))
        return "<g>\n" + "\n".join(parts) + "\nPriority: rules > memory > kb > skills\n</g>\n"

    @staticmethod
    def _escape(text: str) -> str:
        if not text:
            return ""
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")