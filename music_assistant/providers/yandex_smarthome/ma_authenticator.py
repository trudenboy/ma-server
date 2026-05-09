"""Music-Assistant Device-Flow authenticator for ya-dialogs-api.

Adapter that wraps :class:`ya_passport_auth.PassportClient` Device Flow
behind the :data:`ya_dialogs_api.AuthenticatorCM` Protocol — a no-arg
async-context-manager factory yielding an authorized
``aiohttp.ClientSession``.

UX:
- Hosts a temporary HTML activation page on ``mass.webserver`` showing the
  Device Code prominently (Yandex's ya.ru/device strips query params on
  the redirect-to-login, so we cannot pre-fill the code there).
- Opens the page in a popup via ``music_assistant.helpers.auth.AuthenticationHelper``.
- After the user enters the code at ya.ru/device, the page polls a status
  endpoint and self-closes on success.

Cache fast-path:
- If a valid cached ``x_token`` is provided, we skip Device Flow entirely
  and call ``refresh_passport_cookies`` directly. On any failure during
  refresh we fall back to a fresh Device Flow.

Body is ported verbatim from the deleted ``provider/auto_skill.py:_default_authenticator``;
the only structural change is the wrapper — async-iterator → ``@asynccontextmanager``
to match the lib's :data:`AuthenticatorCM` Protocol.

Pattern originally adapted from ``ma-provider-yandex-station/provider/auth.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import aiohttp
    from ya_dialogs_api import AuthenticatorCM

    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

# Hard cap on how long we'll wait for the user to enter the code.
DEVICE_FLOW_TIMEOUT_SECONDS = 300.0

_DEVICE_CODE_PAGE_PATH = "/yandex_smarthome/device_code"
# Keep the intermediate HTML page alive long enough for the browser to
# observe the done/failed state transition. The page polls every 2s
# (see _build_device_code_page → setTimeout(pollStatus, 2000)), so we
# need at least one full poll interval + RTT margin after flipping the
# server-side state, otherwise the route gets unregistered before the
# page can fetch the final state and self-close.
_POST_AUTH_GRACE_SECONDS = 3
# Server-suggested interval from Yandex is 5s (RFC 8628) but after the
# user has confirmed the code we want to detect it promptly; 2s is the
# RFC-recommended minimum. If Yandex returns SLOW_DOWN, ya-passport-auth
# bumps the interval automatically.
_DEVICE_FLOW_POLL_INTERVAL = 2.0
_SAFE_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def make_authenticator(  # noqa: PLR0915
    *,
    mass: MusicAssistant,
    session_id: str,
    timeout: float = DEVICE_FLOW_TIMEOUT_SECONDS,
    cached_x_token: str | None = None,
    on_token_obtained: Callable[[str], None] | None = None,
) -> AuthenticatorCM:
    """Build an :data:`AuthenticatorCM` for ``ya_dialogs_api.auto_create_skill``.

    The returned no-arg callable produces an ``aiohttp.ClientSession``
    context manager. On ``__aenter__`` it either:

    1. Reuses ``cached_x_token`` (``refresh_passport_cookies`` fast-path), or
    2. Runs the full Device Flow: registers an HTML activation page on
       ``mass.webserver``, opens it via ``AuthenticationHelper(mass, session_id)``,
       polls Yandex Passport, then refreshes passport cookies.

    After a successful Device Flow, ``on_token_obtained(x_token_str)`` is
    called so the caller can persist the new token for the next run.
    Callback failures are logged but never break authentication.

    :param mass: MusicAssistant runtime — used for ``mass.webserver``
        route registration and ``AuthenticationHelper`` popup
        management.
    :param session_id: Frontend-supplied session id (matches what
        ``AuthenticationHelper`` listens on for popup open/close).
        Must be safe for URL paths.
    :param timeout: Hard cap on Device Flow polling (seconds). Default
        5 min.
    :param cached_x_token: Optional Yandex Passport ``x_token`` from a
        prior Device Flow. If still valid, skips Device Flow entirely.
    :param on_token_obtained: Optional callback invoked with the fresh
        ``x_token`` (plain ``str``, unwrapped from ``SecretStr``)
        after a successful Device Flow. Use to persist into MA config
        so the next run can use the cache.
    :raises ValueError: ``session_id`` doesn't match the safe
        character set.
    """
    if not _SAFE_SESSION_ID_RE.match(session_id):
        msg = "invalid session_id for device authentication"
        raise ValueError(msg)

    @asynccontextmanager
    async def _cm() -> AsyncIterator[aiohttp.ClientSession]:  # noqa: PLR0915
        # Imports kept inline so the module can be imported without MA in test envs.
        from aiohttp import web  # noqa: PLC0415
        from ya_passport_auth import ClientConfig, PassportClient  # noqa: PLC0415
        from ya_passport_auth.config import DEFAULT_ALLOWED_HOSTS  # noqa: PLC0415
        from ya_passport_auth.credentials import SecretStr as PpSecretStr  # noqa: PLC0415

        from music_assistant.helpers.auth import AuthenticationHelper  # noqa: PLC0415

        allowed = DEFAULT_ALLOWED_HOSTS | frozenset({"dialogs.yandex.ru"})
        config = ClientConfig(allowed_hosts=allowed)

        async with PassportClient.create(config=config) as client:
            # Cache fast-path: try cached x_token first. If the token is
            # still valid Yandex returns fresh session cookies and we skip
            # Device Flow.
            if cached_x_token:
                try:
                    await client.refresh_passport_cookies(PpSecretStr(cached_x_token))
                    _LOGGER.info(
                        "auto-skill: reused cached Yandex Passport x_token (no Device Flow needed)"
                    )
                    yield client._session
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.info(
                        "auto-skill: cached x_token rejected (%s) — "
                        "falling back to fresh Device Flow",
                        exc,
                    )

            # Device Flow path
            device_session = await client.start_device_login()
            # Don't log user_code — it's a time-limited credential and writing
            # it to shared log backends would leak access.
            _LOGGER.info(
                "device flow started — verification_url=%s",
                device_session.verification_url,
            )

            page_path = f"{_DEVICE_CODE_PAGE_PATH}/{session_id}"
            status_path = f"{page_path}/status"
            base_url = str(mass.webserver.base_url).rstrip("/")
            status_url = f"{base_url}{status_path}"
            page_url = f"{base_url}{page_path}"
            state = {"value": "pending"}

            page_html = _build_device_code_page(
                device_session.user_code,
                device_session.verification_url,
                status_url,
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
                return web.json_response(
                    {"state": state["value"]},
                    headers={"Cache-Control": "no-store"},
                )

            mass.webserver.register_dynamic_route(page_path, _serve_page, "GET")
            mass.webserver.register_dynamic_route(status_path, _serve_status, "GET")
            _LOGGER.warning(
                "auto-skill: device-code popup URL %s (path=%s) "
                "— if the popup does not open or points at an unreachable "
                "address, open the path directly in your browser (the page "
                "displays the user_code) or fix Settings → Core → Webserver "
                "→ Base URL",
                page_url,
                page_path,
            )
            try:
                async with AuthenticationHelper(mass, session_id) as auth_helper:
                    auth_helper.send_url(page_url)
                    try:
                        creds = await client.poll_device_until_confirmed(
                            device_session,
                            total_timeout=timeout,
                            poll_interval=_DEVICE_FLOW_POLL_INTERVAL,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        state["value"] = "failed"
                        await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
                        raise
                    state["value"] = "done"
                    await asyncio.sleep(_POST_AUTH_GRACE_SECONDS)
            finally:
                mass.webserver.unregister_dynamic_route(page_path, "GET")
                mass.webserver.unregister_dynamic_route(status_path, "GET")

            await client.refresh_passport_cookies(creds.x_token)

            # Persist the new x_token so subsequent auto-create runs can skip
            # Device Flow. Best-effort: a callback failure must not break auth.
            # Unwrap SecretStr → str so the callback can store it via the MA
            # config plumbing (SECURE_STRING serialiser expects a plain str).
            if on_token_obtained is not None:
                try:
                    on_token_obtained(creds.x_token.get_secret())
                except Exception:
                    _LOGGER.exception(
                        "auto-skill: on_token_obtained callback failed; x_token will not be cached"
                    )

            yield client._session

    return _cm


def _build_device_code_page(user_code: str, verification_url: str, status_url: str) -> str:
    """Render the HTML page shown during Device Flow login.

    Yandex's ya.ru/device page does not pre-fill from query params and
    strips them on redirect-to-login, so the only reliable way to show
    the code is to host our own page in MA's webserver that displays
    the code prominently and opens ya.ru/device in a new tab.

    Pattern copied from ``ma-provider-yandex-station/provider/auth.py``.
    """
    import html  # noqa: PLC0415

    safe_code = html.escape(user_code)
    safe_url = html.escape(verification_url, quote=True)
    safe_status_url = json.dumps(status_url).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Yandex Smart Home — Device Code</title>
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
        h1 {{ margin: 0 0 .5rem; font-size: 1.25rem; }}
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
    </style>
</head>
<body>
    <div class="card" id="card">
        <h1>Authorise Music Assistant for skill creation</h1>
        <p>Open the link below, log in to your Yandex account, and enter this code.</p>
        <div id="code">{safe_code}</div>
        <div>
            <button id="copy" type="button">Copy code</button>
        </div>
        <a class="btn" href="{safe_url}" target="_blank" rel="noopener">Continue to Yandex</a>
    </div>
    <script>
        const copyButton = document.getElementById('copy');
        const codeElement = document.getElementById('code');
        const card = document.getElementById('card');
        const statusUrl = {safe_status_url};

        function selectCodeForManualCopy() {{
            if (!codeElement) return;
            const selection = window.getSelection();
            if (!selection) return;
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
            card.innerHTML = '<h1>' + title + '</h1><p>' + message + '</p>';
        }}

        async function pollStatus() {{
            try {{
                const r = await fetch(statusUrl, {{ cache: 'no-store' }});
                if (r.ok) {{
                    const data = await r.json();
                    if (data.state === 'done') {{
                        showResult('Authorisation successful', 'You can close this window.');
                        setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, 300);
                        return;
                    }}
                    if (data.state === 'failed') {{
                        showResult(
                            'Authorisation failed',
                            'Return to Music Assistant and try again.'
                        );
                        return;
                    }}
                }}
            }} catch (e) {{ /* network hiccup — retry */ }}
            setTimeout(pollStatus, 2000);
        }}
        setTimeout(pollStatus, 2000);
    </script>
</body>
</html>"""
