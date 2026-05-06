# ruff: noqa: RUF001
"""Tests for provider/dialogs_control.py — playback control NLU + executor."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.yandex_alice.dialogs_control import (
    ParsedControl,
    _plural_ru,
    control_confirmation,
    execute_control,
    format_list_players,
    parse_control,
)


class TestParseControl:
    """Table-driven tests for parse_control across all action families."""

    @pytest.mark.parametrize(
        ("phrase", "expected_action", "expected_value", "expected_hint"),
        [
            # pause
            ("пауза", "pause", None, None),
            ("на паузу", "pause", None, None),
            ("поставь на паузу", "pause", None, None),
            ("останови музыку", "pause", None, None),
            ("пауза на кухне", "pause", None, "кухне"),
            ("поставь на паузу на кухне", "pause", None, "кухне"),
            # resume
            ("продолжи", "resume", None, None),
            ("продолжить", "resume", None, None),
            ("включи снова", "resume", None, None),
            ("возобнови", "resume", None, None),
            # stop
            ("стоп", "stop", None, None),
            ("останови", "stop", None, None),
            ("выключи", "stop", None, None),
            ("выключи музыку", "stop", None, None),
            ("стоп на спальне", "stop", None, "спальне"),
            # next
            ("следующая", "next", None, None),
            ("следующий трек", "next", None, None),
            ("дальше", "next", None, None),
            ("переключи", "next", None, None),
            # previous
            ("предыдущая", "previous", None, None),
            ("предыдущий трек", "previous", None, None),
            ("назад", "previous", None, None),
            ("вернись", "previous", None, None),
            # volume relative
            ("громче", "volume_up", None, None),
            ("сделай громче", "volume_up", None, None),
            ("прибавь", "volume_up", None, None),
            ("прибавь громкость", "volume_up", None, None),
            ("тише", "volume_down", None, None),
            ("сделай тише", "volume_down", None, None),
            ("убавь", "volume_down", None, None),
            ("убавь громкость", "volume_down", None, None),
            ("громче на кухне", "volume_up", None, "кухне"),
            # volume set
            ("громкость 50", "volume_set", 50, None),
            ("громкость на 30", "volume_set", 30, None),
            ("громкость на 30 процентов", "volume_set", 30, None),
            ("сделай громкость 75", "volume_set", 75, None),
            ("громкость 50 на кухне", "volume_set", 50, "кухне"),
            # volume set clamping
            ("громкость 200", "volume_set", 100, None),
            # mute / unmute
            ("приглуши", "mute", None, None),
            ("выключи звук", "mute", None, None),
            ("беззвучно", "mute", None, None),
            ("включи звук", "unmute", None, None),
            ("сделай звук", "unmute", None, None),
            # list_players (no player_hint, no value)
            ("сколько колонок", "list_players", None, None),
            ("сколько колонок ты видишь", "list_players", None, None),
            ("сколько колонок ты знаешь", "list_players", None, None),
            ("какие колонки", "list_players", None, None),
            ("какие колонки видишь", "list_players", None, None),
            ("какие колонки ты видишь", "list_players", None, None),
            ("какие колонки есть", "list_players", None, None),
            ("какие у тебя колонки", "list_players", None, None),
            ("перечисли колонки", "list_players", None, None),
            ("список колонок", "list_players", None, None),
            ("покажи колонки", "list_players", None, None),
            ("назови колонки", "list_players", None, None),
            # forget_player — clears the saved default-player so the
            # next play command without a hint asks again.
            ("забудь колонку", "forget_player", None, None),
            ("сбрось колонку", "forget_player", None, None),
            ("забудь плеер", "forget_player", None, None),
            ("забудь выбор", "forget_player", None, None),
            ("сбрось выбор", "forget_player", None, None),
            ("выбери колонку заново", "forget_player", None, None),
            ("поменяй колонку", "forget_player", None, None),
            ("сменить колонку", "forget_player", None, None),
            # now_playing — info query
            ("что играет", "now_playing", None, None),
            ("что сейчас играет", "now_playing", None, None),
            ("что слушаем", "now_playing", None, None),
            ("что мы слушаем", "now_playing", None, None),
            ("что за песня", "now_playing", None, None),
            ("что за трек", "now_playing", None, None),
            ("какой трек", "now_playing", None, None),
            ("какой сейчас трек", "now_playing", None, None),
            ("что играет на кухне", "now_playing", None, "кухне"),
            # shuffle on/off
            ("перемешай", "shuffle_on", None, None),
            ("включи перемешивание", "shuffle_on", None, None),
            ("случайный порядок", "shuffle_on", None, None),
            ("в случайном порядке", "shuffle_on", None, None),
            ("выключи перемешивание", "shuffle_off", None, None),
            ("не перемешивай", "shuffle_off", None, None),
            ("по порядку", "shuffle_off", None, None),
            ("перемешай на кухне", "shuffle_on", None, "кухне"),
            # repeat one/all/off
            ("повтор песни", "repeat_one", None, None),
            ("повтори песню", "repeat_one", None, None),
            ("повтори трек", "repeat_one", None, None),
            ("повтор эту", "repeat_one", None, None),
            ("повтор всё", "repeat_all", None, None),
            ("повтори все", "repeat_all", None, None),
            ("повтор очередь", "repeat_all", None, None),
            ("повторяй", "repeat_all", None, None),
            ("включи повтор", "repeat_all", None, None),
            ("выключи повтор", "repeat_off", None, None),
            ("не повторяй", "repeat_off", None, None),
            # seek_forward (with optional unit)
            ("вперёд 30", "seek_forward", 30, None),
            ("вперед 30", "seek_forward", 30, None),
            ("перемотай вперёд 30", "seek_forward", 30, None),
            ("перемотай вперёд на 30", "seek_forward", 30, None),
            ("перемотай вперёд на 30 секунд", "seek_forward", 30, None),
            ("перемотай вперёд на 1 минуту", "seek_forward", 60, None),
            ("перемотай вперёд на 2 минуты", "seek_forward", 120, None),
            # seek_back
            ("назад 30", "seek_back", 30, None),
            ("перемотай назад 30", "seek_back", 30, None),
            ("перемотай назад на 1 минуту", "seek_back", 60, None),
            ("назад на 5 секунд", "seek_back", 5, None),
            # seek_start
            ("к началу", "seek_start", None, None),
            ("в начало", "seek_start", None, None),
            ("перемотай к началу", "seek_start", None, None),
            ("начни заново", "seek_start", None, None),
            ("начни трек заново", "seek_start", None, None),
            # transfer (target captured into player_hint)
            ("переведи на спальню", "transfer", None, "спальню"),
            ("перенеси на спальню", "transfer", None, "спальню"),
            ("продолжи в спальне", "transfer", None, "спальне"),
            ("переведи музыку на кухню", "transfer", None, "кухню"),
            # alice prefix tolerated
            ("Алиса, пауза", "pause", None, None),
        ],
    )
    def test_parse(
        self,
        phrase: str,
        expected_action: str,
        expected_value: int | None,
        expected_hint: str | None,
    ) -> None:
        """Each parametrized phrase maps to the expected ParsedControl."""
        result = parse_control(phrase)
        assert result is not None, f"phrase={phrase!r} returned None"
        assert result.action == expected_action, f"phrase={phrase!r}"
        assert result.value == expected_value, f"phrase={phrase!r}"
        assert result.player_hint == expected_hint, f"phrase={phrase!r}"

    @pytest.mark.parametrize(
        "phrase",
        [
            "",
            "включи Metallica",
            "включи джаз на кухне",
            "включи песню Yesterday",
            "включи мою волну",
            "что-то непонятное",
            "включи альбом Black Album",
        ],
    )
    def test_play_phrases_return_none(self, phrase: str) -> None:
        """Phrases that should fall through to the play parser return None."""
        assert parse_control(phrase) is None


class TestPluralRu:
    """Tests for the Russian quantitative-form picker."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, "колонку"),
            (2, "колонки"),
            (3, "колонки"),
            (4, "колонки"),
            (5, "колонок"),
            (10, "колонок"),
            (11, "колонок"),  # 11 is exception — uses 5+ form
            (12, "колонок"),
            (14, "колонок"),
            (21, "колонку"),  # 21 → 1-form
            (22, "колонки"),
            (25, "колонок"),
            (101, "колонку"),
            (111, "колонок"),
            (0, "колонок"),
        ],
    )
    def test_plural(self, n: int, expected: str) -> None:
        """Russian quantitative agreement matches expected form for `n`."""
        assert _plural_ru(n, ("колонку", "колонки", "колонок")) == expected


