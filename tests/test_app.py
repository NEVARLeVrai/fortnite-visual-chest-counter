"""
Desktop-app (v0.2.0) checks that need no Qt: the worker protocol, the stop channel, the preview block, the app's count
editing, finding the program folder from the release runtime, and - with the sample recordings present - proof that the
app's worker counts exactly what the command line counts.        Run:   python tests/test_app.py     (or pytest)
"""

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "src"))   # run without installing

import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from fortnite_chest_counter import counter as C
from fortnite_chest_counter import paths, settings
from fortnite_chest_counter.console import CONSOLE


@pytest.fixture(autouse=True)
def isolate_discord_credentials(monkeypatch):
    from fortnite_chest_counter import discord_group, discord_summary

    monkeypatch.setattr(discord_group, "load_webhook", lambda: None)
    monkeypatch.setattr(discord_summary, "load", lambda: None)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "video tests" / "Fortnite-PS5-Pro-HD-No-Audio.mp4"


def tmp_cfg(tmp):
    tmp = Path(tmp)
    cfg, _, _ = settings.load_config(None, {("state", "path"): str(tmp / "state" / "chest_state.json"),
                                            ("state", "obs_dir"): str(tmp / "obs_output"), ("logging", "dir"): str(tmp / "logs")})
    return cfg


@contextlib.contextmanager
def machine_mode():
    buf = io.StringIO()
    CONSOLE.machine = True
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        CONSOLE.machine = False


def messages(buf):
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ------------------------------------------------------------------ worker protocol

def test_machine_mode_turns_every_message_into_one_json_line():
    ev = {"time": 1.0, "source": "fused", "adjusted_tier": "HIGH", "certain_counted": True, "approx_counted": True,
          "popup_score": 0.93, "reason": "popup 0.93 en:CHEST -> HIGH", "event_id": "E0001", "stamp": "00:01.00"}
    with machine_mode() as buf:
        CONSOLE.info("hello")
        CONSOLE.warn("careful")
        CONSOLE.error("broken\nsecond line")
        CONSOLE.line("plain", "bold")
        CONSOLE.status("15 fps", fps=15.0, kind="x")
        CONSOLE.event(ev, "1", "1", 1, 1)
        CONSOLE.emit("counts", certain=1, approx=1, kind="kind is an ordinary field")
    out = messages(buf)
    assert [m["type"] for m in out] == ["info", "warn", "error", "line", "status", "event", "counts"]
    assert out[2]["text"] == "broken\nsecond line"
    assert out[4]["fps"] == 15.0 and out[4]["kind"] == "x"
    assert out[5]["verdict"] == "certain" and out[5]["certain"] == 1 and out[5]["evidence"].startswith("match 0.93")
    assert "\x1b[" not in buf.getvalue()                      # never colour codes in machine mode


def test_machine_mode_sends_rejected_and_generic_events_whatever_the_verbosity():
    CONSOLE.verbosity = "minimal"
    try:
        with machine_mode() as buf:
            CONSOLE.event({"source": "interaction_object", "certain_counted": False, "approx_counted": False}, "0", "0")
            CONSOLE.event({"source": "popup", "popup_score": 0.7, "adjusted_tier": "LOW", "certain_counted": False,
                           "approx_counted": False}, "0", "0")
    finally:
        CONSOLE.verbosity = "normal"
    out = messages(buf)
    assert [(m["verdict"], m["generic"]) for m in out] == [("rejected", True), ("rejected", False)]


def test_text_console_is_unchanged_by_machine_mode_code():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CONSOLE.emit("counts", certain=1)                       # no-op outside machine mode
        CONSOLE.info("hello")
    assert buf.getvalue() == "hello\n"


def test_session_announces_counts_and_overlay():
    with tempfile.TemporaryDirectory() as tmp, machine_mode() as buf:
        session = C.CounterSession(tmp_cfg(tmp))
        session.apply([{"time": 1.0, "source": "popup", "adjusted_tier": "HIGH", "certain_counted": True,
                        "approx_counted": True, "reason": "t"}], 2.0)
        session.announce()
    out = messages(buf)
    assert out[0]["type"] == "event" and out[0]["certain"] == 1 and out[0]["approx"] == 1
    counts = [m for m in out if m["type"] == "counts"]
    assert counts[-1] == {"type": "counts", "certain": 1, "approx": 1, "certain_text": "1", "approx_text": "1",
                          "overlay": "1 certain • ~1 estimated"}
    types = [m for m in out if m["type"] == "types"]                  # v0.2.2: the chest-type split follows the counts
    assert out[-1]["type"] == "types" and types[-1]["breakdown"]["unknown"] == {"certain": 1, "approx": 1}
    assert types[-1]["breakdown"]["unclassified"] == {"certain": 0, "approx": 0}


# ------------------------------------------------------------------ stop channel

def test_app_control_stops_on_the_stop_line_and_on_end_of_input():
    stopped = []
    control = C.AppControl(io.StringIO("hello\nstop\n"))
    control.attach(lambda: stopped.append(1))
    assert control.requested.wait(2) and control.reason == "stopped in the app"
    control = C.AppControl(io.StringIO(""))                      # the app vanished: Windows closes the pipe
    assert control.requested.wait(2) and control.reason == "the app closed"
    late = []
    control.attach(lambda: late.append(1))                       # a source created after the request still stops
    assert late == [1]


class _FakeSource:
    """Frames forever until request_stop - like a camera."""
    label = "fake"
    frames_processed = frames_dropped = 0

    def __init__(self):
        self._stop = threading.Event()

    def open(self, timeout=15.0):
        return {"input_resolution": (1280, 720), "input_fps": 30.0}

    def request_stop(self):
        self._stop.set()

    def frames(self):
        t = 0.0
        while not self._stop.is_set():
            t += 1 / 15
            self.frames_processed += 1
            yield t, np.zeros((720, 1280, 3), np.uint8)
            time.sleep(0.005)

    def elapsed(self):
        return 1.0

    def close(self, wait=True):
        pass


