from hashlib import sha256
import os
import sys
from pathlib import Path
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fortnite_chest_counter import match_lifecycle


START = (b"[2026.11.10-20.04.47:107][363]LogBattleRoyaleGamePhaseLogic: HandleGamePhaseChanged. "
         b"OldPhase = EAthenaGamePhase::Setup. NewPhase = EAthenaGamePhase::Warmup\r\n")
FRONTEND = (b"[2026.11.10-20.05.02:589][955]LogWorld: Bringing World /Game/Maps/Frontend.Frontend up for play "
            b"(max tick rate 360) at 2026.11.10-21.05.02\r\n")


SESSION_A = b"0123456789abcdef0123456789abcdef"
SESSION_B = b"fedcba9876543210fedcba9876543210"


def session_line(ident, stamp=b"2026.11.10-20.04.47:106", frame=b"363"):
    return b"[" + stamp + b"][" + frame + b']LogCsvProfiler: Display: Metadata set : sessionid="' + ident + b'"\r\n'


def append(path, data):
    with path.open("ab") as stream:
        stream.write(data)


def test_tail_only_parses_new_complete_exact_markers(tmp_path, monkeypatch):
    path = tmp_path / "FortniteGame.log"
    path.write_bytes(START + FRONTEND + START[:30])
    monkeypatch.setattr(match_lifecycle.LogTail, "READ_SIZE", len(START) + len(FRONTEND))
    tail = match_lifecycle.LogTail(path)
    assert tail.poll() == (False, [])

    append(path, START[30:] + START[:20])
    assert tail.poll() == (False, [])
    append(path, START[20:] + FRONTEND)
    assert tail.poll() == (False, ["start", "finish"])

    append(path, START.replace(b"OldPhase", b"OtherPhase") + FRONTEND.replace(b"Frontend.Frontend", b"Other.Frontend"))
    assert tail.poll() == (False, [])
    assert tail.poll() == (False, [])


