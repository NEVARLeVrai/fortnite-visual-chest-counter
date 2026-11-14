"""
The updater in the app window (v0.2.3): notifications, settings, progress, confirmation and results after a restart. The network, the
preparation and the installer are stand-ins; the window is the real one, off screen. The device scan is switched off, so no FFmpeg runs.
Run:   python tests/test_update_gui.py   (or pytest)
"""

import sys
from pathlib import Path as _P

ROOT = _P(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))   # run without installing

import ast
import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fortnite_chest_counter import counter as C
from fortnite_chest_counter import paths, settings
from fortnite_chest_counter.gui import update_manager as UM
from fortnite_chest_counter.update import check as Check
from fortnite_chest_counter.update import download as D
from fortnite_chest_counter.update import install as I
from fortnite_chest_counter.update import install_helper as H
from fortnite_chest_counter.update import prepare as P
from fortnite_chest_counter.update.releases import UpdateError
from fortnite_chest_counter.version import __version__

NEW = "9.9.9"
MB = 1_000_000


def fake_update(version=NEW, notes="Better things.\n<b>not bold</b>", page="https://github.com/x"):
    return SimpleNamespace(manifest=SimpleNamespace(version=version), release=SimpleNamespace(notes=notes, page_url=page))


def available(update=None):
    return Check.CheckResult("available", f"Version v{NEW} is available.", update=update or fake_update())


UP_TO_DATE = Check.CheckResult("up_to_date", "You have the latest version.")
NO_NETWORK = Check.CheckResult("failed", "Could not reach GitHub.", "network")
REJECTED = Check.CheckResult("rejected", "This update could not be verified, so it will not be installed.", "signature_invalid")


class FakeCheck:
    def __init__(self, result):
        self._result = result

    def start(self):
        return self

    def result(self):
        return self._result


class FakePreparation:
    """Stands in for prepare.Preparation. Tests set .current and .outcome by hand."""

    made = []

    def __init__(self, update, **options):
        self.update, self.options = update, options
        self.current, self.outcome, self.cancelled = P.PreparationStatus(), None, False
        FakePreparation.made.append(self)

    def start(self):
        return self

    def cancel(self):
        self.cancelled = True

    def status(self):
        return self.current

    def result(self):
        return self.outcome


class FakeWorker:
    """The counting worker, with no process behind it."""

    def __init__(self):
        self.is_running, self.stop_requests, self.stderr_tail = False, 0, []

    def running(self):
        return self.is_running

    def stop(self):
        self.stop_requests += 1

    def kill(self):
        self.is_running = False

    def drain(self, limit=500):
        return []


class Installer:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def __call__(self, **options):
        self.calls.append(options)
        if self.error:
            raise self.error


def manager(result=UP_TO_DATE, packaged=True, is_counting=lambda: False, installer=None):
    FakePreparation.made.clear()
    checks = []

    def factory():
        checks.append(1)
        return FakeCheck(result)

    made = UM.UpdateManager(is_counting, packaged=packaged, check_factory=factory, preparation_factory=FakePreparation, begin_install=installer or Installer())
    made.checks = checks
    return made


def prepared_manager(**options):
    m = manager(available(), **options)
    m.check()
    m.poll()
    assert m.state == UM.AVAILABLE
    m.start_preparing(D.BandwidthPolicy())
    FakePreparation.made[-1].outcome = P.PreparationResult("ready", "ready")
    m.poll()
    assert m.state == UM.READY
    return m


# ---- checking

def test_nothing_starts_a_download_by_itself():
    m = manager(available())
    m.check()
    m.poll()
    assert m.state == UM.AVAILABLE and FakePreparation.made == []


def test_a_check_only_finishes_when_its_thread_does():
    m = manager(None)
    assert m.check() is True and m.state == UM.CHECKING and m.poll() is False and m.state == UM.CHECKING


def test_no_update_available():
    m = manager(UP_TO_DATE)
    m.check(manual=True)
    assert m.check_message == "Checking for updates..."
    assert m.poll() is True and m.state == UM.IDLE and m.check_message == UP_TO_DATE.message and m.indicator_text() == ""


def test_an_available_update_shows_its_version():
    m = manager(available())
    m.check()
    m.poll()
    assert m.state == UM.AVAILABLE and m.version_text() == NEW and m.indicator_text() == f"Update available: v{NEW}"


def test_only_a_manual_check_opens_the_window():
    m = manager(available())
    m.check(manual=False)
    m.poll()
    assert m.take_open_request() is False
    m = manager(available())
    m.check(manual=True)
    m.poll()
    assert m.take_open_request() is True and m.take_open_request() is False


def test_a_rejected_release_is_never_offered():
    m = manager(REJECTED)
    m.check(manual=True)
    m.poll()
    assert m.state == UM.IDLE and m.update is None and m.indicator_text() == "" and "could not be verified" in m.check_message
    m = manager(REJECTED)
    m.check(manual=False)
    m.poll()
    assert m.state == UM.IDLE and m.check_message == REJECTED.message


