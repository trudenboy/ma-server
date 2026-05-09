# ruff: noqa: D102, PT018, RUF001
# mypy: disable-error-code="union-attr"
"""Unit tests for ``provider.dialogs_grammar`` — platform intent mapping.

Covers:

* ``parse_platform_intent`` slot extraction for the v1.4.0 slot-bearing
  intents (volume set / increase / decrease, seek forward / back).
* The no-slot ``_CONTROL_INTENT_MAP`` registry — every entry yields the
  expected ``ParsedControl(action=...)`` and the registry is in lockstep
  with the literal ``ControlAction`` type.
* Edge cases the runtime is silent about: missing slots, malformed
  payloads, multiple intents in one block, boolean-typed values.
"""

from __future__ import annotations

import importlib.resources
from typing import Any

import pytest

from music_assistant.providers.yandex_alice.dialogs_control import ControlAction
from music_assistant.providers.yandex_alice.dialogs_grammar import (
    _CONTROL_INTENT_MAP,
    build_entities,
    build_grammar,
    extract_trailing_player_hint,
    parse_platform_intent,
)
from music_assistant.providers.yandex_alice.dialogs_nlu import ParsedCommand


def _intent(slots: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a single ``request.nlu.intents.<form>`` payload for tests."""
    return {"slots": slots} if slots is not None else {}


class TestNoSlotControlMap:
    """Bare control intents map directly to a ParsedControl(action)."""

    def test_every_no_slot_intent_round_trips(self) -> None:
        """Each ``_CONTROL_INTENT_MAP`` entry yields the expected action."""
        for form_name, expected_action in _CONTROL_INTENT_MAP.items():
            result = parse_platform_intent({form_name: {}})
            assert result is not None
            assert result.action == expected_action, form_name

    def test_map_actions_are_valid_control_action_literals(self) -> None:
        """Guard against typos: every mapped action must be a known literal."""
        valid_actions = set(ControlAction.__args__)  # type: ignore[attr-defined]
        for form_name, action in _CONTROL_INTENT_MAP.items():
            assert action in valid_actions, f"{form_name} → {action!r} not a ControlAction"

    def test_map_covers_v140_baseline(self) -> None:
        """Every v1.4.0 baseline form_name is present in the map.

        Pinned by membership rather than a count so adding a new no-slot
        intent in a follow-up only requires extending the map and the
        grammar — no edit here.
        """
        baseline = {
            "control.pause",
            "control.resume",
            "control.next",
            "control.previous",
            "control.stop",
            "control.volume_up",
            "control.volume_down",
            "control.shuffle_on",
            "control.shuffle_off",
            "control.now_playing",
            "control.mute",
            "control.unmute",
            "control.seek_start",
            "control.repeat_one",
            "control.repeat_all",
            "control.repeat_off",
            "control.list_players",
            "control.forget_player",
        }
        assert baseline <= set(_CONTROL_INTENT_MAP)


class TestVolumeSetSlot:
    """``control.volume_set`` carries a ``level: YANDEX.NUMBER`` slot."""

    def test_int_slot_value_passes_through(self) -> None:
        result = parse_platform_intent({"control.volume_set": _intent({"level": {"value": 50}})})
        assert result is not None and result.action == "volume_set" and result.value == 50

    def test_float_slot_value_coerced_to_int(self) -> None:
        """Yandex sometimes sends fractions — we round-down to int."""
        result = parse_platform_intent({"control.volume_set": _intent({"level": {"value": 50.7}})})
        assert result is not None and result.value == 50

    def test_value_above_100_is_clamped(self) -> None:
        result = parse_platform_intent({"control.volume_set": _intent({"level": {"value": 150}})})
        assert result is not None and result.value == 100

    def test_negative_value_is_clamped_to_zero(self) -> None:
        result = parse_platform_intent({"control.volume_set": _intent({"level": {"value": -5}})})
        assert result is not None and result.value == 0

    def test_missing_slot_falls_through(self) -> None:
        """No level slot → None, regex fallback gets a chance."""
        assert parse_platform_intent({"control.volume_set": _intent({})}) is None

    def test_bool_value_is_rejected(self) -> None:
        """``isinstance(True, int)`` is True in Python — guard against it."""
        assert (
            parse_platform_intent({"control.volume_set": _intent({"level": {"value": True}})})
            is None
        )


class TestVolumeRelativeSlot:
    """Increase / decrease intents map to action='volume_relative' with sign."""

    def test_increase_with_positive_delta(self) -> None:
        result = parse_platform_intent(
            {"control.volume_increase": _intent({"delta": {"value": 20}})}
        )
        assert result is not None
        assert result.action == "volume_relative"
        assert result.value == 20

    def test_decrease_with_positive_delta_yields_negative_value(self) -> None:
        result = parse_platform_intent(
            {"control.volume_decrease": _intent({"delta": {"value": 15}})}
        )
        assert result is not None and result.value == -15

    def test_increase_without_slot_uses_default_magnitude(self) -> None:
        """Bare «прибавь» (no number) — magnitude defaults to 10."""
        result = parse_platform_intent({"control.volume_increase": _intent({})})
        assert result is not None and result.action == "volume_relative" and result.value == 10

    def test_decrease_without_slot_uses_default_magnitude(self) -> None:
        result = parse_platform_intent({"control.volume_decrease": _intent({})})
        assert result is not None and result.value == -10

    def test_negative_delta_is_normalised(self) -> None:
        """delta=-5 with form_name=increase is treated as |delta|=5."""
        result = parse_platform_intent(
            {"control.volume_increase": _intent({"delta": {"value": -5}})}
        )
        assert result is not None and result.value == 5

    def test_huge_delta_is_clamped(self) -> None:
        result = parse_platform_intent(
            {"control.volume_increase": _intent({"delta": {"value": 999}})}
        )
        assert result is not None and result.value == 100


class TestSeekSlots:
    """seek_forward / seek_back carry amount + time_unit slots."""

    def test_forward_seconds(self) -> None:
        result = parse_platform_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 30}, "unit": {"value": "seconds"}}
                )
            }
        )
        assert result is not None and result.action == "seek_forward" and result.value == 30

    def test_forward_minutes_converts_to_seconds(self) -> None:
        result = parse_platform_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 2}, "unit": {"value": "minutes"}}
                )
            }
        )
        assert result is not None and result.value == 120

    def test_back_seconds(self) -> None:
        result = parse_platform_intent(
            {"control.seek_back": _intent({"amount": {"value": 15}, "unit": {"value": "seconds"}})}
        )
        assert result is not None and result.action == "seek_back" and result.value == 15

    def test_back_minutes_converts(self) -> None:
        result = parse_platform_intent(
            {"control.seek_back": _intent({"amount": {"value": 1}, "unit": {"value": "minutes"}})}
        )
        assert result is not None and result.value == 60

    def test_unit_missing_defaults_to_seconds(self) -> None:
        """If the time_unit slot is absent, fall back to seconds."""
        result = parse_platform_intent({"control.seek_forward": _intent({"amount": {"value": 45}})})
        assert result is not None and result.value == 45

    def test_zero_amount_falls_through(self) -> None:
        """amount=0 is meaningless; fall back to regex (None)."""
        assert (
            parse_platform_intent({"control.seek_forward": _intent({"amount": {"value": 0}})})
            is None
        )

    def test_negative_amount_falls_through(self) -> None:
        assert (
            parse_platform_intent({"control.seek_back": _intent({"amount": {"value": -5}})}) is None
        )

    def test_seconds_exceeding_cap_falls_through(self) -> None:
        """Out-of-range seek (>24h equivalent) → None, no skip dispatched."""
        result = parse_platform_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 100_000}, "unit": {"value": "seconds"}}
                )
            }
        )
        assert result is None

    def test_minutes_exceeding_cap_falls_through(self) -> None:
        """Same cap applies after minutes → seconds conversion."""
        result = parse_platform_intent(
            {
                "control.seek_forward": _intent(
                    {"amount": {"value": 2000}, "unit": {"value": "minutes"}}
                )
            }
        )
        assert result is None

    def test_missing_amount_falls_through(self) -> None:
        assert parse_platform_intent({"control.seek_forward": _intent({})}) is None


class TestPlayIntents:
    """play.my_wave is the only platform-side play intent we ship."""

    def test_my_wave_returns_parsed_command(self) -> None:
        result = parse_platform_intent({"play.my_wave": {}})
        assert isinstance(result, ParsedCommand)
        assert result.kind == "my_wave"
        assert result.radio_mode is True
        assert result.query == ""


class TestEdgeCases:
    """parse_platform_intent must be robust to noisy / malformed payloads."""

    def test_none_returns_none(self) -> None:
        assert parse_platform_intent(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert parse_platform_intent({}) is None

    def test_unknown_intent_returns_none(self) -> None:
        assert parse_platform_intent({"unknown.intent": {}}) is None

    def test_non_dict_input_returns_none(self) -> None:
        assert parse_platform_intent("not a dict") is None  # type: ignore[arg-type]

    def test_first_recognised_intent_wins_when_multiple_present(self) -> None:
        """Yandex may surface several intents; we pick the first known one."""
        result = parse_platform_intent(
            {
                "unknown.intent": {},
                "control.pause": {},
                "control.next": {},
            }
        )
        assert result is not None and result.action in ("pause", "next")


class TestExtractTrailingPlayerHint:
    """Recover trailing "на <player>" suffix attached to intent payloads.

    Yandex's static intents can't enumerate the per-user player list,
    so the hint comes from raw command text. The function rejects
    "на" tokens that introduce a numeric slot value or an action-content
    word like "паузу".
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Positive — trailing player hint.
            ("пауза на кухне", "кухне"),
            ("следующая на спальне", "спальне"),
            ("приглуши на гостиной", "гостиной"),
            # Multiple "на" — only the rightmost suffix is taken.
            ("поставь на паузу на кухне", "кухне"),
            ("громкость на 50 на кухне", "кухне"),
            ("перемотай вперёд на 30 секунд на кухне", "кухне"),
            # Multi-word hint stays intact.
            ("пауза на колонке у окна", "колонке у окна"),
            # Capitalisation is normalised.
            ("Пауза На Кухне", "кухне"),
        ],
    )
    def test_returns_hint_when_trailing_suffix_is_a_player(
        self,
        text: str,
        expected: str,
    ) -> None:
        assert extract_trailing_player_hint(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            # No "на" boundary at all.
            "",
            "пауза",
            "следующий трек",
            # Trailing "на" leads into a numeric slot, not a player name.
            "громкость на 50",
            "прибавь на 20",
            "убавь на 25 процентов",
            "перемотай вперёд на 30",
            "перемотай вперёд на 30 секунд",
            "перемотай назад на 1 минуту",
            # Trailing "на" leads into a unit word with no number — still
            # not a player name.
            "поставь на паузу",
        ],
    )
    def test_returns_none_when_suffix_is_a_slot_value(self, text: str) -> None:
        assert extract_trailing_player_hint(text) is None