def test_tail_recovers_from_rotation_truncation_and_overlong_lines(tmp_path, monkeypatch):
    path = tmp_path / "FortniteGame.log"
    tail = match_lifecycle.LogTail(path)
    path.write_bytes(START)
    assert tail.poll() == (False, ["start"])
    append(path, FRONTEND[:25])
    assert tail.poll() == (False, [])
    path.rename(tmp_path / "FortniteGame-backup.log")
    path.write_bytes(FRONTEND[25:] + START)
    assert tail.poll() == (True, ["start"])

    path.write_bytes(FRONTEND + b"new content of a rewritten file\n" + START)
    assert tail.poll() == (True, ["finish", "start"])
    path.write_bytes(b"")
    assert tail.poll() == (True, [])

    monkeypatch.setattr(tail, "READ_SIZE", 250)
    monkeypatch.setattr(tail, "LINE_LIMIT", 200)
    append(path, b"x" * 500 + START + FRONTEND)
    assert tail.poll() == (False, [])
    assert tail.poll() == (False, [])
    assert tail.poll() == (False, [])
    assert tail.poll() == (False, ["finish"])


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PySide6.QtWidgets")
    from fortnite_chest_counter.gui import app as gui

    application = qt.QApplication.instance() or qt.QApplication([])

    class Worker:
        def __init__(self):
            self.alive = False
            self.starts = 0
            self.stops = 0
            self.stderr_tail = []

        def running(self):
            return self.alive

        def start(self, argv):
            self.alive = True
            self.starts += 1

        def stop(self):
            self.stops += 1

    log = tmp_path / "FortniteGame.log"
    monkeypatch.setattr(gui.paths, "APP_DIR", tmp_path)
    monkeypatch.setattr(gui.discord_group, "load_webhook", lambda: None)
    monkeypatch.setattr(gui.discord_summary, "load", lambda: None)
    monkeypatch.setattr(match_lifecycle, "local_log_path", lambda: log)
    monkeypatch.setattr(gui.MainWindow, "refresh_sources", lambda self: None)
    monkeypatch.setattr(gui, "ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(gui, "show_error", lambda parent, message: pytest.fail(message))
    win = gui.MainWindow()
    win.timer.stop()
    win.slow.stop()
    win.worker = Worker()
    win._fill_sources([], [(0, 0, 1280, 720), (1280, 0, 1920, 1080)])
    win.dashboard.source.setCurrentIndex(win.dashboard.source.findData({"source": "desktop", "monitor": 1}))
    try:
        yield gui, win, log
    finally:
        win.set_match_automation(False)
        win._workers_pending.clear()
        win.worker.alive = False
        win.close()
        application.processEvents()


def seed(gui, win, counts):
    gui.C.set_counts(win._cfg_for_actions(), *counts)
    win.read_state(force=True)


def exit_worker(win, code=0, acknowledged=True):
    if acknowledged:
        win.handle({"type": "stopped", "stop_reason": "stopped in the app"})
    win.worker.alive = False
    win.handle({"type": "exit", "code": code})


def online_bot(win, config):
    from types import SimpleNamespace
    from fortnite_chest_counter.gui import app as gui

    win.set_group_role("host")
    gui.C.update_config_file(gui.config_file(), "app", {"group_share_matches": True})
    win.discord.runner = SimpleNamespace(status=("online", ""), config=config, report_config=config, stop=lambda **_: None)


@pytest.mark.parametrize("send_enabled", [False, True])
def test_qt_drains_before_saving_and_before_next_start(app, monkeypatch, send_enabled):
    gui, win, log = app
    delivered = Event()
    summaries = []

    def send(config, content):
        summaries.append(content)
        delivered.set()

    win.set_match_automation(False)
    online_bot(win, object())
    monkeypatch.setattr(gui.discord_summary, "send", send)
    seed(gui, win, (5, 10))
    log.write_bytes(START)
    win.poll_match_log()
    assert win.counts == (5, 10)
    win.settings_page.auto_match.setChecked(True)
    win.discord.save_report_mode(send_enabled)
    win.settings_page.save()
    win.poll_match_log()
    assert win.counts == (5, 10)
    assert win.worker.starts == 0

    append(log, FRONTEND + START + START)
    from PySide6.QtTest import QTest

    win.match_timer.setInterval(10)
    from time import monotonic

    deadline = monotonic() + 2
    while win.worker.starts == 0 and monotonic() < deadline:
        QTest.qWait(10)
    assert win.counts == (0, 0)
    assert win.worker.starts == 1

    seed(gui, win, (8, 13))
    append(log, FRONTEND + FRONTEND + START)
    win.poll_match_log()
    assert win.worker.stops == 1
    assert gui.match_history.read_history(win.match_history_path()) == []
    seed(gui, win, (9, 14))
    exit_worker(win)
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.certain, saved.total) == (9, 14)
    assert win.counts == (9, 14)
    assert win.worker.starts == 1
    _, cumulative = win.dashboard.match_summaries()
    assert (cumulative.matches, cumulative.total) == (1, 14)
    display = {**gui.load_cfg()[0]["display"], "screen_show_matches": True}
    assert "Matches · 1" in win.dashboard.screen_overlay_text(display)

    assert delivered.wait(2 if send_enabled else 0.05) is send_enabled
    assert summaries == ([saved] if send_enabled else [])

    win.poll_match_log()
    assert win.worker.starts == 2
    assert win.counts == (0, 0)


def test_qt_save_failure_disarms_and_preserves_counts(app, monkeypatch):
    gui, win, log = app
    win.set_match_automation(True)
    append(log, START)
    win.poll_match_log()
    seed(gui, win, (7, 9))
    with monkeypatch.context() as failing:
        def fail(*args):
            raise OSError("test history write failure")

        failing.setattr(gui.match_history, "save_match", fail)
        append(log, FRONTEND + START)
        win.poll_match_log()
        exit_worker(win)
    win.poll_match_log()
    assert win.worker.starts == 1
    assert win.counts == (7, 9)
    assert win._match_tail is None and not win.settings_page.auto_match.isChecked()
    assert not gui.load_cfg()[0]["app"]["auto_match_lifecycle"]
    win.finish_match()
    assert gui.match_history.read_history(win.match_history_path())[0].total == 9