def test_automatic_network_failures_only_show_feedback_in_settings():
    m = manager(NO_NETWORK)
    m.check(manual=False)
    assert m.poll() is True and m.state == UM.IDLE and m.check_message == NO_NETWORK.message and m.indicator_text() == ""


def test_manual_network_failures_are_explained():
    m = manager(NO_NETWORK)
    m.check(manual=True)
    m.poll()
    assert m.state == UM.IDLE and m.check_message == NO_NETWORK.message


def test_a_failed_check_does_not_forget_a_known_update():
    m = manager(available())
    m.check()
    m.poll()
    m._check_factory = lambda: FakeCheck(NO_NETWORK)
    m.check(manual=True)
    m.poll()
    assert m.state == UM.AVAILABLE and m.version_text() == NEW


def test_checks_only_run_in_the_packaged_app():
    m = manager(available(), packaged=False)
    assert m.check() is False and m.checks == [] and m.check_message == ""
    assert m.check(manual=True) is False and m.check_message == UM.NOT_PACKAGED


def test_no_second_check_while_busy():
    m = manager(None)
    m.check()
    assert m.check() is False and len(m.checks) == 1


def test_a_check_while_ready_keeps_the_prepared_update():
    m = prepared_manager()
    assert m.check(manual=True) is False and m.state == UM.READY and m.take_open_request() is True


# ---- preparing

def test_preparation_uses_the_chosen_speed_and_follows_counting():
    counting = [True]
    m = manager(available(), is_counting=lambda: counting[0])
    m.check()
    m.poll()
    policy = D.BandwidthPolicy("custom", custom_limit=250_000)
    assert m.start_preparing(policy) is True and m.state == UM.PREPARING
    options = FakePreparation.made[-1].options
    assert options["policy"] is policy and options["is_counting"]() is True
    counting[0] = False
    assert options["is_counting"]() is False


def test_every_stage_has_its_own_name():
    expected = {"downloading": "Downloading", "verifying": "Verifying the download", "extracting": "Extracting the files",
                "validating": "Checking the extracted files", "checking": "Testing the new version"}
    m = manager()
    for stage, name in expected.items():
        m.status = P.PreparationStatus(stage=stage)
        assert m.stage_text() == name


def test_progress_shows_percentage_bytes_and_speed():
    status = P.PreparationStatus("downloading", 12_300_000, 155_600_000, 1_000_000.0, 1_000_000)
    assert UM.percent(status) == 7
    assert UM.progress_text(status) == "12.3 MB of 155.6 MB (7%), 1.0 MB/s (limited to 1.0 MB/s)"
    assert UM.progress_text(P.PreparationStatus("downloading", 500_000, MB, 0.0, None)) == "500 KB of 1.0 MB (50%)"
    assert UM.progress_text(P.PreparationStatus("extracting", 5, 10, 1.0, None)) == ""
    assert UM.percent(P.PreparationStatus()) == 0


def test_the_indicator_follows_the_preparation():
    m = manager(available())
    m.check()
    m.poll()
    m.start_preparing(D.BandwidthPolicy())
    job = FakePreparation.made[-1]
    job.current = P.PreparationStatus("downloading", 42, 100)
    assert m.poll() is True and m.indicator_text() == "Preparing update: 42%"
    assert m.poll() is False
    job.current = P.PreparationStatus("extracting", 100, 100)
    m.poll()
    assert m.indicator_text() == "Preparing update..."
    job.outcome = P.PreparationResult("ready", "ok")
    m.poll()
    assert m.state == UM.READY and m.indicator_text() == f"Update ready: v{NEW}"


def test_cancelling_keeps_the_update_available():
    m = manager(available())
    m.check()
    m.poll()
    m.start_preparing(D.BandwidthPolicy())
    job = FakePreparation.made[-1]
    m.cancel()
    assert job.cancelled and m.cancelling and m.stage_text() == "Cancelling..." and m.state == UM.PREPARING
    job.outcome = P.PreparationResult("cancelled", "stopped", "cancelled")
    m.poll()
    assert m.state == UM.AVAILABLE and "continues from where it left off" in m.notice and not m.cancelling and m.error == ""


def test_a_failed_preparation_explains_and_can_be_retried():
    m = manager(available())
    m.check()
    m.poll()
    m.start_preparing(D.BandwidthPolicy())
    FakePreparation.made[-1].outcome = P.PreparationResult("failed", "The connection failed.", "network", detail="timeout at 40 MB")
    m.poll()
    assert m.state == UM.AVAILABLE and m.error == "The connection failed." and m.error_detail == "timeout at 40 MB"
    assert m.start_preparing(D.BandwidthPolicy()) is True and m.error == "" and len(FakePreparation.made) == 2


