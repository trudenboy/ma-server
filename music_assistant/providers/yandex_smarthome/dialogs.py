"""HTTP handler for the Yandex Dialogs custom-skill webhook (experimental).

Registers a single exact route on the MA webserver — the secret is
**baked into the path string** at registration time, not a route
template variable:

  POST /api/yandex_dialogs/webhook/<secret-as-literal-segment>

Therefore ``request.match_info`` is empty in production; the handler
parses the secret from ``request.path`` (last segment) for the
constant-time compare. Tests that pass an explicit ``match_info`` cover
the alternative branch.

Yandex Dialogs does not send an Authorization header on webhook calls,
so authentication is two-layered:

  1. Path secret (``CONF_DIALOG_WEBHOOK_SECRET``) — random UUID stored
     only in the user's MA config and in the skill's Backend URL. Knowing
     it requires access to the Yandex Dialogs developer console.
  2. ``body.session.skill_id == CONF_DIALOG_SKILL_ID`` — sanity check;
     skill_id is not secret on its own but stops cross-skill misroutes.

A request is rejected with 404 if the secret doesn't match (no leak via
401 timing) and with 401 if the skill_id doesn't match (configured
skill received a payload from a different skill — should never happen).

Session memory: the handler does not keep any in-process LRU. The
"last player used" default is round-tripped through Yandex's three
state buckets (priority: ``state.session`` → ``state.application`` →
``state.user``), which survives plugin reloads and even MA restarts
for the application/user tiers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .constants import (
    DIALOG_RESOLVE_TIMEOUT,
    DIALOG_WEBHOOK_BASE_PATH,
)
from .dialogs_control import (
    control_confirmation,
    execute_control,
    format_list_players,
    parse_control,
)
from .dialogs_nlu import (
    _VERB_RE,
    ParsedCommand,
    list_exposed_players,
    parse_command,
    resolve_player,
    resolve_player_candidates,
)
from .dialogs_player import play_for_alice, resolve_query

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)


# Static stress-mark dictionary for common response words (P0.2).
# Keys are case-insensitive whole-word matches; the marker is `+` placed
# directly before the stressed vowel — Yandex Alice TTS supports this
# inline syntax. Keep small and high-confidence; band/track names are
# left as-is (those need a separate phoneme dict — P2.3).
_TTS_STRESS_MARKS: dict[str, str] = {
    "включаю": "включ+аю",
    "ставлю": "ст+авлю",
    "пауза": "п+ауза",
    "продолжаю": "продолж+аю",
    "следующая": "сл+едующая",
    "предыдущая": "пред+ыдущая",
    "громче": "гр+омче",
    "тише": "т+ише",
    "громкость": "гр+омкость",
    "колонке": "кол+онке",
    "колонку": "кол+онку",
}

_TTS_WORD_RE = re.compile(r"[А-Яа-яЁё]+")


def _tts_for(text: str) -> str:
    """Add `+` stress markers to known words for cleaner Alice TTS.

    Pure substitution — unknown words pass through unchanged. The map is
    intentionally small (high-confidence Russian response words only);
    expand via PRs as patterns emerge.
    """
    if not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        replacement = _TTS_STRESS_MARKS.get(word.lower())
        if replacement is None:
            return word
        if word[:1].isupper():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    return _TTS_WORD_RE.sub(_sub, text)


def _safe_dict(value: Any) -> dict[str, Any]:
    """Return value if it's a dict, else an empty dict (defensive parsing)."""
    return value if isinstance(value, dict) else {}