def test_qt_active_rotation_preserves_counts_without_inventing_a_finish(app):
    gui, win, log = app
    win.set_match_automation(True)
    append(log, START)
    win.poll_match_log()
    seed(gui, win, (3, 5))
    log.rename(log.with_suffix(".backup"))
    log.write_bytes(FRONTEND + START)
    win.poll_match_log()
    assert win._match_tail is None
    assert win.counts == (3, 5)
    assert gui.match_history.read_history(win.match_history_path()) == []


@pytest.mark.parametrize("draining,code,acknowledged", [(False, 0, False), (True, 0, False), (True, 1, True)])
def test_qt_incomplete_exit_disarms_without_saving(app, draining, code, acknowledged):
    gui, win, log = app
    win.set_match_automation(True)
    append(log, START)
    win.poll_match_log()
    seed(gui, win, (5, 9))
    if draining:
        append(log, FRONTEND)
        win.poll_match_log()
    exit_worker(win, code=code, acknowledged=acknowledged)
    append(log, FRONTEND + START)
    win.poll_match_log()
    assert win._match_tail is None
    assert win.counts == (5, 9)
    assert gui.match_history.read_history(win.match_history_path()) == []


def test_qt_restores_state_file_if_reset_cannot_save(app, monkeypatch):
    gui, win, log = app
    seed(gui, win, (5, 9))
    cfg = win._cfg_for_actions()
    primary = Path(cfg["state"]["path"])
    previous = primary.read_bytes()

    def fail_write(path, state):
        raise OSError("test state write failure")

    monkeypatch.setattr(gui.C, "save_state", fail_write)
    win.set_match_automation(True)
    append(log, START)
    win.poll_match_log()
    assert primary.read_bytes() == previous
    assert win.counts == (5, 9)
    assert win.worker.starts == 0
    assert win._match_tail is None


def test_qt_skips_stale_backlog_and_leaves_legacy_capture_untouched(app, monkeypatch):
    gui, win, log = app
    seed(gui, win, (2, 4))
    cfg = win._cfg_for_actions()
    secondary = gui.profiles.secondary_state_path(cfg["state"]["path"])
    secondary.parent.mkdir(parents=True, exist_ok=True)
    before = b"Legacy secondary capture data"
    secondary.write_bytes(before)
    win.set_match_automation(True)
    monkeypatch.setattr(match_lifecycle.LogTail, "READ_SIZE", len(START))
    append(log, START + FRONTEND)
    win.poll_match_log()
    assert win.worker.starts == 0
    win.poll_match_log()
    assert win.worker.starts == 0
    assert win.counts == (2, 4)
    append(log, START)
    win.poll_match_log()
    assert win.worker.starts == 1
    assert secondary.read_bytes() == before


def test_qt_deferred_start_waits_for_modal_and_source_scan(app, monkeypatch):
    gui, win, log = app
    from PySide6.QtWidgets import QApplication

    win.start_counting()
    win.set_match_automation(True)
    append(log, START)
    win.poll_match_log()
    assert win.worker.stops == 1
    exit_worker(win)
    with monkeypatch.context() as blocked:
        blocked.setattr(QApplication, "activeModalWidget", lambda: object())
        win.poll_match_log()
        assert win.worker.starts == 1
    win._source_scan = object()
    win.poll_match_log()
    assert win.worker.starts == 1
    win._source_scan = None
    win.poll_match_log()
    assert win.worker.starts == 2