def test_the_bandwidth_settings_become_a_policy():
    adaptive = UM.bandwidth_policy({"download_bandwidth": "adaptive", "custom_limit_kb_per_second": 2000})
    assert adaptive.mode == "adaptive" and adaptive.limit(True) == 1_000_000 and adaptive.limit(False) is None
    unlimited = UM.bandwidth_policy({"download_bandwidth": "unlimited", "custom_limit_kb_per_second": 2000})
    assert unlimited.limit(True) is None
    custom = UM.bandwidth_policy({"download_bandwidth": "custom", "custom_limit_kb_per_second": 750})
    assert custom.limit(True) == 750_000 and custom.limit(False) == 750_000


# ---- installing, at the manager level

def finish(m):
    deadline = time.monotonic() + 10
    while m._install_thread is not None and time.monotonic() < deadline:
        m.poll()
        time.sleep(0.002)
    assert m._install_thread is None


def test_the_window_never_waits_for_the_installer_checks():
    release = threading.Event()
    calls = []

    def slow_installer(**options):
        calls.append(options)
        release.wait(10)

    m = prepared_manager(installer=slow_installer)
    started = time.monotonic()
    assert m.install("state", []) is True and time.monotonic() - started < 0.5 and m.state == UM.INSTALLING
    assert m.poll() is False and m.take_install_started() is False
    release.set()
    finish(m)
    assert len(calls) == 1 and m.state == UM.INSTALLING and m.take_install_started() is True and m.take_install_started() is False


def test_an_installer_failure_keeps_the_app_ready():
    installer = Installer(UpdateError("disk_space", "Not enough space.", "free 10 MB"))
    m = prepared_manager(installer=installer)
    assert m.install("state", []) is True
    finish(m)
    assert m.state == UM.READY and m.error == "Not enough space." and m.error_detail == "free 10 MB" and m.take_install_started() is False


def test_a_damaged_preparation_goes_back_to_downloading():
    m = prepared_manager(installer=Installer(UpdateError("not_prepared", "It needs to be prepared again.")))
    m.install("state", [])
    finish(m)
    assert m.state == UM.AVAILABLE and m.take_install_started() is False


def test_an_unexpected_installer_error_cannot_escape():
    m = prepared_manager(installer=Installer(ValueError("bug")))
    m.install("state", [])
    finish(m)
    assert m.state == UM.READY and "ValueError" in m.error_detail


def test_install_needs_a_prepared_update():
    installer = Installer()
    m = manager(available(), installer=installer)
    assert m.install("state", []) is False and installer.calls == []


# ---- what the user is told afterwards

def test_results_are_described_plainly():
    level, text = UM.describe_result({"outcome": "committed", "to": "0.2.4"}, "0.2.4")
    assert level == "success" and text.startswith("Updated to v0.2.4")
    level, text = UM.describe_result({"outcome": "rolled_back", "to": "0.2.4", "detail": "It closed early."}, "0.2.3")
    assert level == "rolled_back" and "went back to v0.2.3" in text and "It closed early." in text and "restored" not in text
    level, text = UM.describe_result({"outcome": "rolled_back", "to": "0.2.4", "restored": ["state/x.json"]}, "0.2.3")
    assert "state\\backups" in text
    level, text = UM.describe_result({"outcome": "aborted", "to": "0.2.4", "detail": "A file is in use."}, "0.2.3")
    assert level == "attention" and "not installed" in text and "A file is in use." in text


def test_success_is_never_claimed_for_a_version_that_is_not_running():
    assert UM.describe_result({"outcome": "committed", "to": "0.2.4"}, "0.2.3") is None
    assert UM.describe_result({"outcome": "committed"}, "0.2.3") is None
    assert UM.describe_result({"outcome": "something new", "to": "0.2.4"}, "0.2.4") is None
    assert UM.describe_result({}, "0.2.4") is None


# ---- settings

def load_update_settings(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text, encoding="utf-8")
        cfg, _, warnings = settings.load_config(path)
        return cfg["update"], warnings


def test_update_settings_have_safe_defaults():
    assert settings.DEFAULTS["update"] == {"check_automatically": True, "download_bandwidth": "adaptive", "custom_limit_kb_per_second": 1000}


def test_update_settings_are_validated():
    good, warnings = load_update_settings('[update]\ncheck_automatically = false\ndownload_bandwidth = "custom"\ncustom_limit_kb_per_second = 500\n')
    assert good == {"check_automatically": False, "download_bandwidth": "custom", "custom_limit_kb_per_second": 500} and warnings == []
    for bad in ('download_bandwidth = "fast"', "custom_limit_kb_per_second = 10", "custom_limit_kb_per_second = 2000000", 'custom_limit_kb_per_second = "a lot"',
                "custom_limit_kb_per_second = true", 'check_automatically = "yes"'):
        values, warnings = load_update_settings(f"[update]\n{bad}\n")
        assert values == settings.DEFAULTS["update"] and len(warnings) == 1, bad


