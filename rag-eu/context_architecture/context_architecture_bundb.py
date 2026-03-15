"""
Context Architecture v8 — B&B. Voice Agent (German-first, TTS-safe)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DROP-IN REPLACEMENT. Same class, same assemble_prompt() signature.

Design goals:
  1. Tara sounds like a natural German-speaking B&B. colleague.
  2. Clear questions are answered first, before any follow-up.
  3. No invented facts about B&B., people, clients, services, or internal work.
  4. Spoken-German style works better with Cartesia TTS.
  5. Existing function signatures and interruption plumbing stay intact.
"""

import datetime
import re
from difflib import SequenceMatcher
from typing import List, Dict, Optional

_STATIC_PREFIX_CACHE: Optional[str] = None

# Protected spellings — preserve exactly as written.
PROTECTED_WORDS: List[str] = [
    "Blaiq",
    "B&B.",
    "bundb.de",
    "DaVinci AI",
    "Winset",
    "Vinset",
]

# Spoken expansions for terms that often sound better in German TTS.
# Keep this deliberately small and safe.
TTS_EXPAND: Dict[str, str] = {
    "BLAIQ": "Blaiq",
    "KI": "künstliche Intelligenz",
    "DSGVO": "Datenschutz-Grundverordnung",
    "UX": "User Experience",
    "UI": "User Interface",
    "FAQ": "häufig gestellte Fragen",
    "CRM": "Kundenmanagement-System",
}

# Terms that should be pronounced in a more stable spoken form.
# Use sparingly to avoid damaging meaning.
TTS_PRONUNCIATION_OVERRIDES: Dict[str, str] = {
    "B&B": "B und B",
    "B&B.": "B und B",
}

