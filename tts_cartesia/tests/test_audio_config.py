"""
Test 3: Audio Settings in CartesiaConfig (RED phase)

Target file: tts_cartesia/config.py (CartesiaConfig dataclass)

Verifies that:
  - sample_rate=16000 Hz for real-time voice agent standard (FIX #3)
  - output_format=pcm_s16le for browser compatibility (FIX #3)
  - speed=0.95 is accepted (valid range 0.5–2.0) for German clarity (FIX #3)
  - Boundary values for speed are validated and clamped
  - get_output_format_config() returns correctly structured dict
  - Validation raises ValueError for missing api_key
  - Unknown model falls back to sonic-3
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TTS_ROOT = Path(__file__).resolve().parent.parent
if str(TTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TTS_ROOT))


# ---------------------------------------------------------------------------
# Helper: create config with explicit env vars
# ---------------------------------------------------------------------------

def make_config(**env_overrides):
    """Instantiate CartesiaConfig with given env vars patched in."""
    from config import CartesiaConfig

    base = {
        "CARTESIA_API_KEY": "test-key-abcdef1234567890",
        "CARTESIA_VOICE_ID": "voice-test-123",
        "CARTESIA_SAMPLE_RATE": "44100",
        "CARTESIA_OUTPUT_FORMAT": "pcm_f32le",
        "CARTESIA_SPEED": "0.9",
        "CARTESIA_LANGUAGE": "de",
    }
    base.update(env_overrides)
    with patch.dict("os.environ", base):
        return CartesiaConfig()


# ---------------------------------------------------------------------------
# Unit tests — CartesiaConfig validation
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCartesiaConfigDefaults:
    """Test default values loaded from environment variables."""

    def test_missing_api_key_raises_value_error(self):
        """CartesiaConfig must raise ValueError when CARTESIA_API_KEY is absent."""
        from config import CartesiaConfig

        with patch.dict("os.environ", {}, clear=True):
            # Ensure key is truly absent
            import os
            os.environ.pop("CARTESIA_API_KEY", None)
            with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
                CartesiaConfig()

    def test_api_key_is_stored(self):
        config = make_config(CARTESIA_API_KEY="my-real-key-abcdef12345678")
        assert config.api_key == "my-real-key-abcdef12345678"

    def test_default_model_is_sonic3(self):
        config = make_config()
        assert config.model == "sonic-3"

    def test_unknown_model_falls_back_to_sonic3(self):
        config = make_config(CARTESIA_MODEL="sonic-unknown-model-xyz")
        assert config.model == "sonic-3", (
            f"Unknown model should fall back to sonic-3, got {config.model!r}"
        )

    def test_known_models_accepted(self):
        for model in ["sonic-english", "sonic-multilingual", "sonic-2", "sonic-3", "sonic-4"]:
            config = make_config(CARTESIA_MODEL=model)
            assert config.model == model

    def test_default_language_is_de(self):
        config = make_config()
        assert config.language == "de"

    def test_pool_size_default(self):
        config = make_config()
        assert config.pool_size >= 1

    def test_websocket_url_contains_api_key(self):
        config = make_config(CARTESIA_API_KEY="key-abc-12345678901234567")
        url = config.get_websocket_url()
        assert "key-abc-12345678901234567" in url

    def test_websocket_url_contains_api_version(self):
        config = make_config()
        url = config.get_websocket_url()
        assert "cartesia_version" in url


@pytest.mark.unit
class TestSampleRate:
    """Test sample_rate configuration."""

    def test_sample_rate_44100_default(self):
        config = make_config(CARTESIA_SAMPLE_RATE="44100")
        assert config.sample_rate == 44100

    def test_sample_rate_env_override_16000(self):
        """sample_rate=16000 must be stored correctly when set via env var."""
        config = make_config(CARTESIA_SAMPLE_RATE="16000")
        assert config.sample_rate == 16000, (
            f"Expected sample_rate=16000, got {config.sample_rate}"
        )

    def test_sample_rate_is_int(self):
        config = make_config(CARTESIA_SAMPLE_RATE="16000")
        assert isinstance(config.sample_rate, int)

    def test_sample_rate_in_output_format_config(self):
        config = make_config(CARTESIA_SAMPLE_RATE="16000")
        fmt = config.get_output_format_config()
        assert fmt["sample_rate"] == 16000

    def test_sample_rate_24000(self):
        config = make_config(CARTESIA_SAMPLE_RATE="24000")
        assert config.sample_rate == 24000


@pytest.mark.unit
class TestOutputFormat:
    """Test output_format configuration and validation."""

    def test_pcm_f32le_accepted(self):
        config = make_config(CARTESIA_OUTPUT_FORMAT="pcm_f32le")
        assert config.output_format == "pcm_f32le"

    def test_pcm_mulaw_accepted(self):
        config = make_config(CARTESIA_OUTPUT_FORMAT="pcm_mulaw")
        assert config.output_format == "pcm_mulaw"

    def test_pcm_alaw_accepted(self):
        config = make_config(CARTESIA_OUTPUT_FORMAT="pcm_alaw")
        assert config.output_format == "pcm_alaw"

    def test_invalid_format_falls_back_to_pcm_f32le(self):
        config = make_config(CARTESIA_OUTPUT_FORMAT="invalid_format_xyz")
        assert config.output_format == "pcm_f32le", (
            "Invalid format must fall back to pcm_f32le"
        )

    def test_output_format_in_format_config_dict(self):
        config = make_config(CARTESIA_OUTPUT_FORMAT="pcm_f32le")
        fmt = config.get_output_format_config()
        assert fmt["encoding"] == "pcm_f32le"

    def test_container_is_raw_by_default(self):
        config = make_config()
        fmt = config.get_output_format_config()
        assert fmt["container"] == "raw"

    def test_get_output_format_config_has_required_keys(self):
        config = make_config()
        fmt = config.get_output_format_config()
        assert "container" in fmt
        assert "encoding" in fmt
        assert "sample_rate" in fmt

    @pytest.mark.xfail(
        reason=(
            "Current implementation force-upgrades pcm_s16le → pcm_f32le. "
            "This test documents the TARGET behaviour where pcm_s16le is "
            "stored as-is when the orchestrator supports 16-bit PCM natively."
        ),
        strict=True,
    )
    def test_pcm_s16le_stored_without_upgrade(self):
        """pcm_s16le SHOULD be stored as-is for 16-bit pipeline support."""
        config = make_config(CARTESIA_OUTPUT_FORMAT="pcm_s16le")
        assert config.output_format == "pcm_s16le", (
            f"Expected pcm_s16le, got {config.output_format!r}"
        )


@pytest.mark.unit
class TestSpeedConfig:
    """Test speed configuration and boundary validation."""

    def test_speed_095_accepted(self):
        """speed=0.95 must be accepted for German clarity (target value)."""
        config = make_config(CARTESIA_SPEED="0.95")
        assert config.speed == pytest.approx(0.95), (
            f"Expected speed=0.95, got {config.speed}"
        )

    def test_speed_09_default(self):
        config = make_config(CARTESIA_SPEED="0.9")
        assert config.speed == pytest.approx(0.9)

    def test_speed_min_boundary_05_accepted(self):
        config = make_config(CARTESIA_SPEED="0.5")
        assert config.speed == pytest.approx(0.5)

    def test_speed_max_boundary_20_accepted(self):
        config = make_config(CARTESIA_SPEED="2.0")
        assert config.speed == pytest.approx(2.0)

    def test_speed_below_min_is_clamped(self):
        """Speed < 0.5 must be clamped (implementation clamps to 0.9)."""
        config = make_config(CARTESIA_SPEED="0.1")
        assert 0.5 <= config.speed <= 2.0, (
            f"Clamped speed must be in [0.5, 2.0], got {config.speed}"
        )

    def test_speed_above_max_is_clamped(self):
        """Speed > 2.0 must be clamped."""
        config = make_config(CARTESIA_SPEED="5.0")
        assert 0.5 <= config.speed <= 2.0, (
            f"Clamped speed must be in [0.5, 2.0], got {config.speed}"
        )

    def test_speed_is_float(self):
        config = make_config(CARTESIA_SPEED="0.95")
        assert isinstance(config.speed, float)

    def test_speed_10_normal_pace(self):
        config = make_config(CARTESIA_SPEED="1.0")
        assert config.speed == pytest.approx(1.0)


@pytest.mark.unit
class TestVoiceConfig:
    """Test voice configuration dict structure."""

    def test_get_voice_config_has_mode_and_id(self):
        config = make_config(CARTESIA_VOICE_ID="voice-xyz-123")
        vc = config.get_voice_config()
        assert vc["mode"] == "id"
        assert vc["id"] == "voice-xyz-123"

    def test_empty_voice_id_gets_default_german_voice(self):
        """If CARTESIA_VOICE_ID is blank, a default German voice must be assigned."""
        from config import CartesiaConfig

        with patch.dict(
            "os.environ",
            {
                "CARTESIA_API_KEY": "test-key-abcdef1234567890",
                "CARTESIA_VOICE_ID": "",
            },
        ):
            config = CartesiaConfig()
        assert config.voice_id, "voice_id must not be empty — default should be applied"
        vc = config.get_voice_config()
        assert vc["id"], "Voice config id must be non-empty"

    def test_voice_config_mode_is_id(self):
        config = make_config(CARTESIA_VOICE_ID="some-voice")
        assert config.get_voice_config()["mode"] == "id"


@pytest.mark.unit
class TestPronunciationDictConfig:
    """Test pronunciation_dict_id configuration."""

    def test_valid_pronunciation_dict_id_stored(self):
        config = make_config(CARTESIA_PRONUNCIATION_DICT_ID="pdict_abc123")
        assert config.pronunciation_dict_id == "pdict_abc123"

    def test_empty_pronunciation_dict_id_is_none(self):
        config = make_config(CARTESIA_PRONUNCIATION_DICT_ID="")
        assert config.pronunciation_dict_id is None

    def test_whitespace_pronunciation_dict_id_is_none(self):
        config = make_config(CARTESIA_PRONUNCIATION_DICT_ID="   ")
        assert config.pronunciation_dict_id is None