def test_the_example_config_documents_the_update_settings():
    cfg, _, warnings = settings.load_config(ROOT / "config.example.toml")
    assert warnings == [] and cfg["update"] == settings.DEFAULTS["update"]
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert "does not measure your connection" in text


# ---- the real window, off screen

def _qt():
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return None
    return QApplication.instance() or QApplication([])


@contextlib.contextmanager
def patched(target, **values):
    saved = {name: getattr(target, name) for name in values}
    for name, value in values.items():
        setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(target, name, value)


@contextlib.contextmanager
def window(result=UP_TO_DATE, config=None, installer=None):
    """-> the real MainWindow in a throwaway folder, with stand-ins for the network, the preparation, the installer and the worker."""
    qt = _qt()
    if qt is None:
        print("      (skipped: PySide6 not installed)")
        yield None
        return
    from fortnite_chest_counter.gui import app as A
    here, app_dir = os.getcwd(), paths.APP_DIR
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        os.chdir(tmp)
        paths.APP_DIR = Path(tmp).resolve()
        if config:
            (paths.APP_DIR / "config.toml").write_text(config, encoding="utf-8")
        answers, errors, asked = [True], [], []

        def fake_ask(parent, title, text, *args, **kwargs):
            asked.append(text)
            return answers[0]

        win = None
        try:
            with patched(A.MainWindow, refresh_sources=lambda self: None), patched(A, ask=fake_ask, show_error=lambda parent, text: errors.append(text)):
                win = A.MainWindow()
                win.timer.stop()
                win.slow.stop()
                win.worker = FakeWorker()
                installer = installer or Installer()
                FakePreparation.made.clear()
                checks = []

                def factory():
                    checks.append(1)
                    return FakeCheck(result)

                win.updates = UM.UpdateManager(win.worker.running, packaged=True, check_factory=factory, preparation_factory=FakePreparation, begin_install=installer)
                yield SimpleNamespace(win=win, A=A, qt=qt, checks=checks, installer=installer, answers=answers, errors=errors, asked=asked, dir=paths.APP_DIR)
        finally:
            if win is not None:
                win.worker = FakeWorker()
                win.close()
                win.deleteLater()
                qt.processEvents()
            os.chdir(here)
            paths.APP_DIR = app_dir


def settle(w):
    for _ in range(5):
        w.qt.processEvents()


def poll(w):
    if w.win.updates.poll():
        w.win.update_changed()


def to_available(w):
    w.win.check_for_updates()
    poll(w)
    assert w.win.updates.state == UM.AVAILABLE
    w.win.update_dialog.hide()


def finish_install(w):
    deadline = time.monotonic() + 10
    while w.win.updates._install_thread is not None and time.monotonic() < deadline:
        poll(w)
        time.sleep(0.002)
    assert w.win.updates._install_thread is None


def to_ready(w):
    to_available(w)
    w.win.download_update()
    FakePreparation.made[-1].outcome = P.PreparationResult("ready", "ready")
    poll(w)
    assert w.win.updates.state == UM.READY


def test_gui_the_window_never_checks_by_itself_while_being_built():
    with window() as w:
        if w:
            settle(w)
            assert w.checks == [] and w.win.update_link.isHidden()


def test_gui_automatic_check_runs_soon_after_starting_when_enabled():
    with window() as w:
        if w:
            with patched(w.A, AUTO_CHECK_DELAY_MS=0):
                w.win.after_start()
                assert w.checks == [], "the check must not happen during start-up itself"
                settle(w)
            assert len(w.checks) == 1


def test_gui_automatic_check_is_off_when_disabled():
    with window(config="[update]\ncheck_automatically = false\n") as w:
        if w:
            with patched(w.A, AUTO_CHECK_DELAY_MS=0):
                w.win.after_start()
                settle(w)
            assert w.checks == []


def test_gui_automatic_check_does_not_open_anything():
    with window(available()) as w:
        if w:
            with patched(w.A, AUTO_CHECK_DELAY_MS=0):
                w.win.after_start()
                settle(w)
            poll(w)
            assert not w.win.update_link.isHidden() and w.win.update_link.text() == f"Update available: v{NEW}"
            assert w.win.update_dialog is None or not w.win.update_dialog.isVisible()


def test_gui_automatic_failures_show_feedback_in_settings():
    for result in (NO_NETWORK, REJECTED):
        with window(result) as w:
            if w:
                w.win.auto_check()
                poll(w)
                assert w.win.update_link.isHidden() and w.win.settings_page.update_status.text() == result.message and w.win.dashboard.notice.isHidden()


def test_gui_manual_check_gives_feedback():
    for result, expected in ((UP_TO_DATE, UP_TO_DATE.message), (NO_NETWORK, NO_NETWORK.message), (REJECTED, REJECTED.message)):
        with window(result) as w:
            if w:
                w.win.check_for_updates()
                poll(w)
                assert w.win.settings_page.update_status.text() == expected and w.win.update_link.isHidden()


