"""Tests for the i18n module — enforce en/ko symmetry and translation quality."""

from __future__ import annotations

import re

from llm_relay.i18n import MESSAGES, SUPPORTED_LANGS, get_lang, t

# ---------------------------------------------------------------------------
# Symmetry
# ---------------------------------------------------------------------------


def test_en_ko_symmetric():
    """Every EN key must exist in KO and vice versa."""
    en_keys = set(MESSAGES["en"].keys())
    ko_keys = set(MESSAGES["ko"].keys())
    missing_ko = en_keys - ko_keys
    missing_en = ko_keys - en_keys
    assert not missing_ko, f"Keys missing in ko: {sorted(missing_ko)}"
    assert not missing_en, f"Keys missing in en: {sorted(missing_en)}"


def test_supported_langs_match_messages():
    """SUPPORTED_LANGS must cover exactly the locales in MESSAGES."""
    assert set(SUPPORTED_LANGS) == set(MESSAGES.keys())


# ---------------------------------------------------------------------------
# Value quality
# ---------------------------------------------------------------------------


def test_all_values_are_strings():
    """Every value in every locale must be a str."""
    for lang in MESSAGES:
        for key, value in MESSAGES[lang].items():
            assert isinstance(value, str), f"MESSAGES[{lang!r}][{key!r}] is {type(value).__name__}, not str"


def test_no_empty_values():
    """No key should map to an empty string."""
    for lang in MESSAGES:
        for key, value in MESSAGES[lang].items():
            assert value.strip(), f"MESSAGES[{lang!r}][{key!r}] is empty"


def test_format_placeholders_consistent():
    """Format placeholders ({n}, {pct}, etc.) must be identical in en and ko."""
    ph_re = re.compile(r"\{(\w+)\}")
    for key in MESSAGES["en"]:
        en_ph = set(ph_re.findall(MESSAGES["en"][key]))
        ko_ph = set(ph_re.findall(MESSAGES["ko"][key]))
        assert en_ph == ko_ph, (
            f"Placeholder mismatch for {key!r}: en={en_ph}, ko={ko_ph}"
        )


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------


def test_key_naming_convention():
    """All keys must follow the dot-separated lowercase pattern."""
    pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    for key in MESSAGES["en"]:
        assert pattern.match(key), f"Key {key!r} does not match naming convention"


def test_minimum_key_count():
    """Guard against accidental mass deletion of keys."""
    assert len(MESSAGES["en"]) >= 20, (
        f"Expected >= 20 keys, got {len(MESSAGES['en'])}. "
        f"Did keys get accidentally removed?"
    )


# ---------------------------------------------------------------------------
# t() function
# ---------------------------------------------------------------------------


def test_t_returns_en_by_default():
    assert t("zone.safe") == "safe"


def test_t_returns_ko():
    assert t("zone.safe", "ko") == "\uc548\uc804"


def test_t_unknown_key_returns_key():
    assert t("nonexistent.key.here") == "nonexistent.key.here"


def test_t_format_kwargs():
    result = t("zone.turn.red", n=300)
    assert "300" in result


def test_t_format_multiple_kwargs():
    result = t("zone.ratio.red", pct=95, cur=950, ceil=1000)
    assert "95" in result
    assert "950" in result
    assert "1000" in result


def test_t_unknown_locale_falls_back_to_en():
    assert t("zone.safe", "fr") == "safe"


# ---------------------------------------------------------------------------
# get_lang()
# ---------------------------------------------------------------------------


def test_get_lang_returns_string():
    lang = get_lang()
    assert isinstance(lang, str)
    assert lang in SUPPORTED_LANGS
