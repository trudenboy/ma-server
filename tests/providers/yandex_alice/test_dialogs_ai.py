# ruff: noqa: RUF001
"""Tests for AI-engine answers to conversational questions in the Dialogs webhook."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.yandex_alice import dialogs as dialogs_module
from music_assistant.providers.yandex_alice.dialogs import DialogsWebhookHandler
from music_assistant.providers.yandex_alice.dialogs_ai import (
    ALICE_TEXT_LIMIT,
    build_prompt,
    is_ai_question,
    is_exit_phrase,
    is_poll_phrase,
    sanitize_answer,
    starts_with_play_verb,
)

from .test_dialogs import MockPlayer, _build_request, _make_mass, _response_body

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "кто такой фредди меркьюри",
        "что такое техно",
        "почему небо голубое",
        "как зовут солиста",
        "сколько лет этой песне",
        "расскажи про эту группу",
        "объясни что такое синкопа",
        "а почему так",
    ],
)
def test_is_ai_question_matches_conversational_phrases(command: str) -> None:
    """Phrases starting with a question or conversational word go to the AI engine."""
    assert is_ai_question(command)


@pytest.mark.parametrize(
    "command",
    [
        "включи как на войне",
        "поставь кто я без тебя",
        "metallica",
        "рок на кухне",
        "какао",
        "",
    ],
)
def test_is_ai_question_ignores_music_requests(command: str) -> None:
    """Play commands and bare search queries keep going to the music search."""
    assert not is_ai_question(command)


@pytest.mark.parametrize("command", ["ну что", "и", "ну", "готово", "что там", "ну что там"])
def test_is_poll_phrase_matches_short_follow_ups(command: str) -> None:
    """Short follow-ups ask for a pending answer."""
    assert is_poll_phrase(command)


@pytest.mark.parametrize("command", ["ну что такое техно", "включи рок", "пауза", "дальше"])
def test_is_poll_phrase_ignores_real_requests(command: str) -> None:
    """Anything longer than a short follow-up is a new request."""
    assert not is_poll_phrase(command)


@pytest.mark.parametrize(
    "command", ["хватит", "спасибо", "всё", "все спасибо", "пока", "ну хватит"]
)
def test_is_exit_phrase_matches_goodbyes(command: str) -> None:
    """Goodbye phrases end an AI conversation."""
    assert is_exit_phrase(command)


@pytest.mark.parametrize("command", ["спасибо а кто это", "всё равно расскажи"])
def test_is_exit_phrase_ignores_longer_phrases(command: str) -> None:
    """A goodbye word inside a longer request is not an exit."""
    assert not is_exit_phrase(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("включи их последний альбом", True),
        ("а поставь эту песню", True),
        ("ну давай включи", True),
        ("а сколько ему лет", False),
        ("как включить блютус", False),
    ],
)
def test_starts_with_play_verb(command: str, expected: bool) -> None:
    """A play verb at the start of a follow-up (after fillers) routes to music."""
    assert starts_with_play_verb(command) is expected


def test_sanitize_answer_strips_markdown_and_emoji() -> None:
    """Markdown markup, list markers, links and emoji never reach Alice."""
    raw = "## Ответ\n**Queen** — британская группа 🎸.\n- Основана в 1970.\n[Вики](https://x.y)"
    assert sanitize_answer(raw) == "Ответ Queen — британская группа. Основана в 1970. Вики"


def test_sanitize_answer_cuts_at_sentence_boundary() -> None:
    """Long answers are cut at the last full sentence that fits the limit."""
    sentence = "Это одно довольно длинное предложение про музыку."
    raw = " ".join([sentence] * 40)
    result = sanitize_answer(raw)
    assert len(result) <= ALICE_TEXT_LIMIT
    assert result.endswith(".")
    assert result.startswith(sentence)


def test_sanitize_answer_cuts_at_word_without_sentence_end() -> None:
    """A single over-long sentence is cut at a word boundary with an ellipsis."""
    raw = "слово " * 400
    result = sanitize_answer(raw, limit=50)
    assert len(result) <= 50
    assert result.endswith("…")
    assert "  " not in result


def test_build_prompt_includes_context_and_history() -> None:
    """The prompt carries now-playing info, previous turns and the question."""
    prompt = build_prompt(
        "сколько ему лет",
        history=[("кто поёт", "Это Фредди Меркьюри.")],
        now_playing="Queen - Bohemian Rhapsody",
    )
    assert "Queen - Bohemian Rhapsody" in prompt
    assert "кто поёт" in prompt
    assert "Это Фредди Меркьюри." in prompt
    assert prompt.rstrip().endswith("сколько ему лет")


def test_build_prompt_omits_now_playing_when_unknown() -> None:
    """Without a resolvable player the prompt has no now-playing line."""
    prompt = build_prompt("почему небо голубое", history=[], now_playing=None)
    assert "Сейчас играет" not in prompt


# ---------------------------------------------------------------------------
# Webhook integration
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    """Stand-in for MA's AIEngine: id, display name and the owning provider."""

    id: str
    name: str
    provider: Any


