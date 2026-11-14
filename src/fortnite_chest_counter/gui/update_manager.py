"""
What the app window knows about updates, without any widgets.

The window calls poll() from its timer, the same way it reads the counting worker. Checking and preparing run on their own threads
(update/check.py, update/prepare.py), so nothing here ever waits for the network or the disk. Only check() and start_preparing() start
work, and the window calls start_preparing() only after the user asks: nothing is downloaded or installed by itself.
"""

import threading

from fortnite_chest_counter import paths
from fortnite_chest_counter.update import check as update_check
from fortnite_chest_counter.update import download, install, prepare
from fortnite_chest_counter.update.releases import UpdateError

IDLE, CHECKING, AVAILABLE, PREPARING, READY, INSTALLING = "idle", "checking", "available", "preparing", "ready", "installing"
STAGE_NAMES = {"waiting": "Starting", "downloading": "Downloading", "verifying": "Verifying the download", "extracting": "Extracting the files",
               "validating": "Checking the extracted files", "checking": "Testing the new version", "finished": "Finishing"}
NOT_PACKAGED = "Update checks work in the installed app, not when running from source code."


def bandwidth_policy(update_settings):
    """The [update] settings as a download policy."""
    mode = update_settings["download_bandwidth"]
    return download.BandwidthPolicy(mode, custom_limit=update_settings["custom_limit_kb_per_second"] * 1000 if mode == "custom" else None)


def size_text(size):
    return f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{size / 1000:.0f} KB"


def speed_text(rate):
    return f"{rate / 1_000_000:.1f} MB/s" if rate >= 1_000_000 else f"{rate / 1000:.0f} KB/s"


def percent(status):
    return min(100, int(status.received * 100 / status.total)) if status.total else 0


def progress_text(status):
    """'12.3 MB of 155.6 MB (8%), 1.0 MB/s' while downloading; nothing for the later steps, which have no meaningful size."""
    if status.stage != "downloading" or not status.total:
        return ""
    text = f"{size_text(status.received)} of {size_text(status.total)} ({percent(status)}%)"
    if status.bytes_per_second:
        text += f", {speed_text(status.bytes_per_second)}"
    if status.limit is not None:
        text += f" (limited to {speed_text(status.limit)})"
    return text


def describe_result(result, installed):
    """What the last installation did, for the user. -> (level, text) or None. Level: success | rolled_back | attention.
    Success is only ever reported when the helper recorded a commit for the version that is running now."""
    outcome, target, detail = result.get("outcome"), result.get("to"), result.get("detail") or ""
    if outcome == "committed":
        return ("success", f"Updated to v{target}. Your counts, profiles and settings were kept.") if target == installed else None
    if outcome == "rolled_back":
        text = f"The update to v{target} did not start correctly, so the app went back to v{installed}. {detail}".rstrip()
        if result.get("restored"):
            text += " Some saved data was restored from before the update. The newer copy is kept in state\\backups."
        return "rolled_back", text
    if outcome == "aborted":
        return "attention", f"The update to v{target} was not installed. {detail}".rstrip()
    return None