class TestBuildersConsistency:
    """build_grammar / build_entities — declarative state must round-trip."""

    def test_skill_toml_resource_is_packaged(self) -> None:
        """``provider/data/skill.toml`` must be reachable via importlib.resources.

        Guard against the packaging foot-gun where ``provider/data/``
        lacks an ``__init__.py`` and ``setuptools.find_packages``
        silently drops the directory from the built wheel — the
        manifest then can't be loaded after a pip install.
        """
        ref = importlib.resources.files("music_assistant.providers.yandex_alice.data").joinpath(
            "skill.toml"
        )
        text = ref.read_text(encoding="utf-8")
        assert "schema_version" in text
        assert "control.pause" in text

    def test_build_entities_returns_time_unit(self) -> None:
        entities = build_entities()
        assert len(entities) == 1
        assert entities[0].name == "time_unit"
        # Must declare both seconds and minutes — required by seek slots.
        value_names = [v.name for v in entities[0].values]
        assert "seconds" in value_names and "minutes" in value_names

    def test_build_grammar_covers_v140_baseline(self) -> None:
        """v1.4.0 baseline: 18 no-slot control + 5 slot-bearing + 1 play.

        Pinned by membership so extending the grammar in a follow-up
        doesn't require touching this assertion.
        """
        grammar = build_grammar()
        form_names = {i.form_name for i in grammar}
        baseline = {
            "control.pause",
            "control.resume",
            "control.next",
            "control.previous",
            "control.stop",
            "control.volume_up",
            "control.volume_down",
            "control.shuffle_on",
            "control.shuffle_off",
            "control.now_playing",
            "control.mute",
            "control.unmute",
            "control.seek_start",
            "control.repeat_one",
            "control.repeat_all",
            "control.repeat_off",
            "control.list_players",
            "control.forget_player",
            "control.volume_set",
            "control.volume_increase",
            "control.volume_decrease",
            "control.seek_forward",
            "control.seek_back",
            "play.my_wave",
        }
        assert baseline <= form_names

    def test_build_grammar_form_names_unique(self) -> None:
        grammar = build_grammar()
        form_names = [i.form_name for i in grammar]
        assert len(form_names) == len(set(form_names))

    def test_every_no_slot_form_in_map_has_intent(self) -> None:
        """Every ``_CONTROL_INTENT_MAP`` key must appear in build_grammar()."""
        grammar_form_names = {i.form_name for i in build_grammar()}
        for form_name in _CONTROL_INTENT_MAP:
            assert form_name in grammar_form_names, form_name

    def test_slot_bearing_intents_declare_inline_slots_block(self) -> None:
        """Each slot-bearing form_name must include a ``slots:`` block in source_text.

        Since v1.5.0 the manifest carries the slots inline in the
        Granet ``sourceText`` (so it round-trips with the dev-console
        editor); ``IntentDraft.slots`` is left empty and ya-dialogs-api
        recognises the existing block.
        """
        slot_forms = {
            "control.volume_set",
            "control.volume_increase",
            "control.volume_decrease",
            "control.seek_forward",
            "control.seek_back",
        }
        by_form = {i.form_name: i for i in build_grammar()}
        for form_name in slot_forms:
            intent = by_form[form_name]
            assert "slots:" in intent.source_text, f"{form_name} missing slots: block"

    @pytest.mark.parametrize(
        ("form_name", "expected_slot_names"),
        [
            ("control.volume_set", {"level"}),
            ("control.volume_increase", {"delta"}),
            ("control.volume_decrease", {"delta"}),
            ("control.seek_forward", {"amount", "unit"}),
            ("control.seek_back", {"amount", "unit"}),
        ],
    )
    def test_slot_names_present_in_source_text(
        self, form_name: str, expected_slot_names: set[str]
    ) -> None:
        """Each expected slot name appears as a key in the inline slots: block."""
        by_form = {i.form_name: i for i in build_grammar()}
        source = by_form[form_name].source_text
        for name in expected_slot_names:
            # Slot names are introduced as ``    <name>:`` inside the slots
            # block — searching for the keyed line is unambiguous because
            # entity-value names share the same shape but live elsewhere.
            assert f"    {name}:" in source, f"{form_name} missing slot {name!r}"