def test_gui_manual_check_opens_the_update_window_with_notes_as_plain_text():
    with window(available()) as w:
        if w:
            w.win.check_for_updates()
            poll(w)
            dialog = w.win.update_dialog
            assert dialog.isVisible() and dialog.title.text() == f"Version v{NEW} is available"
            assert dialog.notes.toPlainText() == "Better things.\n<b>not bold</b>" and dialog.notes.isReadOnly()
            assert dialog.primary.text() == "Download && Install" and not dialog.later.isHidden() and not dialog.page.isHidden()


def test_gui_later_closes_without_interrupting_again():
    with window(available()) as w:
        if w:
            to_available(w)
            w.win.open_update_dialog()
            w.win.update_dialog.later.click()
            assert not w.win.update_dialog.isVisible() and not w.win.update_link.isHidden()
            w.win.auto_check()
            poll(w)
            assert not w.win.update_dialog.isVisible() and FakePreparation.made == []


def test_gui_the_indicator_reopens_the_window():
    with window(available()) as w:
        if w:
            to_available(w)
            w.win.update_link.click()
            assert w.win.update_dialog.isVisible()


def test_gui_download_shows_stages_and_progress():
    with window(available()) as w:
        if w:
            to_available(w)
            w.win.open_update_dialog()
            dialog = w.win.update_dialog
            dialog.primary.click()
            job = FakePreparation.made[-1]
            job.current = P.PreparationStatus("downloading", 77_800_000, 155_600_000, 980_000.0, 1_000_000)
            poll(w)
            assert dialog.stage.text() == "Downloading" and dialog.progress.value() == 50 and dialog.progress.maximum() == 100
            assert dialog.detail.text() == "77.8 MB of 155.6 MB (50%), 980 KB/s (limited to 1.0 MB/s)"
            assert not dialog.cancel_button.isHidden() and dialog.primary.isHidden() and dialog.later.isHidden()
            assert w.win.update_link.text() == "Preparing update: 50%"
            for stage, name in (("verifying", "Verifying the download"), ("extracting", "Extracting the files"), ("validating", "Checking the extracted files"), ("checking", "Testing the new version")):
                job.current = P.PreparationStatus(stage, 155_600_000, 155_600_000)
                poll(w)
                assert dialog.stage.text() == name and dialog.progress.maximum() == 0 and dialog.detail.text() == ""


def test_gui_counting_continues_while_downloading():
    with window(available()) as w:
        if w:
            to_available(w)
            w.win.worker.is_running = True
            w.win.download_update()
            assert w.win.worker.stop_requests == 0 and w.asked == []
            assert FakePreparation.made[-1].options["is_counting"]() is True


def test_gui_uses_the_saved_download_speed():
    with window(available(), config='[update]\ndownload_bandwidth = "custom"\ncustom_limit_kb_per_second = 300\n') as w:
        if w:
            to_available(w)
            w.win.download_update()
            assert FakePreparation.made[-1].options["policy"].limit(False) == 300_000


def test_gui_cancel_and_retry():
    with window(available()) as w:
        if w:
            to_available(w)
            w.win.open_update_dialog()
            dialog = w.win.update_dialog
            dialog.primary.click()
            job = FakePreparation.made[-1]
            dialog.cancel_button.click()
            assert job.cancelled and dialog.stage.text() == "Cancelling..." and not dialog.cancel_button.isEnabled()
            job.outcome = P.PreparationResult("cancelled", "stopped", "cancelled")
            poll(w)
            assert w.win.updates.state == UM.AVAILABLE and "continues from where it left off" in dialog.problem.text()
            dialog.primary.click()
            FakePreparation.made[-1].outcome = P.PreparationResult("failed", "The connection failed.", "network")
            poll(w)
            assert dialog.problem.text() == "The connection failed." and dialog.primary.text() == "Try again"
            dialog.primary.click()
            assert len(FakePreparation.made) == 3 and w.win.updates.state == UM.PREPARING


def test_gui_settings_are_saved_and_read_back():
    with window() as w:
        if w:
            page = w.win.settings_page
            page.load()
            assert page.update_auto.isChecked() and page.update_bandwidth.currentData() == "adaptive" and page.update_limit.isHidden()
            assert "does not measure your connection" in page.update_hint.text()
            page.update_auto.setChecked(False)
            page.update_bandwidth.setCurrentIndex(page.update_bandwidth.findData("custom"))
            page.update_unit.setCurrentIndex(1)
            page.update_limit.setValue(3)
            assert not page.update_limit.isHidden() and not page.update_unit.isHidden()
            page.save()
            cfg, _, warnings = settings.load_config(w.dir / "config.toml")
            assert warnings == [] and cfg["update"] == {"check_automatically": False, "download_bandwidth": "custom", "custom_limit_kb_per_second": 3000}
            page.update_auto.setChecked(True)
            page.load()
            assert not page.update_auto.isChecked() and page.update_unit.currentData() == 1000 and page.update_limit.value() == 3
            page.update_unit.setCurrentIndex(0)
            page.update_limit.setValue(750)
            page.save()
            page.load()
            assert page.update_unit.currentData() == 1 and page.update_limit.value() == 750


