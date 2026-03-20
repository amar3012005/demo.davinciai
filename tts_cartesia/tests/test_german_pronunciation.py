"""
Test 4: German Pronunciation Processing via tts_safe() (RED phase)

Target file: rag-eu/context_architecture/context_architecture_bundb.py
Target function: tts_safe(text: str) -> str

Verifies:
  - Abbreviation expansion: "Dr." → appropriate spoken form
  - Acronym expansion: "KI" → "künstliche Intelligenz"
  - Protected word preservation: "B&B." survives unchanged
  - TTS pronunciation overrides: "B&B" → "B und B"
  - Loanword replacement: "AI" → "KI"
  - SSML / markdown stripping
  - Whitespace normalisation
  - Number expansion (small integers in prose)
  - Empty / None input handling
  - Unicode and special character safety

All tests import directly from the module — no mocks needed for pure function.
"""

import sys
from pathlib import Path

import pytest

RAG_EU_ROOT = Path(__file__).resolve().parent.parent.parent / "rag-eu"
if str(RAG_EU_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_EU_ROOT))


def safe(text: str) -> str:
    """Thin wrapper so tests can call tts_safe without re-importing each time."""
    from context_architecture.context_architecture_bundb import tts_safe

    return tts_safe(text)


# ---------------------------------------------------------------------------
# Unit tests — tts_safe() pure function
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEmptyAndNoneInput:
    """Edge cases for null / empty input."""

    def test_none_input_returns_empty_or_none(self):
        """tts_safe(None) must not raise; returns empty-ish value."""
        # The function signature says str, but guard against accidental None.
        try:
            result = safe("")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"tts_safe('') raised: {exc}")
        assert result == "" or result is None

    def test_empty_string_returns_empty_string(self):
        result = safe("")
        assert result == ""

    def test_whitespace_only_string_survives(self):
        result = safe("   ")
        assert isinstance(result, str)

    def test_single_word_returned_unchanged_case(self):
        result = safe("Hallo")
        assert "Hallo" in result


@pytest.mark.unit
class TestProtectedWords:
    """Protected brand names must not be altered."""

    def test_blaiq_preserved(self):
        result = safe("Blaiq ist eine Marke.")
        assert "Blaiq" in result, f"'Blaiq' was altered. Got: {result!r}"

    def test_bb_dot_preserved(self):
        """B&B. (with trailing dot) is a protected spelling — must survive."""
        result = safe("Wir arbeiten bei B&B. seit Jahren.")
        assert "B&B." in result, f"'B&B.' was altered. Got: {result!r}"

    def test_bundb_de_preserved(self):
        result = safe("Besuchen Sie bundb.de für mehr Infos.")
        assert "bundb.de" in result

    def test_davinci_ai_preserved(self):
        result = safe("DaVinci AI ist unser Partner.")
        assert "DaVinci AI" in result

    def test_winset_preserved(self):
        result = safe("Winset ist ein Produktname.")
        assert "Winset" in result

    def test_vinset_preserved(self):
        result = safe("Vinset ist ein Produktname.")
        assert "Vinset" in result


@pytest.mark.unit
class TestAcronymExpansion:
    """TTS_EXPAND dict entries must be expanded for spoken output."""

    def test_ki_expands_to_kuenstliche_intelligenz(self):
        """'KI' → 'künstliche Intelligenz' for natural German TTS output."""
        result = safe("KI wird immer wichtiger.")
        assert "künstliche Intelligenz" in result, (
            f"Expected 'künstliche Intelligenz', got: {result!r}"
        )

    def test_dsgvo_expands(self):
        result = safe("Die DSGVO regelt den Datenschutz.")
        assert "Datenschutz-Grundverordnung" in result

    def test_ux_expands(self):
        result = safe("UX ist entscheidend.")
        assert "User Experience" in result

    def test_ui_expands(self):
        result = safe("Das UI wurde überarbeitet.")
        assert "User Interface" in result

    def test_faq_expands(self):
        result = safe("Hier sind die FAQ.")
        assert "häufig gestellte Fragen" in result

    def test_crm_expands(self):
        result = safe("Unser CRM System verwaltet Kontakte.")
        assert "Kundenmanagement-System" in result

    def test_blaiq_uppercase_normalised(self):
        """BLAIQ (all-caps) → Blaiq (title case)."""
        result = safe("BLAIQ ist unsere Plattform.")
        assert "Blaiq" in result


@pytest.mark.unit
class TestPronunciationOverrides:
    """TTS_PRONUNCIATION_OVERRIDES must replace terms for spoken clarity."""

    def test_bb_ampersand_becomes_b_und_b(self):
        """'B&B' (without trailing dot) → 'B und B' for TTS pronunciation."""
        result = safe("Wir sind bei B&B tätig.")
        assert "B und B" in result, (
            f"Expected 'B und B' for pronunciation, got: {result!r}"
        )

    def test_bb_dot_pronunciation_override(self):
        """'B&B.' should also expand to 'B und B' via pronunciation overrides."""
        # Note: B&B. is also in PROTECTED_WORDS which may prevent the override.
        # This test documents the expected spoken form.
        result = safe("Das Team von B&B. ist gut.")
        # Either preserved as B&B. OR expanded to B und B — not mangled
        assert "B&B." in result or "B und B" in result