def _without_pending(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `state` with `pending_command` and `awaiting_query` removed.

    Used after the disambiguation/elicitation flow successfully completes
    so the next turn doesn't accidentally re-enter the saved branch.
    """
    return {k: v for k, v in state.items() if k not in {"pending_command", "awaiting_query"}}


class DialogsWebhookHandler:
    """Handles incoming voice-command webhook calls from a Yandex Dialogs skill."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        skill_id: str,
        webhook_secret: str,
        exposed_player_ids: set[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            mass: MusicAssistant instance.
            skill_id: Configured ``CONF_DIALOG_SKILL_ID``; payloads with a
                different ``session.skill_id`` are rejected.
            webhook_secret: Random secret embedded in the webhook URL.
            exposed_player_ids: Optional restriction set; only these players
                are addressable by voice (passed to the player resolver).
            logger: Optional logger override.
        """
        self._mass = mass
        self._skill_id = skill_id
        self._webhook_secret = webhook_secret
        self._exposed_player_ids = exposed_player_ids
        self._logger = logger or _LOGGER
        self._unregister_callbacks: list[Callable[[], None]] = []

    def register_routes(self) -> None:
        """Register the webhook route on mass.webserver."""
        path = f"{DIALOG_WEBHOOK_BASE_PATH}/{self._webhook_secret}"
        redacted = f"{DIALOG_WEBHOOK_BASE_PATH}/...{self._webhook_secret[-4:]}"
        try:
            unregister = self._mass.webserver.register_dynamic_route(
                path, self._handle_webhook, "POST"
            )
        except RuntimeError:
            self._logger.exception("Failed to register Dialogs webhook route %s", redacted)
            raise
        self._unregister_callbacks.append(unregister)
        self._logger.info("Dialogs webhook registered at %s", redacted)

    def unregister_routes(self) -> None:
        """Unregister the webhook route."""
        for cb in self._unregister_callbacks:
            try:
                cb()
            except Exception:
                self._logger.debug("Error unregistering dialog route", exc_info=True)
        self._unregister_callbacks.clear()

    # -------------------------------------------------------------------
    # Webhook entry point
    # -------------------------------------------------------------------

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        # Path secret already enforced by the route URL — getting here means
        # the secret matches. Still constant-time-compare it via the captured
        # path arg in case aiohttp routing ever changes.
        url_secret = request.match_info.get("secret") or request.path.rsplit("/", 1)[-1]
        if not secrets.compare_digest(url_secret, self._webhook_secret):
            return web.Response(status=404)

        try:
            body = await request.json()
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._yandex_response(
                incoming_session={},
                text="Что-то пошло не так с запросом.",
            )
        if not isinstance(body, dict):
            return self._yandex_response(
                incoming_session={},
                text="Что-то пошло не так с запросом.",
            )

        session = body.get("session") or {}
        if not isinstance(session, dict):
            session = {}
        req = body.get("request") or {}
        if not isinstance(req, dict):
            req = {}

        # skill_id sanity check — reject if absent or mismatched.
        incoming_skill_id = str(session.get("skill_id") or "")
        if not incoming_skill_id or not secrets.compare_digest(incoming_skill_id, self._skill_id):
            self._logger.warning(
                "Rejecting dialog payload: skill_id %r != configured %r",
                incoming_skill_id or "<missing>",
                self._skill_id,
            )
            return web.Response(status=401)

        # State buckets (P0.1 — replace in-memory LRU with Yandex state).
        state = body.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        session_state_in = _safe_dict(state.get("session"))
        app_state_in = _safe_dict(state.get("application"))
        user_state_in = _safe_dict(state.get("user"))

        default_id_raw = (
            session_state_in.get("last_player_id")
            or app_state_in.get("last_player_id")
            or user_state_in.get("preferred_player_id")
        )
        default_id = str(default_id_raw) if default_id_raw else None

        is_new = bool(session.get("new"))
        command = str(req.get("command") or "").strip()

        # Single summary log per incoming request — surfaces the wire-shape
        # bits we route on. Sensitive fields (skill_id, webhook_secret,
        # raw payload IDs) are excluded; user/session IDs are opaque
        # opaque tokens and DEBUG is opt-in, so they're included as-is.
        self._logger.debug(
            "Webhook recv: cmd=%r req_type=%s is_new=%s pending=%s "
            "awaiting=%s default_player=%s session_id=%s",
            command,
            req.get("type", "SimpleUtterance"),
            is_new,
            bool(session_state_in.get("pending_command")),
            bool(session_state_in.get("awaiting_query")),
            default_id,
            session.get("session_id", ""),
        )

        if is_new and not command:
            text = "Привет! Скажи, что включить и на какой колонке."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )

        if not command:
            text = "Не понял команду. Скажи, например: включи рок на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )

        # P0.6 — try control commands (pause/next/volume/...) FIRST, on
        # the raw command. Doing this before the awaiting-query synthesis
        # lets the user pivot from a slot-elicit prompt straight into a
        # control intent ("Включи." → "Что включить?" → "пауза на кухне")
        # without the prefix-prepend turning it into "включи пауза…".
        # If control matches, drop any pending/awaiting state — the user
        # is no longer in either of those flows.
        if control := parse_control(command):
            self._logger.debug("Parsed dialog control %r → %r", command, control)
            return self._handle_control(
                session=session,
                control=control,
                default_id=default_id,
                session_state_in=_without_pending(session_state_in),
                app_state_in=app_state_in,
            )

        # P0.4 — awaiting-query re-entry. If the previous turn asked "Что
        # включить?" and the new utterance isn't a control phrase, treat
        # it as the missing query slot. Prepend a synthetic "включи " so
        # the existing kind classifier runs ("песню X", "альбом Y",
        # "мою волну", etc.). Skip the synthetic prefix if the user
        # already said one of the verbs.
        if session_state_in.get("awaiting_query") and not _VERB_RE.match(command):
            command = f"включи {command}"
            self._logger.debug("Awaiting-query branch: synthesised cmd=%r", command)

        # P0.3 — pending-command re-entry. If a previous turn asked the
        # user to disambiguate which player to use, the new utterance (or
        # button press) carries the answer; replay the saved play intent.
        pending = session_state_in.get("pending_command")
        if isinstance(pending, dict):
            self._logger.debug(
                "Pending-command branch: kind=%s query=%r radio=%s; cmd=%r payload=%s",
                pending.get("kind"),
                pending.get("query"),
                pending.get("radio_mode"),
                command,
                bool(_safe_dict(req.get("payload")).get("player_id")),
            )
            replay_response = await self._try_resume_pending(
                session=session,
                req=req,
                command=command,
                pending=pending,
                session_state_in=session_state_in,
                app_state_in=app_state_in,
            )
            if replay_response is not None:
                return replay_response
            self._logger.debug(
                "Pending-command branch: could not resume — falling through to parse_command"
            )

        parsed = parse_command(command)
        self._logger.debug("Parsed dialog command %r → %r", command, parsed)
        return await self._dispatch_play(
            session=session,
            parsed=parsed,
            default_id=default_id,
            session_state_in=session_state_in,
            app_state_in=app_state_in,
        )

    # -------------------------------------------------------------------
    # Play dispatch (slot-elicit + resolve + disambiguate + play)
    # -------------------------------------------------------------------

    async def _dispatch_play(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        default_id: str | None,
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
    ) -> web.Response:
        """Slot-elicit / resolve player / disambiguate / play (or fail)."""
        # P0.4 — slot elicitation: bare verb with no actionable content.
        if parsed.kind == "search" and not parsed.query and not parsed.player_hint:
            self._logger.debug("Slot-elicit branch: empty query, asking 'Что включить?'")
            text = "Что включить? Можно сказать имя артиста, песни или плейлиста."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state={
                    **_without_pending(session_state_in),
                    "awaiting_query": True,
                },
            )

        candidates = resolve_player_candidates(
            self._mass,
            parsed.player_hint,
            default_id=default_id,
            exposed_ids=self._exposed_player_ids,
        )
        if not candidates:
            # Special case: no hint, no default, multiple exposed players.
            # `resolve_player_candidates` returns [] with no hint when it
            # can't pick deterministically — for the user that's ambiguity,
            # not "not found". Surface all exposed players for
            # disambiguation instead of the misleading "не нашёл колонку
            # «(не указано)»".
            if parsed.player_hint is None and default_id is None:
                all_exposed = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
                if len(all_exposed) >= 2:
                    self._logger.debug(
                        "Play branch: no hint + no default + %d exposed → "
                        "disambiguation across all exposed players",
                        len(all_exposed),
                    )
                    return self._build_disambiguation_response(
                        session=session,
                        parsed=parsed,
                        candidates=all_exposed,
                        session_state_in=session_state_in,
                    )
            hint = parsed.player_hint or "(не указано)"
            self._logger.info(
                "Play branch: no player resolved for hint=%r (default_id=%s); "
                "responding 'не нашёл колонку'",
                parsed.player_hint,
                default_id,
            )
            text = f"Не нашёл колонку «{hint}». Скажи, например: на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )
        if len(candidates) > 1:
            self._logger.debug(
                "Play branch: ambiguous, %d candidates → disambiguation prompt",
                len(candidates),
            )
            return self._build_disambiguation_response(
                session=session,
                parsed=parsed,
                candidates=candidates,
                session_state_in=session_state_in,
            )

        self._logger.debug(
            "Play branch: resolved → player %s (%s)",
            candidates[0].name or candidates[0].player_id,
            candidates[0].player_id,
        )
        return await self._play_with_player(
            session=session,
            parsed=parsed,
            player=candidates[0],
            base_session_state=session_state_in,
            base_app_state=app_state_in,
        )

    # -------------------------------------------------------------------
    # Control execution helper (P0.6)
    # -------------------------------------------------------------------

    def _handle_control(
        self,
        *,
        session: dict[str, Any],
        control: Any,
        default_id: str | None,
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
    ) -> web.Response:
        """Resolve player + dispatch a control action; build response."""
        # list_players is informational — no player resolution / dispatch.
        if control.action == "list_players":
            players = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
            text = format_list_players(players)
            self._logger.debug(
                "Control list_players → %d player(s): %s",
                len(players),
                [getattr(p, "name", None) or p.player_id for p in players],
            )
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )

        player = resolve_player(
            self._mass,
            control.player_hint,
            default_id=default_id,
            exposed_ids=self._exposed_player_ids,
        )
        if player is None:
            self._logger.info(
                "Control %s: no player resolved (hint=%r, default_id=%s)",
                control.action,
                control.player_hint,
                default_id,
            )
            # Distinguish "no hint + ambiguous" from "hint given but unknown"
            # so the message matches the actual cause.
            if control.player_hint:
                text = f"Не нашёл колонку «{control.player_hint}». Скажи, например: на кухне."
            else:
                text = "Скажи, на какой колонке. Например: пауза на кухне."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                end_session=False,
                session_state=session_state_in,
            )
        self._logger.debug(
            "Control %s → player %s (%s) value=%s",
            control.action,
            player.name or player.player_id,
            player.player_id,
            control.value,
        )
        self._mass.create_task(execute_control(self._mass, control, player))
        new_session_state = {**session_state_in, "last_player_id": player.player_id}
        new_app_state = {**app_state_in, "last_player_id": player.player_id}
        user_obj = session.get("user") or {}
        user_state_update: dict[str, Any] | None = None
        if isinstance(user_obj, dict) and user_obj.get("user_id"):
            user_state_update = {"preferred_player_id": player.player_id}
        text = control_confirmation(control)
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            session_state=new_session_state,
            application_state=new_app_state,
            user_state_update=user_state_update,
        )

    # -------------------------------------------------------------------
    # Play execution helper (shared by initial flow and pending replay)
    # -------------------------------------------------------------------

    async def _play_with_player(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        player: Any,
        base_session_state: dict[str, Any],
        base_app_state: dict[str, Any],
    ) -> web.Response:
        """Search media, fire-and-forget play, build response with persisted state."""
        try:
            media = await asyncio.wait_for(
                resolve_query(self._mass, parsed), timeout=DIALOG_RESOLVE_TIMEOUT
            )
        except TimeoutError:
            self._logger.warning(
                "Music search timed out (>%.1fs) for query %r",
                DIALOG_RESOLVE_TIMEOUT,
                parsed.query,
            )
            text = "Поиск занял слишком долго, попробуй ещё раз."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                session_state=_without_pending(base_session_state),
            )

        if media is None:
            text = f"Не нашёл такую музыку: {parsed.query}."
            return self._yandex_response(
                incoming_session=session,
                text=text,
                tts=_tts_for(text),
                session_state=_without_pending(base_session_state),
            )

        # Fire-and-forget — Alice has a 4.5s budget; play_media may take longer
        # to actually start streaming. mass.create_task tracks the task in the
        # MA lifecycle (cancelled on shutdown) and logs unhandled exceptions.
        self._mass.create_task(
            play_for_alice(
                self._mass,
                player.player_id,
                media,
                radio_mode=parsed.radio_mode,
            )
        )

        new_session_state = {
            **_without_pending(base_session_state),
            "last_player_id": player.player_id,
        }
        new_app_state = {**base_app_state, "last_player_id": player.player_id}
        user_obj = session.get("user") or {}
        user_state_update: dict[str, Any] | None = None
        if isinstance(user_obj, dict) and user_obj.get("user_id"):
            user_state_update = {"preferred_player_id": player.player_id}

        spoken_query = parsed.query or "музыку"
        text = f"Включаю {spoken_query} на {player.name or player.player_id}."
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            session_state=new_session_state,
            application_state=new_app_state,
            user_state_update=user_state_update,
        )

    # -------------------------------------------------------------------
    # Disambiguation (P0.3)
    # -------------------------------------------------------------------

    def _build_disambiguation_response(
        self,
        *,
        session: dict[str, Any],
        parsed: ParsedCommand,
        candidates: list[Any],
        session_state_in: dict[str, Any],
    ) -> web.Response:
        """Ask the user which player to use; offer buttons for each candidate."""
        # Yandex caps ItemsList at 5 anyway; cap our buttons to the same.
        capped = candidates[:5]
        names = [p.name or p.player_id for p in capped]
        text = f"На какой колонке: {', '.join(names)}?"
        buttons = [
            {
                "title": (p.name or p.player_id)[:64],
                "payload": {"player_id": p.player_id},
                "hide": True,
            }
            for p in capped
        ]
        # Clear any prior `awaiting_query` / `pending_command` before
        # writing the new one. Without this, slot-elicitation state from
        # an earlier turn would leak into the disambiguation response —
        # the next utterance ("Кухня маленькая") would get auto-prefixed
        # with "включи " by the awaiting-query branch and miss the
        # pending-command resolver.
        new_session_state = {
            **_without_pending(session_state_in),
            "pending_command": {
                "kind": parsed.kind,
                "query": parsed.query[:200],
                "radio_mode": parsed.radio_mode,
            },
        }
        return self._yandex_response(
            incoming_session=session,
            text=text,
            tts=_tts_for(text),
            end_session=False,
            session_state=new_session_state,
            buttons=buttons,
        )

    async def _try_resume_pending(
        self,
        *,
        session: dict[str, Any],
        req: dict[str, Any],
        command: str,
        pending: dict[str, Any],
        session_state_in: dict[str, Any],
        app_state_in: dict[str, Any],
    ) -> web.Response | None:
        """Attempt to resume a saved pending_command using button payload or text.

        Returns a response if the pending command was resumed (success or
        decided failure). Returns None when the new utterance doesn't
        resolve to a player at all — caller falls through to normal
        parse_command flow.
        """
        chosen_player: Any = None

        # Button press: payload.player_id is what we sent on the previous turn.
        # Validate against the *currently exposed* player set rather than
        # blindly trusting the payload — guards against stale or crafted
        # payloads that target a player that's been disabled / removed /
        # un-exposed since we offered the buttons. Payload integrity is
        # already enforced upstream by `body.session.skill_id`, but
        # defence-in-depth is cheap here.
        payload = req.get("payload")
        if isinstance(payload, dict):
            pid = payload.get("player_id")
            if isinstance(pid, str):
                exposed = list_exposed_players(self._mass, exposed_ids=self._exposed_player_ids)
                chosen_player = next((p for p in exposed if p.player_id == pid), None)
                if chosen_player is None:
                    self._logger.warning(
                        "Pending replay: ButtonPressed payload player_id=%r "
                        "not in exposed-player set; ignoring",
                        pid,
                    )

        if chosen_player is None:
            # Free-text follow-up: treat the utterance as a player hint.
            followup = parse_command(command)
            hint = followup.player_hint or command
            candidates = resolve_player_candidates(
                self._mass,
                hint,
                default_id=None,
                exposed_ids=self._exposed_player_ids,
            )
            if len(candidates) == 1:
                chosen_player = candidates[0]
            elif len(candidates) > 1:
                # Re-ask using the same pending command.
                return self._build_disambiguation_response(
                    session=session,
                    parsed=ParsedCommand(
                        kind=str(pending.get("kind", "search")),  # type: ignore[arg-type]
                        query=str(pending.get("query", "")),
                        radio_mode=bool(pending.get("radio_mode", False)),
                    ),
                    candidates=candidates,
                    session_state_in=session_state_in,
                )

        if chosen_player is None:
            return None

        replay = ParsedCommand(
            kind=str(pending.get("kind", "search")),  # type: ignore[arg-type]
            query=str(pending.get("query", "")),
            radio_mode=bool(pending.get("radio_mode", False)),
        )
        return await self._play_with_player(
            session=session,
            parsed=replay,
            player=chosen_player,
            base_session_state=session_state_in,
            base_app_state=app_state_in,
        )

    # -------------------------------------------------------------------
    # Yandex Dialogs response envelope
    # -------------------------------------------------------------------

    @staticmethod
    def _yandex_response(
        *,
        incoming_session: dict[str, Any],
        text: str,
        tts: str | None = None,
        end_session: bool = True,
        session_state: dict[str, Any] | None = None,
        application_state: dict[str, Any] | None = None,
        user_state_update: dict[str, Any] | None = None,
        buttons: list[dict[str, Any]] | None = None,
    ) -> web.Response:
        """Build a Yandex Dialogs response envelope.

        ``session_state`` / ``application_state`` are full overwrites per
        Yandex spec; ``user_state_update`` is merged into the existing
        user-scoped state (set keys to None to clear). Omit a parameter
        to leave that bucket unchanged on Yandex's side.
        """
        # Yandex envelopes carry two user_id fields: the deprecated root
        # `session.user_id` (always present in current API revisions for
        # backwards compatibility) and the nested `session.user.user_id`
        # (set only when the user is account-linked). Prefer the root for
        # historical reasons but fall back to the nested form so the
        # echo doesn't leak an empty string if a future Yandex API
        # revision drops the root field.
        user_id = incoming_session.get("user_id") or _safe_dict(incoming_session.get("user")).get(
            "user_id", ""
        )
        echoed = {
            "session_id": incoming_session.get("session_id", ""),
            "message_id": incoming_session.get("message_id", 0),
            "user_id": user_id,
        }
        response_body: dict[str, Any] = {
            "text": text,
            "tts": tts if tts is not None else text,
            "end_session": end_session,
        }
        if buttons:
            response_body["buttons"] = buttons
        payload: dict[str, Any] = {
            "version": "1.0",
            "session": echoed,
            "response": response_body,
        }
        if session_state is not None:
            payload["session_state"] = session_state
        if application_state is not None:
            payload["application_state"] = application_state
        if user_state_update is not None:
            payload["user_state_update"] = user_state_update
        return web.json_response(payload)
