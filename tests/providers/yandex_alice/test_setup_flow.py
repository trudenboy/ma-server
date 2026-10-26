"""Tests for the Yandex Alice interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType
from music_assistant_models.errors import InvalidDataError, LoginFailed
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState, load_artifacts
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.exceptions import InvalidCredentialsError
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import (
    AbortFlow,
    SetupFlowContext,
    SetupSession,
    StepExpiredError,
)
from music_assistant.providers.yandex_alice import setup_flow as flow
from music_assistant.providers.yandex_alice.auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
)
from music_assistant.providers.yandex_alice.auto_update import AutoUpdateOutcome
from music_assistant.providers.yandex_alice.constants import (
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_YM_INSTANCE,
)

BASE_URL = "https://ma.example.org"
CREDS = Credentials(x_token=SecretStr("x-own"), display_login="alice.user")


def _done(skill_id: str = "sk-1") -> AutoCreateOutcome:
    return AutoCreateOutcome(
        artifacts=SkillCreationArtifacts(
            state=SkillCreationState.DONE, skill_id=skill_id, last_known_name="Music Assistant"
        ),
        x_token=None,
        user_message="Skill created.",
        stage=LocalAutoCreateStage.DONE,
    )


def _duplicate() -> AutoCreateOutcome:
    return AutoCreateOutcome(
        artifacts=SkillCreationArtifacts(),
        x_token=None,
        user_message="A skill with this name exists.",
        stage=LocalAutoCreateStage.DUPLICATE_DETECTED,
        pending_duplicate_skill_id="sk-old",
        pending_duplicate_skill_name="Music Assistant",
    )


def _failed(message: str = "Yandex said no", *, x_token: str | None = None) -> AutoCreateOutcome:
    return AutoCreateOutcome(
        artifacts=SkillCreationArtifacts(state=SkillCreationState.FAILED, last_error=message),
        x_token=x_token,
        user_message=message,
        stage=LocalAutoCreateStage.FAILED,
    )


class _FakeSession:
    """Scripted SetupSession: answers forms in order and runs progress awaitables."""

    def __init__(
        self,
        responses: list[tuple[str, dict[str, Any]]],
        *,
        kind: str = "setup",
        reason: str = "user",
        setup_data: dict[str, Any] | None = None,
        step_errors: dict[str, list[BaseException]] | None = None,
    ) -> None:
        """Queue the form answers and the errors to raise from given progress steps."""
        self.responses = list(responses)
        self.context = SimpleNamespace(kind=kind, reason=reason, setup_data=setup_data or {})
        self.mass = mock.MagicMock()
        self.step_errors = step_errors or {}
        self.steps: list[str] = []
        self.form_errors: dict[str, list[dict[str, Any] | None]] = {}
        self.form_params: dict[str, list[list[str] | None]] = {}
        self.form_entries: dict[str, list[Any]] = {}
        self.external: list[tuple[str, list[str] | None]] = []
        self.images: list[str | None] = []
        self.finished: dict[str, Any] | None = None

    async def form(
        self,
        entries: list[Any],
        step_id: str = "user",
        errors: dict[str, Any] | None = None,
        last_step: bool | None = None,
        expires_in: float | None = None,
        translation_params: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the next scripted answer, checking it targets this step."""
        self.steps.append(step_id)
        self.form_errors.setdefault(step_id, []).append(errors)
        self.form_params.setdefault(step_id, []).append(translation_params)
        self.form_entries[step_id] = entries
        assert self.responses, f"unexpected form {step_id!r}"
        expected, values = self.responses.pop(0)
        assert expected == step_id, f"expected form {expected!r}, got {step_id!r}"
        return values

    async def progress_until(
        self,
        awaitable: Awaitable[Any],
        step_id: str,
        text: str | None = None,
        image: str | None = None,
        expires_in: float | None = None,
    ) -> Any:
        """Run the awaitable, or raise a queued error for this step."""
        self.steps.append(step_id)
        self.images.append(image)
        return await self._run(step_id, awaitable)

    async def external_until(
        self,
        awaitable: Awaitable[Any],
        url: str,
        step_id: str = "auth",
        expires_in: float | None = None,
        translation_params: list[str] | None = None,
    ) -> Any:
        """Record the external URL step and run the awaitable."""
        self.steps.append(step_id)
        self.external.append((url, translation_params))
        return await self._run(step_id, awaitable)

    async def finish(self, values: dict[str, Any]) -> dict[str, str]:
        """Record the finished values."""
        self.finished = dict(values)
        return {"instance_id": "yandex_alice"}

    async def _run(self, step_id: str, awaitable: Awaitable[Any]) -> Any:
        if queued := self.step_errors.get(step_id):
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise queued.pop(0)
        return await awaitable