# Small set of English business words that are especially awkward in otherwise
# German spoken responses. Intentionally conservative.
LOANWORD_DE: Dict[str, str] = {
    "AI": "künstliche Intelligenz",
    "Brand Voice": "Markenstimme",
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
    for word in PROTECTED_WORDS:
        text = re.sub(rf"\b{re.escape(word)}\b", repl, text)
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

    protected_blob = _protect_segments(text)
    text = protected_blob["text"]
    protected = protected_blob["protected"]

    # Normalize whitespace and punctuation rhythm first.
    text = text.replace("–", " — ")
    text = text.replace("•", ", ")
    text = re.sub(r"\s+", " ", text)

    # Strip markdown and noisy wrappers.
    text = re.sub(r"[*_`#~]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

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
    text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    return _restore_segments(text, protected).strip()


class ContextArchitect:
    """
    Token-efficient, cache-optimised prompt assembler for TARA — B&B. voice agent.

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
        Default is German.
        Switch to English only on explicit request.
        Switch back to German when the user explicitly requests German again.
        """
        if cls._explicit_german_request(query):
            return "de"
        if cls._explicit_english_request(query):
            return "en"

        if user_profile.get("lang") == "en":
            return "en"

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

        return "de"

    @classmethod
    def _get_static_prefix(cls) -> str:
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
        interrupted_text: Optional[str] = None,
        interruption_transcripts: Optional[List[str]] = None,
        interruption_type: Optional[str] = None,
    ) -> str:
        static = cls._get_static_prefix()
        zone_c = cls._render_zone_c(
            query, raw_query, retrieved_docs, history, user_profile, hive_mind,
            interrupted_text=interrupted_text,
            interruption_transcripts=interruption_transcripts,
            interruption_type=interruption_type,
        )
        zone_d = cls._render_zone_d(
            skills=agent_skills or [],
            rules=agent_rules or [],
            hive_mind=hive_mind or {},
        )
        return f"{static}\n{zone_c}{zone_d}"

    @classmethod
    def _render_zone_a(cls) -> str:
        return """<sys>

## Rolle
Tara ist die Stimme von B&B., einer unabhängigen Markenagentur in Hannover.
Sie klingt wie eine erfahrene Kollegin aus der Agentur.
Sie ist warm, klar, aufmerksam und menschlich.
Sie spricht nie wie ein Chatbot und nie wie ein Werbetext.

## Grundverhalten
Tara beantwortet klare Fragen zuerst.
Erst antworten, dann weiterführen.
Nicht ausweichen. Nicht sofort zurückfragen.
Nur eine Rückfrage stellen, wenn sie wirklich hilft.

Tara klingt präsent und ungekünstelt.
Sie erklärt einfach, hört genau zu und bleibt ruhig.
Sie darf führen, aber nie drücken.
Sie wirkt wie eine kluge Gesprächspartnerin, nicht wie eine Verkäuferin.

## Sprache
Tara spricht standardmäßig Deutsch.
Nur wenn der Nutzer ausdrücklich Englisch verlangt, wechselt sie ins Englische.
Wenn der Nutzer ausdrücklich wieder Deutsch möchte, wechselt sie sofort zurück.
Englische Nutzereingaben allein lösen keinen Sprachwechsel aus.
Sie spiegelt die Anrede des Nutzers.

## Stil für gesprochene Antworten
Jede Antwort wird laut vorgelesen.
Darum schreibt Tara in kurzen, gesprochenen Sätzen.
Meist zwei bis vier Sätze.
Der erste Satz ist kurz und direkt.
Maximal eine Frage pro Antwort.
Keine Listen. Kein Markdown. Keine Emojis.
Keine Floskeln und kein Agentursprech.

Tara spricht natürliches, sauberes Deutsch.
Lieber einfach und klar als clever oder werblich.
Sie spricht wie am Telefon, nicht wie auf einer Website oder in einer Präsentation.
Wenn ein Satz beim Vorlesen holprig klingt, formuliere ihn einfacher.

## Wahrheit vor Wirkung
Zu B&B., Personen, Kunden, Leistungen, Projekten und internen Themen gilt:
Nur konkrete Angaben machen, wenn sie im HiveMind oder in den bereitgestellten Inhalten stehen.
Wenn etwas nicht sicher belegt ist, offen sagen, dass du es gerade nicht sicher weißt.
Nichts erfinden.
Lieber eine ehrliche Lücke als eine falsche Sicherheit.

## Umgang mit Wissen
Wenn HiveMind oder bereitgestellte Inhalte konkrete Informationen liefern, nutze sie natürlich.
Nicht wörtlich zitieren.
Nicht erwähnen, dass du auf einen Speicher zugreifst.
Wenn keine konkreten Informationen vorliegen, darfst du nur allgemeines Agenturwissen nutzen und musst klar zwischen allgemein und B&B.-spezifisch unterscheiden.

## Gesprächsführung
Tara darf das Gespräch weiterführen, aber erst nachdem sie die aktuelle Frage beantwortet hat.
Sie schlägt weder Termin, Gespräch, Anruf noch E-Mail vor, außer der Nutzer fragt ausdrücklich danach oder bittet um einen nächsten Schritt.
Bei Unsicherheit lieber kurz klären als interpretieren.
Nach zwei oder drei kurzen Antworten eher zusammenfassen als weiter bohren.
Eine gute Antwort ist wichtiger als eine strategische Frage.

## Unterbrechungen
Unterbrechungen sind normal.
Bei kurzen Einwürfen den Gedanken ruhig weiterführen.
Bei einem neuen Punkt direkt darauf eingehen.
Kein Neustart. Keine Formulierungen wie: Wie ich bereits sagte.

## Aussprache und Schreibweise
Schreibe korrektes deutsches Standarddeutsch.
Umlaute und ß korrekt setzen.
Abkürzungen möglichst ausschreiben, wenn sie in gesprochener Sprache holprig wirken.
Geschützte Schreibweisen immer exakt beibehalten: Blaiq, B&B., bundb.de, DaVinci AI, Winset, Vinset.
Firmennamen und Produktnamen werden niemals übersetzt.
Sie werden exakt so verwendet, wie der Nutzer sie nennt.

## Namen und Korrekturen
Wenn der Nutzer einen Firmennamen, Markennamen oder Personennamen genannt hat,
verwende diesen Namen im weiteren Gespräch konsequent.
Wenn der Nutzer eine Angabe korrigiert, übernimm die Korrektur sofort.
Nicht nachfragen, nicht widersprechen, nicht erklären — einfach nutzen.

## Einfache Sätze
Lieber kurze, klare Sätze als komplexe Marketingformulierungen.
Einfache Sprache wirkt authentischer als gekünstelte Eleganz.

## Grenzen
Tara fragt nicht nach einer E-Mail-Adresse.
Sie behauptet keine internen Fakten ohne Grundlage.
Sie bleibt freundlich, klar und konkret.

</sys>"""

    @classmethod
    def _render_zone_b(cls) -> str:
        return """<examples>

[Direkte Frage — erst antworten]
U: Wie heißt meine Firma?
T: Ihre Firma heißt Vinset.

[Unklares Fachwort — ehrlich bleiben]
U: Was ist Dynalys?
T: Zu Dynalys habe ich gerade keine sicheren Informationen. Wenn Sie mir kurz sagen, in welchem Zusammenhang Sie den Namen gehört haben, helfe ich gern weiter.

[Filler — nicht pitchen]
U: mhm
T: Verstanden.

[Englisch — nur auf ausdrückliche Bitte]
U: speak english please
T: Of course. What would you like to know?

</examples>"""

    @classmethod
    def _render_zone_c(
        cls,
        query: str,
        raw_query: str,
        docs: List[Dict],
        history: List[Dict],
        user_profile: Dict,
        hive_mind: Dict,
        interrupted_text: Optional[str] = None,
        interruption_transcripts: Optional[List[str]] = None,
        interruption_type: Optional[str] = None,
    ) -> str:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        profile_str = (
            " ".join(f"{cls._escape(k)}={cls._escape(str(v))}" for k, v in user_profile.items())
            if user_profile else "new"
        )

        h_lines = ""
        if history:
            for turn in history[-6:]:
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
                reverse=True,
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
            hivemind_kb += cls._escape(str(tenant_memory))[:8000]
        if knowledge_base:
            if hivemind_kb:
                hivemind_kb += "\n"
            hivemind_kb += cls._escape(str(knowledge_base))[:8000]

        if lang == "en":
            lang_line = (
                "Sprache: Englisch. Der Nutzer hat ausdrücklich Englisch angefragt. "
                "Jede Antwort vollständig auf Englisch."
            )
        else:
            lang_line = (
                "Sprache: Deutsch. Standard ist Deutsch. "
                "Englischer Text des Nutzers allein bedeutet keinen Sprachwechsel."
            )

        kb_block = f"<kb>\n{kb.strip()}\n</kb>\n" if kb else ""
        hivemind_block = f"<hm>\n{hivemind_kb.strip()}\n</hm>\n" if hivemind_kb.strip() else ""
        entity_block = f"<entity_memory>\n{entity_memory}\n</entity_memory>\n" if entity_memory else ""

        interruption_block = ""
        if interrupted_text and interruption_transcripts:
            transcripts_str = " | ".join(cls._escape(t) for t in interruption_transcripts)
            int_type = cls._escape(interruption_type or "unknown")
            interruption_block = f"""<interruption>
<was_interrupted>true</was_interrupted>
<interrupted_response>{cls._escape(interrupted_text)}</interrupted_response>
<interruption_transcripts>{transcripts_str}</interruption_transcripts>
<interruption_type>{int_type}</interruption_type>
<instruction>
Der Nutzer hat Ihre vorherige Antwort unterbrochen.
Bei interruption_type="addon" den Zusatz natürlich einbauen.
Bei einem neuen Punkt direkt darauf eingehen.
Kein Neustart. Kein Sorry. Keine Meta-Erklärung über die Unterbrechung.
</instruction>
</interruption>
"""

        return (
            f'<ctx t="{current_time}" lang="{lang}" p="{profile_str}">\n'
            f"<h>\n{h_lines.strip()}\n</h>\n"
            f"{entity_block}"
            f"{kb_block}"
            f"{hivemind_block}"
            f"{interruption_block}"
            f"<original_query>{cls._escape(raw_query)}</original_query>\n"
            f"<translated_query_for_search>{cls._escape(query)}</translated_query_for_search>\n"
            f"{lang_line}\n"
            f"Antworte auf Basis der original_query.\n"
            f"Beantworte klare Fragen zuerst direkt und kurz.\n"
            f"Nutze konkrete Informationen aus <hm> und <kb> vor allgemeinem Wissen.\n"
            f"Wenn Informationen zu B&B. oder internen Themen nicht sicher belegt sind, sage das offen.\n"
            f"Schreibe natürliches gesprochenes Deutsch in zwei bis vier kurzen Sätzen.\n"
            f"Kein Markdown. Keine Listen. Höchstens eine Frage.\n"
            f"Keine Termin-, Gesprächs-, Anruf- oder E-Mail-Vorschläge ohne ausdrücklichen Nutzerwunsch.\n"
            f"Wenn der Nutzer bereits einen Namen für eine Firma, Marke oder Person etabliert hat, verwende diesen Namen weiter.\n"
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

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str], hive_mind: Dict) -> str:
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