def test_qt_fresh_manual_finish_schedules_one_report_with_current_bot(app, monkeypatch):
    gui, win, _ = app
    seed(gui, win, (5, 9))
    win.discord.auto_report.setChecked(True)
    delivered = Event()
    calls = []
    setup = object()
    online_bot(win, setup)

    def send(config, summary):
        calls.append((config, summary, gui.threading.current_thread().daemon))
        delivered.set()

    monkeypatch.setattr(gui.discord_summary, "send", send)
    win.finish_match()
    win.finish_match()
    saved, = gui.match_history.read_history(win.match_history_path())
    assert delivered.wait(2)
    assert calls == [(setup, saved, False)]


def test_qt_saved_match_recovers_group_outbox_after_enqueue_failure(app, monkeypatch):
    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    monkeypatch.setattr(group, "load_webhook", lambda: webhook)
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Rin", "group_share_matches": True})
    seed(gui, win, (5, 9))
    delivered = Event()
    sent = []
    queue_match = group.queue_match
    attempts = 0

    def queue(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated local outbox failure")
        return queue_match(*args)

    def publish(values, event, name, count, certain, chest_types):
        sent.append((values.id, event, name, count, certain, chest_types))
        delivered.set()

    monkeypatch.setattr(group, "queue_match", queue)
    monkeypatch.setattr(group, "_publish", publish)
    win.finish_match()
    win.finish_match()
    saved, = gui.match_history.read_history(win.match_history_path())
    assert saved.group_webhook_id == webhook.id and saved.player == "Rin"
    assert delivered.wait(3)
    assert len(sent) == 1 and sent[0][2:] == ("Rin", 9, 5, {"unclassified": 9})


def test_qt_solo_match_stays_local_with_group_configured(app, monkeypatch):
    gui, win, _ = app
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Rin", "auto_discord_summary": True})
    win.group.share_matches.setChecked(False)
    monkeypatch.setattr(gui.discord_group, "queue_match", lambda *_: pytest.fail("Unexpected group contribution"))
    monkeypatch.setattr(win, "deliver_saved_match", lambda *_: pytest.fail("Unexpected automatic report"))
    seed(gui, win, (2, 4))
    win.finish_match()
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.player, saved.total, saved.group_webhook_id) == ("Rin", 4, "")



def test_session_requires_a_nearby_exact_warmup_across_chunks(tmp_path, monkeypatch):
    path = tmp_path / "FortniteGame.log"
    tail = match_lifecycle.LogTail(path)
    first = session_line(SESSION_A.upper()) + b"other event\n" * 9
    monkeypatch.setattr(tail, "READ_SIZE", len(first))
    append(path, first + START)
    assert tail.poll() == (False, [])
    assert tail.poll() == (False, [("start", sha256(SESSION_A).hexdigest())])

    monkeypatch.setattr(tail, "READ_SIZE", match_lifecycle.LogTail.READ_SIZE)
    append(path, session_line(SESSION_B, frame=b"362") + START)
    append(path, session_line(SESSION_B, stamp=b"2026.11.10-20.04.47:050") + START)
    append(path, session_line(SESSION_B) + b"\n" * 32 + START)
    append(path, session_line(SESSION_B) + FRONTEND + START)
    assert tail.poll() == (False, ["start", "start", "start", "finish", "start"])

    append(path, session_line(SESSION_B))
    assert tail.poll() == (False, [])
    path.rename(path.with_suffix(".old"))
    path.write_bytes(START + session_line(SESSION_B) + START)
    assert tail.poll() == (True, ["start", ("start", sha256(SESSION_B).hexdigest())])