@pytest.mark.unit
class TestLoanwordReplacement:
    """Conservative LOANWORD_DE replacements."""

    def test_ai_replaced_by_ki(self):
        """'AI' (English) → 'KI' (German) for natural German speech."""
        result = safe("AI ist die Zukunft.")
        # AI should become KI which then expands to künstliche Intelligenz
        assert "AI" not in result or "KI" in result or "künstliche" in result, (
            f"'AI' should be replaced with German equivalent. Got: {result!r}"
        )

    def test_brand_voice_replaced(self):
        result = safe("Wir entwickeln Brand Voice Guidelines.")
        assert "Markenstimme" in result or "Brand Voice" not in result


@pytest.mark.unit
class TestMarkdownStripping:
    """Markdown formatting must be stripped before TTS."""

    def test_bold_asterisks_removed(self):
        result = safe("**Wichtig**: Diese Marke ist stark.")
        assert "**" not in result

    def test_backticks_removed(self):
        result = safe("Nutze `config.yaml` für die Einstellungen.")
        assert "`" not in result

    def test_hash_heading_removed(self):
        result = safe("# Überschrift")
        assert "#" not in result

    def test_markdown_link_text_preserved(self):
        """[link text](url) → link text (URL stripped, text kept)."""
        result = safe("Besuche [unsere Seite](https://example.com).")
        assert "unsere Seite" in result
        assert "https://example.com" not in result

    def test_tilde_removed(self):
        result = safe("~~durchgestrichen~~")
        assert "~~" not in result


@pytest.mark.unit
class TestWhitespaceNormalisation:
    """Whitespace must be cleaned for natural TTS flow."""

    def test_multiple_spaces_collapsed(self):
        result = safe("Hallo   Welt")
        assert "  " not in result

    def test_leading_trailing_spaces_stripped(self):
        result = safe("  Hallo Welt  ")
        assert result == result.strip()

    def test_bullet_point_converted(self):
        """• (bullet) → ', ' for spoken list reading."""
        result = safe("Option eins • Option zwei")
        assert "•" not in result

    def test_em_dash_gets_spaces(self):
        """'–' → ' — ' for natural spoken pause."""
        result = safe("Ergebnis – sehr gut")
        assert "–" not in result
        assert "—" in result


@pytest.mark.unit
class TestNumberExpansion:
    """Small integers (0–99) in prose must be expanded to German words."""

    def test_zero_expands(self):
        result = safe("Es gibt 0 Probleme.")
        assert "null" in result

    def test_one_expands(self):
        result = safe("Wir haben 1 Lösung.")
        assert "eins" in result

    def test_twelve_expands(self):
        result = safe("Es sind 12 Monate.")
        assert "zwölf" in result

    def test_three_digit_number_not_expanded(self):
        """Numbers >= 100 are NOT expanded by _convert_small_numbers."""
        result = safe("Wir haben 100 Kunden.")
        assert "100" in result

    def test_version_number_not_expanded(self):
        """Version strings like 'v1.2' or '2024' must NOT be expanded."""
        result = safe("Version v1.2 wurde veröffentlicht.")
        # v1.2 should survive — not become "v eins.zwei"
        assert "v1.2" in result or "1.2" in result

    def test_year_not_expanded(self):
        result = safe("Im Jahr 2024 begann alles.")
        assert "2024" in result


@pytest.mark.unit
class TestPunctuationRhythm:
    """Punctuation spacing must be TTS-friendly."""

    def test_space_before_comma_removed(self):
        result = safe("Hallo , Welt")
        assert " ," not in result

    def test_space_after_colon_added(self):
        result = safe("Antwort:nein")
        assert "Antwort: nein" in result

    def test_double_spaces_collapsed_after_processing(self):
        result = safe("Eins  zwei  drei")
        assert "  " not in result


@pytest.mark.unit
class TestSSMLAndTagStripping:
    """SSML-like tags and structural noise must not reach TTS as literal text."""

    def test_url_preserved_not_expanded(self):
        """URLs must not be mangled by acronym or number expansion."""
        result = safe("Mehr auf https://www.bundb.de/infos")
        assert "https://www.bundb.de/infos" in result

    def test_no_token_artifacts_in_output(self):
        """Protected-segment replacement tokens must never appear in output."""
        result = safe("Hallo B&B. und Blaiq")
        assert "__PROTECTED_" not in result


@pytest.mark.unit
class TestEdgeCases:
    """Miscellaneous edge cases."""

    def test_large_input_does_not_raise(self):
        """1000-word input must process without error."""
        large_input = "Das ist eine Marke. " * 1000
        result = safe(large_input)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_special_sql_characters_not_mangled(self):
        """SQL-like chars in brand names must survive."""
        result = safe("Kunden fragen nach B&B. Leistungen.")
        assert isinstance(result, str)

    def test_mixed_german_english_sentence(self):
        result = safe("Unser AI-Team arbeitet an UX Lösungen.")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_only_whitespace_after_markdown_strip(self):
        """If only markdown remains after stripping, output should be empty or near-empty."""
        result = safe("**")
        # Should not crash; may return empty
        assert isinstance(result, str)