class UpdateManager:
    def __init__(self, is_counting, packaged=None, check_factory=update_check.BackgroundCheck, preparation_factory=prepare.Preparation,
                 begin_install=install.begin_install):
        self.is_counting = is_counting
        self.packaged = paths.PACKAGED if packaged is None else packaged
        self._check_factory, self._preparation_factory, self._begin_install = check_factory, preparation_factory, begin_install
        self.state = IDLE
        self.update = None
        self.status = prepare.PreparationStatus()
        self.check_message = ""                # feedback in Settings
        self.last_problem_kind = None
        self.error = ""
        self.error_detail = ""
        self.notice = ""
        self.installing_text = ""
        self.cancelling = False
        self._job = None
        self._manual = False
        self._before_check = IDLE
        self._open_requested = False
        self._install_thread = None
        self._install_outcome = None
        self._install_started = False

    def version_text(self):
        return self.update.manifest.version if self.update else ""

    def busy(self):
        return self.state in (CHECKING, PREPARING, INSTALLING)

    def take_open_request(self):
        """True once after a manual check finds an update. Automatic checks never open anything."""
        requested, self._open_requested = self._open_requested, False
        return requested

    def take_install_started(self):
        """True once when the installer is running and the app must close."""
        started, self._install_started = self._install_started, False
        return started

    def indicator_text(self):
        """The short text in the sidebar, or "" for nothing to show."""
        if self.state == AVAILABLE:
            return f"Update available: v{self.version_text()}"
        if self.state == PREPARING:
            return f"Preparing update: {percent(self.status)}%" if self.status.stage == "downloading" else "Preparing update..."
        if self.state == READY:
            return f"Update ready: v{self.version_text()}"
        if self.state == INSTALLING:
            return "Installing update..."
        return ""

    def stage_text(self):
        return "Cancelling..." if self.cancelling else STAGE_NAMES.get(self.status.stage, "Working")

    # ---- checking
    def check(self, manual=False):
        """Start one check. -> True if it started."""
        if not self.packaged:
            if manual:
                self.check_message = NOT_PACKAGED
            return False
        if self.busy() or self.state == READY:
            if manual and self.state == READY:
                self.check_message = f"Version v{self.version_text()} is ready to install."
                self._open_requested = True
            return False
        self._manual, self._before_check = manual, self.state
        self.check_message = "Checking for updates..."
        self._job = self._check_factory().start()
        self.state = CHECKING
        return True

    def _finish_check(self, result):
        self._job = None
        self.last_problem_kind = result.kind
        if result.status == "available":
            self.update, self.state, self.error, self.notice = result.update, AVAILABLE, "", ""
            self._open_requested = self._manual
        elif result.status in ("up_to_date", "rejected"):              # a release that did not verify is never offered
            self.update, self.state = None, IDLE
        else:
            self.state = self._before_check                            # no answer says nothing about what was known before
        self.check_message = result.message

    # ---- preparing
    def start_preparing(self, policy):
        """Only ever called because the user asked to download. -> True if it started."""
        if self.state != AVAILABLE or self.update is None:
            return False
        self.error, self.error_detail, self.notice, self.cancelling = "", "", "", False
        self.status = prepare.PreparationStatus()
        self._job = self._preparation_factory(self.update, policy=policy, is_counting=self.is_counting).start()
        self.state = PREPARING
        return True

    def cancel(self):
        if self.state == PREPARING and self._job is not None:
            self.cancelling = True
            self._job.cancel()

    def _finish_preparing(self, result):
        self._job, self.cancelling = None, False
        if result.status == "ready":
            self.state = READY
        elif result.status == "cancelled":
            self.state, self.notice = AVAILABLE, "Stopped. Download & Install continues from where it left off."
        else:
            self.state, self.error, self.error_detail = AVAILABLE, result.message, result.detail

    def poll(self):
        """-> True if anything the window shows has changed."""
        if self.state == CHECKING:
            result = self._job.result()
            if result is None:
                return False
            self._finish_check(result)
            return True
        if self.state == PREPARING:
            status, changed = self._job.status(), False
            if status != self.status:
                self.status, changed = status, True
            result = self._job.result()
            if result is not None:
                self._finish_preparing(result)
                changed = True
            return changed
        if self.state == INSTALLING and self._install_outcome is not None:
            state, self.error, self.error_detail = self._install_outcome
            self._install_thread, self._install_outcome = None, None
            if state is None:
                self._install_started = True
            else:
                self.state, self.installing_text = state, ""
            return True
        return False

    # ---- installing
    def begin_stopping(self):
        """Counting is being stopped so the update can be installed."""
        self.state, self.error, self.installing_text = INSTALLING, "", "Stopping counting and saving..."

    def refuse_install(self, message):
        self.state, self.error, self.error_detail, self.installing_text = READY, message, "", ""

    def install(self, state_dir, data_paths):
        """Start the installer, on its own thread because it checks every prepared file again first. poll() reports how it went, and
        the app closes only once it is running. A failure keeps the app as it is. -> True if it started."""
        if self.state not in (READY, INSTALLING) or self._install_thread is not None:
            return False
        self.state, self.error, self.installing_text, self._install_outcome = INSTALLING, "", "Starting the installer...", None
        self._install_thread = threading.Thread(target=self._run_install, args=(state_dir, data_paths), daemon=True, name="update-install")
        self._install_thread.start()
        return True

    def _run_install(self, state_dir, data_paths):
        try:
            self._begin_install(state_dir=state_dir, data_paths=data_paths)
            self._install_outcome = (None, "", "")
        except UpdateError as error:
            self._install_outcome = (AVAILABLE if error.kind == "not_prepared" else READY, str(error), error.detail)
        except Exception as error:             # whatever it was, the installed app is untouched and stays open
            self._install_outcome = (READY, "The update could not be started. Please try again.", f"{type(error).__name__}: {error}")