def test_gui_worker_runs_live_and_stops_cleanly_on_request():
    from fortnite_chest_counter.input import video_sources as sources
    real_make, real_ff = sources.make_source, paths.ffmpeg_executable
    sources.make_source = lambda *a, **k: _FakeSource()
    paths.ffmpeg_executable = lambda: "ffmpeg"
    read, write = os.pipe()
    stdin = sys.stdin
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[logging]\nenabled = true\n', encoding="utf-8")
            sys.stdin = os.fdopen(read, "r")
            threading.Timer(1.5, lambda: os.write(write, b"stop\n")).start()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = C.main(["live", "--gui", "--state", str(Path(tmp) / "s.json"), "--obs-dir", str(Path(tmp) / "o"),
                               "--log-dir", str(Path(tmp) / "l"), "--config", str(config)])
            log = [json.loads(l) for l in next((Path(tmp) / "l").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    finally:
        sources.make_source, paths.ffmpeg_executable, sys.stdin = real_make, real_ff, stdin
        CONSOLE.machine, paths.HIDE_CHILD_CONSOLES = False, False
        os.close(write)
    out = messages(buf)
    kinds = [m["type"] for m in out]
    assert code == 0 and "connected" in kinds and kinds[-1] == "stopped"
    assert out[-1]["stop_reason"] == "stopped in the app" and out[-1]["frames_processed"] > 10
    assert log[-1]["event"] == "session_end" and log[-1]["stop_reason"] == "stopped in the app"
    assert next(r for r in log if r.get("event") == "session_start")["ui"] == "app"


# ------------------------------------------------------------------ live preview

def test_preview_block_round_trip_and_only_when_wanted():
    from fortnite_chest_counter.preview import PreviewReader, PreviewWriter, block_name
    name = block_name(uuid.uuid4().hex[:12])
    reader = PreviewReader(name)
    writer = PreviewWriter(name, fps=1000)
    frame = np.random.default_rng(1).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    try:
        assert writer.offer(1.0, frame) is False and reader.read() is None          # hidden preview: nothing copied
        reader.set_wanted(True)
        time.sleep(0.002)
        assert writer.offer(2.5, frame) is True
        t, w, h, data = reader.read()
        assert (t, w, h) == (2.5, 640, 360)
        assert np.array_equal(np.frombuffer(data, np.uint8).reshape(360, 640, 3), frame[::2, ::2])
        assert reader.read() is None                                                # the same picture is not returned twice
    finally:
        writer.close()
        reader.close()


def test_hdr_capture_can_publish_a_preview_without_detector_frames():
    from types import SimpleNamespace

    from fortnite_chest_counter.input.video_sources import LiveSource, tone_map_scrgb
    from fortnite_chest_counter.preview import PreviewReader, PreviewWriter, block_name

    name = block_name(uuid.uuid4().hex[:12])
    reader = PreviewReader(name)
    writer = PreviewWriter(name)
    source = LiveSource("test HDR", [], 15, hdr=True)
    frame = np.full((720, 1280, 4), 2.0, dtype="<f2")
    source._process = SimpleNamespace(stdout=io.BytesIO(frame.tobytes()))
    source._t0 = time.monotonic()
    source.set_preview(writer)
    try:
        reader.set_wanted(True)
        source._read_frames()
        assert source.frames_processed == 0
        _, width, height, data = reader.read()
        expected = tone_map_scrgb(frame[::2, ::2], exposure=0.25)
        assert np.array_equal(np.frombuffer(data, np.uint8).reshape(height, width, 3), expected)
        _, detector_frame = next(source.frames())
        assert np.array_equal(detector_frame, tone_map_scrgb(frame))
    finally:
        source.set_preview(None)
        writer.close()
        reader.close()


# ------------------------------------------------------------------ the app's count editing

def test_set_counts_and_reset_from_the_app():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = tmp_cfg(tmp)
        cfg["logging"]["enabled"] = True
        session = C.set_counts(cfg, 3, 150)
        assert (session.state["certain"], session.state["approximate"]) == (3, 150)
        assert C.load_state(cfg["state"]["path"])["approximate"] == 150
        assert (Path(tmp) / "obs_output" / "chest_counter.txt").read_text(encoding="utf-8") == \
            "3 certain • ~150 estimated\n"
        for bad in ((5, 4), (-1, 3), (0, C.MAX_STARTING_COUNT + 1)):
            try:
                C.set_counts(cfg, *bad)
                raise AssertionError(f"accepted {bad}")
            except C.UserError:
                pass
        assert C.load_state(cfg["state"]["path"])["approximate"] == 150             # refused values change nothing
        C.set_counts(cfg, 0, 0, "reset")
        state = C.load_state(cfg["state"]["path"])
        assert (state["certain"], state["approximate"]) == (0, 0)
        actions = [json.loads(line).get("action") for log in (Path(tmp) / "logs").glob("*.jsonl")
                   for line in log.read_text(encoding="utf-8").splitlines()]
        assert "set_count" in actions and "reset" in actions


def test_a_running_counter_adopts_the_apps_change():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = tmp_cfg(tmp)
        live = C.CounterSession(cfg)
        live.save()
        time.sleep(0.02)
        C.set_counts(cfg, 0, 40)
        assert live.sync_external_changes() is True and live.state["approximate"] == 40


# ------------------------------------------------------------------ program folder

def test_release_folder_is_found_from_the_runtime_python():
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp) / "My Counter"
        (app / "runtime" / "lib" / "fortnite_chest_counter").mkdir(parents=True)
        (app / "runtime" / "python").mkdir(parents=True)
        assert paths._release_root(app / "runtime" / "python") == app
        assert paths._release_root(Path(tmp) / ".venv" / "Scripts") is None
        assert paths._release_root(app / "runtime") is None


def test_child_process_flags_hide_consoles_only_when_asked_or_windowless():
    before = paths.HIDE_CHILD_CONSOLES
    try:
        paths.HIDE_CHILD_CONSOLES = True
        assert paths.child_process_flags() == (0x08000000 if os.name == "nt" else 0)
    finally:
        paths.HIDE_CHILD_CONSOLES = before


# ------------------------------------------------------------------ language choice (Settings dropdown)

def test_language_choices_map_to_the_detector_setting_and_back():
    names = [name for name, _ in settings.LANGUAGE_CHOICES]
    assert names == ["English", "Portuguese (Brazil)", "Both (English + Portuguese (Brazil))", "French"]
    for i, (_, langs) in enumerate(settings.LANGUAGE_CHOICES):
        assert settings.language_choice(langs) == i
    assert settings.language_choice(["pt", "en"]) == 2                 # order does not matter
    assert settings.language_choice(settings.DEFAULTS["detector"]["languages"]) == 0   # English


def test_language_choice_is_saved_and_existing_configs_keep_their_language():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        # a v0.1.x config that chose Portuguese only, with comments and other settings
        path.write_text('# my settings\n[detector]\nlanguages = ["pt"]\nprocess_fps = 15\n\n[display]\nshow = "approx"\n',
                        encoding="utf-8")
        cfg, _, warnings = settings.load_config(path)
        assert not warnings and settings.language_choice(cfg["detector"]["languages"]) == 1
        for _, langs in settings.LANGUAGE_CHOICES:                       # what the dropdown writes, read back
            C.update_config_file(path, "detector", {"languages": langs})
            cfg, _, warnings = settings.load_config(path)
            assert not warnings and cfg["detector"]["languages"] == langs
        text = path.read_text(encoding="utf-8")
        assert "# my settings" in text and 'show = "approx"' in text and "process_fps = 15" in text


def test_token_odds_use_total_and_separate_estimated_chests():
    qt = _qt()
    if qt is None:
        return
    from PySide6.QtCore import Qt
    from fortnite_chest_counter.gui import app as A

    previous_dir, previous_app_dir = os.getcwd(), paths.APP_DIR
    personal_template = "Opened {total}\nConfirmed {certain} · Estimated {approx}\nOdds {chance}\nGames {matches}\n{types}"
    cumulative_template = "Lifetime {total}\nLifetime odds {chance}\nGames {matches}"
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        paths.APP_DIR = Path(tmp)
        win = None

        def check_shared_output():
            text = win.dashboard.screen_overlay_text(win.overlay.display)
            assert win.dashboard.screen_overlay.plain_text == text
            assert win.dashboard.overlay_preview.plain_text == text
            assert Path(win.overlay.obs_path.text()).read_text(encoding="utf-8") == text + "\n"

        try:
            C.update_config_file(A.config_file(), "app", {"screen_overlay_shared": False, "auto_match_lifecycle": False})
            C.update_config_file(A.config_file(), "display", {
                "use_custom_sections": True, "screen_show_token": True, "screen_show_matches": False,
                "screen_show_types": False, "screen_personal_title": "My match",
                "screen_personal_template": personal_template, "screen_team_enabled": False,
                "screen_cumulative_enabled": False,
            })
            win = A.MainWindow()
            win.timer.stop()
            win._set(5, 10, "set-count")
            win.read_state(force=True)
            assert win.dashboard.certain.value.text() == "5"
            assert win.dashboard.approx.value.text() == "5"
            assert win.dashboard.total.text() == "10"
            assert win.dashboard.token_odds.text() == "0.007272%"
            win.dashboard.token_boost.setCurrentIndex(1)
            assert win.dashboard.token_odds.text() == "0.010909%"
            win.dashboard.token_boost.setCurrentIndex(2)
            assert win.dashboard.token_odds.text() == "0.014545%"
            win.dashboard.token_boost.setCurrentIndex(0)
            win.overlay.show_screen_overlay.setChecked(True)
            overlay = win.dashboard.screen_overlay
            assert overlay.windowFlags() & Qt.WindowTransparentForInput
            assert overlay.windowFlags() & Qt.WindowStaysOnTopHint
            assert win.overlay.share_screen_overlay.isEnabled()
            assert not win.overlay.share_screen_overlay.isChecked()
            win.overlay.share_screen_overlay.setChecked(True)
            assert overlay.plain_text.splitlines() == [
                "My match", "Opened 10", "Confirmed 5 · Estimated 5", "Odds 0.007272%",
            ]
            check_shared_output()
            win.dashboard.token_mode.setCurrentText("Squad")
            assert win.dashboard.token_odds.text() == "0.007272%"
            win.dashboard.token_mode.setCurrentText("Solo")
            assert win.dashboard.token_odds.text() == "0.007272%"
            win._set(0, 0, "reset")
            win.read_state(force=True)
            win.dashboard.token_mode.setCurrentText("Duo")
            assert win.dashboard.token_odds.text() == "0.000000%"
            assert "0.000000%" in overlay.plain_text

            win.overlay.section_titles["personal"].setText("My counter")
            win.overlay.show_types.setChecked(True)
            win.overlay.show_matches.setChecked(True)
            win.overlay.transparency.setValue(35)
            win._set(2, 3, "set-count")
            win.read_state(force=True)
            win.overlay.apply()
            assert overlay.plain_text.splitlines() == [
                "My counter", "Opened 3", "Confirmed 2 · Estimated 1", "Odds 0.002182%",
                "Games 1", "Chest types · not classified 3",
            ]
            assert win.overlay.preview.plain_text == overlay.plain_text
            assert not overlay.caption.wordWrap()
            assert abs(overlay.windowOpacity() - 0.65) < 0.01
            check_shared_output()

            win.overlay.section_templates["personal"].setPlainText("Opened " * 180 + "{total}")
            win.overlay.apply()
            assert overlay.caption.wordWrap()
            assert overlay.caption.minimumWidth() <= overlay.caption.maximumWidth()
            check_shared_output()
            win.overlay.section_templates["personal"].setPlainText(personal_template)
            win.overlay.show_token.setChecked(False)
            win.overlay.section_enabled["team"].setChecked(True)
            win.overlay.section_enabled["cumulative"].setChecked(True)
            win.overlay.section_titles["cumulative"].setText("All games")
            win.overlay.section_templates["cumulative"].setPlainText(cumulative_template)
            win.overlay.apply()
            assert "Odds" not in overlay.plain_text and "Lifetime odds" not in overlay.plain_text
            assert "Opened 3" in overlay.plain_text and "Lifetime 3" in overlay.plain_text
            assert win.overlay.section_titles["team"].text() not in overlay.plain_text.splitlines()
            assert overlay.plain_text.splitlines()[-3:] == ["All games", "Lifetime 3", "Games 1"]
            check_shared_output()
            win.overlay.show_screen_overlay.setChecked(False)
            assert overlay.isHidden()
            assert not win.overlay.share_screen_overlay.isEnabled()

            win.dashboard.token_mode.setCurrentText("Squad")
            win.dashboard.token_boost.setCurrentIndex(1)
            win.dashboard.show_preview.setChecked(True)
            win.dashboard.show_log.setChecked(False)
            monitors = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
            win._fill_sources([], monitors)
            assert win.dashboard.source.findData({"source": "desktop", "monitor": 2}) >= 0
            assert not hasattr(win.dashboard, "secondary_source")
            win.overlay.show_screen_overlay.setChecked(True)
            win.overlay.share_screen_overlay.setChecked(True)
            if win._source_scan is not None:
                win._source_scan.join(30)
            win.close()
            qt.processEvents()

            win = A.MainWindow()
            win.timer.stop()
            win._fill_sources([], monitors)
            assert win.dashboard.token_mode.currentText() == "Squad"
            assert win.dashboard.token_boost.currentData() == 1.5
            assert win.dashboard.show_preview.isChecked() and not win.dashboard.show_log.isChecked()
            assert win.overlay.show_screen_overlay.isChecked() and win.overlay.share_screen_overlay.isChecked()
            assert win.overlay.show_types.isChecked() and win.overlay.show_matches.isChecked()
            assert not win.overlay.show_token.isChecked()
            assert win.overlay.section_titles["personal"].text() == "My counter"
            assert win.overlay.section_templates["personal"].toPlainText() == personal_template
            assert win.overlay.section_enabled["cumulative"].isChecked()
            assert win.overlay.section_titles["cumulative"].text() == "All games"
            assert win.overlay.section_templates["cumulative"].toPlainText() == cumulative_template
            assert win.overlay.transparency.value() == 35
            check_shared_output()
        finally:
            if win is not None:
                if win._source_scan is not None:
                    win._source_scan.join(30)
                win.close()
                qt.processEvents()
            paths.APP_DIR = previous_app_dir
            os.chdir(previous_dir)


def test_hdr_tone_map_preserves_chromatic_highlights():
    from fortnite_chest_counter.input.video_sources import tone_map_scrgb

    pixels = np.array([[[8, 6, 4.4, 1], [4, 2.6, 4.4, 1], [0, 0, 0, 1],
                        [1.2, 1.2, 1.2, 1]]], dtype=np.float16)
    converted = tone_map_scrgb(pixels, exposure=0.25)
    detector = tone_map_scrgb(pixels / 4)
    assert detector[0, 1].tolist() == converted[0, 1].tolist()
    assert min(detector[0, 1, 0], detector[0, 1, 2]) > 170
    assert converted.dtype == np.uint8
    assert 255 >= converted[0, 0, 0] > converted[0, 0, 1] > converted[0, 0, 2]
    red, green, blue = map(int, converted[0, 1])
    assert green + 8 < min(red, blue)
    assert not converted[0, 2].any()
    assert 145 <= converted[0, 3, 0] <= 151


def _feed(cap, seconds, fps=15, start_t=100.0):
    """Synthetic frames (a moving bar, cheap to encode) at recording speed, never overrunning the queue."""
    for i in range(int(seconds * fps)):
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[:, (i * 7) % 1280] = 255
        while cap._queue.full():
            time.sleep(0.002)
        cap.offer(start_t + i / fps, frame)


def test_capture_events_mode_keeps_only_the_video_around_decisions_and_marks():
    from fortnite_chest_counter.capture import Capture, read_labels
    from fortnite_chest_counter import review
    with tempfile.TemporaryDirectory() as tmp:
        cap = Capture(tmp, "events", 15, 1280, 720, before=10, after=10, mark_before=30, mark_after=10)
        _feed(cap, 35)                                                  # live t 100 .. 135
        cap.add_events([{"event_id": "E0001", "time": 135.0, "stamp": "02:15.00", "source": "popup",
                         "adjusted_tier": "HIGH", "popup_score": 0.9, "popup_template": "en:CHEST",
                         "certain_counted": True, "approx_counted": True, "reason": "t"}])
        _feed(cap, 35, start_t=135.0)                                   # live t 135 .. 170
        cap.add_mark(160.0, "missed")
        summary = cap.close()
        video = Path(summary["folder"]) / "video"
        kept = sorted(p.name for p in video.glob("seg_*.mkv"))
        # keep windows (recording s): event 35 -> 25..45, mark 60 -> 30..70  =>  segments 20-30 .. 60-70 only
        assert kept == [f"seg_{i:05d}.mkv" for i in range(2, 7)], kept
        clips = json.loads((video / "clips.json").read_text(encoding="utf-8"))
        assert len(clips) == 1 and clips[0]["start"] == 20.0 and clips[0]["end"] == 70.0
        assert summary["frames_not_recorded"] == 0 and summary["events"] == 1 and summary["marks"] == 1
        events = [json.loads(l) for l in (Path(summary["folder"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert events[0]["recording_time"] == 35.0 and events[0]["clip_time"] == 15.0
        n = sum(1 for _ in review.frames(Path(summary["folder"]) / clips[0]["file"], 15))
        assert n == 750                                                 # 50 s of the clip, every frame
        labels = (Path(summary["folder"]) / "labels.csv").read_text(encoding="utf-8-sig").splitlines()
        assert labels[0].startswith("kind,id,live_time") and len(labels) == 3 and read_labels(summary["folder"]) == []


def test_capture_session_mode_keeps_everything_and_off_starts_nothing():
    from fortnite_chest_counter.capture import Capture
    with tempfile.TemporaryDirectory() as tmp:
        cap = Capture(tmp, "session", 15, 1280, 720)
        _feed(cap, 22)
        summary = cap.close()
        clips = json.loads((Path(summary["folder"]) / "video" / "clips.json").read_text(encoding="utf-8"))
        assert [c["file"] for c in clips] == ["video/session.ffconcat"] and clips[0]["end"] > 21.9
        cfg = tmp_cfg(tmp)
        assert cfg["capture"]["mode"] == "off" and C._open_capture(cfg, "file", "x", None, None, 15) is None


def test_lossless_capture_replays_the_exact_frames_and_generic_prompts_keep_no_video():
    from fortnite_chest_counter.capture import Capture
    from fortnite_chest_counter import review
    rng = np.random.default_rng(7)
    frames_in = [rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8) for _ in range(3)]
    with tempfile.TemporaryDirectory() as tmp:
        cap = Capture(tmp, "events", 15, 1280, 720, quality="lossless")
        for i in range(45):                                             # 3 s of noise frames
            while cap._queue.full():
                time.sleep(0.002)
            cap.offer(i / 15, frames_in[i % 3])
        cap.add_events([{"event_id": "E0001", "time": 1.0, "source": "interaction_object", "adjusted_tier": "-",
                         "certain_counted": False, "approx_counted": False}])     # generic prompt: no video kept
        assert cap.keep == []
        cap.add_mark(2.0)
        summary = cap.close()
        info = json.loads((Path(summary["folder"]) / "capture.json").read_text(encoding="utf-8"))
        clips = json.loads((Path(summary["folder"]) / "video" / "clips.json").read_text(encoding="utf-8"))
        assert info["codec"] == "FFV1 lossless RGB" and len(clips) == 1
        out = [f for _, f in review.frames(Path(summary["folder"]) / clips[0]["file"], 15)]
        assert len(out) == 45 and all(np.array_equal(out[i], frames_in[i % 3]) for i in range(45))   # bit-exact


def test_capture_never_blocks_counting_even_if_the_encoder_dies():
    from fortnite_chest_counter.capture import Capture
    with tempfile.TemporaryDirectory() as tmp:
        cap = Capture(tmp, "events", 15, 1280, 720)
        cap._encoder.kill()                                             # the encoder vanishes mid-session
        frame = np.zeros((720, 1280, 3), np.uint8)
        t0 = time.monotonic()
        for i in range(200):                                            # far more than the queue holds
            cap.offer(i / 15, frame)
        assert time.monotonic() - t0 < 1.0                              # offer() never waits
        summary = cap.close(timeout=3)
        assert time.monotonic() - t0 < 15 and summary["stopped_early"]


def test_review_scores_live_and_replay_against_the_labels():
    from fortnite_chest_counter import review
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        live = [{"event_id": "E0001", "time": 10.0, "adjusted_tier": "HIGH", "certain_counted": True, "approx_counted": True},
                {"event_id": "E0002", "time": 30.0, "adjusted_tier": "LOW", "certain_counted": False, "approx_counted": False},
                {"event_id": "E0003", "time": 70.0, "adjusted_tier": "MEDIUM", "certain_counted": False, "approx_counted": True}]
        (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in live), encoding="utf-8")
        (folder / "labels.csv").write_text(
            "kind,id,live_time,clip,clip_time,decision,tier,evidence,truth,note\n"
            "event,E0001,00:10.00,,,certain,HIGH,,chest,\n"
            "event,E0002,00:30.00,,,not counted,LOW,,chest,blurry\n"
            "missed,,00:50.00,,,,,,missed_chest,nobody saw it\n"
            "event,E0003,01:10.00,,,approx,MEDIUM,,not_chest,a supply drop\n"
            "missed,,soon,,,,,,chest,bad time is skipped\n", encoding="utf-8-sig")
        replayed = [{"live_time": 10.1, "adjusted_tier": "HIGH", "certain_counted": True, "approx_counted": True},
                    {"live_time": 30.0, "adjusted_tier": "MEDIUM", "certain_counted": False, "approx_counted": True}]
        result = review.compare(folder, replayed)
        assert result["same"] == 1 and len(result["labels"]) == 4
        assert review.score(result["labels"], "live") == {"real": 3, "found_certain": 1, "found_approx": 1,
                                                         "wrongly_counted": 1, "labelled_not_chest": 1}
        assert review.score(result["labels"], "replay") == {"real": 3, "found_certain": 1, "found_approx": 2,
                                                           "wrongly_counted": 0, "labelled_not_chest": 1}
        text = review.report(folder, result, settings.DEFAULTS["detector"])
        assert "<- different" in text and "real chests 3" in text


# ------------------------------------------------------------------ progress / time left

def test_time_left_comes_only_from_measured_speed():
    from fortnite_chest_counter.progress import Eta, describe, clock_text
    now = [0.0]
    eta = Eta(clock=lambda: now[0])
    assert eta.update(0, 100) is None                                   # first report starts the clock
    now[0] = 1.0
    assert eta.update(5, 100) is None                                   # too early to trust
    for i in range(2, 9):                                               # steady 5 video-s per wall-s
        now[0] = float(i)
        left = eta.update(5 * i, 100)
    assert left is not None and abs(left - (100 - 40) / 5) < 0.5
    assert Eta(clock=lambda: 0).update(10, 0) is None                    # unknown total: never an estimate
    assert describe(None) == "" and describe(5) == "less than 10 s left" and describe(130) == "about 2 min 10 s left"
    assert describe(3 * 3600 + 600) == "about 3 h 10 min left" and clock_text(3725) == "1:02:05"


def _synthetic_capture(tmp, seconds=12):
    from fortnite_chest_counter.capture import Capture
    cap = Capture(tmp, "session", 15, 1280, 720)
    _feed(cap, seconds)
    return Path(cap.close()["folder"])


def test_review_reports_progress_finishes_and_can_be_stopped():
    from fortnite_chest_counter import review
    with tempfile.TemporaryDirectory() as tmp:
        folder = _synthetic_capture(tmp)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "fortnite_chest_counter", "review", tmp, "--gui"], cwd=tmp, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            out = process.stdout.read().decode("utf-8")                  # stdin stays open: no stop requested
            process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
        msgs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        kinds = [m["type"] for m in msgs]
        assert process.returncode == 0 and kinds.count("review_start") == 1 and "review_report" in kinds
        start = next(m for m in msgs if m["type"] == "review_start")
        assert start["captures"] == 1 and 11.5 < start["total_seconds"] < 12.5
        done = [m["done"] for m in msgs if m["type"] == "review_progress"]
        assert done == sorted(done) and done[-1] == start["total_seconds"]
        assert kinds[-1] == "review_finished"
        try:                                                            # the Stop button
            review.run(folder, out=lambda text: None, should_stop=lambda: True)
            raise AssertionError("review did not stop")
        except review.Stopped:
            pass


def test_review_of_an_empty_folder_fails_visibly():
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-m", "fortnite_chest_counter", "review", tmp, "--gui"], cwd=tmp, env=env,
                                stdin=subprocess.PIPE, capture_output=True, timeout=60)
        msgs = [json.loads(l) for l in result.stdout.decode("utf-8").splitlines() if l.startswith("{")]
        assert result.returncode == 1 and any(m["type"] == "error" and "no diagnostic captures" in m["text"] for m in msgs)


def test_recording_can_be_switched_while_counting_continues():
    """capture marks -> mark -> capture session -> capture off, all while the same live session keeps counting."""
    from fortnite_chest_counter.input import video_sources as sources
    real_make, real_ff = sources.make_source, paths.ffmpeg_executable
    sources.make_source = lambda *a, **k: _FakeSource()
    paths.ffmpeg_executable = lambda: "ffmpeg"
    read, write = os.pipe()
    stdin = sys.stdin
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text(f'[capture]\nmode = "off"\ndir = "{Path(tmp).as_posix()}/captures"\n',
                                                  encoding="utf-8")
            (Path(tmp) / "s.json").write_text(json.dumps({"certain": 5, "approximate": 12}), encoding="utf-8")
            sys.stdin = os.fdopen(read, "r")
            script = [(0.6, b"capture marks\n"), (1.4, b"mark\n"), (2.0, b"capture events\n"), (2.8, b"capture session\n"),
                      (3.6, b"capture off\n"), (4.2, b"mark\n"), (4.8, b"stop\n")]
            for at, line in script:
                threading.Timer(at, lambda line=line: os.write(write, line)).start()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = C.main(["live", "--gui", "--config", str(Path(tmp) / "config.toml"), "--state", str(Path(tmp) / "s.json"),
                               "--obs-dir", str(Path(tmp) / "o"), "--log-dir", str(Path(tmp) / "l")])
            msgs = messages(buf)
            folders = sorted(p.parent for p in Path(tmp, "captures").glob("*/capture.json"))
            infos = [json.loads((f / "capture.json").read_text(encoding="utf-8")) for f in folders]
            first_marks = (folders[0] / "marks.jsonl").read_text(encoding="utf-8").splitlines() if folders else []
            final_state = json.loads((Path(tmp) / "s.json").read_text(encoding="utf-8"))
            from fortnite_chest_counter import review
            reviewed = review.run(folders[0], out=lambda text: None)          # the marks-only capture replays
    finally:
        sources.make_source, paths.ffmpeg_executable, sys.stdin = real_make, real_ff, stdin
        CONSOLE.machine, paths.HIDE_CHILD_CONSOLES = False, False
        os.close(write)
    states = [(m["state"], m.get("mode")) for m in msgs if m["type"] == "capture"]
    assert code == 0 and [s for s in states if s[0] == "recording"] == [("recording", "marks"), ("recording", "events"),
                                                                       ("recording", "session")] and ("off", "off") in states
    assert sum(1 for s in states if s[0] == "saved") == 3                    # every recording finished cleanly
    assert [i["mode"] for i in infos] == ["marks", "events", "session"] and all(i["ended_at"] for i in infos)
    assert (final_state["certain"], final_state["approximate"]) == (5, 12)  # switching never touches the totals
    assert reviewed == [folders[0]]
    assert len(first_marks) == 1                                         # the mark went into the marks-only recording
    marks = [m for m in msgs if m["type"] == "mark"]
    assert [m["recorded"] for m in marks] == [True, False]               # the last mark had no recording to go into
    stopped = msgs[-1]
    assert stopped["type"] == "stopped" and stopped["stop_reason"] == "stopped in the app" and stopped["frames_processed"] > 50


def test_mark_button_and_hotkey_need_a_running_recording():
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from fortnite_chest_counter.gui import app as A
    except ImportError:
        print("      (skipped: PySide6 not installed)")
        return

    class FakeWorker:
        def __init__(self):
            self.sent = []

        def running(self):
            return True

        def send(self, line):
            self.sent.append(line)
            return True

        def drain(self):
            return []

    qt = QApplication.instance() or QApplication([])
    here, app_dir = os.getcwd(), paths.APP_DIR
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        paths.APP_DIR = Path(tmp)
        try:
            win = A.MainWindow()
            win.timer.stop()
            fake = FakeWorker()
            assert win.mark_moment() is False and "start counting" in win.dashboard.rec_status.text()
            win.worker = fake
            win.set_mode("counting")
            assert not win.dashboard.mark.isEnabled() and win.mark_moment() is False
            assert "recording is off" in win.dashboard.rec_status.text() and fake.sent == []
            win.dashboard.rec_mode.setCurrentIndex(win.dashboard.rec_mode.findData("marks"))   # while counting
            assert fake.sent == ["capture marks"]
            assert settings.load_config(Path(tmp) / "config.toml")[0]["capture"]["mode"] == "marks"
            win.handle({"type": "capture", "state": "recording", "mode": "session", "folder": "S"})   # worker's real mode
            assert win.dashboard.rec_mode.currentData() == "session" and fake.sent == ["capture marks"]   # shown, not re-sent
            win.handle({"type": "capture", "state": "recording", "mode": "marks", "folder": "X"})
            assert win.dashboard.rec_mode.currentData() == "marks"
            assert win.dashboard.mark.isEnabled() and win.mark_moment() is True and fake.sent[-1] == "mark missed chest"
            win.handle({"type": "mark", "stamp": "01:02.00", "recorded": True})
            assert "Marked 01:02.00" in win.dashboard.rec_status.text()
            win.handle({"type": "capture", "state": "saved", "folder": "OTHER"})            # an older recording finishing
            assert win.recording is not None
            win.handle({"type": "capture", "state": "saved", "folder": "X"})
            assert win.recording is None and not win.dashboard.mark.isEnabled()
            win.handle({"type": "capture", "state": "off", "mode": "events"})                  # a start that failed
            assert win.dashboard.rec_mode.currentData() == "off" and fake.sent[-1] == "mark missed chest"  # shown honestly, not re-sent
            C.update_config_file(Path(tmp) / "config.toml", "logging", {
                "enabled": True, "save_snapshots": True, "log_candidates": True})
            win.settings_page.load()
            win.dashboard.rec_mode.setCurrentIndex(win.dashboard.rec_mode.findData("marks"))
            win.dashboard.rec_mode.setCurrentIndex(win.dashboard.rec_mode.findData("off"))
            saved = settings.load_config(Path(tmp) / "config.toml")[0]
            assert saved["capture"]["mode"] == "off"
            assert all(saved["logging"][key] is False for key in ("enabled", "save_snapshots", "log_candidates"))
            assert not win.settings_page.logging.isChecked() and not win.settings_page.snapshots.isChecked()
            assert fake.sent[-1] == "capture off"
            if win._source_scan is not None:                              # the device scan runs ffmpeg in this folder
                win._source_scan.join(30)
            win.preview_reader and win.preview_reader.close()
            win.deleteLater()
            qt.processEvents()
        finally:
            os.chdir(here)
            paths.APP_DIR = app_dir


def _qt():
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return None
    return QApplication.instance() or QApplication([])


def test_window_layout_round_trip_keeps_size_position_and_maximised_state():
    qt = _qt()
    if qt is None:
        print("      (skipped: PySide6 not installed)")
        return
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMainWindow
    from fortnite_chest_counter.gui import app as A
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state" / "window.json"
        w = QMainWindow()
        w.setGeometry(110, 120, 500, 400)
        w.show()
        qt.processEvents()
        assert A.save_window(w, path) and path.exists()
        again = QMainWindow()
        assert A.restore_window(again, path) and again.geometry() == w.geometry()
        w.showMaximized()                                            # closed while maximised
        qt.processEvents()
        A.save_window(w, path)
        again = QMainWindow()
        assert A.restore_window(again, path) and again.windowState() & Qt.WindowMaximized
        assert again.normalGeometry() == w.normalGeometry()          # un-maximising returns to the normal size and place
        for widget in (w, again):
            widget.deleteLater()
        qt.processEvents()


def test_window_layout_is_moved_back_when_its_screen_is_gone_and_ignored_when_damaged():
    qt = _qt()
    if qt is None:
        print("      (skipped: PySide6 not installed)")
        return
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QMainWindow
    from fortnite_chest_counter.gui import app as A
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "window.json"
        far = QMainWindow()
        far.setGeometry(4000, 3000, 500, 400)                        # e.g. a second monitor that is unplugged now
        far.show()
        qt.processEvents()
        A.save_window(far, path)
        back = QMainWindow()
        assert A.restore_window(back, path)
        assert QGuiApplication.primaryScreen().availableGeometry().contains(back.geometry())
        moved = QMainWindow()                                        # saved on the right-hand monitor of two
        moved.setGeometry(2700, 100, 900, 700)
        A.keep_visible(moved, [QRect(0, 0, 1920, 1040)])
        assert QRect(0, 0, 1920, 1040).contains(moved.geometry()) and moved.size().width() == 900
        kept = QMainWindow()
        kept.setGeometry(100, 100, 900, 700)
        A.keep_visible(kept, [QRect(0, 0, 1920, 1040)])
        assert kept.geometry() == QRect(100, 100, 900, 700)          # a visible window is never moved
        path.write_text("{damaged", encoding="utf-8")
        assert A.restore_window(QMainWindow(), path) is False
        path.unlink()
        assert A.restore_window(QMainWindow(), path) is False        # first start: default layout
        for widget in (far, back, moved, kept):
            widget.deleteLater()
        qt.processEvents()


def test_remember_window_setting_is_on_by_default_saved_on_close_and_off_means_default_layout():
    qt = _qt()
    if qt is None:
        print("      (skipped: PySide6 not installed)")
        return
    from PySide6.QtCore import Qt
    from fortnite_chest_counter.gui import app as A
    here, app_dir = os.getcwd(), paths.APP_DIR
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        paths.APP_DIR = Path(tmp)
        windows = []
        try:
            cfg_path = Path(tmp) / "config.toml"
            cfg_path.write_text('[capture]\nquality = "lossless"\n', encoding="utf-8")      # a config from before this setting
            cfg, _, warnings = settings.load_config(cfg_path)
            assert cfg["app"]["remember_window"] is True and cfg["capture"]["quality"] == "lossless" and not warnings

            def window():
                win = A.MainWindow()
                win.timer.stop()
                windows.append(win)
                return win

            win = window()
            assert win.restore_layout() is False and win.size().width() == 1120          # nothing saved yet
            win.show()
            win.showMaximized()
            qt.processEvents()
            win.close()
            assert A.window_file().exists()                                              # saved on closing
            win = window()
            assert win.restore_layout() and win.windowState() & Qt.WindowMaximized       # reopens the way it was closed
            win.settings_page.load()
            assert win.settings_page.remember_window.isChecked()
            win.settings_page.remember_window.setChecked(False)
            win.settings_page.save()
            cfg = settings.load_config(cfg_path)[0]
            assert cfg["app"]["remember_window"] is False and cfg["capture"]["quality"] == "lossless"   # others kept
            assert not A.window_file().exists()                                          # back to the default layout
            win.show()
            qt.processEvents()
            win.close()
            assert not A.window_file().exists()                                          # off: nothing saved
            win = window()
            assert win.restore_layout() is False and not win.windowState() & Qt.WindowMaximized
        finally:
            for win in windows:
                if win._source_scan is not None:                      # the device scan runs ffmpeg in this folder
                    win._source_scan.join(30)
                win.preview_reader and win.preview_reader.close()
                win.deleteLater()
            qt.processEvents()
            os.chdir(here)
            paths.APP_DIR = app_dir


def test_recordings_started_in_the_same_second_get_their_own_folders():
    from fortnite_chest_counter.capture import Capture
    with tempfile.TemporaryDirectory() as tmp:
        caps = [Capture(tmp, mode, 15, 1280, 720) for mode in ("marks", "events", "session")]   # well within one second
        folders = [c.folder for c in caps]
        for c in caps:
            c.close(timeout=10)
        assert len(set(folders)) == 3 and all(f.is_dir() for f in folders)


def test_marks_arrive_through_the_app_control_channel():
    control = C.AppControl(io.StringIO("mark\nmark missed chest\nstop\n"))
    assert control.requested.wait(2)
    assert control.take_marks() == ["mark", "missed chest"] and control.take_marks() == []


def test_live_capture_replays_to_the_same_decisions():
    """End to end on a real recording: live worker with capture -> review replays the clips -> same decisions."""
    if not SAMPLE.exists():
        print("      (skipped: samples/ not present)")
        return
    from fortnite_chest_counter import review
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "config.toml").write_text('[capture]\nmode = "events"\ndir = "captures"\n', encoding="utf-8")
        cmd = [sys.executable, "-m", "fortnite_chest_counter", "live", "--gui", "--source", "file", "--video", str(SAMPLE),
               "--start", "55", "--duration", "28", "--config", str(tmp / "config.toml"),
               "--state", str(tmp / "s.json"), "--obs-dir", str(tmp / "o"), "--log-dir", str(tmp / "l")]
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
        process = subprocess.Popen(cmd, cwd=tmp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        threading.Timer(12, lambda: (process.stdin.write(b"mark\n"), process.stdin.flush())).start()
        out = process.stdout.read().decode("utf-8", "replace")
        process.wait(timeout=120)
        msgs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        saved = next(m for m in msgs if m["type"] == "capture" and m["state"] == "saved")
        assert saved["events"] >= 2 and saved["marks"] == 1 and saved["frames_not_recorded"] == 0, saved
        folder = Path(saved["folder"])
        live = [json.loads(l) for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        replayed = review.replay(folder, tmp_cfg(tmp)["detector"])
        result = review.compare(folder, replayed)
        assert result["same"] == len(live), review.report(folder, result, tmp_cfg(tmp)["detector"])


# ------------------------------------------------------------------ same counts as the command line (needs samples/)

def _run_video(args, tmp):
    cmd = [sys.executable, "-m", "fortnite_chest_counter", "video", str(SAMPLE), "--start", "55", "--until", "200",
           "--state", str(tmp / "s.json"), "--obs-dir", str(tmp / "o"), "--log-dir", str(tmp / "l"), *args]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    # Like the app: stdin stays OPEN while the worker runs (its end means "the app closed - stop").
    process = subprocess.Popen(cmd, cwd=tmp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8")
    streams = {}
    readers = [threading.Thread(target=lambda n=n: streams.__setitem__(n, getattr(process, n).read())) for n in ("stdout", "stderr")]
    for r in readers:
        r.start()
    process.wait(timeout=600)
    process.stdin.close()
    for r in readers:
        r.join()
    result = subprocess.CompletedProcess(cmd, process.returncode, streams["stdout"], streams["stderr"])
    rows = [json.loads(l) for l in next((tmp / "l").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    events = [{k: e.get(k) for k in ("time", "source", "adjusted_tier", "popup_score", "certain_counted", "approx_counted")}
              for e in rows if "time" in e]
    return result, events


def test_app_worker_counts_exactly_what_the_command_line_counts():
    if not SAMPLE.exists():
        print("      (skipped: samples/ not present)")
        return
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        cli, cli_events = _run_video([], Path(a))
        app, app_events = _run_video(["--gui"], Path(b))
    assert cli.returncode == 0 and app.returncode == 0, (cli.stderr, app.stderr)
    assert cli_events and cli_events == app_events
    finished = [json.loads(l) for l in app.stdout.splitlines() if l.startswith("{")][-1]
    assert finished["type"] == "finished"
    assert (finished["certain"], finished["approx"]) == (sum(e["certain_counted"] for e in cli_events),
                                                         sum(e["approx_counted"] for e in cli_events))
    assert "\x1b[" not in app.stdout and all(l.startswith("{") for l in app.stdout.splitlines() if l.strip())


def test_live_source_waits_after_forced_kill_and_closes_decoder_pipes():
    from fortnite_chest_counter.input.video_sources import LiveSource

    class Process:
        def __init__(self):
            self.calls = []
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def poll(self):
            return None

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def wait(self, timeout):
            self.calls.append(("wait", timeout))
            if self.calls.count(("wait", timeout)) == 1:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return 0

    source = LiveSource("fixture", [], 15)
    source._process = process = Process()
    source.close()
    assert process.calls == ["terminate", ("wait", 3), "kill", ("wait", 3)]
    assert process.stdout.closed and process.stderr.closed

def test_stopped_review_releases_decoder_children_behind_a_command_wrapper():
    from unittest.mock import patch
    from fortnite_chest_counter import review

    ffmpeg = paths.ffmpeg_executable()
    if not ffmpeg:
        print("      (skipped: ffmpeg not available)")
        return
    real_popen = subprocess.Popen
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = folder / "wrapped-review.mp4"
        completed = folder / "decoder-finished.txt"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=15",
                        "-t", "12", "-c:v", "mpeg4", str(video)], check=True)
        wrapper = ("import pathlib, subprocess, sys; "
                   "child = subprocess.Popen(sys.argv[2:]); "
                   "result = child.wait(); "
                   "pathlib.Path(sys.argv[1]).write_text(str(result)); "
                   "sys.exit(result)")
        processes = []

        def wrapped_popen(command, **kwargs):
            process = real_popen([sys.executable, "-c", wrapper, str(completed), ffmpeg, *command[1:]], **kwargs)
            processes.append(process)
            return process

        with patch.object(review.subprocess, "Popen", side_effect=wrapped_popen):
            reader = review.frames(video, 15)
            try:
                _, frame = next(reader)
                assert frame.shape == (720, 1280, 3)
            finally:
                reader.close()
        assert completed.exists(), "the command wrapper exited before waiting for its decoder"
        assert processes[0].poll() is not None
        video.unlink()  # Windows must release the file before review cancellation returns.


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok    {t.__name__}")
        except Exception as error:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {t.__name__}: {error!r}")
    print(f"{len(tests) - failed}/{len(tests)} app checks passed")
    raise SystemExit(1 if failed else 0)

def test_missed_chest_mark_uses_short_window_and_capture_metrics(monkeypatch):
    from fortnite_chest_counter.capture import Capture

    capture = Capture.__new__(Capture)
    capture.marks, capture.keep = [], []
    capture.mark_before, capture.mark_after = 30, 10
    monkeypatch.setattr(capture, "recording_time", lambda live_t: live_t)
    saved = []
    monkeypatch.setattr(capture, "_append", lambda name, row: saved.append((name, row)))
    metrics = {"hud_scale": 0.73, "target_fps": 15, "measured_fps": 13.7, "skipped_frames": 0}
    capture.add_mark(24, "missed chest", metrics)
    capture.add_mark(24)
    assert capture._keep_ranges() == [(18, 28), (-6, 34)]
    # The rolling encoder may still be behind when the mark is created.
    monkeypatch.setattr(capture, "recording_time", lambda live_t: live_t + 3)
    assert capture._keep_ranges() == [(21, 31), (-3, 37)]
    assert saved[0][0] == "marks.jsonl" and saved[0][1]["diagnostics"] == metrics
    assert "diagnostics" not in saved[1][1]

def test_export_chest_diagnostic_keeps_only_the_selected_segments(tmp_path):
    import json
    from zipfile import ZipFile
    from fortnite_chest_counter import capture_export

    video = tmp_path / "video"
    video.mkdir()
    (tmp_path / "capture.json").write_text(json.dumps({
        "ended_at": "finished", "fps": 15, "app": {"version": "0.3.4", "source": "private URL"},
    }), encoding="utf-8")
    (tmp_path / "marks.jsonl").write_text(json.dumps({
        "mark_id": "M001", "label": "missed chest", "clip": "video/session.ffconcat",
        "recording_time": 17, "stamp": "00:17.00",
        "diagnostics": {"hud_scale": 0.73, "measured_fps": 13.7},
    }) + "\n", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        '\n'.join(json.dumps({"recording_time": t, "popup_score": 0.8}) for t in (1, 15, 25, 35)),
        encoding="utf-8",
    )
    (video / "segments.csv").write_text(
        "".join(f"seg_{i:05}.mkv,{10 * i},{10 * (i + 1)}\n" for i in range(4)), encoding="utf-8",
    )
    for i in range(4):
        (video / f"seg_{i:05}.mkv").write_bytes(bytes([i]))
    (tmp_path / "config.toml").write_text("private configuration", encoding="utf-8")
    output = tmp_path / "report.zip"
    capture_export.export_report(tmp_path, "M001", output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "report.json", "decisions.jsonl", "video/report.ffconcat", "video/seg_00001.mkv", "video/seg_00002.mkv",
        }
        report = json.loads(archive.read("report.json"))
        assert report["seconds_into_clip"] == 7
        assert report["version"] == "0.3.4" and report["diagnostics"]["measured_fps"] == 13.7
        assert "private" not in archive.read("report.json").decode()
        assert [json.loads(row)["recording_time"] for row in archive.read("decisions.jsonl").splitlines()] == [15, 25]