def test_qt_only_binds_latest_started_session_and_keeps_it_through_drain(app):
    gui, win, log = app
    win.set_match_automation(True)
    win._source_scan = object()
    append(log, session_line(SESSION_A) + START)
    win.poll_match_log()
    assert win.worker.starts == 0
    win._source_scan = None
    append(log, FRONTEND + session_line(SESSION_B) + START)
    win.poll_match_log()
    assert win.worker.starts == 1
    seed(gui, win, (1, 2))
    append(log, FRONTEND + session_line(SESSION_A) + START)
    win.poll_match_log()
    seed(gui, win, (2, 3))
    exit_worker(win)
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.total, saved.session_fingerprint) == (3, sha256(SESSION_B).hexdigest())

    win.poll_match_log()
    assert win.worker.starts == 2
    seed(gui, win, (3, 5))
    append(log, START + FRONTEND)
    win.poll_match_log()
    exit_worker(win)
    saved, manual = gui.match_history.read_history(win.match_history_path())
    assert (manual.total, manual.session_fingerprint) == (5, "")


def test_qt_rotated_active_session_only_saves_personal_counts(app):
    gui, win, log = app
    win.set_match_automation(True)
    append(log, session_line(SESSION_A) + START)
    win.poll_match_log()
    seed(gui, win, (3, 5))
    log.rename(log.with_suffix(".old"))
    log.write_bytes(FRONTEND)
    win.poll_match_log()
    assert win._match_tail is None
    win.stop_counting()
    exit_worker(win)
    win.finish_match()
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.total, saved.session_fingerprint) == (5, "")



