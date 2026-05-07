# ruff: noqa: RUF001
"""Yandex Dialogs custom-intent grammars for the Music Assistant skill.

Each intent here is delivered to Yandex via `ya_dialogs_api.IntentDraft` +
`set_intents()` during skill provisioning. At runtime, when Yandex matches
a user's phrase against one of these grammars, it pre-classifies the
intent and surfaces it in `request.nlu.intents.<form_name>` — the
webhook handler reads that block first and only falls back to the
in-house regex parsers (`parse_command` / `parse_control`) when the
platform produced no match.

Design notes:

* **Conservative baseline.** This module ships a *subset* of our
  regex-covered intents. The aim is platform-side coverage of the most
  common phrasings, not 1:1 parity. Phrases that don't match here fall
  through to the regex parsers (which remain authoritative for the
  long tail).
* **`%lemma` directive** matches all morphological forms of the lemma
  (e.g. `%lemma включить` covers «включи / включите / включай /
  включить / включим»). Applied conservatively to verbs that have
  multiple commonly-used forms.
* **Grammar source is server-validated synchronously.** Bad syntax
  surfaces as `DialogsIntentValidationError` from `set_intents()`, so
  the test suite for this module asserts the fixtures load without
  raising once contributed.
* **Positive tests** double as documentation and a self-check —
  Yandex's "Протестировать" button in the dev console can run them
  individually for visual regression.

Adding a new intent: append a new entry, regenerate the skill via the
plugin's "Apply skill changes" form action, observe the moderation
cycle complete (minutes to hours for private skills), then exercise the
phrase against a live device.
"""

from __future__ import annotations

from typing import Any

from ya_dialogs_api import IntentDraft

from .dialogs_control import ParsedControl
from .dialogs_nlu import ParsedCommand

# ---------------------------------------------------------------------------
# Grammar fragments — control intents
# ---------------------------------------------------------------------------

_PAUSE_GRAMMAR = """\
root:
    %lemma пауза
    поставь на паузу
    %lemma останови музыку
    на паузу
"""

_RESUME_GRAMMAR = """\
root:
    %lemma продолжить
    %lemma возобновить
    включи снова
"""

_NEXT_GRAMMAR = """\
root:
    %lemma следующая
    %lemma следующий трек
    %lemma дальше
    %lemma переключи
"""

_PREVIOUS_GRAMMAR = """\
root:
    %lemma предыдущая
    %lemma предыдущий трек
    %lemma назад
    %lemma вернись
"""

_STOP_GRAMMAR = """\
root:
    %lemma стоп
    %lemma останови
    %lemma выключи
    выключи музыку
"""

_VOLUME_UP_GRAMMAR = """\
root:
    %lemma громче
    сделай громче
    %lemma прибавь
"""

_VOLUME_DOWN_GRAMMAR = """\
root:
    %lemma тише
    сделай тише
    %lemma убавь
"""

_SHUFFLE_ON_GRAMMAR = """\
root:
    %lemma перемешай
    включи перемешивание
    случайный порядок
    в случайном порядке
"""

_SHUFFLE_OFF_GRAMMAR = """\
root:
    выключи перемешивание
    не перемешивай
    по порядку
"""

_NOW_PLAYING_GRAMMAR = """\
root:
    что играет
    что сейчас играет
    что мы слушаем
    что за песня
    что за трек
"""


# ---------------------------------------------------------------------------
# Grammar fragments — play intents
# ---------------------------------------------------------------------------

