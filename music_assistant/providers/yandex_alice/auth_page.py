"""Yandex Passport Device Flow with a custom HTML user_code page.

The form-side click on *Sign in to Yandex Passport* is a single
**blocking** action: ``perform_device_auth`` starts a Device Flow,
opens an MA-hosted landing page (with the user_code + a Continue
button) as an ``AuthenticationHelper`` popup, polls Passport until
the user confirms in Yandex, and returns the resulting ``x_token``.
The dispatcher writes that token into ``values[CONF_AUTH_X_TOKEN]``,
and the next form render flips into Step 2.

The blocking pattern is dictated by MA's frontend: in *add-provider*
mode hidden ``ConfigEntry``s do not round-trip between successive
``ACTION`` clicks until the provider is saved, so a multi-click
self-resuming flow is impossible there. Both ``yandex-music`` and
``yandex-smarthome`` use the same blocking pattern.

The HTML page is registered as a dynamic route and torn down inside
the same ``async with`` so it never outlives the auth attempt. The
status endpoint flips to ``done`` once the backend captures the
``x_token`` so the page can auto-close itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import PassportClient
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    YaPassportError,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)

DEVICE_CODE_PAGE_BASE_PATH = "/yandex_alice/device_code"

# Seconds to keep the status endpoint alive after the flow finishes so
# the intermediate page has a chance to poll once more and close itself.
_POST_AUTH_GRACE_SECONDS = 3

# Callable returning the current device-flow state for status polls.
StateProvider = Callable[[], str]

__all__ = [
    "DEVICE_CODE_PAGE_BASE_PATH",
    "StateProvider",
    "build_device_code_page_url",
    "perform_device_auth",
    "register_device_code_route",
    "unregister_device_code_route",
]


def build_device_code_page_url(base_url: str, session_id: str) -> str:
    """Return the absolute URL of the device-code page for a session."""
    if not base_url or not session_id:
        return ""
    return f"{base_url.rstrip('/')}{DEVICE_CODE_PAGE_BASE_PATH}/{session_id}"


def _build_device_code_page(
    *,
    user_code: str,
    verification_url: str,
    status_url: str,
    skill_name: str,
) -> str:
    """Render the HTML page shown to the user during Device Flow login."""
    safe_code = html.escape(user_code)
    safe_url = html.escape(verification_url, quote=True)
    safe_skill = html.escape(skill_name or "Music Assistant")
    safe_status_url = json.dumps(status_url).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Yandex Alice — Device Code</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {{ color-scheme: light; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; padding: 2rem 1rem;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; box-sizing: border-box;
            background: #f5f5f7; color: #1d1d1f;
        }}
        .card {{
            background: #ffffff; color: #1d1d1f;
            border-radius: 14px; padding: 2rem;
            max-width: 28rem; width: 100%;
            box-shadow: 0 4px 20px rgba(0,0,0,.08);
            text-align: center;
        }}
        h1 {{ margin: 0 0 .5rem; font-size: 1.25rem; color: #1d1d1f; }}
        p {{ margin: .5rem 0 1.25rem; color: #4a4a52; line-height: 1.45; }}
        #code {{
            display: inline-block;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 2rem; font-weight: 600; letter-spacing: .15em;
            padding: .75rem 1.25rem; border-radius: 10px;
            background: #f2f2f7; color: #1d1d1f;
            user-select: all;
        }}
        button, .btn {{
            display: inline-block; margin-top: 1.5rem; padding: .75rem 1.5rem;
            font-size: 1rem; font-weight: 600; text-decoration: none;
            border: none; border-radius: 10px; cursor: pointer;
            background: #ffcc00; color: #1d1d1f;
        }}
        button:hover, .btn:hover {{ background: #ffd633; }}
        #copy {{
            margin-top: .75rem; background: transparent; color: #1d1d1f;
            border: 1px solid #c8c8cd; padding: .4rem 1rem;
            font-size: .85rem; font-weight: 400;
        }}
        #copy:hover {{ background: #f2f2f7; }}
        .skill {{ font-weight: 600; color: #1d1d1f; }}
    </style>
</head>
<body>
    <div class="card" id="card">
        <h1>Sign in to Yandex Passport</h1>
        <p>
            Music Assistant will register the
            <span class="skill">{safe_skill}</span> dialog skill on your behalf.
            Open the link below and enter this code in Yandex Passport.
        </p>
        <div id="code">{safe_code}</div>
        <div>
            <button id="copy" type="button">Copy code</button>
        </div>
        <a class="btn" href="{safe_url}" target="_blank" rel="noopener">Continue to Yandex</a>
        <p style="margin-top:1.5rem;font-size:.85rem;color:#6e6e76">
            After confirming in Yandex, return to Music Assistant and click
            "I confirmed - continue". This page will close itself once the
            backend captures your sign-in.
        </p>
    </div>
    <script>
        const copyButton = document.getElementById('copy');
        const codeElement = document.getElementById('code');
        const card = document.getElementById('card');
        const statusUrl = {safe_status_url};

        function selectCodeForManualCopy() {{
            if (!codeElement) return;
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(codeElement);
            selection.removeAllRanges();
            selection.addRange(range);
            if (copyButton) copyButton.textContent = 'Press Ctrl/Cmd+C';
        }}

        copyButton?.addEventListener('click', async function() {{
            const code = codeElement?.textContent?.trim();
            if (!code) return;
            if (!navigator.clipboard?.writeText) {{
                selectCodeForManualCopy();
                return;
            }}
            try {{
                await navigator.clipboard.writeText(code);
                this.textContent = 'Copied';
            }} catch {{
                selectCodeForManualCopy();
            }}
        }});

        function showResult(title, message) {{
            card.innerHTML =
                '<h1>' + title + '</h1><p>' + message + '</p>';
        }}

        async function pollStatus() {{
            try {{
                const r = await fetch(statusUrl, {{ cache: 'no-store' }});
                if (r.ok) {{
                    const data = await r.json();
                    if (data.state === 'done') {{
                        showResult(
                            'Sign-in complete',
                            'You can close this window and return to Music Assistant.'
                        );
                        setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, 800);
                        return;
                    }}
                    if (data.state === 'failed') {{
                        showResult(
                            'Sign-in failed',
                            'Please return to Music Assistant and try again.'
                        );
                        return;
                    }}
                }}
            }} catch (e) {{ /* network hiccup - retry */ }}
            setTimeout(pollStatus, 2500);
        }}
        setTimeout(pollStatus, 2500);
    </script>
</body>
</html>
"""