def test_qt_republishes_a_team_update_when_ranking_is_unchanged(app, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtTest import QTest

    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    ident = "234567890123456789"
    ranking = [group.Player("Sam", 30, matches=2)]
    team = group.TeamSummary(1, 1, 10, 13750)
    message = {"id": ident, "channel_id": "345678901234567890", "webhook_id": webhook.id,
               "embeds": [{"footer": {"text": gui.discord_scoreboard._MARKER}}]}
    edits = []

    def request(_, suffix, method, payload=None, **kwargs):
        if method == "PATCH":
            message.update(payload)
            edits.append(payload)
        return message

    def wait_for_board():
        for _ in range(100):
            if not win.group._scoreboard_pending:
                return
            QTest.qWait(10)
        pytest.fail("The shared board did not finish updating")

    monkeypatch.setattr(group.GroupStore, "board", lambda _: (ranking, team))
    monkeypatch.setattr(gui.discord_scoreboard, "_request", request)
    win.set_group_role("host")
    win.discord.runner = SimpleNamespace(status=("online", ""), webhook=webhook, stop=lambda **_: None)
    win.group._webhook = webhook
    win.group._scoreboard_id = ident
    win.group._next_scoreboard_refresh = 0
    win.group.sync_scoreboard()
    wait_for_board()
    assert len(edits) == 1
    assert win.group.team.text() == team.text()

    team = group.TeamSummary(1, 2, 14, 7639)
    win.group._next_scoreboard_refresh = 0
    win.group.sync_scoreboard()
    wait_for_board()
    assert len(edits) == 2
    assert edits[0]["embeds"][0]["fields"] == edits[1]["embeds"][0]["fields"]
    assert win.group.team.text() == team.text()

    win.group.role.setCurrentIndex(win.group.role.findData("guest"))
    wait_for_board()
    assert len(edits) == 2
    assert win.group.team.text() == team.text()
    assert win.group.ranks.topLevelItem(0).text(1) == "Sam"

def test_qt_select_all_and_delete_match_history(app):
    gui, win, _ = app
    for counts in [(1, 2), (3, 5)]:
        seed(gui, win, counts)
        assert win.save_current_match()
    group = win.group
    group.load()
    assert group.history.topLevelItemCount() == 2
    assert not group.delete.isEnabled()
    group.select_matches.click()
    assert len(group.checked_items(group.history)) == 2
    assert group.delete.isEnabled()
    group.delete.click()
    assert group.history.topLevelItemCount() == 0
    assert not group.select_matches.isChecked()
    assert not group.delete.isEnabled()
    assert gui.match_history.read_history(win.match_history_path()) == []

def test_qt_guest_follows_published_settings_and_restores_personal_choices(app):
    gui, win, _ = app
    mode, boost = win.dashboard.token_mode, win.dashboard.token_boost
    win.set_group_role("guest")
    win.group.share_matches.setChecked(False)
    mode.setCurrentText("Solo")
    boost.setCurrentIndex(boost.findData(1.0))
    webhook = gui.discord_group.Webhook("123456789012345678", "fake-token")
    ident = "234567890123456789"
    win.group._webhook = webhook
    win.group._scoreboard_id = ident
    win.group.share_matches.setChecked(True)
    win.group._scoreboard_result((webhook, ident, False, (), None, ("Duo", 1.5), None, ""))
    assert (mode.currentText(), boost.currentData()) == ("Duo", 1.5)
    assert not mode.isEnabled() and not boost.isEnabled()
    win.group.share_matches.setChecked(False)
    assert (mode.currentText(), boost.currentData()) == ("Solo", 1.0)
    assert mode.isEnabled() and boost.isEnabled()

@pytest.mark.parametrize(("playlist", "mode"), [
    (b"Playlist_DefaultSolo", "Solo"),
    (b"Playlist_NoBuildBR_Duo", "Duo"),
    (b"Playlist_DefaultTrio", "Trio"),
    (b"Playlist_DefaultSquad", "Squad"),
    (b"Playlist_HabaneroTrio", "Trio"),
    (b"Playlist_FrontEnd", None),
])
def test_match_playlist_from_same_warmup(tmp_path, playlist, mode):
    log = tmp_path / "FortniteGame.log"
    tail = match_lifecycle.LogTail(log)
    metadata = (b'[2026.11.10-20.04.47:106][363]LogCsvProfiler: Display: Metadata set : playlistid="'
                + playlist + b'"\n')
    log.write_bytes(metadata + START)
    assert tail.poll() == (False, [("start", "", mode)] if mode else ["start"])


def test_qt_ranked_warmup_starts_capture_and_selects_mode(app):
    gui, win, log = app
    win.set_match_automation(True)
    seed(gui, win, (3, 5))
    append(log, b'[2026.11.10-20.04.47:106][363]LogCsvProfiler: Display: Metadata set : '
                b'playlistid="Playlist_HabaneroTrio"\r\n' + START)
    win.poll_match_log()
    assert win.worker.starts == 1
    assert win.counts == (0, 0)
    assert win.current_game_mode() == "Trio"


def test_qt_auto_playlist_does_not_start_capture_and_allows_manual_override(app):
    gui, win, log = app
    box = win.dashboard.token_mode
    win.set_match_automation(False)
    box.setCurrentIndex(box.findData("Auto"))
    log.write_bytes(b'[2026.11.10-20.04.47:106][363]LogCsvProfiler: Display: Metadata set : '
                    b'playlistid="Playlist_NoBuildBR_Duo"\n' + START + FRONTEND)
    win.poll_match_log()
    assert win.current_game_mode() == "Duo"
    assert win.worker.starts == 0
    assert gui.load_cfg()[0]["app"]["token_mode"] == "Auto"
    box.setCurrentText("Squad")
    assert win.current_game_mode() == "Squad"
    assert gui.load_cfg()[0]["app"]["token_mode"] == "Squad"

def test_qt_reset_all_settings_keeps_other_preferences_and_counts(app, monkeypatch):
    gui, win, _ = app
    page = win.settings_page
    path = gui.config_file()
    gui.C.update_config_file(path, "detector", {"process_fps": 5})
    gui.C.update_config_file(path, "app", {"group_player": "Alex", "token_mode": "Duo"})
    seed(gui, win, (2, 5))
    page.load()
    from PySide6.QtWidgets import QPushButton

    reset = next(button for button in page.findChildren(QPushButton) if button.text() == "Reset all")
    monkeypatch.setattr(gui, "ask", lambda *args: False)
    reset.click()
    assert gui.load_cfg()[0]["detector"]["process_fps"] == 5
    monkeypatch.setattr(gui, "ask", lambda *args: True)
    reset.click()
    cfg = gui.load_cfg()[0]
    assert cfg["detector"]["process_fps"] == gui.settings.DEFAULTS["detector"]["process_fps"]
    assert cfg["app"]["group_player"] == "Alex"
    assert cfg["app"]["token_mode"] == "Duo"
    assert win.counts == (2, 5)
    assert page.saved.text() == "Defaults restored."

def test_qt_custom_overlay_sections_opacity_and_team_history(app, monkeypatch):
    gui, win, _ = app
    now = 1_790_000_000.0
    webhook = gui.discord_group.Webhook("123456789012345678", "fixture-placeholder")
    win.group._webhook = webhook
    sessions = (
        gui.discord_group.TeamSession("a" * 64, (
            gui.discord_group.SessionPlayer("Alex", 8, 10, 1, True, 1, now),
            gui.discord_group.SessionPlayer("Sam", 4, 5, 2, True, 1, now),
        )),
    )
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Alex", "group_share_matches": True})
    seed(gui, win, (8, 10))
    win.save_current_match(session_fingerprint="a" * 64)
    # This local save did not resolve a webhook in the fixture, so explicitly model a shared saved result.
    rows = gui.match_history.read_history(win.match_history_path())
    from dataclasses import replace
    gui.match_history._write_history(win.match_history_path(), [replace(rows[0], group_webhook_id=webhook.id)])
    win.group.load()
    win.group._webhook = webhook
    win.group._sessions = sessions
    win.group.refresh_history_teams()
    win.dashboard.set_recent_matches(gui.match_history.read_history(win.match_history_path()))
    item = win.group.history.topLevelItem(0)
    assert item.text(7) == "Alex · Sam" and item.text(8) == "15"
    page = win.overlay
    page.section_templates["personal"].setPlainText("Mine {total}")
    page.section_templates["team"].setPlainText("Team {total} / {players}")
    page.section_templates["cumulative"].setPlainText("{matches} games / {total} / {chance}")
    page.transparency.setValue(45)
    page.apply()
    cfg = gui.load_cfg()[0]["display"]
    assert cfg["screen_opacity"] == 55
    personal, team, cumulative = win.dashboard.screen_overlay_sections(cfg)
    assert personal == "Mine 10"
    assert team == "Team 15 / 2"
    assert cumulative.startswith("1 games / 15 / ")
    assert abs(win.dashboard.screen_overlay.windowOpacity() - 0.55) < 0.01
    # Sharing off hides current team stats, while saved group history remains in the lifetime result.
    win.group.share_matches.setChecked(False)
    assert win.dashboard.screen_overlay_sections(cfg)[1] == ""


def test_qt_group_cooldown_defers_network_but_keeps_latest_counts(app, monkeypatch):
    from time import monotonic

    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    monkeypatch.setattr(group, "load_webhook", lambda: webhook)
    monkeypatch.setattr(group, "webhook_retry_after", lambda _: 17.25)
    sent = []
    monkeypatch.setattr(group, "queue_live", lambda *args: sent.append(args))
    monkeypatch.setattr(group, "flush", lambda _: pytest.fail("HTTP flush during Discord cooldown"))
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Alex", "group_share_matches": True})
    win.group._webhook = webhook
    win.group._scoreboard_id = "234567890123456789"
    win.group._publish_channel = "345678901234567890"
    win.group._next_scoreboard_refresh = 0
    before = monotonic()
    win.group.sync_scoreboard(publish=True)
    assert not win.group._scoreboard_pending
    assert win.group._next_scoreboard_refresh >= before + 17.25
    assert win.group._publish_channel == "345678901234567890"
    assert "paused" in win.group.player_status.text()

    win.discord.retry_group_outbox()
    assert win.discord._next_outbox_retry >= before + 17.25
    assert not win.discord._outbox_active.locked()
    seed(gui, win, (8, 10))
    win._match_active = True
    win._match_fingerprint = "a" * 64
    win.worker.alive = True
    win._next_live_share = 0
    win.publish_live_counts()
    assert sent and sent[-1][2:5] == ("Alex", 8, 10)
    assert win._next_live_share >= before + 17.25

@pytest.mark.parametrize("enabled, queue_fails", [(False, False), (True, True), (True, False)])
def test_verified_replay_cleanup_waits_for_durable_result(app, monkeypatch, tmp_path, enabled, queue_fails):
    gui, win, _ = app
    replay = gui.replay_comparison
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    demos = replay.demos_path()
    demos.mkdir(parents=True)
    path = demos / "UnsavedReplay-2026.11.11-13.05.25.replay"
    path.write_bytes(b"replay fixture")
    match = gui.match_history.Match("test-match", "2026-11-11 13:13", "p", "Default", "Duo",
                                    1, 3, "0%", player="Rin")
    gui.match_history.save_match(win.match_history_path(), match)
    gui.C.update_config_file(gui.config_file(), "app", {"delete_verified_replay": enabled})
    webhook = gui.discord_group.Webhook("123456789012345678", "fixture-webhook-token")

    def queue(_webhook, verified, player):
        assert player == "Rin"
        assert gui.match_history.read_history(win.match_history_path()) == [verified]
        assert path.exists()
        if queue_fails:
            raise OSError("outbox unavailable")

    monkeypatch.setattr(gui.discord_group, "queue_verified", queue)
    monkeypatch.setattr(win.discord, "retry_group_outbox", lambda: None)
    timeline = [(1, "Chest", False), (2, "Chest", True), (3, "Buried chest", False)]
    win.apply_verified_replay((match, webhook, (path, replay.replay_identity(path), 3, {}, 1, timeline, "Rin")))
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.total, saved.replay["destroyed"], saved.replay["direct"]["estimated"]) == (2, 1, 3)
    assert path.exists() == (not enabled or queue_fails)

