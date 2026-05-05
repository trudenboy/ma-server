"""HTTP handler for the Yandex Dialogs «Навык» webhook (experimental).

Registers a single dynamic route on the MA webserver:

  POST /api/yandex_dialogs/webhook/{secret}

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
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .constants import (
    DIALOG_RESOLVE_TIMEOUT,
    DIALOG_SESSION_CACHE_MAX,
    DIALOG_SESSION_TTL_SEC,
    DIALOG_WEBHOOK_BASE_PATH,
)
from .dialogs_nlu import parse_command, resolve_player
from .dialogs_player import play_for_alice, resolve_query

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)


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
        self._last_player: OrderedDict[str, tuple[str, float]] = OrderedDict()

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
    # Session memory
    # -------------------------------------------------------------------

    def _remember_player(self, session_id: str, player_id: str) -> None:
        now = time.monotonic()
        self._last_player[session_id] = (player_id, now)
        self._last_player.move_to_end(session_id)
        # Evict oldest by insertion order if cap exceeded.
        while len(self._last_player) > DIALOG_SESSION_CACHE_MAX:
            self._last_player.popitem(last=False)
        # Also evict TTL-expired entries opportunistically.
        cutoff = now - DIALOG_SESSION_TTL_SEC
        for sid in list(self._last_player.keys()):
            if self._last_player[sid][1] < cutoff:
                self._last_player.pop(sid, None)
            else:
                break

    def _get_default_player(self, session_id: str) -> str | None:
        entry = self._last_player.get(session_id)
        if entry is None:
            return None
        player_id, ts = entry
        if time.monotonic() - ts > DIALOG_SESSION_TTL_SEC:
            self._last_player.pop(session_id, None)
            return None
        return player_id

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
            return self._yandex_response(session_state={}, text="Что-то пошло не так с запросом.")  # noqa: RUF001
        if not isinstance(body, dict):
            return self._yandex_response(session_state={}, text="Что-то пошло не так с запросом.")  # noqa: RUF001

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

        session_id = str(session.get("session_id") or "")
        is_new = bool(session.get("new"))
        command = str(req.get("command") or "").strip()

        if is_new and not command:
            return self._yandex_response(
                session_state=session,
                text="Привет! Скажи, что включить и на какой колонке.",
                end_session=False,
            )

        if not command:
            return self._yandex_response(
                session_state=session,
                text="Не понял команду. Скажи, например: включи рок на кухне.",  # noqa: RUF001
                end_session=False,
            )

        parsed = parse_command(command)
        self._logger.debug("Parsed dialog command %r → %r", command, parsed)

        default_id = self._get_default_player(session_id) if session_id else None
        player = resolve_player(
            self._mass,
            parsed.player_hint,
            default_id=default_id,
            exposed_ids=self._exposed_player_ids,
        )
        if player is None:
            hint = parsed.player_hint or "(не указано)"
            return self._yandex_response(
                session_state=session,
                text=f"Не нашёл колонку «{hint}». Скажи, например: на кухне.",  # noqa: RUF001
                end_session=False,
            )

        try:
            media = await asyncio.wait_for(
                resolve_query(self._mass, parsed), timeout=DIALOG_RESOLVE_TIMEOUT
            )
        except TimeoutError:
            self._logger.warning(
                "Music search timed out (>%.1fs) for query %r", DIALOG_RESOLVE_TIMEOUT, parsed.query
            )
            return self._yandex_response(
                session_state=session,
                text="Поиск занял слишком долго, попробуй ещё раз.",
            )

        if media is None:
            return self._yandex_response(
                session_state=session,
                text=f"Не нашёл такую музыку: {parsed.query}.",  # noqa: RUF001
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

        if session_id:
            self._remember_player(session_id, player.player_id)

        spoken_query = parsed.query or "музыку"
        return self._yandex_response(
            session_state=session,
            text=f"Включаю {spoken_query} на {player.name or player.player_id}.",
        )

    # -------------------------------------------------------------------
    # Yandex Dialogs response envelope
    # -------------------------------------------------------------------

    @staticmethod
    def _yandex_response(
        *,
        session_state: dict[str, Any],
        text: str,
        end_session: bool = True,
    ) -> web.Response:
        """Build a minimal Yandex Dialogs response envelope."""
        echoed = {
            "session_id": session_state.get("session_id", ""),
            "message_id": session_state.get("message_id", 0),
            "user_id": session_state.get("user_id", ""),
        }
        payload = {
            "version": "1.0",
            "session": echoed,
            "response": {
                "text": text,
                "tts": text,
                "end_session": end_session,
            },
        }
        return web.json_response(payload)