def test_gui_saving_settings_keeps_other_settings():
    with window(config='[display]\nshow = "certain"\n') as w:
        if w:
            w.win.settings_page.load()
            w.win.settings_page.save()
            cfg, _, _ = settings.load_config(w.dir / "config.toml")
            assert cfg["display"]["show"] == "certain"


def test_gui_ready_offers_restart_and_install():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.open_update_dialog()
            dialog = w.win.update_dialog
            assert dialog.title.text() == f"Version v{NEW} is ready to install" and dialog.primary.text() == "Restart && Install"
            assert "reopen by itself" in dialog.body.text() and "Counting will stop" not in dialog.body.text()
            assert w.win.update_link.text() == f"Update ready: v{NEW}"
            w.win.worker.is_running = True
            dialog.refresh()
            assert "Counting will stop first and your counts will be saved." in dialog.body.text()


def test_gui_install_when_not_counting_needs_no_extra_question():
    with window(available()) as w:
        if w:
            to_ready(w)
            closed = []
            with patched(w.win, close=lambda: closed.append(1)):
                w.win.restart_and_install()
                finish_install(w)
            assert len(w.installer.calls) == 1 and closed == [1] and w.asked == [] and w.win.updates.state == UM.INSTALLING
            options = w.installer.calls[0]
            assert options["state_dir"] == "state"
            assert {Path(p).name for p in options["data_paths"]} == {"state", "obs_output", "logs", "captures"}


def test_gui_active_counting_needs_confirmation():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.worker.is_running = True
            w.answers[0] = False
            w.win.restart_and_install()
            assert len(w.asked) == 1 and "Counting will stop" in w.asked[0] and w.win.worker.stop_requests == 0
            assert w.installer.calls == [] and w.win.updates.state == UM.READY


def test_gui_install_waits_for_a_clean_stop():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.worker.is_running = True
            closed = []
            with patched(w.win, close=lambda: closed.append(1)):
                w.win.restart_and_install()
                assert w.win.worker.stop_requests == 1 and w.installer.calls == [] and closed == [] and w.win.updates.state == UM.INSTALLING
                assert w.win.updates.installing_text == "Stopping counting and saving..."
                w.win.worker.is_running = False
                w.win.handle({"type": "exit", "code": 0})
                finish_install(w)
            assert len(w.installer.calls) == 1 and closed == [1]


def test_gui_failed_worker_shutdown_does_not_install():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.worker.is_running = True
            closed = []
            with patched(w.win, close=lambda: closed.append(1)):
                w.win.restart_and_install()
                w.win.worker.is_running = False
                w.win.handle({"type": "exit", "code": 1})
            assert w.installer.calls == [] and closed == [] and w.win.updates.state == UM.READY
            assert "did not stop cleanly" in w.win.updates.error and w.win._install_pending is False


def test_gui_a_worker_that_never_stops_does_not_install():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.worker.is_running = True
            w.win.restart_and_install()
            w.win._install_stop_deadline()
            assert w.installer.calls == [] and w.win.updates.state == UM.READY and w.win._install_pending is False
            w.win.worker.is_running = False
            w.win.handle({"type": "exit", "code": 0})
            assert w.installer.calls == [], "a late exit must not start an installation that was already refused"


def test_gui_installer_failure_keeps_the_window_open_and_explains():
    with window(available(), installer=Installer(UpdateError("runtime_has_user_files", "Move my_file.txt first."))) as w:
        if w:
            to_ready(w)
            closed = []
            w.win.open_update_dialog()
            with patched(w.win, close=lambda: closed.append(1)):
                w.win.restart_and_install()
                finish_install(w)
            assert closed == [] and w.win.updates.state == UM.READY and w.win.update_dialog.problem.text() == "Move my_file.txt first."
            assert w.win.dashboard.start.isEnabled(), "Start counting must come back after a failed install"
            assert w.win.update_dialog.primary.text() == "Restart && Install" and w.win.update_dialog.primary.isEnabled()


def test_gui_a_second_press_cannot_start_a_second_install():
    with window(available()) as w:
        if w:
            to_ready(w)
            with patched(w.win, close=lambda: None):
                w.win.restart_and_install()
                w.win.restart_and_install()
                finish_install(w)
            assert len(w.installer.calls) == 1


