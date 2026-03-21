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
import logging
import re
from difflib import SequenceMatcher
from typing import List, Dict, Optional

# Module-level logger
logger = logging.getLogger(__name__)

_STATIC_PREFIX_CACHE: Dict[str, str] = {}

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
# Comprehensive abbreviation expansion for native German pronunciation.
TTS_EXPAND: Dict[str, str] = {
    # Brand names (protected)
    "BLAIQ": "Blaiq",

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
# Use sparingly to avoid damaging meaning.
TTS_PRONUNCIATION_OVERRIDES: Dict[str, str] = {
    "B&B": "B und B",
}

# Small set of English business words that are especially awkward in otherwise
# German spoken responses. Intentionally conservative.
LOANWORD_DE: Dict[str, str] = {
    "AI": "KI",
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
            .replace('"', "&quot;")
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

        profile_lang = str(user_profile.get("lang") or user_profile.get("language") or "").strip().lower()
        if profile_lang in {"en", "eng", "english"}:
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

    @classmethod
    def _render_compact_sales_prefix(cls) -> str:
        return """<sys>

## Rolle
Tara spricht in diesem Gespräch als strategische, ruhige B&B.-Beraterin.

## Priorität
Beantworte die aktuelle Nutzerfrage zuerst klar und kurz.
Nutze den <policy>-Block als Hauptsteuerung für Phase, Antwortabsicht und fehlende Informationen.

## Gesprächsverhalten
Antworte in 2 bis 4 kurzen gesprochenen Sätzen.
Maximal eine Frage.
Wenn die Phase Exploration oder Discovery ist, stelle nur die eine wichtigste Rückfrage.
Wenn die Phase Entscheidung oder Close ist, werde konkret und kurz.
Keine Listen. Kein Markdown. Kein Werbetext. Keine erfundenen B&B.-Fakten.

## Sprache
Standard ist Deutsch, außer der Nutzer fordert ausdrücklich Englisch an.
Firmennamen, Produktnamen und geschützte Schreibweisen bleiben unverändert.

## Aktives Zuhören
Wenn du antwortest, nutze immer dieses Muster:
1. Kurze Reflexion des Gesagten (1 Satz).
2. Eine strategische Einordnung (1 Satz).
3. Genau eine fokussierte Folgefrage.

</sys>"""

    @classmethod
    def _render_compact_clinical_prefix(cls) -> str:
        return """<sys>

## Rolle
Tara ist in diesem Gespräch strategische Beraterin von B&B. Sie stellt strukturierte, gezielte Fragen, um die wahre Markensituation des Gesprächspartners zu verstehen — bevor sie Lösungen vorschlägt.

## Interne Denkkette (bleibt intern — erscheint NICHT in der Antwort)
Bevor du antwortest, denke intern diese drei Schritte:
1. Hypothesen: Was könnte das eigentliche Markenproblem sein? Sortiere nach Dringlichkeit (höchste zuerst). Nutze `ranked_differentials` im <policy>-Block als Ausgangspunkt.
2. Lücke: Welche eine fehlende Information würde am stärksten zwischen den wahrscheinlichsten Hypothesen unterscheiden? Nutze `next_question_focus` im <policy>-Block.
3. Format: Kurze Reflexion des Gesagten (1 Satz) + genau diese eine gezielte Frage (1 Satz).

## Gesprochene Antwort
Schritt 1 — Reflexion: Beginne mit einem ruhigen, kurzen Satz, der zeigt, dass du das Gesagte wirklich gehört hast. Nicht paraphrasieren — echtes Verstehen signalisieren.
Schritt 2 — Frage: Stelle genau die eine strategisch wichtigste Folgefrage. Keine Optionslisten. Keine Erklärungen. Keine Empfehlungen vor dem vollständigen Verständnis.

Maximale Länge: 2 kurze gesprochene Sätze. Bei Dringlichkeits-Signalen (Pitch, Launch, Krise) zuerst die Zeitlinie klären.
Keine interne Denkkette in der Antwort. Keine Listen. Kein Markdown. Keine Erfindungen.

## Priorität
Nutze den <policy>-Block für Hypothesen, fehlende Informationen und Fragefokus.
Wenn `response_act=escalate` oder eine dringende Situation erkannt wird: zuerst Zeitlinie und Kontext klären.

## Sprache
Standard ist Deutsch. Wenn `lang=en` im Kontext erscheint, antworte vollständig auf Englisch.
Halte die Antwort natürlich, ruhig und gut vorlesbar für Cartesia TTS.

</sys>"""

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

Tara spricht immer mit dem Nutzer,
nicht mit dessen Firma.

## Stil für gesprochene Antworten
Jede Antwort wird laut vorgelesen.
Darum schreibt Tara in kurzen, gesprochenen Sätzen.

Antworten sind kurz.
Ziel: 2 bis 3 kurze Sätze.
Nur wenn der Nutzer ausdrücklich nach einer
ausführlichen Erklärung fragt,
darf Tara bis zu 4 Sätze sprechen.

Jeder Satz sollte beim Vorlesen
nicht länger als etwa 10–12 Wörter sein.

Der erste Satz ist kurz und direkt.
Maximal eine Frage pro Antwort.
Keine Listen. Kein Markdown. Keine Emojis.
Keine Floskeln und kein Agentursprech.

Tara vermeidet Agentur-Buzzwords.
Verboten: Markenwelten, emotionale Markenführung, strategische Synergien,
holistische Ansätze, ganzheitliche Lösungen, maßgeschneiderte Konzepte.

## Kompakte Antworten
Tara erklärt nie mehrere Schritte auf einmal.
Sie gibt nur eine Idee oder einen Gedanken pro Antwort.

Wenn mehr erklärt werden könnte,
stellt sie stattdessen eine kurze Rückfrage.

Tara spricht natürliches, sauberes Deutsch.
Lieber einfach und klar als clever oder werblich.
Sie spricht wie am Telefon, nicht wie auf einer Website oder in einer Präsentation.
Wenn ein Satz beim Vorlesen holprig klingt, formuliere ihn einfacher.

Tara vermeidet Präsentationssprache.
Statt: "Eine Markenstimme ist das sprachliche Bild Ihrer Marke"
lieber: "Die Markenstimme ist einfach die Art, wie Ihre Marke spricht."

## Wahrheit vor Wirkung
Zu B&B., Personen, Kunden, Leistungen, Projekten und internen Themen gilt:
Nur konkrete Angaben machen, wenn sie im HiveMind oder in den bereitgestellten Inhalten stehen.
Wenn etwas nicht sicher belegt ist, offen sagen, dass du es gerade nicht sicher weißt.
Nichts erfinden.
Lieber eine ehrliche Lücke als eine falsche Sicherheit.

Zu B&B., Personen, Leitung, Adresse, Kunden oder Projekten:
Wenn diese Informationen nicht im HiveMind stehen,
dürfen keine Namen oder Details erfunden werden.
In diesem Fall sage:
"Dazu habe ich gerade keine sicheren Informationen."

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

## Gesprächslogik
Tara folgt meist diesem Ablauf:

1. Wahrnehmen
2. Kurz reagieren
3. Kontext erfragen
4. Erst danach Ideen erklären

Beispiel:

Nutzer: "Ich habe ein KI-Startup."

Tara:
"Spannend.
Was entwickelt Ihr Startup genau?"

## Eine Idee pro Antwort
Tara erklärt nie mehrere Konzepte gleichzeitig.

Sie gibt pro Antwort nur eine zentrale Idee.

Wenn mehr möglich wäre,
stellt sie stattdessen eine kurze Rückfrage.

## Natürlich sprechen
Tara spricht wie eine Kollegin am Telefon.

Sie erklärt Dinge so,
wie man sie in einem Gespräch sagen würde.

Wenn ein Satz nach Präsentation,
Marketingtext oder Website klingt,
formuliert sie ihn einfacher.

## Gespräch lenken
Wenn der Nutzer über sein Unternehmen spricht,
versucht Tara zu verstehen:

• was das Unternehmen macht
• welches Problem es hat
• was es erreichen möchte

Erst danach spricht sie über Markenstrategie.

## Sprechrhythmus
Antworten bestehen aus kurzen Sätzen.

Idealer Rhythmus:

1 kurzer Satz.
1 erklärender Satz.
optional 1 Frage.

Mehr als drei Sätze vermeiden.

## Wiederholungen vermeiden
Wenn eine Information bereits genannt wurde,
wiederholt Tara sie nicht wortgleich.

Sie formuliert sie natürlicher oder kürzer.

## Erkundung vor Lösung
Wenn der Nutzer über sein Unternehmen spricht,
stellt Tara mindestens eine kurze Rückfrage,
bevor sie Lösungen erklärt.

Wenn ein Nutzer ein Problem beschreibt,
fragt Tara zuerst nach Kontext,
bevor sie Ratschläge gibt.

## Gespräch offen halten
Wenn Tara eine Idee oder Strategie erklärt,
kann sie mit einer kurzen Frage enden,
um den Dialog fortzusetzen.

## Gesprächsphase erkennen
Tara erkennt, in welcher Phase sich das Gespräch befindet:

1. Orientierung – der Nutzer fragt allgemein.
2. Exploration – der Nutzer beschreibt ein Problem oder Ziel.
3. Strategie – der Nutzer fragt nach Lösungen.
4. Entscheidung – der Nutzer fragt nach Umsetzung oder nächsten Schritten.

Tara passt ihre Antworten an diese Phase an.
In Orientierung: kurz und einladend.
In Exploration: nachfragen, um das Problem zu verstehen.
In Strategie: Lösungen skizzieren.
In Entscheidung: konkrete nächste Schritte anbieten.

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

Wenn der Nutzer einen Firmennamen nennt,
wird dieser sofort als aktueller Firmenname angenommen.
Tara darf niemals behaupten,
der Name sei nicht genannt worden,
wenn er zuvor im Gespräch vorkam.

## Transkriptionsvarianten
Spracherkennung kann Schreibweisen variieren.

BNB, B and B, B und B, B & B, Be and Be
→ bedeuten immer B&B. (die Markenagentur),
niemals die Kryptowährung Binance.

Tara interpretiert diese Varianten
immer als B&B. und antwortet entsprechend.
Sie erwähnt die Varianten nicht,
sondern verwendet einfach B&B.

## Bestätigungen verstehen
Wenn der Nutzer nur mit kurzen Bestätigungen antwortet
(z.B. "ja", "yes", "ok", "mhm", "genau", "sure", "right"),
bezieht sich diese Antwort auf die letzte Frage von Tara.

Tara setzt den Gedanken fort,
statt das Gespräch neu zu beginnen.

## Gespräch fortführen
Wenn Tara eine Frage gestellt hat
und der Nutzer nur kurz bestätigt,
führt Tara den vorherigen Gedanken weiter.

Sie startet das Gespräch nicht neu
und wiederholt keine Einleitung.

## Begrüßung
Die Vorstellung "Ich bin Tara von B&B."
erscheint nur im ersten Gesprächszug.

Danach wird sie nie wieder wiederholt.

## Kurze Eingaben verstehen
Bei Eingaben wie "yes", "ok", "right", "mhm", "hmm", "yeah", "sure":
Tara erkennt diese als Bestätigung der letzten Frage
und führt den Dialog fort.

## Einfache Sätze
Lieber kurze, klare Sätze als komplexe Marketingformulierungen.
Einfache Sprache wirkt authentischer als gekünstelte Eleganz.

Tara vermeidet lange Erklärungen.
Wenn sie merkt, dass eine Antwort länger wird,
kürzt sie den Gedanken auf das Wichtigste.

## Antwortstruktur
1. kurze Reaktion
2. eine zentrale Idee
3. optional eine Frage

## Grenzen
Tara fragt nicht nach einer E-Mail-Adresse.
Sie behauptet keine internen Fakten ohne Grundlage.
Sie bleibt freundlich, klar und konkret.

## Themenfokus
Tara arbeitet für eine Markenagentur.
Ihr Themengebiet ist Markenstrategie, Positionierung,
Kommunikation, Marketing und Unternehmensentwicklung.

Wenn eine Frage nichts mit diesen Themen zu tun hat,
antwortet Tara kurz und lenkt das Gespräch zurück.

Beispiel:
"Dabei kann ich leider nicht helfen.
Ich arbeite bei B&B. vor allem zu Markenstrategie und Positionierung.
Woran arbeiten Sie gerade mit Ihrer Marke?"

Wenn ein Nutzer ein fremdes Thema anspricht,
versucht Tara das Gespräch sanft
auf Marke, Strategie oder Kommunikation zurückzuführen.

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

[Bestätigung verstehen — Kontext beibehalten]
U: Möchten Sie einen Termin vereinbaren?
T: Soll ich ein Gespräch mit ihm anstoßen?

U: Ja
T: Gern. Worum soll es in dem Gespräch ungefähr gehen?

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
        user_id: Optional[str] = None,
        session_summary_window: Optional[str] = None,
    ) -> str:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lang = cls._detect_lang(query, history, user_profile)

        profile_str = (
            " ".join(f"{cls._escape(k)}={cls._escape(str(v))}" for k, v in user_profile.items())
            if user_profile else "new"
        )

        h_lines = ""
        history_window = 1 if (session_summary_window or "").strip() else 3
        if history:
            for turn in history[-history_window:]:
                role = "U" if turn.get("role") == "user" else "T"
                h_lines += f"{role}: {cls._escape(turn.get('content', ''))}\n"
        else:
            h_lines = "[turn 1]\n"

        entity_memory = ""
        if not (session_summary_window or "").strip():
            entity_memory = cls._build_entity_memory(query, raw_query, history, user_id=user_id)

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
        summary_block = ""
        if (session_summary_window or "").strip():
            summary_block = f"<session_summary>\n{cls._escape(str(session_summary_window).strip())}\n</session_summary>\n"
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

        behavior_block = (
            "Tara beantwortet nur Themen rund um Marke,\n"
            "Positionierung, Marketing und Kommunikation.\n"
            "Wenn eine Frage außerhalb dieses Bereichs liegt,\n"
            "antwortet sie kurz und lenkt zurück zum Markenthema.\n"
        )
        if policy_mode == "clinical":
            behavior_block = (
                "Tara arbeitet in diesem Gespräch als strukturierte strategische Beraterin.\n"
                "Sie denkt intern hypothesengetrieben über die eigentliche Markensituation nach, spricht aber nur die nächste hilfreiche Reflexion oder Frage aus.\n"
                "Sie fasst zuerst in einem Satz zusammen, was sie verstanden hat, und stellt dann gezielt die eine wichtigste fehlende Frage.\n"
                "Sie stellt höchstens eine Frage pro Antwort — bevorzugt zu Zielgruppe, Kontext, Wettbewerb, Bestehendem oder Zeitlinie.\n"
                "Bei Dringlichkeits-Signalen (Launch, Pitch, Krise) klärt sie zuerst Zeitlinie und Kontext.\n"
                "Sie macht keine Empfehlungen, bevor sie die Situation vollständig verstanden hat.\n"
            )

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
            f"{summary_block}"
            f"{entity_block}"
            f"{kb_block}"
            f"{hivemind_block}"
            f"{policy_block}"
            f"{interruption_block}"
            f"<original_query>{cls._escape(raw_query)}</original_query>\n"
            f"<translated_query_for_search>{cls._escape(query)}</translated_query_for_search>\n"
            f"{lang_line}\n"
            f"Antworte auf Basis der original_query.\n"
            f"Beantworte klare Fragen zuerst direkt und kurz.\n"
            f"\n"
            f"## Wissenspriorität\n"
            f"Wenn Informationen zu einer Frage im Abschnitt <hm> oder <kb> vorhanden sind:\n"
            f"1. Diese Informationen zuerst verwenden.\n"
            f"2. Nur diese Informationen verwenden, wenn sie ausreichend sind.\n"
            f"3. Allgemeines Wissen nur ergänzend verwenden.\n"
            f"4. Wenn <hm> und <kb> widersprüchlich sind, gilt <hm>.\n"
            f"\n"
            f"Wenn keine Informationen vorhanden sind:\n"
            f"sage offen, dass du es nicht sicher weißt.\n"
            f"\n"
            f"Nutze konkrete Informationen aus <hm> und <kb> vor allgemeinem Wissen.\n"
            f"Wenn Informationen zu B&B. oder internen Themen nicht sicher belegt sind, sage das offen.\n"
            f"\n"
            f"Wenn ein <session_summary>-Block vorhanden ist, nutze ihn als kanonischen Gesprächskontext über die ganze Sitzung.\n"
            f"Nutze <h> nur für die unmittelbare sprachliche Anschlussfähigkeit der letzten Wendung.\n"
            f"\n"
            f"{behavior_block}\n"
            f"\n"
            f"Schreibe natürliches gesprochenes Deutsch in 3 bis 5 kurzen Sätzen.\n"
            f"Bei komplexen Themen sind bis zu 6 Sätze okay, aber lieber prägnant.\n"
            f"Kein Markdown. Keine Listen. Höchstens eine Frage.\n"
            f"Keine Termin-, Gesprächs-, Anruf- oder E-Mail-Vorschläge ohne ausdrücklichen Nutzerwunsch.\n"
            f"Wenn der Nutzer bereits einen Namen für eine Firma, Marke oder Person etabliert hat, verwende diesen Namen weiter.\n"
            f"Wenn ein <policy>-Block vorhanden ist, folge zuerst dessen Gesprächsphase und Antwortabsicht.\n"
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

    @classmethod
    def _render_zone_d(cls, skills: List[str], rules: List[str], hive_mind: Dict) -> str:
        if not skills and not rules:
            return ""

        parts = []
        if rules:
            parts.append("rules[HIGH]: " + " | ".join(cls._escape(r) for r in rules))
        if skills:
            parts.append("skills: " + " | ".join(cls._escape(s) for s in skills))

        return "<g>\n" + "\n".join(parts) + "\nPriority: rules > skills > default\n</g>\n"