_MY_WAVE_GRAMMAR = """\
root:
    %lemma включи мою волну
    %lemma включи моё радио
    %lemma поставь мою волну
    моя волна
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_grammar() -> list[IntentDraft]:
    """Return the full list of custom intents to declare on the skill."""
    return [
        IntentDraft(
            form_name="control.pause",
            human_readable_name="Пауза",
            source_text=_PAUSE_GRAMMAR,
            positive_tests="пауза\nпоставь на паузу\nостанови музыку\nна паузу",
            negative_tests="включи\nследующая",
        ),
        IntentDraft(
            form_name="control.resume",
            human_readable_name="Продолжить",
            source_text=_RESUME_GRAMMAR,
            positive_tests="продолжи\nпродолжить\nвозобнови\nвключи снова",
        ),
        IntentDraft(
            form_name="control.next",
            human_readable_name="Следующий трек",
            source_text=_NEXT_GRAMMAR,
            positive_tests="следующая\nследующий трек\nдальше\nпереключи",
        ),
        IntentDraft(
            form_name="control.previous",
            human_readable_name="Предыдущий трек",
            source_text=_PREVIOUS_GRAMMAR,
            positive_tests="предыдущая\nпредыдущий трек\nназад\nвернись",
        ),
        IntentDraft(
            form_name="control.stop",
            human_readable_name="Стоп",
            source_text=_STOP_GRAMMAR,
            positive_tests="стоп\nостанови\nвыключи\nвыключи музыку",
        ),
        IntentDraft(
            form_name="control.volume_up",
            human_readable_name="Громче",
            source_text=_VOLUME_UP_GRAMMAR,
            positive_tests="громче\nсделай громче\nприбавь",
        ),
        IntentDraft(
            form_name="control.volume_down",
            human_readable_name="Тише",
            source_text=_VOLUME_DOWN_GRAMMAR,
            positive_tests="тише\nсделай тише\nубавь",
        ),
        IntentDraft(
            form_name="control.shuffle_on",
            human_readable_name="Включить перемешивание",
            source_text=_SHUFFLE_ON_GRAMMAR,
            positive_tests="перемешай\nвключи перемешивание\nслучайный порядок",
        ),
        IntentDraft(
            form_name="control.shuffle_off",
            human_readable_name="Выключить перемешивание",
            source_text=_SHUFFLE_OFF_GRAMMAR,
            positive_tests="выключи перемешивание\nне перемешивай\nпо порядку",
        ),
        IntentDraft(
            form_name="control.now_playing",
            human_readable_name="Что играет",
            source_text=_NOW_PLAYING_GRAMMAR,
            positive_tests="что играет\nчто сейчас играет\nчто мы слушаем",
        ),
        IntentDraft(
            form_name="play.my_wave",
            human_readable_name="Моя волна",
            source_text=_MY_WAVE_GRAMMAR,
            positive_tests="включи мою волну\nпоставь мою волну\nвключи моё радио",
        ),
    ]


# ---------------------------------------------------------------------------
# Runtime: map platform-classified intents back to our internal dataclasses
# ---------------------------------------------------------------------------

# Each grammar above declares an intent under a stable ``form_name``. When
# Yandex matches a user phrase against one of them, the webhook receives
# the intent name in ``request.nlu.intents.<form_name>`` (with any slot
# values, though our control intents declare none yet). This map keeps the
# runtime mapping in lockstep with the grammars — adding a new intent
# requires touching this dict so misclassification can't sneak through
# unnoticed.
_CONTROL_INTENT_MAP: dict[str, str] = {
    "control.pause": "pause",
    "control.resume": "resume",
    "control.next": "next",
    "control.previous": "previous",
    "control.stop": "stop",
    "control.volume_up": "volume_up",
    "control.volume_down": "volume_down",
    "control.shuffle_on": "shuffle_on",
    "control.shuffle_off": "shuffle_off",
    "control.now_playing": "now_playing",
}


def parse_platform_intent(
    nlu_intents: dict[str, Any] | None,
) -> ParsedControl | ParsedCommand | None:
    """Map a ``request.nlu.intents`` block to our dispatcher's dataclass.

    Returns ``None`` when:
    * The block is missing / empty (no grammar declared, or no match).
    * The matched intent name isn't one we ship a runtime handler for.

    Returns the FIRST recognised intent in iteration order. Yandex doesn't
    guarantee a single match — when grammars overlap the platform may
    surface several — but for our conservative grammar set the overlap
    is engineered out (each phrase pattern lives in exactly one intent).
    """
    if not isinstance(nlu_intents, dict) or not nlu_intents:
        return None
    for form_name in nlu_intents:
        action = _CONTROL_INTENT_MAP.get(form_name)
        if action is not None:
            return ParsedControl(action=action)  # type: ignore[arg-type]
        if form_name == "play.my_wave":
            return ParsedCommand(kind="my_wave", query="", radio_mode=True)
    return None