class TestFormatListPlayers:
    """Tests for the `list_players` confirmation builder."""

    def test_zero_players(self) -> None:
        """Empty list → 'не вижу'."""
        assert format_list_players([]) == "Не вижу ни одной колонки."

    def test_one_player(self) -> None:
        """Single player → singular form."""
        p = MagicMock()
        p.name = "Кухня"
        p.player_id = "p1"
        assert format_list_players([p]) == "Вижу одну колонку: Кухня."

    def test_three_players(self) -> None:
        """Three players → 2-4 form ('колонки') with comma-separated names."""
        ps = []
        for name, pid in [("Кухня", "p1"), ("Спальня", "p2"), ("Гостиная", "p3")]:
            p = MagicMock()
            p.name = name
            p.player_id = pid
            ps.append(p)
        assert format_list_players(ps) == "Вижу 3 колонки: Кухня, Спальня, Гостиная."

    def test_five_players(self) -> None:
        """Five players → 5+ form ('колонок')."""
        ps = []
        for i in range(5):
            p = MagicMock()
            p.name = f"Player{i}"
            p.player_id = f"p{i}"
            ps.append(p)
        text = format_list_players(ps)
        assert text.startswith("Вижу 5 колонок:")


class TestControlConfirmation:
    """Tests for the user-facing confirmation strings."""

    @pytest.mark.parametrize(
        ("action", "value", "expected"),
        [
            ("pause", None, "Пауза."),
            ("resume", None, "Продолжаю."),
            ("stop", None, "Остановил."),
            ("next", None, "Следующая."),
            ("previous", None, "Предыдущая."),
            ("volume_up", None, "Громче."),
            ("volume_down", None, "Тише."),
            ("volume_set", 50, "Громкость 50."),
            ("mute", None, "Звук выключен."),
            ("unmute", None, "Звук включен."),
            ("shuffle_on", None, "Включил перемешивание."),
            ("shuffle_off", None, "Выключил перемешивание."),
            ("repeat_off", None, "Выключил повтор."),
            ("repeat_one", None, "Повтор песни."),
            ("repeat_all", None, "Повтор очереди."),
            ("seek_forward", 60, "Перемотал на 60 секунд вперёд."),
            ("seek_back", 30, "Перемотал на 30 секунд назад."),
            ("seek_start", None, "Перемотал к началу."),
        ],
    )
    def test_confirmation(self, action: str, value: int | None, expected: str) -> None:
        """Confirmation text matches the expected per-action template."""
        ctrl = ParsedControl(action=action, value=value)  # type: ignore[arg-type]
        assert control_confirmation(ctrl) == expected