class _FakeAIProvider:
    """Records prompts and answers them, optionally after a delay or with an error."""

    def __init__(
        self,
        answer: str = "Это Фредди Меркьюри.",
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        """Configure the canned behaviour."""
        self.answer = answer
        self.delay = delay
        self.error = error
        self.prompts: list[str] = []
        self.engine_ids: list[str | None] = []
        self.cancelled = 0

    async def ai_query(self, query: str, engine_id: str | None = None) -> str:
        """Answer the query the way an AI_QUERY plugin would."""
        self.prompts.append(query)
        self.engine_ids.append(engine_id)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error is not None:
            raise self.error
        return self.answer


ENGINE_UID = "openai--gpt"


@pytest.fixture
def ai_provider(monkeypatch: pytest.MonkeyPatch) -> _FakeAIProvider:
    """Patch engine resolution so the configured uid maps to a fake engine."""
    provider = _FakeAIProvider()

    async def _resolve(_mass: object, selected: str | None) -> _FakeEngine | None:
        return (
            _FakeEngine(id="gpt", name="GPT", provider=provider) if selected == ENGINE_UID else None
        )

    monkeypatch.setattr(dialogs_module, "resolve_ai_engine", _resolve)
    return provider


def _handler(mass: MagicMock, *, ai_engine_uid: str | None = ENGINE_UID) -> DialogsWebhookHandler:
    return DialogsWebhookHandler(
        mass,
        skill_id="skill-uuid-1",
        webhook_secret="topsecret",
        ai_engine_uid=ai_engine_uid,
    )


def _body(
    command: str,
    *,
    session_id: str = "s1",
    message_id: int | None = None,
    intents: dict[str, Any] | None = None,
    **state: Any,
) -> dict[str, Any]:
    session: dict[str, Any] = {"skill_id": "skill-uuid-1", "session_id": session_id, "new": False}
    if message_id is not None:
        session["message_id"] = message_id
    request: dict[str, Any] = {"command": command}
    if intents is not None:
        request["nlu"] = {"intents": intents}
    body: dict[str, Any] = {"session": session, "request": request}
    if state:
        body["state"] = state
    return body


async def _say(handler: DialogsWebhookHandler, command: str, **kwargs: Any) -> dict[str, Any]:
    resp = await handler._handle_webhook(_build_request(_body(command, **kwargs)))
    assert resp.status == 200
    return _response_body(resp)


@pytest.mark.asyncio
class TestAIAnswers:
    """End-to-end webhook behaviour of the AI answer path."""

    async def test_disabled_routes_question_to_music_search(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """Without a configured engine a question still goes to the music search."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass, ai_engine_uid=None)
        await _say(handler, "почему небо голубое")
        mass.music.search.assert_awaited()
        assert ai_provider.prompts == []

    async def test_missing_engine_routes_question_to_music_search(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """A configured engine that no longer exists falls back to the music search."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass, ai_engine_uid="gone--engine")
        await _say(handler, "почему небо голубое")
        mass.music.search.assert_awaited()
        assert ai_provider.prompts == []

    async def test_question_is_answered_by_engine(self, ai_provider: _FakeAIProvider) -> None:
        """A question gets the engine's answer, keeps the session open, skips search."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "кто поёт эту песню")
        assert body["response"]["text"] == "Это Фредди Меркьюри."
        assert body["response"]["end_session"] is False
        assert ai_provider.engine_ids == ["gpt"]
        mass.music.search.assert_not_awaited()

    async def test_control_intent_wins_over_question_word(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """A grammar-matched control phrase starting with a question word stays control."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "какая песня сейчас играет", intents={"control.now_playing": {}})
        assert ai_provider.prompts == []
        assert "играет" in body["response"]["text"]

    async def test_control_intent_while_pending_is_not_a_poll(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """«Дальше» recognised as next-track skips the track and drops the pending query."""
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.01)
        ai_provider.delay = 10
        mass = _make_mass([MockPlayer()])
        mass.player_queues.next = AsyncMock()
        handler = _handler(mass)
        await _say(handler, "почему небо голубое")
        await _say(handler, "дальше", intents={"control.next": {}})
        await asyncio.sleep(0)
        mass.player_queues.next.assert_awaited_once()
        assert ai_provider.cancelled == 1

    async def test_question_during_pending_command_keeps_pending_flow(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """While the skill waits for a player choice, a question is not sent to the AI."""
        mass = _make_mass([MockPlayer(player_id="p1"), MockPlayer(player_id="p2", name="Зал")])
        handler = _handler(mass)
        pending = {"kind": "search", "query": "queen", "radio_mode": False}
        await _say(handler, "как насчёт кухни", session={"pending_command": pending})
        assert ai_provider.prompts == []

    async def test_question_while_awaiting_query_keeps_music_flow(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """After «Что включить?» a question-like answer is a music query, not an AI question."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "как на войне", session={"awaiting_query": True})
        assert ai_provider.prompts == []

    async def test_missing_session_id_skips_ai(self, ai_provider: _FakeAIProvider) -> None:
        """Requests without a session id never share one anonymous AI conversation."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "почему небо голубое", session_id="")
        assert ai_provider.prompts == []

    async def test_follow_up_uses_history(self, ai_provider: _FakeAIProvider) -> None:
        """Inside an AI conversation a follow-up without question word reaches the engine."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню")
        ai_provider.answer = "Ему было бы 79 лет."
        body = await _say(handler, "а ему сейчас лет")
        assert body["response"]["text"] == "Ему было бы 79 лет."
        assert "кто поёт эту песню" in ai_provider.prompts[-1]
        assert "Это Фредди Меркьюри." in ai_provider.prompts[-1]

    async def test_history_is_capped_at_three_pairs(self, ai_provider: _FakeAIProvider) -> None:
        """Only the last three question/answer pairs are sent along."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        for idx in range(5):
            await _say(handler, f"расскажи факт номер {idx}")
        await _say(handler, "почему так")
        last = ai_provider.prompts[-1]
        assert "факт номер 0" not in last
        assert "факт номер 1" not in last
        assert "факт номер 4" in last

    async def test_history_is_per_session(self, ai_provider: _FakeAIProvider) -> None:
        """A new skill session starts without previous turns."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню", session_id="s1")
        await _say(handler, "почему небо голубое", session_id="s2")
        assert "кто поёт эту песню" not in ai_provider.prompts[-1]

    async def test_play_verb_in_conversation_starts_music(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """A play command after an AI answer goes to the music search."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer()], search_track=track)
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню")
        await _say(handler, "а включи их лучшие песни")
        assert len(ai_provider.prompts) == 1
        mass.music.search.assert_awaited()

    async def test_goodbye_in_conversation_ends_session(self, ai_provider: _FakeAIProvider) -> None:
        """«Хватит» after an AI answer closes the session without asking the engine."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню")
        body = await _say(handler, "спасибо")
        assert body["response"]["end_session"] is True
        assert len(ai_provider.prompts) == 1
        mass.music.search.assert_not_awaited()

    async def test_conversation_ends_after_a_non_ai_turn(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """Only the turn right after an AI answer is a follow-up; later bare queries search."""
        mass = _make_mass([MockPlayer()])
        mass.player_queues.pause = AsyncMock()
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню", message_id=1)
        await _say(handler, "пауза", message_id=2, intents={"control.pause": {}})
        await _say(handler, "queen", message_id=3)
        assert len(ai_provider.prompts) == 1
        mass.music.search.assert_awaited()

    async def test_follow_up_right_after_answer_uses_message_ids(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """With message ids, the next turn after an answer is still a follow-up."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню", message_id=1)
        await _say(handler, "а ему сейчас лет", message_id=2)
        assert len(ai_provider.prompts) == 2

    async def test_bare_query_outside_conversation_starts_music(
        self, ai_provider: _FakeAIProvider
    ) -> None:
        """Without a prior AI answer a non-question phrase keeps today's search."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "metallica")
        assert ai_provider.prompts == []
        mass.music.search.assert_awaited()

    async def test_now_playing_reaches_prompt(self, ai_provider: _FakeAIProvider) -> None:
        """The track on the single playing player is part of the prompt."""
        player = MockPlayer()
        player.playback_state = PlaybackState.PLAYING  # type: ignore[attr-defined]
        mass = _make_mass([player])
        queue = MagicMock()
        queue.current_item.name = "Queen - Bohemian Rhapsody"
        mass.player_queues.get.return_value = queue
        handler = _handler(mass)
        await _say(handler, "кто поёт эту песню")
        assert "Queen - Bohemian Rhapsody" in ai_provider.prompts[0]

    async def test_engine_error_returns_fallback_text(self, ai_provider: _FakeAIProvider) -> None:
        """An engine failure becomes a polite reply, never HTTP 500 or silence."""
        ai_provider.error = RuntimeError("boom")
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "почему небо голубое")
        assert body["response"]["text"] == "Не получилось получить ответ, попробуй спросить иначе."
        assert body["response"]["end_session"] is False

    async def test_answer_is_sanitized(self, ai_provider: _FakeAIProvider) -> None:
        """Markdown and emoji from the engine are stripped from text and tts."""
        ai_provider.answer = "**Queen** 🎸 — группа."
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "кто такие queen")
        assert body["response"]["text"] == "Queen — группа."
        assert "*" not in body["response"]["tts"]

    async def test_slow_answer_is_delivered_on_poll(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A late answer yields 'думаю' first, then the answer on 'ну что'."""
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.01)
        ai_provider.delay = 0.05
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        first = await _say(handler, "почему небо голубое")
        assert "думаю" in first["response"]["text"].lower()
        assert first["response"]["end_session"] is False
        await asyncio.sleep(0.1)
        second = await _say(handler, "ну что")
        assert second["response"]["text"] == "Это Фредди Меркьюри."

    async def test_poll_while_running_says_still_thinking_twice(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Polls before the answer is ready get 'ещё думаю' at most twice."""
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.01)
        ai_provider.delay = 10
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "почему небо голубое")
        assert "ещё думаю" in (await _say(handler, "ну что"))["response"]["text"].lower()
        assert "ещё думаю" in (await _say(handler, "и"))["response"]["text"].lower()
        third = await _say(handler, "готово")
        assert third["response"]["text"] == "Не получилось получить ответ, попробуй спросить иначе."

    async def test_query_timeout_returns_fallback_text(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An engine slower than the overall AI timeout ends with the fallback reply."""
        monkeypatch.setattr(dialogs_module, "AI_QUERY_TIMEOUT", 0.02)
        ai_provider.delay = 10
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "почему небо голубое")
        assert body["response"]["text"] == "Не получилось получить ответ, попробуй спросить иначе."
        assert ai_provider.cancelled == 1

    async def test_slow_engine_lookup_stays_within_budget(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow engine lookup cannot push the reply past Alice's response window."""

        async def _slow_resolve(_mass: object, _selected: str | None) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(dialogs_module, "resolve_ai_engine", _slow_resolve)
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.05)
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        started = asyncio.get_running_loop().time()
        body = await _say(handler, "почему небо голубое")
        assert asyncio.get_running_loop().time() - started < 1
        assert body["response"]["text"] == "Не получилось получить ответ, попробуй спросить иначе."

    async def test_non_string_answer_is_handled(self, ai_provider: _FakeAIProvider) -> None:
        """A plugin returning a non-string answer yields the fallback reply."""
        ai_provider.answer = None  # type: ignore[assignment]
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        body = await _say(handler, "почему небо голубое")
        assert body["response"]["text"] == "Не получилось получить ответ, попробуй спросить иначе."

    async def test_unregister_cancels_pending_queries(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unloading the provider cancels AI queries still running in the background."""
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.01)
        ai_provider.delay = 10
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        await _say(handler, "почему небо голубое")
        handler.unregister_routes()
        await asyncio.sleep(0)
        assert ai_provider.cancelled == 1

    async def test_other_phrase_cancels_pending_answer(
        self, ai_provider: _FakeAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new request while the answer is pending drops the pending query."""
        monkeypatch.setattr(dialogs_module, "AI_INLINE_TIMEOUT", 0.01)
        ai_provider.delay = 10
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer()], search_track=track)
        handler = _handler(mass)
        await _say(handler, "почему небо голубое")
        await _say(handler, "включи metallica")
        mass.music.search.assert_awaited()
        await asyncio.sleep(0)
        assert ai_provider.cancelled == 1
        body = await _say(handler, "ну что")
        assert "думаю" not in body["response"]["text"].lower()

    async def test_texts_logged_only_at_debug(
        self, ai_provider: _FakeAIProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO logs name the engine and timing but never the user's words."""
        mass = _make_mass([MockPlayer()])
        handler = _handler(mass)
        with caplog.at_level(logging.INFO, logger=dialogs_module.__name__):
            await _say(handler, "почему небо голубое")
        info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert info
        assert all("небо" not in msg and "Меркьюри" not in msg for msg in info)