def test_disabling_replay_verification_keeps_the_live_result(app):
    gui, win, _ = app
    page = win.settings_page
    page.load()
    assert page.auto_verify_replay.isChecked()
    assert page.delete_verified_replay.isEnabled()
    page.auto_verify_replay.setChecked(False)
    assert not page.delete_verified_replay.isEnabled()
    gui.C.update_config_file(gui.config_file(), "app", {"auto_verify_replay": False})
    match = gui.match_history.Match("live", "2026-11-11 13:13", "p", "Default", "Duo", 1, 3, "0%")
    gui.match_history.save_match(win.match_history_path(), match)
    win.apply_verified_replay((match, None, (None, None, 1, {}, 0, [(1, "Chest", False)], "Rin")))
    assert gui.match_history.read_history(win.match_history_path()) == [match]

def test_local_replay_uses_timestamps_instead_of_group_alias(tmp_path):
    from datetime import datetime, timedelta
    import json
    from fortnite_chest_counter import match_history, replay_comparison

    start = datetime(2026, 11, 11, 14, 47, 19).astimezone()
    match = match_history.Match("match", "2026-11-11 15:04", "p", "Default", "Squad",
                                0, 21, "0%", player="Lionel")
    row = {
        "MatchStartUtc": start.isoformat(),
        "DurationSeconds": 996,
        "PlayerName": "lionicolas399",
        "IsReplayOwner": True,
        "IsBattleRoyale": True,
        "ChestsOpened": 1,
        "Chests": [{"TimeSeconds": 103.3, "Kind": "Chest", "Destroyed": False}],
    }
    report = tmp_path / "data.json"
    report.write_text(json.dumps({"Matches": [row]}), encoding="utf-8")
    assert replay_comparison.read_report(report, match, local_start=start)[4] == 1
    with pytest.raises(ValueError, match="Expected one replay"):
        replay_comparison.read_report(report, match, local_start=start + timedelta(seconds=4))
    with pytest.raises(ValueError, match="Expected one replay"):
        replay_comparison.read_report(report, match)