class _FakeClient:
    """Canned PassportClient for device and QR logins."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.device_logins = 0

    async def start_device_login(self, **_kwargs: Any) -> DeviceCodeSession:
        self.device_logins += 1
        return DeviceCodeSession(
            device_code=SecretStr("dc"),
            user_code=f"CODE-{self.device_logins}",
            verification_url="https://ya.ru/device",
            expires_in=300,
            interval=5,
        )

    async def poll_device_until_confirmed(self, _session: Any, **_kwargs: Any) -> Credentials:
        if self.error is not None:
            raise self.error
        return CREDS

    async def start_qr_login(self) -> QrSession:
        return QrSession(track_id="t", csrf_token="c", qr_url="https://passport.yandex.ru/qr/abc")

    async def poll_qr_until_confirmed(self, _qr: Any, **_kwargs: Any) -> Credentials:
        return CREDS


@pytest.fixture
def client() -> _FakeClient:
    """Provide the canned Passport client the patched PassportClient returns."""
    return _FakeClient()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> SimpleNamespace:
    """Patch every network-facing collaborator of the flow."""
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=client)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    passport = mock.MagicMock()
    passport.create.return_value = ctx
    ns = SimpleNamespace(
        create=mock.AsyncMock(return_value=_done()),
        adopt=mock.AsyncMock(return_value=_done("sk-old")),
        recreate=mock.AsyncMock(return_value=_done("sk-new")),
        update=mock.AsyncMock(),
        status=mock.AsyncMock(return_value="on_air"),
        cookies=mock.AsyncMock(return_value=CREDS),
        borrowed=mock.MagicMock(return_value="x-borrowed"),
    )
    monkeypatch.setattr(flow, "PassportClient", passport)
    monkeypatch.setattr(flow, "run_create_skill", ns.create)
    monkeypatch.setattr(flow, "adopt_existing_skill", ns.adopt)
    monkeypatch.setattr(flow, "delete_existing_skill_then_recreate", ns.recreate)
    monkeypatch.setattr(flow, "run_auto_update", ns.update)
    monkeypatch.setattr(flow, "fetch_skill_publication_status", ns.status)
    monkeypatch.setattr(flow, "login_with_cookies", ns.cookies)
    monkeypatch.setattr(flow, "borrowed_x_token", ns.borrowed)
    monkeypatch.setattr(flow, "SkillManifestProvider", lambda _mass: mock.MagicMock())
    monkeypatch.setattr(flow, "try_detect_public_https_url", lambda _mass: BASE_URL)
    monkeypatch.setattr(
        flow, "list_yandex_music_instances", lambda _mass: [("ym-1", "Main account")]
    )
    return ns


def _skill(name: str = "Music Assistant", url: str = BASE_URL) -> tuple[str, dict[str, Any]]:
    return (
        "skill",
        {
            flow.CONF_SKILL_MODE: flow.SKILL_MODE_CREATE,
            CONF_DIALOG_SKILL_NAME: name,
            CONF_EXTERNAL_BASE_URL: url,
            CONF_DIALOG_SKILL_ID: "",
        },
    )


OWN = ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
DEVICE = ("method", {flow.CONF_METHOD: flow.METHOD_DEVICE})


# ---------------------------------------------------------------------------
# Account and sign-in
# ---------------------------------------------------------------------------


async def test_borrow_skips_sign_in(api: SimpleNamespace) -> None:
    """Borrowing uses the linked instance's x_token and stores no own token."""
    session = _FakeSession([("user", {CONF_YM_INSTANCE: "ym-1"}), _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert "method" not in session.steps
    assert api.create.await_args.kwargs["cached_x_token"] == "x-borrowed"
    assert session.finished is not None
    assert session.finished[CONF_YM_INSTANCE] == "ym-1"
    assert session.finished[CONF_AUTH_X_TOKEN] is None


async def test_borrow_without_token_shows_error(api: SimpleNamespace) -> None:
    """A linked instance without a session token keeps the user on the account step."""
    api.borrowed.side_effect = [LoginFailed("no token"), "x-borrowed"]
    session = _FakeSession(
        [("user", {CONF_YM_INSTANCE: "ym-1"}), ("user", {CONF_YM_INSTANCE: "ym-1"}), _skill()]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["user"][1] == {"base": "no token"}


async def test_device_login_shows_code_and_stores_token(api: SimpleNamespace) -> None:
    """The device step opens ya.ru/device with the code; the x_token and login are stored."""
    session = _FakeSession([OWN, DEVICE, _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.external == [("https://ya.ru/device", ["CODE-1"])]
    assert api.create.await_args.kwargs["cached_x_token"] == "x-own"
    assert session.finished is not None
    assert session.finished[CONF_YM_INSTANCE] == BORROW_SOURCE_OWN
    assert session.finished[CONF_AUTH_X_TOKEN] == "x-own"
    assert session.finished[CONF_AUTH_USER_NAME] == "alice.user"


@pytest.mark.usefixtures("api")
async def test_device_code_refreshes_on_expiry(client: _FakeClient) -> None:
    """An expired device code is replaced by a fresh one in place."""
    session = _FakeSession(
        [OWN, DEVICE, _skill()], step_errors={"device_login": [StepExpiredError()]}
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert client.device_logins == 2
    assert session.external[-1] == ("https://ya.ru/device", ["CODE-2"])


@pytest.mark.usefixtures("api")
async def test_denied_login_aborts(client: _FakeClient) -> None:
    """A login denied in the Yandex app aborts the flow with a localized reason."""
    client.error = InvalidCredentialsError("denied")
    session = _FakeSession([OWN, DEVICE])
    with pytest.raises(AbortFlow) as err:
        await flow.run_setup(session)  # type: ignore[arg-type]
    assert err.value.reason == "login_denied"


@pytest.mark.usefixtures("api")
async def test_qr_login_shows_qr_image() -> None:
    """QR login renders the code as an inline SVG image."""
    session = _FakeSession([OWN, ("method", {flow.CONF_METHOD: flow.METHOD_QR}), _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    images = [image for image in session.images if image]
    assert images
    assert images[0].startswith("data:image/svg+xml")
    assert session.finished is not None
    assert session.finished[CONF_AUTH_X_TOKEN] == "x-own"


async def test_cookie_login_retries_on_bad_cookies(api: SimpleNamespace) -> None:
    """Rejected cookies re-show the cookies step with the error."""
    api.cookies.side_effect = [InvalidDataError("Empty cookies string"), CREDS]
    session = _FakeSession(
        [
            OWN,
            ("method", {flow.CONF_METHOD: flow.METHOD_COOKIES}),
            ("cookies", {flow.CONF_COOKIES: "bad"}),
            ("cookies", {flow.CONF_COOKIES: "Session_id=1"}),
            _skill(),
        ]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["cookies"][1] == {"base": "Empty cookies string"}
    assert session.finished is not None
    assert session.finished[CONF_AUTH_X_TOKEN] == "x-own"


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


async def test_create_skill_stores_skill_data(api: SimpleNamespace) -> None:
    """A created skill finishes with id, name, URL, secret, artifacts and status."""
    session = _FakeSession([OWN, DEVICE, _skill(name="Кухонный DJ")])
    await flow.run_setup(session)  # type: ignore[arg-type]
    kwargs = api.create.await_args.kwargs
    data = session.finished
    assert data is not None
    assert kwargs["skill_name"] == "Кухонный DJ"
    assert kwargs["backend_uri"].startswith(f"{BASE_URL}/api/yandex_dialogs/webhook/")
    assert kwargs["backend_uri"].endswith(data[CONF_DIALOG_WEBHOOK_SECRET])
    assert data[CONF_DIALOG_SKILL_ID] == "sk-1"
    assert data[CONF_DIALOG_SKILL_NAME] == "Кухонный DJ"
    assert data[CONF_EXTERNAL_BASE_URL] == BASE_URL
    assert data[CONF_DIALOG_PUBLICATION_STATUS] == "on_air"
    assert load_artifacts(data[CONF_DIALOG_AUTO_CREATE_ARTIFACTS]).skill_id == "sk-1"


@pytest.mark.usefixtures("api")
async def test_skill_step_prefills_detected_base_url() -> None:
    """The base URL field starts with MA's detected public HTTPS URL."""
    session = _FakeSession([OWN, DEVICE, _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    entry = next(e for e in session.form_entries["skill"] if e.key == CONF_EXTERNAL_BASE_URL)
    assert entry.default_value == BASE_URL


async def test_invalid_base_url_is_rejected_before_api_call(api: SimpleNamespace) -> None:
    """A non-public URL re-shows the skill step and never reaches Yandex."""
    session = _FakeSession([OWN, DEVICE, _skill(url="http://192.168.1.5:8095"), _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["skill"][1] == {CONF_EXTERNAL_BASE_URL: "invalid_base_url"}
    assert api.create.await_count == 1


async def test_existing_skill_shows_webhook_url(api: SimpleNamespace) -> None:
    """The existing-skill path stores the id and shows the webhook URL; no API call."""
    session = _FakeSession(
        [
            OWN,
            DEVICE,
            (
                "skill",
                {
                    flow.CONF_SKILL_MODE: flow.SKILL_MODE_EXISTING,
                    CONF_DIALOG_SKILL_NAME: "Music Assistant",
                    CONF_EXTERNAL_BASE_URL: BASE_URL,
                    CONF_DIALOG_SKILL_ID: "sk-manual",
                },
            ),
            ("webhook_info", {}),
        ]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    api.create.assert_not_awaited()
    data = session.finished
    assert data is not None
    assert data[CONF_DIALOG_SKILL_ID] == "sk-manual"
    params = session.form_params["webhook_info"][0]
    assert params is not None
    assert params[0] == f"{BASE_URL}/api/yandex_dialogs/webhook/{data[CONF_DIALOG_WEBHOOK_SECRET]}"


@pytest.mark.usefixtures("api")
async def test_existing_skill_requires_skill_id() -> None:
    """The manual path does not finish without a skill id."""
    existing = {
        flow.CONF_SKILL_MODE: flow.SKILL_MODE_EXISTING,
        CONF_DIALOG_SKILL_NAME: "Music Assistant",
        CONF_EXTERNAL_BASE_URL: BASE_URL,
    }
    session = _FakeSession(
        [
            OWN,
            DEVICE,
            ("skill", {**existing, CONF_DIALOG_SKILL_ID: ""}),
            ("skill", {**existing, CONF_DIALOG_SKILL_ID: "sk-manual"}),
            ("webhook_info", {}),
        ]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["skill"][1] == {CONF_DIALOG_SKILL_ID: "skill_id_required"}


@pytest.mark.parametrize(
    ("choice", "skill_id"),
    [("adopt", "sk-old"), ("recreate", "sk-new")],
)
async def test_duplicate_resolution(api: SimpleNamespace, choice: str, skill_id: str) -> None:
    """A duplicate name can be adopted or recreated."""
    api.create.return_value = _duplicate()
    session = _FakeSession(
        [OWN, DEVICE, _skill(), ("duplicate", {flow.CONF_DUPLICATE_CHOICE: choice})]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_params["duplicate"][0] == ["Music Assistant", "sk-old"]
    resolver = api.adopt if choice == "adopt" else api.recreate
    assert resolver.await_args.kwargs["existing_skill_id"] == "sk-old"
    assert session.finished is not None
    assert session.finished[CONF_DIALOG_SKILL_ID] == skill_id


async def test_duplicate_rename_returns_to_skill_step(api: SimpleNamespace) -> None:
    """Choosing Rename goes back to the skill step for another name."""
    api.create.side_effect = [_duplicate(), _done("sk-2")]
    session = _FakeSession(
        [
            OWN,
            DEVICE,
            _skill(),
            ("duplicate", {flow.CONF_DUPLICATE_CHOICE: flow.DUPLICATE_RENAME}),
            _skill(name="Музыка дома"),
        ]
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert api.create.await_args.kwargs["skill_name"] == "Музыка дома"
    assert session.finished is not None
    assert session.finished[CONF_DIALOG_SKILL_ID] == "sk-2"


async def test_failed_creation_retries_from_skill_step(api: SimpleNamespace) -> None:
    """A failed pipeline shows its message on the skill step and resumes its artifacts."""
    failed = _failed()
    api.create.side_effect = [failed, _done()]
    session = _FakeSession([OWN, DEVICE, _skill(), _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["skill"][1] == {"base": "Yandex said no"}
    assert api.create.await_args.kwargs["artifacts"] == failed.artifacts


async def test_rejected_token_during_creation_asks_to_sign_in_again(
    api: SimpleNamespace,
) -> None:
    """When Yandex rejects the x_token the user signs in again and creation resumes."""
    api.create.side_effect = [_failed("expired", x_token=""), _done()]
    session = _FakeSession([OWN, DEVICE, _skill(), OWN, DEVICE, _skill()])
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.steps.count("user") == 2
    assert session.finished is not None
    assert session.finished[CONF_DIALOG_SKILL_ID] == "sk-1"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------

EXISTING_DATA: dict[str, Any] = {
    CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
    CONF_AUTH_X_TOKEN: "x-old",
    CONF_AUTH_USER_NAME: "alice.user",
    CONF_DIALOG_SKILL_ID: "sk-1",
    CONF_DIALOG_SKILL_NAME: "Music Assistant",
    CONF_DIALOG_WEBHOOK_SECRET: "old-secret",
    CONF_EXTERNAL_BASE_URL: BASE_URL,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS: "",
}


def _updated(**kwargs: Any) -> AutoUpdateOutcome:
    return AutoUpdateOutcome(
        artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        x_token=None,
        user_message="Skill updated.",
        **kwargs,
    )


def _reconfigure(action: str) -> tuple[str, dict[str, Any]]:
    return ("reconfigure", {flow.CONF_RECONFIGURE: action})


async def test_reconfigure_sign_in_keeps_skill(api: SimpleNamespace) -> None:
    """Signing in again replaces only the account data."""
    session = _FakeSession(
        [_reconfigure(flow.RECONFIGURE_SIGN_IN), OWN, DEVICE],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    data = session.finished
    assert data is not None
    assert data[CONF_AUTH_X_TOKEN] == "x-own"
    assert data[CONF_DIALOG_SKILL_ID] == "sk-1"
    assert data[CONF_DIALOG_WEBHOOK_SECRET] == "old-secret"
    api.create.assert_not_awaited()


@pytest.mark.usefixtures("api")
async def test_reconfigure_for_auth_error_opens_sign_in() -> None:
    """A reconfigure started by an auth error skips the menu."""
    session = _FakeSession(
        [OWN, DEVICE], kind="reconfigure", reason="auth", setup_data=dict(EXISTING_DATA)
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert "reconfigure" not in session.steps
    assert session.finished is not None


async def test_reconfigure_edit_skill_pushes_to_yandex(api: SimpleNamespace) -> None:
    """Editing the skill sends name, extra phrases and voice and stores them."""
    api.update.return_value = _updated()
    session = _FakeSession(
        [
            _reconfigure(flow.RECONFIGURE_EDIT),
            (
                "edit_skill",
                {
                    CONF_DIALOG_SKILL_NAME: "Музыка",
                    CONF_DIALOG_ACTIVATION_PHRASE_2: "домашняя музыка",
                    "dialog_activation_phrase_3": "",
                    "dialog_activation_phrase_4": "",
                    CONF_DIALOG_SKILL_VOICE: "shitova.us",
                },
            ),
        ],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    kwargs = api.update.await_args.kwargs
    assert kwargs["cached_x_token"] == "x-old"
    assert kwargs["activation_phrases"] == ["Музыка", "домашняя музыка"]
    assert kwargs["voice"] == "shitova.us"
    data = session.finished
    assert data is not None
    assert data[CONF_DIALOG_SKILL_NAME] == "Музыка"
    assert data[CONF_DIALOG_ACTIVATION_PHRASE_2] == "домашняя музыка"


async def test_reconfigure_edit_failure_shows_error(api: SimpleNamespace) -> None:
    """A rejected update re-shows the edit step with the message."""
    api.update.side_effect = [
        AutoUpdateOutcome(
            artifacts=SkillCreationArtifacts(state=SkillCreationState.FAILED),
            x_token=None,
            user_message="Name taken",
        ),
        _updated(),
    ]
    edit = (
        "edit_skill",
        {CONF_DIALOG_SKILL_NAME: "Музыка", CONF_DIALOG_SKILL_VOICE: "shitova.us"},
    )
    session = _FakeSession(
        [_reconfigure(flow.RECONFIGURE_EDIT), edit, edit],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.form_errors["edit_skill"][1] == {"base": "Name taken"}


async def test_reconfigure_new_webhook_secret_reregisters_backend(api: SimpleNamespace) -> None:
    """A regenerated secret is pushed to Yandex as the new backend URL."""
    api.update.return_value = _updated()
    session = _FakeSession(
        [
            _reconfigure(flow.RECONFIGURE_WEBHOOK),
            ("webhook", {CONF_EXTERNAL_BASE_URL: BASE_URL, flow.CONF_NEW_SECRET: True}),
        ],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    data = session.finished
    assert data is not None
    assert data[CONF_DIALOG_WEBHOOK_SECRET] != "old-secret"
    assert api.update.await_args.kwargs["backend_uri"].endswith(data[CONF_DIALOG_WEBHOOK_SECRET])


async def test_reconfigure_edit_asks_for_missing_base_url(api: SimpleNamespace) -> None:
    """Editing a skill whose stored address is unusable asks for the address first."""
    api.update.return_value = _updated()
    session = _FakeSession(
        [
            _reconfigure(flow.RECONFIGURE_EDIT),
            ("webhook", {CONF_EXTERNAL_BASE_URL: BASE_URL}),
            ("edit_skill", {CONF_DIALOG_SKILL_NAME: "Music Assistant"}),
        ],
        kind="reconfigure",
        setup_data={**EXISTING_DATA, CONF_EXTERNAL_BASE_URL: ""},
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert api.update.await_args.kwargs["backend_uri"].startswith(BASE_URL)
    assert session.finished is not None
    assert session.finished[CONF_EXTERNAL_BASE_URL] == BASE_URL


async def test_reconfigure_webhook_with_rejected_token_signs_in_again(
    api: SimpleNamespace,
) -> None:
    """A rejected token while re-registering the webhook asks to sign in, then retries."""
    rejected = AutoUpdateOutcome(
        artifacts=SkillCreationArtifacts(state=SkillCreationState.FAILED, skill_id="sk-1"),
        x_token="",
        user_message="expired",
    )
    api.update.side_effect = [rejected, _updated()]
    session = _FakeSession(
        [
            _reconfigure(flow.RECONFIGURE_WEBHOOK),
            ("webhook", {CONF_EXTERNAL_BASE_URL: BASE_URL}),
            OWN,
            DEVICE,
        ],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert api.update.await_count == 2
    assert api.update.await_args.kwargs["cached_x_token"] == "x-own"
    assert session.finished is not None


@pytest.mark.usefixtures("api")
async def test_switching_to_existing_skill_clears_stale_status() -> None:
    """Registering a hand-made skill forgets the previous skill's publication status."""
    session = _FakeSession(
        [
            _reconfigure(flow.RECONFIGURE_RECREATE),
            (
                "skill",
                {
                    flow.CONF_SKILL_MODE: flow.SKILL_MODE_EXISTING,
                    CONF_DIALOG_SKILL_NAME: "Music Assistant",
                    CONF_EXTERNAL_BASE_URL: BASE_URL,
                    CONF_DIALOG_SKILL_ID: "sk-manual",
                },
            ),
            ("webhook_info", {}),
        ],
        kind="reconfigure",
        setup_data={**EXISTING_DATA, CONF_DIALOG_PUBLICATION_STATUS: "on_air"},
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert session.finished is not None
    assert session.finished[CONF_DIALOG_PUBLICATION_STATUS] == ""


async def test_reconfigure_recreate_runs_skill_step(api: SimpleNamespace) -> None:
    """Creating the skill again goes through the skill step with the stored account."""
    api.create.return_value = _done("sk-9")
    session = _FakeSession(
        [_reconfigure(flow.RECONFIGURE_RECREATE), _skill()],
        kind="reconfigure",
        setup_data=dict(EXISTING_DATA),
    )
    await flow.run_setup(session)  # type: ignore[arg-type]
    assert api.create.await_args.kwargs["cached_x_token"] == "x-old"
    assert session.finished is not None
    assert session.finished[CONF_DIALOG_SKILL_ID] == "sk-9"


# ---------------------------------------------------------------------------
# Real SetupSession
# ---------------------------------------------------------------------------


async def _wait_form(session: SetupSession, step_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step and step.type == FlowStepType.FORM and step.step_id == step_id:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"form {step_id!r} not shown")


@pytest.mark.usefixtures("api")
async def test_flow_runs_on_real_setup_session() -> None:
    """The flow drives MA's real SetupSession from the first form to finish."""
    collected: dict[str, Any] = {}

    async def _finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_alice"}

    context = SetupFlowContext(kind="setup", reason="user", domain="yandex_alice")
    session = SetupSession(mock.Mock(), "flow-test", context, _finish)
    task = asyncio.create_task(flow.run_setup(session))
    await _wait_form(session, "user")
    session.handle_submit({CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
    await _wait_form(session, "method")
    session.handle_submit({flow.CONF_METHOD: flow.METHOD_DEVICE})
    await _wait_form(session, "skill")
    session.handle_submit(
        {
            flow.CONF_SKILL_MODE: flow.SKILL_MODE_CREATE,
            CONF_DIALOG_SKILL_NAME: "Music Assistant",
            CONF_EXTERNAL_BASE_URL: BASE_URL,
        }
    )
    await asyncio.wait_for(task, 5)
    assert collected[CONF_DIALOG_SKILL_ID] == "sk-1"
    assert collected[CONF_AUTH_X_TOKEN] == "x-own"