def test_gui_counting_cannot_start_while_installing():
    with window(available()) as w:
        if w:
            to_ready(w)
            w.win.dashboard.source.addItem("x", {"source": "desktop", "monitor": 1})
            w.win.dashboard.source.setCurrentIndex(w.win.dashboard.source.count() - 1)
            started = []
            w.win.worker.start = lambda argv: started.append(argv)
            with patched(w.win, close=lambda: None):
                w.win.restart_and_install()
                assert not w.win.dashboard.start.isEnabled()
                w.win.start_counting()
                finish_install(w)
            assert started == []


def test_gui_install_waits_for_diagnostics():
    with window(available()) as w:
        if w:
            to_ready(w)
            stop = threading.Event()
            w.win.help._thread = threading.Thread(target=stop.wait)
            w.win.help._thread.start()
            try:
                w.win.restart_and_install()
                assert w.installer.calls == [] and w.win.updates.state == UM.READY and "diagnostics" in w.win.updates.error
            finally:
                stop.set()
                w.win.help._thread.join()
                w.win.help._thread = None


def test_gui_install_waits_for_another_open_window():
    with window(available()) as w:
        if w:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QDialog
            to_ready(w)
            other = QDialog(w.win)
            other.setWindowModality(Qt.ApplicationModal)
            other.show()
            try:
                w.win.restart_and_install()
                assert w.installer.calls == [] and w.win.updates.state == UM.READY and "other open window" in w.win.updates.error
            finally:
                other.hide()
                other.deleteLater()


def test_gui_problems_with_update_records_never_stop_the_window():
    def broken(*args):
        raise OSError("locked")

    with window() as w:
        if w:
            with patched(I, clear_orphaned_journal=broken):
                w.win.after_start()
            assert any("last update" in str(entry) for entry in w.win.entries)


def write_result(w, **fields):
    folder = w.dir / H.UPDATE_FOLDER
    folder.mkdir(exist_ok=True)
    H.write_json_durably(folder / H.RESULT_NAME, fields)


def test_gui_success_is_shown_once_after_restart():
    with window() as w:
        if w:
            write_result(w, outcome="committed", to=__version__)
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            assert not w.win.dashboard.notice.isHidden() and f"Updated to v{__version__}" in w.win.dashboard.notice_text.text()
            assert I.last_result(w.dir) is None, "it must not be shown again next time"
            w.win.dashboard.notice.hide()
            assert w.win.show_update_result() is False and w.win.dashboard.notice.isHidden()


def test_gui_rollback_is_reported():
    with window() as w:
        if w:
            write_result(w, outcome="rolled_back", to="9.9.9", detail="It closed early.", restored=[])
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            text = w.win.dashboard.notice_text.text()
            assert f"went back to v{__version__}" in text and "It closed early." in text and "Updated to" not in text


def test_gui_an_update_needing_attention_is_reported():
    with window() as w:
        if w:
            write_result(w, outcome="aborted", to="9.9.9", detail="ping.exe is still running from the program folder.")
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            assert "was not installed" in w.win.dashboard.notice_text.text() and "ping.exe" in w.win.dashboard.notice_text.text()


def test_gui_an_unfinished_update_asks_for_attention():
    with window() as w:
        if w:
            write_tree = w.dir / H.UPDATE_FOLDER / H.HELPER_FOLDER / "python"
            write_tree.mkdir(parents=True)
            (write_tree / "pythonw.exe").write_bytes(b"x")
            (w.dir / H.UPDATE_FOLDER / H.JOURNAL_NAME).write_text('{"format": 1, "phase": "new_installed"}')
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            assert "did not finish" in w.win.dashboard.notice_text.text()


def test_gui_success_waits_for_the_installers_verdict():
    with window() as w:
        if w:
            with patched(w.A, AUTO_CHECK_DELAY_MS=0):
                w.win.after_start(installing=True)
                settle(w)
            assert w.checks == [], "no update check while still being proved"
            w.win._tick_slow()
            assert w.win.dashboard.notice.isHidden(), "nothing may be claimed before the verdict"
            write_result(w, outcome="committed", to=__version__)
            w.win._tick_slow()
            assert f"Updated to v{__version__}" in w.win.dashboard.notice_text.text() and w.win._result_watch_until is None


def test_gui_a_stale_success_is_not_believed():
    with window() as w:
        if w:
            write_result(w, outcome="committed", to="0.0.1")
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            assert w.win.dashboard.notice.isHidden()


def test_gui_results_never_stop_the_window_from_opening():
    with window() as w:
        if w:
            (w.dir / H.UPDATE_FOLDER).mkdir()
            (w.dir / H.UPDATE_FOLDER / H.RESULT_NAME).write_text("{ not json")
            with patched(w.A, AUTO_CHECK_DELAY_MS=60000):
                w.win.after_start()
            assert w.win.dashboard.notice.isHidden()


# ---- how it is put together

