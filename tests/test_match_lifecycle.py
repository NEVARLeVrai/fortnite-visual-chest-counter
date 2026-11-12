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


def test_qt_restart_publishes_verified_match_after_queue_failure(app, monkeypatch):
    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    match = gui.match_history.Match("recovered", "2026-11-11 13:13", "p", "Default", "Duo",
                                    20, 7, "0%", player="Rin", group_webhook_id=webhook.id)
    gui.match_history.save_match(win.match_history_path(), match)
    group.queue_match(webhook, match, match.player)
    sent = []
    monkeypatch.setattr(group, "_publish", lambda _, event, player, count, certain, types, **flags:
                        sent.append((event, player, count, certain, types, flags)))
    assert group.flush(webhook) == 1

    def fail_queue(*_args):
        raise OSError("Outbox temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(group, "queue_verified", fail_queue)
        timeline = [(index, "Chest", False) for index in range(31)]
        win.apply_verified_replay((match, webhook, (None, None, 31, {}, 0, timeline, "Rin")))
    assert len(sent) == 1
    saved, = gui.match_history.read_history(win.match_history_path())
    assert saved.total == saved.certain == 31
    gui.match_history.edit_match(win.match_history_path(), match.snapshot, 29, 29, "0%")

    restarted = gui.MainWindow()
    restarted.timer.stop()
    restarted.slow.stop()
    monkeypatch.setattr(group, "load_webhook", lambda: webhook)
    monkeypatch.setattr(restarted, "auto_check", lambda: None)
    try:
        restarted.after_start()
        assert restarted.discord._outbox_active.acquire(timeout=3)
        restarted.discord._outbox_active.release()
        assert len(sent) == 2
        assert sent[0][0] == sent[1][0]
        assert sent[1][1:] == ("Rin", 31, 31, {"chest": 31}, {"verified": True})
        assert group.flush(webhook) == 0
    finally:
        restarted.close()


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

def test_qt_guest_fetches_shared_session_while_counting_on_stats_page(app, monkeypatch):
    from PySide6.QtTest import QTest

    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    session = group.TeamSession("a" * 64, (group.SessionPlayer("Sam", 2, 3, 1, updated_at=100),))
    win.set_group_role("guest")
    win.pages.setCurrentWidget(win.stats)
    win.worker.alive = True
    win.group._webhook = webhook
    win.group._scoreboard_id = "234567890123456789"
    win.group._next_scoreboard_refresh = 0
    monkeypatch.setattr(gui.discord_scoreboard, "fetch", lambda *_, **__: ((), None, None, (session,)))

    win.group.sync_scoreboard()
    for _ in range(100):
        if not win.group._scoreboard_pending:
            break
        QTest.qWait(10)
    assert win.group._sessions == (session,)


def test_qt_team_overlay_explains_wait_and_shows_remote_age(app, monkeypatch):
    gui, win, _ = app
    group = gui.discord_group
    win.set_group_role("guest")
    win.worker.alive = True
    win.group._webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    win.group._scoreboard_id = "234567890123456789"
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Alex", "group_share_matches": True})
    cfg = gui.load_cfg()[0]
    status, ages = win.dashboard.team_sync_details(cfg, "", None)
    assert (status, ages) == ("Current match is not identified.", {"alex": "Live · local counter"})

    win._match_fingerprint = "a" * 64
    status, _ = win.dashboard.team_sync_details(cfg, "", None)
    assert "teammate" in status
    monkeypatch.setattr(gui.time, "time", lambda: 100)
    session = group.TeamSession(win._match_fingerprint, (group.SessionPlayer("Sam", 2, 3, 1, updated_at=53),))
    win.group._sessions = (session,)
    team = gui.match_stats.team(session, "Alex", current=(1, 2, 1.0))
    assert win.dashboard.team_sync_details(cfg, "", team) == (
        "", {"alex": "Live · local counter", "sam": "Last update 45s ago"})
    monkeypatch.setattr(group, "webhook_retry_after", lambda _: 10)
    status, ages = win.dashboard.team_sync_details(cfg, "", team)
    assert "paused" in status and "sam" in ages

    win.worker.alive = False
    win._match_active = True
    _, ages = win.dashboard.team_sync_details(cfg, "", team)
    assert ages["alex"] == "Capture paused"


def test_dashboard_diagnostics_use_match_and_capture_status(app, monkeypatch):
    gui, win, _ = app
    win.set_group_role("guest")
    win.worker.alive = True
    win.group._webhook = gui.discord_group.Webhook("123456789012345678", "fixture-webhook-token")
    win.group._scoreboard_id = "234567890123456789"
    gui.C.update_config_file(gui.config_file(), "app", {"group_player": "Alex", "group_share_matches": True})
    win._match_fingerprint = "a" * 64
    monkeypatch.setattr(gui.time, "time", lambda: 100)
    player = gui.discord_group.SessionPlayer("Sam", 2, 3, 1, updated_at=53)
    win.group._sessions = (gui.discord_group.TeamSession(win._match_fingerprint, (player,)),)
    shown = []
    monkeypatch.setattr(gui, "show_notice_dialog", lambda _parent, _title, text: shown.append(text))
    win.dashboard.show_team_sync()
    assert "Match ID · aaaaaaaaaaaa" in shown[0]
    assert "Sam · 3 chests · Last update 45s ago" in shown[0]

    win.set_mode("counting")
    win.connected = {"process_fps": 15, "input_fps": 30, "resolution": [1280, 720]}
    win.last_status = {"fps": 13.7, "dropped": 0, "backlog_s": 1.2, "ms_per_frame": 65}
    monkeypatch.setattr(win.group, "sync_scoreboard", lambda: None)
    monkeypatch.setattr(win.discord, "retry_group_outbox", lambda: None)
    win._tick_slow()
    assert win.dashboard.capture_health.text() == (
        "Target 15 fps · Measured 13.7 fps · 0 frames skipped · 65 ms per frame · Stream backlog 1.2 s"
    )
    win.set_mode("stopped")
    assert win.dashboard.capture_health.text() == "No active capture."


def test_qt_host_reads_local_session_during_webhook_cooldown(app, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtTest import QTest

    gui, win, _ = app
    group = gui.discord_group
    webhook = group.Webhook("123456789012345678", "fixture-webhook-token")
    session = group.TeamSession("a" * 64, (group.SessionPlayer("Sam", 2, 3, 1, updated_at=100),))
    win.set_group_role("host")
    win.discord.runner = SimpleNamespace(status=("online", ""), webhook=webhook, stop=lambda **_: None)
    win.group._webhook = webhook
    win.group._scoreboard_id = "234567890123456789"
    win.group._next_scoreboard_refresh = 0
    monkeypatch.setattr(group, "webhook_retry_after", lambda _: 17.25)
    monkeypatch.setattr(group.GroupStore, "sessions", lambda _: (session,))
    monkeypatch.setattr(gui.discord_scoreboard, "publish", lambda *_, **__: pytest.fail("HTTP during cooldown"))

    win.group.sync_scoreboard()
    for _ in range(100):
        if not win.group._scoreboard_pending:
            break
        QTest.qWait(10)
    assert win.group._sessions == (session,)


def test_manual_mid_match_recovery_needs_recent_linked_warmup_and_no_lobby(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "FortniteGame.log"
    now = datetime(2026, 11, 10, 20, 4, 55, tzinfo=timezone.utc).timestamp()
    path.write_bytes(session_line(SESSION_A) + START)
    os.utime(path, (now, now))
    tail = match_lifecycle.LogTail(path)
    assert tail.active_match(now=now) == (sha256(SESSION_A).hexdigest(), None)
    assert tail.active_match(now=now + 61) is None
    append(path, FRONTEND)
    os.utime(path, (now, now))
    assert tail.active_match(now=now) is None


@pytest.mark.parametrize(("total", "linked"), [(0, True), (7, False)])
def test_qt_manual_capture_links_active_match_only_with_empty_counter(app, monkeypatch, total, linked):
    gui, win, _ = app
    win.set_match_automation(True)
    fingerprint = sha256(SESSION_A).hexdigest()
    monkeypatch.setattr(win._match_tail, "active_match", lambda: (fingerprint, "Squad"))
    seed(gui, win, (total, total))
    assert win.start_counting()
    assert win._match_active is linked
    assert win._match_fingerprint == (fingerprint if linked else "")
    assert win.primary_counts == (total, total)


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

@pytest.mark.parametrize("next_match_started", [False, True])
def test_replay_updates_lobby_display_without_replacing_live_state(app, next_match_started):
    gui, win, _ = app
    seed(gui, win, (1, 2))
    assert win.save_current_match()
    match, = gui.match_history.read_history(win.match_history_path())
    if next_match_started:
        win.reset_match_source()
        seed(gui, win, (1, 1))
        win._match_active = True
    timeline = [(1, "Chest", False), (2, "Rare chest", False), (3, "Buried chest", False),
                (4, "Sprite poster chest", False), (5, "Other chest", False), (6, "Chest", True)]
    win.apply_verified_replay((match, None, (None, None, 5, {}, 1, timeline, "Rin")))
    context = win.dashboard.overlay_context()
    assert (context["personal"]["certain"], context["personal"]["total"]) == (
        (1, 1) if next_match_started else (5, 5))
    assert context["replay_verified"] is not next_match_started
    assert win.counts == ((1, 1) if next_match_started else (1, 2))
    assert win.dashboard.total.text() == ("1" if next_match_started else "5")
    saved, = gui.match_history.read_history(win.match_history_path())
    assert (saved.total, saved.replay["direct"]["certain"], saved.replay["direct"]["estimated"]) == (5, 1, 1)
    if not next_match_started:
        cfg = gui.load_cfg()[0]
        display = {**cfg["display"], "use_custom_sections": True}
        text = win.dashboard.screen_overlay_text(display)
        assert "Chests · 5" in text and "Confirmed · 5" in text and "Estimated · 0" in text
        assert "Replay verified" not in text
        win.dashboard.write_overlay_context(display, context)
        obs = (gui.paths.APP_DIR / cfg["state"]["obs_dir"] / "chest_counter.txt").read_text(encoding="utf-8")
        assert obs.strip() == text
        assert context["cumulative"]["total"] == 5
        for chest in ("Chest", "Rare chest", "Buried chest", "Spirit poster chest", "Unknown type"):
            assert f"{chest} 1" in context["personal"]["types"]
        win.reset_match_source()
        assert not win.dashboard.overlay_context()["replay_verified"]
        assert win.dashboard.total.text() == "0"


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

def test_manual_replay_retry_updates_same_history_row(app, monkeypatch, tmp_path):
    from datetime import datetime

    gui, win, _ = app
    gui.C.update_config_file(gui.config_file(), "app", {"auto_verify_replay": False})
    path = tmp_path / "reader.dll"
    path.touch()
    monkeypatch.setattr(gui.replay_comparison, "reader_path", lambda: path)
    match = gui.match_history.Match("retry", "2026-11-11 13:13", "p", "Default", "Duo", 1, 3, "0%")
    gui.match_history.save_match(win.match_history_path(), match)
    win.go_to(win.pages.indexOf(win.group))
    win.group.load()
    jobs = []

    class PendingThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            jobs.append(self.target)

    monkeypatch.setattr(gui.threading, "Thread", PendingThread)
    monkeypatch.setattr(gui.replay_comparison, "verify_local_replay", lambda *_args, **_kwargs: None)
    win.group.retry_replay.click()
    assert win.group.history.topLevelItem(0).text(10) == "Checking replay..."
    assert not win.group.retry_replay.isEnabled()
    win.start_replay_verification(match, manual=True)
    assert len(jobs) == 1
    jobs.pop()()
    assert win.group.history.topLevelItem(0).text(10) == "No matching replay found"
    assert win.group.retry_replay.isEnabled()

    def verify(selected, finished_at, *, wait_seconds):
        assert selected == match
        assert datetime.fromtimestamp(finished_at) == datetime(2026, 11, 11, 13, 13, 30)
        return path, None, 2, {}, 0, [(1, "Chest", False), (2, "Rare chest", False)], "Rin"

    monkeypatch.setattr(gui.replay_comparison, "verify_local_replay", verify)
    win.group.retry_replay.click()
    jobs.pop()()
    saved, = gui.match_history.read_history(win.match_history_path())
    assert saved.snapshot == match.snapshot and saved.total == saved.certain == 2
    assert win.group.history.topLevelItem(0).text(10) == "Verified"
    assert not win.group.retry_replay.isEnabled()
    assert path.exists()


def test_match_details_show_replay_delta_and_type_changes(app, monkeypatch):
    gui, win, _ = app
    match = gui.match_history.Match("recap", "2026-11-11 13:13", "p", "Default", "Duo",
                                    1, 1, "0%", chest_types={"chest": 1, "unknown": 1})
    path = win.match_history_path()
    gui.match_history.save_match(path, match)
    timeline = [[1, "Chest", False], [2, "Rare chest", False], [3, "Rare chest", False], [4, "Chest", True]]
    verified = gui.match_history.verify_match(path, match.snapshot, 3, "0%", {"chest": 1, "rare": 2},
                                              {"owner": "Rin", "timeline": timeline, "destroyed": 1})
    shown = []
    monkeypatch.setattr(gui, "show_notice_dialog", lambda _parent, _title, text: shown.append(text))
    win.group.load_history()
    win.group._open_match_details(win.group.history.topLevelItem(0), 2)
    win.stats.open_match(verified)
    for details in shown:
        assert "Live 2 → Replay 3 chests (+1)" in details
        assert "Rare chest: 0 → 2" in details
        assert "Unknown type: 1 → 0" in details
    assert len(shown) == 2
    assert "Chest: 1 → 2" not in shown[0]


def test_local_replay_uses_timestamps_instead_of_group_alias(tmp_path):
    from dataclasses import replace
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

    row["SessionId"] = "A" * 32
    report.write_text(json.dumps({"Matches": [row]}), encoding="utf-8")
    linked = replace(match, session_fingerprint=sha256(b"a" * 32).hexdigest())
    assert replay_comparison.read_report(report, linked, local_start=start + timedelta(seconds=7))[4] == 1
    with pytest.raises(ValueError, match="Expected one replay"):
        replay_comparison.read_report(report, replace(linked, session_fingerprint="b" * 64), local_start=start)

def test_replay_selects_unique_match_id_among_nearby_files(tmp_path, monkeypatch):
    from datetime import datetime
    from fortnite_chest_counter import match_history, replay_comparison as replay

    reader = tmp_path / "reader.dll"
    reader.touch()
    demos = tmp_path / "Demos"
    demos.mkdir()
    paths = [demos / f"UnsavedReplay-2026.11.11-13.00.0{i}.replay" for i in range(2)]
    for path in paths:
        path.write_bytes(b"fixture")
    monkeypatch.setattr(replay, "reader_path", lambda: reader)
    monkeypatch.setattr(replay.shutil, "which", lambda _: "dotnet")
    monkeypatch.setattr(replay, "demos_path", lambda: demos)
    monkeypatch.setattr(replay.time, "sleep", lambda _delay: None)
    match = match_history.Match("match", datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "p", "Default", "Duo", 0, 1, "0%", session_fingerprint="a" * 64)
    calls = []

    def read(_reader, path, identity, _match, *, timeout):
        calls.append(path)
        if path == paths[0]:
            raise replay.ReplayAssociationError("wrong session ID")
        return path, identity, 1, {}, 0, [(1, "Chest", False)], "Rin"

    monkeypatch.setattr(replay, "_read_local_candidate", read)
    assert replay.verify_local_replay(match, paths[0].stat().st_mtime)[0] == paths[1]
    assert calls == paths
    from dataclasses import replace
    with pytest.raises(replay.ReplayAssociationError, match="Match ID unavailable"):
        replay.verify_local_replay(replace(match, session_fingerprint=""), paths[0].stat().st_mtime)


def test_dashboard_phase_and_setup_check(app, monkeypatch, tmp_path):
    gui, win, log = app
    log.write_text("Fortnite started", encoding="utf8")
    monkeypatch.setattr(gui.paths, "local_fortnite_hud_scale", lambda: 0.73)
    monkeypatch.setattr(gui.replay_comparison, "reader_path", lambda: tmp_path / "missing.dll")
    report = gui.SetupPage.check_setup(gui.load_cfg()[0], {"source": "desktop-hdr"}, None, "guest", "")
    assert "HDR → SDR selected" in report
    assert "73% read" in report and "Game log readable" in report
    assert "Replay reader or .NET missing" in report
    win._match_active = True
    win.worker.alive = True
    win.mode = "counting"
    win.update_match_phase()
    assert win.dashboard.match_phase.text() == "In match"
    win.set_group_delivery_status("Discord correction pending")
    assert "Discord correction pending" in win.dashboard.match_phase.text()
    win.worker.alive = False
    win._match_active = False
    win.mode = "stopped"
    seed(gui, win, (1, 2))
    assert win.save_current_match()
    saved, = gui.match_history.read_history(win.match_history_path())
    win.set_replay_status(saved, "Checking replay...")
    assert "Lobby · verifying replay" in win.dashboard.match_phase.text()

@pytest.mark.parametrize("automatic_start", [True, False])
def test_restart_can_reattach_nonzero_counts_to_same_active_match(app, monkeypatch, automatic_start):
    gui, win, log = app
    fingerprint = sha256(SESSION_A).hexdigest()
    win.set_match_automation(True)
    if automatic_start:
        append(log, session_line(SESSION_A) + START)
        win.poll_match_log()
    else:
        monkeypatch.setattr(win._match_tail, "active_match", lambda: (fingerprint, "Duo"))
        assert win.start_counting()
    assert win._match_fingerprint == fingerprint
    seed(gui, win, (5, 9))

    restarted = gui.MainWindow()
    restarted.timer.stop()
    restarted.slow.stop()
    restarted.match_timer.stop()
    restarted.worker = type(win.worker)()
    restarted._fill_sources([], [(0, 0, 1280, 720)])
    restarted.dashboard.source.setCurrentIndex(
        restarted.dashboard.source.findData({"source": "desktop", "monitor": 1}))
    restarted.set_match_automation(True)
    monkeypatch.setattr(restarted._match_tail, "active_match", lambda: (fingerprint, "Duo"))
    try:
        assert restarted.counts == (5, 9)
        assert restarted.start_counting()
        assert restarted._match_active and restarted._match_fingerprint == fingerprint
        assert restarted.counts == (5, 9)
        append(log, FRONTEND)
        restarted.poll_match_log()
        exit_worker(restarted)
        saved, = gui.match_history.read_history(restarted.match_history_path())
        assert saved.total == 9 and saved.session_fingerprint == fingerprint
    finally:
        restarted.worker.alive = False
        restarted._workers_pending.clear()
        restarted.close()

def test_match_history_discord_status_and_retry_selected_row(app, monkeypatch):
    gui, win, _ = app
    webhook = gui.discord_group.Webhook("123456789012345678", "fixture-webhook-token")
    monkeypatch.setattr(gui.discord_group, "load_webhook", lambda: webhook)
    match = gui.match_history.Match("selected", "2026-11-11 13:13", "p", "Default", "Duo",
                                    4, 0, "0%", player="Rin", group_webhook_id=webhook.id)
    gui.match_history.save_match(win.match_history_path(), match)
    win.group.load_player_setup()
    win.group.load_history()
    row = win.group.history.currentItem()
    assert row.text(11) == "Pending"
    win.group.receive_delivery_states(webhook.id, {match.snapshot: ("Retry needed", False)})
    assert row.text(11) == "Retry needed" and win.group.retry_discord.isEnabled()
    called = []
    monkeypatch.setattr(win.discord, "retry_group_outbox", lambda *, snapshot: called.append(snapshot) or True)
    win.group.retry_discord.click()
    assert called == [match.snapshot]
    win.group.receive_delivery_states(webhook.id, {match.snapshot: ("Sent", False)})
    assert row.text(11) == "Sent" and not win.group.retry_discord.isEnabled()

def test_recover_interrupted_match_uses_lobby_timestamp_and_saves_once(app, monkeypatch):
    from datetime import datetime, timezone

    gui, win, log = app
    fingerprint = sha256(SESSION_A).hexdigest()
    gui.C.set_counts(win._cfg_for_actions(), 5, 9, match_fingerprint=fingerprint)
    win.read_state(force=True)
    win.set_detected_mode("Squad")
    append(log, session_line(SESSION_A) + START + FRONTEND + session_line(SESSION_B) + START)
    notices = []
    monkeypatch.setattr(gui, "show_notice_dialog", lambda _, title, message: notices.append((title, message)))
    checks = []
    monkeypatch.setattr(win, "start_replay_verification", lambda match, **kwargs: checks.append((match, kwargs)))
    win.recover_interrupted_match()
    win.recover_interrupted_match()

    saved, = gui.match_history.read_history(win.match_history_path())
    ended = datetime(2026, 11, 10, 20, 5, 2, 589000, tzinfo=timezone.utc)
    assert saved.session_fingerprint == fingerprint and saved.recovered_partial and saved.total == 9
    assert saved.finished_at == ended.astimezone().strftime("%Y-%m-%d %H:%M")
    assert saved.mode == "Unknown"
    assert len(checks) == 1 and checks[0][1]["finished_at"] == ended.timestamp()
    assert "already" in notices[-1][1]
    win.group.load_history()
    assert win.group.history.topLevelItem(0).text(10) == "Partial result"


def test_finished_match_requires_lobby_between_two_warmups(tmp_path):
    log = tmp_path / "FortniteGame.log"
    log.write_bytes(session_line(SESSION_A) + START + session_line(SESSION_B) + START + FRONTEND)
    tail = match_lifecycle.LogTail(log)
    assert tail.finished_match(sha256(SESSION_A).hexdigest()) is None
    assert tail.finished_match(sha256(SESSION_B).hexdigest()) is not None

def test_stats_quality_filters_preserve_original_live_data_and_combine(app):
    from datetime import datetime

    gui, win, _ = app
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    history = win.match_history_path()
    match_type = gui.match_history.Match
    def save(snapshot, mode, count, unknown, stamp=date):
        match = match_type(snapshot, stamp, "p", "Default", mode, 0, count, "0%",
                           chest_types={"unknown": unknown})
        gui.match_history.save_match(history, match)

    save("verified", "Duo", 12, 3)
    save("unverified", "Duo", 13, 3)
    save("different-mode", "Solo", 7, 4)
    save("older", "Duo", 7, 4, "2000-01-01 12:00")
    timeline = [[i, "Chest", i >= 7] for i in range(9)]
    gui.match_history.verify_match(history, "verified", 7, "0%", {"chest": 7}, {"timeline": timeline})
    gui.match_history.edit_match(history, "verified", 11, 11, "0%")
    page = win.stats
    page.load()
    page.period.setCurrentIndex(page.period.findData(7))
    page.mode.setCurrentIndex(page.mode.findData("Duo"))

    for quality, snapshot in (("unknown", "verified"), ("unverified", "unverified"), ("delta", "verified")):
        page.quality.setCurrentIndex(page.quality.findData(quality))
        assert page.trend_grouping.currentData() is True
        assert [match.snapshot for match in page.trend.visible_matches] == [snapshot]
        assert "1 matches" in page.summary.text()
