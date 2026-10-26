# ruff: noqa: RUF001
"""
Conversational answers through a Music Assistant AI engine.

Pure helpers for the Dialogs webhook: deciding which phrases are questions
for the AI engine, building the prompt, cleaning the answer for Alice, and
an in-memory per-session store holding the conversation history and a
query that is still running in the background.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .dialogs_nlu import _VERB_RE

# Yandex Dialogs rejects ``response.text`` / ``response.tts`` longer than this.
ALICE_TEXT_LIMIT = 1024
AI_HISTORY_PAIRS = 3
AI_SESSION_TTL_SEC = 300
AI_SESSION_MAX = 200

SYSTEM_PROMPT = (
    "Ты — голосовой помощник в умной колонке. Отвечай по-русски, кратко: "
    "1–3 предложения, без списков, разметки и эмодзи. "
    "Если не знаешь — так и скажи."
)

_FILLERS = r"(?:(?:а|и|ну|тогда|давай|алиса)[, ]+)*"

_QUESTION_RE = re.compile(
    rf"^{_FILLERS}(?:"
    r"кто|что\s+такое|что\s+значит|почему|зачем|как|какой|какая|какое|какие|"
    r"сколько|где|когда|откуда|расскажи|объясни|подскажи"
    r")\b",
    re.IGNORECASE,
)
_POLL_RE = re.compile(
    r"^(?:ну\s+)?(?:что(?:\s+там)?|и|ну|готово|ответ)$",
    re.IGNORECASE,
)
_EXIT_RE = re.compile(
    rf"^{_FILLERS}(?:хватит|достаточно|спасибо|всё|все|пока)(?:\s+(?:спасибо|хватит|пока))?$",
    re.IGNORECASE,
)
_LEADING_FILLERS_RE = re.compile(rf"^{_FILLERS}", re.IGNORECASE)

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_MD_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"\*\*|__|~~|[*`]")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]",
)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,!?;:…])")
_SENTENCE_END = ".!?…"


def is_ai_question(command: str) -> bool:
    """Return True if the phrase starts with a question or conversational word."""
    return bool(_QUESTION_RE.match(command.strip()))


def is_poll_phrase(command: str) -> bool:
    """Return True for a short follow-up asking for a pending answer ("ну что")."""
    return bool(_POLL_RE.match(command.strip()))


def is_exit_phrase(command: str) -> bool:
    """Return True for a goodbye ("хватит", "спасибо") ending an AI conversation."""
    return bool(_EXIT_RE.match(command.strip()))


def starts_with_play_verb(command: str) -> bool:
    """Return True if the phrase is a play request, ignoring leading fillers."""
    stripped = _LEADING_FILLERS_RE.sub("", command.strip())
    return bool(_VERB_RE.match(stripped))


def sanitize_answer(text: str, limit: int = ALICE_TEXT_LIMIT) -> str:
    """
    Turn an AI answer into plain text Alice can speak.

    :param text: Raw answer from the AI engine.
    :param limit: Maximum length of the result.
    """
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = " ".join(text.split())
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return clip_text(text, limit)


def clip_text(text: str, limit: int = ALICE_TEXT_LIMIT) -> str:
    """
    Shorten text to the limit, preferring a sentence boundary.

    :param text: Text to shorten.
    :param limit: Maximum length of the result.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentence_end = max(head.rfind(mark) for mark in _SENTENCE_END)
    if sentence_end > 0:
        return head[: sentence_end + 1]
    words = head[: limit - 1].rsplit(" ", 1)[0]
    return words.rstrip(",;:—- ") + "…"


def build_prompt(
    question: str,
    *,
    history: list[tuple[str, str]],
    now_playing: str | None,
) -> str:
    """
    Compose the single-string query sent to the AI engine.

    :param question: The user's phrase.
    :param history: Previous (question, answer) pairs of this skill session.
    :param now_playing: Track playing on the user's player, if known.
    """
    lines = [SYSTEM_PROMPT]
    if now_playing:
        lines.append(f"Сейчас играет: {now_playing}.")
    if history:
        lines.append("Предыдущий разговор:")
        for previous_question, previous_answer in history:
            lines.append(f"Пользователь: {previous_question}")
            lines.append(f"Помощник: {previous_answer}")
    lines.append(f"Вопрос пользователя: {question}")
    return "\n".join(lines)


@dataclass
class AISession:
    """Conversation state of one skill session."""

    history: list[tuple[str, str]] = field(default_factory=list)
    task: asyncio.Future[str | None] | None = None
    question: str = ""
    polls: int = 0
    last_ai_turn: int | None = None
    touched: float = field(default_factory=time.monotonic)

    def start(self, question: str, task: asyncio.Future[str | None]) -> None:
        """Track a newly started query, dropping any previous one."""
        self.cancel_pending()
        self.question = question
        self.task = task
        self.polls = 0

    def cancel_pending(self) -> None:
        """Cancel the running query, if any."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def take_answer(self) -> str | None:
        """Return the finished query's answer and stop tracking it."""
        task, self.task = self.task, None
        if task is None or not task.done() or task.cancelled():
            return None
        return task.result()

    def is_follow_up(self, message_id: object) -> bool:
        """
        Return True if this turn directly follows an AI turn of the session.

        :param message_id: Yandex ``session.message_id`` of the current turn; when
            absent, any earlier answer keeps the conversation going.
        """
        if not isinstance(message_id, int) or self.last_ai_turn is None:
            return bool(self.history)
        return bool(self.history) and message_id == self.last_ai_turn + 1

    def remember(self, question: str, answer: str) -> None:
        """Append a question/answer pair, keeping only the most recent ones."""
        self.history.append((question, answer))
        del self.history[:-AI_HISTORY_PAIRS]


class AISessions:
    """In-memory AI conversation store keyed by Yandex session id (LRU + TTL)."""

    def __init__(self) -> None:
        """Start with an empty store."""
        self._items: OrderedDict[str, AISession] = OrderedDict()

    def get(self, session_id: str) -> AISession | None:
        """Return the live conversation for a session, or None."""
        item = self._items.get(session_id)
        if item is None:
            return None
        if time.monotonic() - item.touched > AI_SESSION_TTL_SEC:
            self.drop(session_id)
            return None
        item.touched = time.monotonic()
        self._items.move_to_end(session_id)
        return item

    def get_or_create(self, session_id: str) -> AISession:
        """Return the conversation for a session, starting one if needed."""
        item = self.get(session_id)
        if item is None:
            item = AISession()
            self._items[session_id] = item
            while len(self._items) > AI_SESSION_MAX:
                self.drop(next(iter(self._items)))
        return item

    def drop(self, session_id: str) -> None:
        """Cancel the session's running query and forget its conversation."""
        item = self._items.pop(session_id, None)
        if item is not None:
            item.cancel_pending()

    def clear(self) -> None:
        """Cancel every running query and forget all conversations."""
        for session_id in list(self._items):
            self.drop(session_id)