def test_a_new_installation_can_never_report_an_old_result():
    source = (ROOT / "src/fortnite_chest_counter/update/install.py").read_text(encoding="utf-8")
    assert "H.BACKUP_FOLDER, H.RESULT_NAME):" in source


def test_the_manager_has_no_widgets_and_starts_no_network_itself():
    tree = ast.parse((ROOT / "src/fortnite_chest_counter/gui/update_manager.py").read_text(encoding="utf-8"))
    imported = {(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(name.startswith("PySide6") or name in ("urllib", "socket", "http") for name in imported), imported


def test_the_window_only_starts_a_download_from_the_users_button():
    text = (ROOT / "src/fortnite_chest_counter/gui/app.py").read_text(encoding="utf-8")
    assert text.count("start_preparing(") == 1 and text.count("self.win.download_update()") == 1 and text.count("def download_update") == 1


def test_adaptive_is_described_honestly():
    text = (ROOT / "src/fortnite_chest_counter/gui/app.py").read_text(encoding="utf-8")
    assert "it does not measure your connection" in text and "available bandwidth" not in text.lower()


def test_real_preparation_accepts_what_the_window_passes():
    made = P.Preparation(fake_update(), policy=D.BandwidthPolicy(), is_counting=lambda: False)
    assert made.result() is None and made.status().stage == "waiting"


def test_the_whole_flow_on_a_small_release():
    """The real check, signature verification, download, staging and begin_install, driven by the manager the window uses."""
    import test_update as M1
    import test_update_prepare as M2
    import sign_release as S
    package = M2.build_zip()
    key = (S.public_key_hex(M2.KEY),)
    with M2.workspace() as ws, M1.MockGitHub() as github:
        runtime = ws.app / "runtime"
        files = sorted(p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file())
        (runtime / H.PACKAGE_FILES).write_text(json.dumps({"format": 1, "version": "0.2.2", "files": files}))
        python = ws.app.parent / "official-python"
        python.mkdir()
        (python / "python.exe").write_bytes(b"python")
        (python / "pythonw.exe").write_bytes(b"pythonw")
        github.add(M2.VERSION, package=package, keys=(M2.KEY,))
        checks = M2.Checks()
        counting = []

        def check_factory():
            return Check.BackgroundCheck(installed="0.2.2", source=github.source, public_keys=key)

        def preparation_factory(update, **options):
            return P.Preparation(update, app_dir=ws.app, source=github.source, checks=checks, **options)

        def installer(**options):
            return I.begin_install(app_dir=ws.app, installed="0.2.2", public_keys=key, python_folder=python, app_mutex="Local\\fcc-test-m4", spawn=False, **options)

        m = UM.UpdateManager(lambda: bool(counting), packaged=True, check_factory=check_factory, preparation_factory=preparation_factory, begin_install=installer)

        def wait_while(state):
            deadline = time.monotonic() + 30
            while m.state == state and time.monotonic() < deadline:
                m.poll()
                time.sleep(0.005)

        before = M2.snapshot(ws.app)
        assert m.check(manual=True)
        wait_while(UM.CHECKING)
        assert m.state == UM.AVAILABLE and m.version_text() == M2.VERSION, m.check_message
        assert m.start_preparing(UM.bandwidth_policy(settings.DEFAULTS["update"]))
        wait_while(UM.PREPARING)
        assert m.state == UM.READY and len(checks.calls) == 1, (m.error, m.error_detail)
        assert m.install("state", [ws.app / "state", ws.app / "captures"])
        finish(m)
        assert m.take_install_started() is True and m.error == ""
        files_after = M2.snapshot(ws.app)
        files_after.pop("runtime/" + H.PACKAGE_FILES)
        before.pop("runtime/" + H.PACKAGE_FILES)
        assert files_after == before, "nothing may change before the helper runs"
        journal = json.loads((ws.update_dir / H.JOURNAL_NAME).read_text(encoding="utf-8"))
        assert journal["phase"] == H.PREPARED and journal["to_version"] == M2.VERSION and journal["state_dir"] == "state"


def test_real_begin_install_accepts_what_the_window_passes():
    import inspect
    parameters = inspect.signature(I.begin_install).parameters
    assert "state_dir" in parameters and "data_paths" in parameters


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}", flush=True)
        except Exception:
            failed += 1
            import traceback
            print(f"  FAIL  {name}", flush=True)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} update window checks passed")
    sys.exit(1 if failed else 0)

def test_gui_check_runs_after_successful_updater_restart():
    with window(UP_TO_DATE) as w:
        if w:
            with patched(w.A, AUTO_CHECK_DELAY_MS=0):
                w.win.after_start(installing=True)
                settle(w)
                assert w.checks == []
                with patched(w.A.update_install, last_result=lambda: {"outcome": "committed", "to": w.A.__version__}):
                    assert w.win.show_update_result()
                settle(w)
                poll(w)
            assert len(w.checks) == 1
            assert w.win.settings_page.update_status.text() == UP_TO_DATE.message