def _device_code_page_path(session_id: str) -> str:
    return f"{DEVICE_CODE_PAGE_BASE_PATH}/{session_id}"


def _device_code_status_path(session_id: str) -> str:
    return f"{DEVICE_CODE_PAGE_BASE_PATH}/{session_id}/status"


def register_device_code_route(
    mass: MusicAssistant,
    *,
    session_id: str,
    user_code: str,
    verification_url: str,
    skill_name: str,
    state_provider: StateProvider,
) -> str:
    """Register the device-code page + status routes; return the page URL.

    Idempotent: any existing route at the same path is unregistered
    first so this can be called on every form render without piling
    up handlers. Returns ``""`` if the webserver is unavailable.

    The ``state_provider`` callable is invoked on every status poll
    and must return one of ``"pending"``, ``"done"``, ``"failed"``.
    Wired by the dispatcher to read live form state — the page closes
    itself once the dispatcher has captured a fresh ``cached_x_token``
    and cleared the device-flow session blob.
    """
    if not session_id or not user_code or not verification_url:
        return ""
    webserver = getattr(mass, "webserver", None)
    if webserver is None or not hasattr(webserver, "register_dynamic_route"):
        _LOGGER.debug("auth_page: webserver unavailable; skipping route registration")
        return ""

    page_path = _device_code_page_path(session_id)
    status_path = _device_code_status_path(session_id)
    # Both URLs returned to the FE are *path-relative* — that lets the
    # browser resolve them against whatever origin the user is hitting
    # MA on (e.g. `https://ma.example.com`, `http://localhost:8095`),
    # not against ``webserver.base_url`` which can resolve to the docker
    # bridge IP and break the popup link.
    status_url = status_path
    page_html = _build_device_code_page(
        user_code=user_code,
        verification_url=verification_url,
        status_url=status_url,
        skill_name=skill_name,
    )

    async def _serve_page(_request: web.Request) -> web.Response:
        return web.Response(
            text=page_html,
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def _serve_status(_request: web.Request) -> web.Response:
        try:
            state = state_provider()
        except Exception as exc:
            _LOGGER.debug("auth_page: state_provider raised: %r", exc)
            state = "pending"
        return web.json_response(
            {"state": state},
            headers={"Cache-Control": "no-store"},
        )

    # Idempotent registration - drop any existing registration at the
    # same paths first so a re-render doesn't blow up.
    for path in (page_path, status_path):
        with contextlib.suppress(Exception):
            webserver.unregister_dynamic_route(path, "GET")

    try:
        webserver.register_dynamic_route(page_path, _serve_page, "GET")
        webserver.register_dynamic_route(status_path, _serve_status, "GET")
    except Exception as exc:
        _LOGGER.warning("auth_page: failed to register device-code route: %r", exc)
        return ""

    return page_path


def unregister_device_code_route(mass: MusicAssistant, *, session_id: str) -> None:
    """Tear down the device-code page + status routes for a session."""
    if not session_id:
        return
    webserver = getattr(mass, "webserver", None)
    if webserver is None:
        return
    for path in (_device_code_page_path(session_id), _device_code_status_path(session_id)):
        with contextlib.suppress(Exception):
            webserver.unregister_dynamic_route(path, "GET")


# ---------------------------------------------------------------------------
# Blocking Device Flow — single-action sign-in
# ---------------------------------------------------------------------------


async def perform_device_auth(
    mass: MusicAssistant,
    session_id: str,
    *,
    skill_name: str = "Music Assistant",
) -> tuple[str, str]:
    """Run a complete Yandex Passport Device Flow.

    Returns ``(x_token, display_login)`` — the long-lived auth token
    plus the user-visible Yandex login (used in the "Authorized as
    <name>" banner). ``display_login`` is ``""`` when Yandex didn't
    return one.

    Blocks for the lifetime of the user's confirmation step (up to
    ~10 min). Hosts an HTML landing page at
    ``/yandex_alice/device_code/<session_id>`` for the duration and
    forwards it to the user via ``AuthenticationHelper`` popup.

    ``session_id`` **must** be the ``values["session_id"]`` value MA's
    frontend supplies on every ACTION invocation — popping a popup on
    any other channel results in a popup the frontend isn't listening
    for, so it never appears.

    Raises:
        LoginFailed: the Device Flow timed out, was rejected by
            Yandex, or another Passport-level error escaped.
    """
    if not session_id:
        raise LoginFailed(
            "Missing session_id from the config-flow frontend. "
            "Sign-in needs the id MA's frontend supplies on every "
            "ACTION invocation."
        )
    # Local import: ``music_assistant`` is the server package, not the
    # models-only public API — only available inside an MA runtime, so
    # importing it at module load would break unit tests that exercise
    # this module without a real MA instance.
    from music_assistant.helpers.auth import AuthenticationHelper  # noqa: PLC0415

    try:
        async with PassportClient.create() as client:
            session = await client.start_device_login(device_name="Music Assistant")
            _LOGGER.info(
                "Device flow started: open %s (expires in %ss)",
                session.verification_url,
                session.expires_in,
            )
            state: dict[str, str] = {"value": "pending"}

            page_url = register_device_code_route(
                mass,
                session_id=session_id,
                user_code=session.user_code,
                verification_url=session.verification_url,
                skill_name=skill_name,
                state_provider=lambda: state["value"],
            )

            try:
                async with AuthenticationHelper(mass, session_id) as auth_helper:
                    auth_helper.send_url(
                        page_url or f"{session.verification_url}?user_code={session.user_code}"
                    )
                    try:
                        creds = await client.poll_device_until_confirmed(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        state["value"] = "failed"
                        # Give the page one last poll to surface the
                        # failure before we tear the route down.
                        await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
                        raise
                    state["value"] = "done"
                    # Let the page pick up "done" and close itself.
                    await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
            finally:
                unregister_device_code_route(mass, session_id=session_id)

            x_token = creds.x_token.get_secret()
            display_login = (creds.display_login or "").strip()
            _LOGGER.debug(
                "Device flow complete, captured x_token (len=%d) for %r",
                len(x_token),
                display_login or "<unknown>",
            )
            return x_token, display_login

    except DeviceCodeTimeoutError as err:
        raise LoginFailed("Device authentication timed out — please try again.") from err
    except YaPassportError as err:
        raise LoginFailed(f"Yandex Passport error: {err}") from err