@pytest.mark.asyncio
class TestExecuteControl:
    """Tests that execute_control dispatches to the correct MA call."""

    def _make_mass(self) -> MagicMock:
        mass = MagicMock()
        mass.player_queues = MagicMock()
        mass.player_queues.pause = AsyncMock()
        mass.player_queues.resume = AsyncMock()
        mass.player_queues.stop = AsyncMock()
        mass.player_queues.next = AsyncMock()
        mass.player_queues.previous = AsyncMock()
        mass.player_queues.set_shuffle = AsyncMock()
        mass.player_queues.set_repeat = MagicMock()  # NB: sync, not async
        mass.player_queues.skip = AsyncMock()
        mass.player_queues.seek = AsyncMock()
        mass.players = MagicMock()
        mass.players.cmd_volume_up = AsyncMock()
        mass.players.cmd_volume_down = AsyncMock()
        mass.players.cmd_volume_set = AsyncMock()
        mass.players.cmd_volume_mute = AsyncMock()
        return mass

    def _player(self) -> MagicMock:
        player = MagicMock()
        player.player_id = "p1"
        return player

    async def test_pause_calls_pause(self) -> None:
        """action=pause invokes mass.player_queues.pause."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="pause"), self._player())
        mass.player_queues.pause.assert_awaited_once_with("p1")

    async def test_resume_calls_resume(self) -> None:
        """action=resume invokes mass.player_queues.resume."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="resume"), self._player())
        mass.player_queues.resume.assert_awaited_once_with("p1")

    async def test_stop_calls_stop(self) -> None:
        """action=stop invokes mass.player_queues.stop."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="stop"), self._player())
        mass.player_queues.stop.assert_awaited_once_with("p1")

    async def test_next_calls_next(self) -> None:
        """action=next invokes mass.player_queues.next."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="next"), self._player())
        mass.player_queues.next.assert_awaited_once_with("p1")

    async def test_previous_calls_previous(self) -> None:
        """action=previous invokes mass.player_queues.previous."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="previous"), self._player())
        mass.player_queues.previous.assert_awaited_once_with("p1")

    async def test_volume_up(self) -> None:
        """action=volume_up invokes cmd_volume_up."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_up"), self._player())
        mass.players.cmd_volume_up.assert_awaited_once_with("p1")

    async def test_volume_down(self) -> None:
        """action=volume_down invokes cmd_volume_down."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_down"), self._player())
        mass.players.cmd_volume_down.assert_awaited_once_with("p1")

    async def test_volume_set(self) -> None:
        """action=volume_set invokes cmd_volume_set with the requested value."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_set", value=42), self._player())
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 42)

    async def test_volume_set_none_falls_back_to_zero(self) -> None:
        """volume_set with value=None defaults to 0 (defensive)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="volume_set", value=None), self._player())
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 0)

    async def test_mute(self) -> None:
        """action=mute invokes cmd_volume_mute(True)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="mute"), self._player())
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", True)

    async def test_unmute(self) -> None:
        """action=unmute invokes cmd_volume_mute(False)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="unmute"), self._player())
        mass.players.cmd_volume_mute.assert_awaited_once_with("p1", False)

    async def test_list_players_is_a_safe_noop_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`list_players` reaching execute_control logs a warning and no-ops.

        The handler is supposed to short-circuit `list_players` before
        dispatch (it's an informational query), but the typing allows
        it as a `ControlAction` so the explicit branch makes a stray
        call safe rather than a silent no-op.
        """
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(mass, ParsedControl(action="list_players"), self._player())
        # No MA command dispatched.
        mass.player_queues.pause.assert_not_awaited()
        mass.player_queues.resume.assert_not_awaited()
        mass.players.cmd_volume_set.assert_not_awaited()
        # Warning emitted.
        assert any("list_players" in r.getMessage() for r in caplog.records)

    async def test_shuffle_on(self) -> None:
        """action=shuffle_on invokes set_shuffle(True)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="shuffle_on"), self._player())
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=True)

    async def test_shuffle_off(self) -> None:
        """action=shuffle_off invokes set_shuffle(False)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="shuffle_off"), self._player())
        mass.player_queues.set_shuffle.assert_awaited_once_with("p1", shuffle_enabled=False)

    async def test_repeat_off(self) -> None:
        """action=repeat_off invokes set_repeat(RepeatMode.OFF) — sync, not awaited."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_off"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.OFF)

    async def test_repeat_one(self) -> None:
        """action=repeat_one invokes set_repeat(RepeatMode.ONE)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_one"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.ONE)

    async def test_repeat_all(self) -> None:
        """action=repeat_all invokes set_repeat(RepeatMode.ALL)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="repeat_all"), self._player())
        mass.player_queues.set_repeat.assert_called_once_with("p1", RepeatMode.ALL)

    async def test_seek_forward(self) -> None:
        """action=seek_forward(value=N) invokes skip(qid, +N)."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_forward", value=60), self._player())
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=60)

    async def test_seek_back_negates_value(self) -> None:
        """action=seek_back(value=N) invokes skip(qid, -N) — value is positive at parse time."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_back", value=30), self._player())
        mass.player_queues.skip.assert_awaited_once_with("p1", seconds=-30)

    async def test_seek_start(self) -> None:
        """action=seek_start invokes seek(qid, position=0) — absolute reset."""
        mass = self._make_mass()
        await execute_control(mass, ParsedControl(action="seek_start"), self._player())
        mass.player_queues.seek.assert_awaited_once_with("p1", position=0)

    async def test_now_playing_is_safe_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        """`now_playing` reaching execute_control logs warning and no-ops (handler dispatches)."""
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(mass, ParsedControl(action="now_playing"), self._player())
        mass.player_queues.skip.assert_not_awaited()
        assert any("now_playing" in r.getMessage() for r in caplog.records)

    async def test_transfer_is_safe_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        """`transfer` reaching execute_control logs warning and no-ops (handler dispatches)."""
        mass = self._make_mass()
        with caplog.at_level(
            logging.WARNING, logger="music_assistant.providers.yandex_alice.dialogs_control"
        ):
            await execute_control(
                mass, ParsedControl(action="transfer", player_hint="спальню"), self._player()
            )
        mass.player_queues.skip.assert_not_awaited()
        assert any("transfer" in r.getMessage() for r in caplog.records)

    async def test_underlying_failure_is_swallowed(self) -> None:
        """An exception from the MA call is logged + swallowed (no re-raise)."""
        mass = self._make_mass()
        mass.player_queues.pause = AsyncMock(side_effect=RuntimeError("boom"))
        # Must not raise.
        await execute_control(mass, ParsedControl(action="pause"), self._player())
