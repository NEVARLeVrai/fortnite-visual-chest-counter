"""
Fortnite Visual Chest Counter - the desktop app.

    runtime\\python\\pythonw.exe -m fortnite_chest_counter.gui      (what "Fortnite Visual Chest Counter.exe" starts)

The window is a remote control and a viewer. Counting runs in a separate worker process - the same `live` command the
console version uses - and the window only observes it (see worker.py). Closing the activity panel, hiding the preview,
minimising or even a crash of the window never interrupts counting; every setting it changes goes through the same
config.toml / state file as the command line, so the two always agree.
"""

import ctypes
import json
import math
import os
import sqlite3
import sys
import subprocess
import threading
import time
import traceback
import uuid
from collections import Counter, deque
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from PySide6.QtCore import QByteArray, QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, Qt, QSignalBlocker, Signal, QTimer, QUrl, QUrlQuery, QVariantAnimation
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem, QSystemTrayIcon, QVBoxLayout, QWidget)

from fortnite_chest_counter import counter as C
from fortnite_chest_counter import diagnostics_cleanup, discord_bot, discord_group, discord_scoreboard, discord_summary, match_history, match_lifecycle, match_stats, overlay_layout, paths, profiles, replay_comparison, settings
from fortnite_chest_counter.detection import chest_types
from fortnite_chest_counter.gui import theme
from fortnite_chest_counter.gui import update_manager as UM
from fortnite_chest_counter.gui.chest_illustration import ChestIllustration
from fortnite_chest_counter.gui.worker import Worker, run_command
from fortnite_chest_counter.obs import credentials as obs_credentials
from fortnite_chest_counter.obs.link import TextOutputLink
from fortnite_chest_counter.preview import PreviewReader, block_name
from fortnite_chest_counter.progress import Eta, clock_text, describe
from fortnite_chest_counter.update import install as update_install
from fortnite_chest_counter.version import APP_NAME, RELEASE_CHANNEL, TAGLINE, __version__

ASSETS = Path(__file__).resolve().parent / "assets"
APP_ID = "GabrielGzS.FortniteVisualChestCounter"
HOTKEY_ID = 0xC0DE
RECORDING_CHOICES = (("Off (stop diagnostic files)", "off"), ("Only around my marks  (smallest)", "marks"),
                     ("Around chest decisions and my marks", "events"), ("Whole session  (about 10 GB per hour)", "session"))
RECORDING_TEXT = {"marks": "only around your marks", "events": "around chest decisions and your marks",
                  "session": "the whole session"}
REPO_URL = "https://github.com/NEVARLeVrai/fortnite-visual-chest-counter"
UPDATE_HEALTH_DELAY_MS = 2000                                  # the window must have been running this long before it counts as started
AUTO_CHECK_DELAY_MS = 6000                                     # after the window is up, so a check never competes with starting
INSTALL_STOP_DEADLINE_MS = 45000                               # longer than the worker's own 30 seconds to stop
RESULT_WATCH_SECONDS = 300
LOG_LIMIT = 3000
fmt = settings.format_count
# OBS WebSocket output (obs/link.py) states -> (short name, colour)
OBS_STATES = {"off": ("Off", theme.MUTED), "connecting": ("Connecting", theme.INFO), "waiting": ("Waiting for OBS", theme.APPROX),
              "pick": ("Connected - pick a Text source", theme.APPROX), "missing": ("Connected - source not found", theme.APPROX),
              "reads_file": ("Connected - the source reads a file", theme.APPROX), "live": ("Connected", theme.CERTAIN),
              "password": ("Password problem", theme.REJECT), "kicked": ("Disconnected by OBS", theme.REJECT),
              "unsupported": ("Not OBS WebSocket 5", theme.REJECT), "error": ("Problem", theme.REJECT)}


# ==================================================================
# small helpers
# ==================================================================

def app_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addFile(str(ASSETS / f"icon_{size}.png"), QSize(size, size))
    return icon


def set_class_icon(widget):
    """Windows: also give the window CLASS our icon. Qt registers its window class with the icon resource of the running
    .exe - here pythonw.exe, which has none Qt uses - so the class icon was Windows' generic application icon. The window
    itself always carries our icon (WM_SETICON), but when the app is too busy to answer the taskbar's WM_GETICON in time
    (typically while it is still starting up), Windows falls back to the class icon: the generic icon "sometimes" seen in
    the taskbar. With the class icon set as well, every fallback shows our icon. Never fatal."""
    if os.name != "nt":
        return False
    done = 0
    try:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]
        user32.SetClassLongPtrW.restype = ctypes.c_size_t
        hwnd = int(widget.winId())                          # creates the native window if it does not exist yet
        for index, metric in ((-14, 11), (-34, 49)):        # GCLP_HICON / SM_CXICON, GCLP_HICONSM / SM_CXSMICON
            size = user32.GetSystemMetrics(metric)
            icon = user32.LoadImageW(None, str(ASSETS / "app.ico"), 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if icon:
                user32.SetClassLongPtrW(hwnd, index, icon)
                done += 1
    except Exception:
        pass
    return done == 2


def open_path(path):
    path = Path(path)
    if path.is_dir() or path.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


def config_file():
    return paths.APP_DIR / settings.DEFAULT_CONFIG_PATH


def load_cfg():
    """config.toml with the counter file of the ACTIVE count profile (profiles.py) - every read of the counts, every
    reset / edit and the OBS files follow the profile chosen on the Dashboard."""
    cfg, _, warnings = settings.load_config(str(config_file()) if config_file().exists() else None)
    cfg["state"]["path"] = str(paths.APP_DIR / cfg["state"]["path"])          # profiles.json lives next to it
    profiles.apply(cfg, warn=warnings.append)
    return cfg, warnings


# ---- window size and position ([app] remember_window)
WINDOW_FILE = Path("state") / "window.json"           # next to the counts; the release zip never contains it


def window_file():
    return paths.APP_DIR / WINDOW_FILE


def save_window(widget, path):
    """Qt's own saveGeometry (normal size and position, maximised / full-screen state, which screen) -> a small JSON file."""
    try:
        C.atomic_write(path, json.dumps({"version": 1, "geometry": bytes(widget.saveGeometry().toBase64()).decode("ascii")}))
        return True
    except OSError:
        return False


def restore_window(widget, path):
    """Apply a layout saved by save_window, before the window is shown. -> True if one was applied. Qt itself moves a
    window whose screen is gone onto an available screen; keep_visible covers anything still unreachable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        geometry = QByteArray.fromBase64(data["geometry"].encode("ascii"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False                                    # no file yet, or a damaged one: default layout
    if geometry.isEmpty() or not widget.restoreGeometry(geometry):
        return False
    keep_visible(widget)
    return True


def keep_visible(widget, screens=None):
    """If the title bar would not be reachable on any screen (monitor unplugged, resolution lowered), centre the window on
    the primary screen at a size that fits. `screens`: available-area QRects (default: the real screens)."""
    if screens is None:
        screens = [s.availableGeometry() for s in QGuiApplication.screens()]
    if not screens:
        return
    frame = widget.frameGeometry() if widget.isVisible() else widget.geometry()
    title = QRect(frame.left(), frame.top(), frame.width(), 32)
    if any(title.intersected(s).width() >= 120 and title.intersected(s).height() >= 16 for s in screens):
        return
    area = screens[0]
    w, h = min(widget.width(), area.width()), min(widget.height(), area.height())
    widget.resize(w, h)
    widget.move(area.center() - QPoint(w // 2, h // 2))


def read_counts(state_path):
    """(certain, approx) from the counter file, or None. Read-only: unlike load_state it never moves a file."""
    try:
        raw = json.loads(Path(state_path).read_text(encoding="utf-8"))
        certain, approx = raw["certain"], raw["approximate"]
        if isinstance(certain, int) and isinstance(approx, int):
            return certain, max(approx, certain)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def read_types(state_path):
    """The chest-type breakdown of the counter file (counter.type_breakdown), or None. Read-only."""
    try:
        raw = json.loads(Path(state_path).read_text(encoding="utf-8"))
        state = {**C.fresh_state(), **raw}
        cleaned = C._clean_types(raw.get("types"))
        state["types"] = cleaned if cleaned is not None else C.fresh_types()
        return C.type_breakdown(state)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def types_text(breakdown):
    """One compact line: the APPROX total split by chest type (labels only - the totals stay the source of truth)."""
    if not breakdown:
        return ""
    parts = [f"{chest_types.LABELS[k]} {fmt(breakdown[k]['approx'])}" for k in (*chest_types.KNOWN, chest_types.UNKNOWN)
             if k in breakdown and breakdown[k]["approx"]]
    parts += [f"{discord_group.GROUP_TYPE_LABELS.get(k, k)} {fmt(v['approx'])}" for k, v in breakdown.items()
              if k not in (*chest_types.KNOWN, chest_types.UNKNOWN, "unclassified") and v["approx"]]
    rest = breakdown.get("unclassified", {}).get("approx", 0)
    if rest:
        parts.append(f"not classified {fmt(rest)}")
    return "   ·   ".join(parts) if parts else "no chests yet"


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, name=None, tip=None, on_click=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    if tip:
        widget.setToolTip(tip)
    if on_click:
        widget.clicked.connect(on_click)
    widget.setCursor(Qt.PointingHandCursor)
    return widget


def connected_buttons(*buttons):
    row = QHBoxLayout()
    row.setSpacing(2)
    for index, item in enumerate(buttons):
        position = "first" if index == 0 else "last" if index == len(buttons) - 1 else "middle"
        item.setProperty("groupPosition", position)
        row.addWidget(item)
    return row


def restyle(widget, name):
    """Give a widget another style name (e.g. primary -> danger). Qt only re-reads the style sheet after a re-polish."""
    widget.setObjectName(name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def card(title=None, hint=None):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    if title:
        layout.addWidget(label(title, "cardTitle"))
    if hint:
        layout.addWidget(label(hint, "hint", wrap=True))
    return frame, layout


def page_layout(widget, title, subtitle):
    """A scrollable page with a title. -> the content layout."""
    widget.setObjectName("page")
    outer = QVBoxLayout(widget)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    inner = QWidget()
    inner.setObjectName("page")
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(30, 24, 30, 24)
    layout.setSpacing(16)
    layout.addWidget(label(title, "pageTitle"))
    if subtitle:
        layout.addWidget(label(subtitle, "pageSub", wrap=True))
    scroll.setWidget(inner)
    outer.addWidget(scroll)
    return layout


def hline():
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.BORDER};")
    return line


def token_odds_text(total, boost):
    return match_stats.personal(0, total, boost).chance


def rounded_region(rect, radius):
    shape = QPainterPath()
    shape.addRoundedRect(QRectF(rect), radius, radius)
    return QRegion(shape.toFillPolygon().toPolygon())


def apply_window_theme(widget):
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return
    hwnd = ctypes.c_void_p(int(widget.winId()))
    for attribute, color in ((34, theme.BORDER), (35, theme.SURFACE), (36, theme.TEXT)):
        rgb = QColor(color)
        value = ctypes.c_uint32(rgb.red() | rgb.green() << 8 | rgb.blue() << 16)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))


class ThemedDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._entrance = QVariantAnimation(self)
        self._entrance.setDuration(140)
        self._entrance.setStartValue(0.45)
        self._entrance.setEndValue(1.0)
        self._entrance.setEasingCurve(QEasingCurve.OutCubic)
        self._entrance.valueChanged.connect(self.setWindowOpacity)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.windowFlags() & Qt.FramelessWindowHint:
            apply_window_theme(self)
        if theme.animations_enabled() and QGuiApplication.platformName() == "windows":
            self.setWindowOpacity(0.45)
            self._entrance.start()

    def hideEvent(self, event):
        self._entrance.stop()
        if self.windowOpacity() != 1.0:
            self.setWindowOpacity(1.0)
        super().hideEvent(event)


class PromptDialog(ThemedDialog):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("promptDialog")
        self.setMinimumWidth(380)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("promptSurface")
        outer.addWidget(surface)
        self.content = QVBoxLayout(surface)
        self.content.setContentsMargins(24, 24, 24, 24)
        self.content.setSpacing(14)
        heading = label(title, "dialogHeading", wrap=True)
        heading.setTextFormat(Qt.PlainText)
        self.content.addWidget(heading)
        message = label(text, "dialogBody", wrap=True)
        message.setTextFormat(Qt.PlainText)
        message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        message.setMaximumWidth(480)
        self.content.addWidget(message)

    def add_actions(self, yes, no=None, *, danger=False):
        actions = QHBoxLayout()
        actions.addStretch(1)
        confirm = button(yes, "danger" if danger else "primary", on_click=self.accept)
        if no is not None:
            cancel = button(no, on_click=self.reject)
            cancel.setAutoDefault(False)
            actions.addLayout(connected_buttons(cancel, confirm))
        else:
            actions.addWidget(confirm)
        self.content.addLayout(actions)
        if danger and no is not None:
            confirm.setAutoDefault(False)
            cancel.setDefault(True)
            cancel.setFocus()
        else:
            confirm.setDefault(True)
        return confirm


def ask(parent, title, text, yes="Yes", no="Cancel", danger=False):
    dialog = PromptDialog(parent, title, text)
    dialog.add_actions(yes, no, danger=danger)
    return dialog.exec() == QDialog.Accepted


def show_notice_dialog(parent, title, text):
    dialog = PromptDialog(parent, title, text)
    dialog.add_actions("OK")
    dialog.exec()


def show_error(parent, text):
    show_notice_dialog(parent, "Something went wrong", text)


def prompt_name(parent, title, text, action, initial=""):
    dialog = PromptDialog(parent, title, text)
    field = QLineEdit(initial)
    field.setAccessibleName("Profile name")
    dialog.content.addWidget(field)
    confirm = dialog.add_actions(action, "Cancel")
    confirm.setEnabled(bool(initial.strip()))
    field.textChanged.connect(lambda value: confirm.setEnabled(bool(value.strip())))
    field.setFocus()
    field.selectAll()
    accepted = dialog.exec() == QDialog.Accepted
    return field.text(), accepted


# ==================================================================
# activity console formatting
# ==================================================================

VERDICT = {"certain": ("CHEST +1", theme.CERTAIN), "approx": ("CHEST ~+1", theme.APPROX),
           "rejected": ("not counted", theme.REJECT)}
# Livestream URL connection states (StreamSource): colour, Dashboard pill text, text over the preview (None = show video)
STREAM_STATES = {"live": (theme.CERTAIN, "Counting", None),
                 "buffering": (theme.APPROX, "Buffering", "BUFFERING ...\nwaiting for video from the stream"),
                 "ad": (theme.APPROX, "Ad break", "AD BREAK ...\ncounting continues when the game is back"),
                 "incomplete": (theme.APPROX, None, None),
                 "lagging": (theme.APPROX, None, None),
                 "caught_up": (theme.CERTAIN, None, None),
                 "reconnecting": (theme.REJECT, "Reconnecting", "RECONNECTING ...\nthe stream connection was lost"),
                 "offline": (theme.REJECT, "Stream offline", "STREAM OFFLINE\nchecking again automatically")}


def _span(text, color, bold=False):
    weight = "font-weight:600;" if bold else ""
    return f'<span style="color:{color};{weight}">{escape(str(text))}</span>'


def _pad(text, width):
    return escape(text) + "&nbsp;" * max(width - len(text), 0)


def log_entries(message):
    """-> [(debug_only, html)] for one worker message."""
    kind = message.get("type")
    clock = message.get("clock") or time.strftime("%H:%M:%S")
    stamp = _span(clock, theme.DIM) + "&nbsp;&nbsp;"
    if kind == "event":
        text, color = VERDICT.get(message.get("verdict"), ("?", theme.MUTED))
        if message.get("generic"):
            text, color = "prompt only", theme.DIM
        why = f" - {message['why']}" if message.get("why") else ""
        totals = ""
        if message.get("certain_text") is not None:
            totals = "&nbsp;" * 3 + _span(f"CERTAIN {message['certain_text']} | APPROX {message['approx_text']}", theme.DIM)
        main = (stamp + f'<span style="color:{color};font-weight:600">{_pad(text, 12)}</span>'
                + escape(f"{message.get('evidence', '')}{why}") + totals)
        detail = "&nbsp;" * 10 + _span(f"{message.get('event_id', '')} at {message.get('stamp', '')} | {message.get('source', '')}"
                       f" | latency {message.get('latency_s') or 0:.1f}s | {message.get('reason', '')}", theme.DIM)
        return [(bool(message.get("generic")), main), (True, detail)]
    if kind == "status":
        return [(True, stamp + _span(message.get("text", ""), theme.DIM))]
    if kind == "stream":
        color = STREAM_STATES.get(message.get("state"), (theme.MUTED,))[0]
        return [(False, stamp + _span(message.get("text", ""), color, bold=message.get("state") != "incomplete"))]
    if kind in ("info", "warn", "error", "line", "stderr"):
        text = str(message.get("text", "")).strip("\n")
        if not text:
            return []
        color = theme.LOG_COLORS.get(kind, theme.MUTED)
        prefix = {"warn": "Warning: ", "error": "Error: "}.get(kind, "")
        debug_only = kind == "stderr"
        lines = text.split("\n")
        out = [(debug_only, stamp + _span(prefix + lines[0], color, bold=kind == "error"))]
        out += [(debug_only, "&nbsp;" * 10 + _span(line, color)) for line in lines[1:] if line.strip()]
        return out
    return []


# ==================================================================
# widgets
# ==================================================================

class CountCard(QFrame):
    def __init__(self, title, subtitle, color):
        super().__init__()
        self.setObjectName("countCard")
        self.color = color
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        box = QVBoxLayout()
        box.setContentsMargins(0, 9, 0, 9)
        box.setSpacing(0)
        box.addWidget(label(title, "countLabel"), 0, Qt.AlignHCenter)
        self.value = label("0", "countValue")
        self.value.setStyleSheet(f"color: {color};")
        box.addWidget(self.value, 0, Qt.AlignHCenter)
        if subtitle:
            box.addWidget(label(subtitle, "countSub", wrap=True))
        layout.addLayout(box, 1)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.timeout.connect(lambda: self.setStyleSheet(""))

    def set_value(self, value, flash=False):
        text = fmt(value)
        if text != self.value.text() and flash:
            self.setStyleSheet(f"#countCard {{ border: 1px solid {self.color}; background: {theme.rgba(self.color, 0.08)}; }}")
            self._flash.start(900)
        self.value.setText(text)


class MaterialMenuItems(QStyledItemDelegate):
    def paint(self, painter, option, index):
        active = QStyle.State_Selected | QStyle.State_MouseOver
        if option.state & active:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.SURFACE_3))
            painter.drawRoundedRect(QRectF(option.rect.adjusted(2, 2, -2, -2)), 10, 10)
            painter.restore()
            option = QStyleOptionViewItem(option)
            option.state &= ~active
        super().paint(painter, option, index)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 48))
        return size


class MaterialComboBox(QComboBox):
    """Use Qt's menu interaction with a themed popup and a short slide."""

    def __init__(self):
        super().__init__()
        self.setItemDelegate(MaterialMenuItems(self))
        self._popup_window = None
        self._popup_origin = QPoint()
        self._popup_motion = QVariantAnimation(self)
        self._popup_motion.setDuration(180)
        self._popup_motion.setStartValue(0.2)
        self._popup_motion.setEndValue(1.0)
        self._popup_motion.setEasingCurve(QEasingCurve.OutCubic)
        self._popup_motion.valueChanged.connect(self._show_progress)

    def _show_progress(self, amount):
        if self._popup_window is not None:
            self._popup_window.move(self._popup_origin + QPoint(0, round(-6 * (1 - amount))))

    def showPopup(self):
        if not self.count():
            return
        popup = self.view().window()
        if not isinstance(popup, QFrame) or popup.windowType() != Qt.Popup:
            super().showPopup()
            return
        self._popup_motion.stop()
        if self._popup_window is None:
            self._popup_window = popup
            popup.setObjectName("comboPopup")
            popup.setFrameShape(QFrame.NoFrame)
            if popup.layout() is not None:
                popup.layout().setContentsMargins(5, 5, 5, 5)
        animate = theme.animations_enabled()
        super().showPopup()
        if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
            policy = ctypes.c_int(1)  # DWMNCRP_DISABLED: remove the native popup shadow.
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(popup.winId())), 2,
                                                       ctypes.byref(policy), ctypes.sizeof(policy))
        screen = popup.screen()
        if screen is not None and popup.layout() is not None:
            margins = popup.layout().contentsMargins()
            available = screen.availableGeometry()
            height = min(popup.height() + margins.top() + margins.bottom(), available.height())
            popup.resize(popup.width(), height)
            if popup.geometry().bottom() > available.bottom():
                popup.move(popup.x(), available.bottom() - popup.height() + 1)
        self._popup_origin = popup.pos()
        if (screen is not None and popup.y() >= self.mapToGlobal(QPoint(0, self.height())).y() - 2
                and popup.geometry().bottom() + 6 <= screen.availableGeometry().bottom()):
            self._popup_origin += QPoint(0, 6)
        if animate:
            self._show_progress(0.2)
            self._popup_motion.start()
        else:
            popup.move(self._popup_origin)

    def hidePopup(self):
        self._popup_motion.stop()
        super().hidePopup()


class ScreenOverlayCard(QFrame):
    """Render personal, team and lifetime statistics as distinct overlay sections."""

    def __init__(self, flags=Qt.Widget):
        super().__init__(None, flags)
        self._floating = flags != Qt.Widget
        self.plain_text = ""
        self.setObjectName("overlayBox")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 16, 10)
        layout.setSpacing(12)
        self.chest = ChestIllustration(compact=True)
        layout.addWidget(self.chest, 0, Qt.AlignTop)
        content = QVBoxLayout()
        content.setSpacing(8)
        self.sections = []
        for index, title in enumerate(("PERSONAL · CURRENT MATCH", "TEAM · CURRENT MATCH", "MATCH HISTORY + LIVE")):
            group = QFrame()
            group.setObjectName("overlaySection")
            column = QVBoxLayout(group)
            column.setContentsMargins(0, 7 if index else 0, 0, 0)
            column.setSpacing(3)
            heading = label(title, "overlaySectionTitle")
            column.addWidget(heading)
            value = label("", "screenOverlayCaption", wrap=True)
            value.setTextFormat(Qt.PlainText)
            value.setMaximumWidth(720)
            column.addWidget(value)
            if index:
                group.setStyleSheet(f"QFrame#overlaySection {{ border-top: 1px solid {theme.BORDER}; }}")
                group.hide()
            content.addWidget(group)
            self.sections.append((group, value))
            if not hasattr(self, "headings"):
                self.headings = []
            self.headings.append(heading)
        self.caption = self.sections[0][1]
        layout.addLayout(content, 1)

    def set_text(self, text, *, sections=None, titles=None, styled_sections=None):
        if titles is not None:
            for heading, title in zip(self.headings, titles):
                heading.setText(title)
        parts = tuple(sections) if sections is not None else (text, "", "")
        presentation = tuple(styled_sections) if styled_sections is not None else parts
        if text == self.plain_text and (parts, presentation) == getattr(self, "_parts", None):
            return False
        self.plain_text = text
        self._parts = (parts, presentation)
        for (group, value), part, shown in zip(self.sections, parts, presentation):
            group.setVisible(bool(part))
            value.setTextFormat(Qt.RichText if styled_sections is not None else Qt.PlainText)
            value.setText(shown)
            if self._floating:
                value.ensurePolished()
                font = value.font()
                if styled_sections is not None and "<b>" in shown:
                    font.setBold(True)
                metrics = QFontMetrics(font)
                width = max((metrics.horizontalAdvance(line) for line in part.splitlines()), default=0) + 4
                limit = value.maximumWidth()
                value.setWordWrap(width > limit)
                value.setMinimumWidth(min(limit, width))
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._floating:
            self.setMask(rounded_region(self.rect(), 14))


class PreviewPanel(QFrame):
    def __init__(self, caption):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.view = QLabel("Start counting to see the video here")
        self.view.setObjectName("previewBox")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(240, 135)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.caption = label(caption, "hint", wrap=True)
        layout.addWidget(self.caption)
        layout.addWidget(self.view, 1)
        self.image = None
        self.last_frame_at = None

    def show_frame(self, width, height, data):
        self.last_frame_at = time.monotonic()
        self.image = QImage(data, width, height, 3 * width, QImage.Format_RGB888).copy()
        self._render()

    def clear(self, text="Start counting to see the video here"):
        self.image = None
        self.last_frame_at = None
        self.view.setPixmap(QPixmap())
        self.view.setText(text)

    def _render(self):
        if self.image is None:
            return
        size = self.view.size() - QSize(4, 4)
        self.view.setPixmap(QPixmap.fromImage(self.image).scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


# ==================================================================
# dialogs
# ==================================================================

class CountsDialog(ThemedDialog):
    """Set the counter (also: start from the chests already opened)."""

    def __init__(self, parent, certain, total):
        super().__init__(parent)
        self.setWindowTitle("Edit counts")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label("Edit chest counts", "cardTitle"))
        layout.addWidget(label("Edit the confirmed and estimated counts. Total is their sum.", "hint", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.approx = QSpinBox()
        self.certain = QSpinBox()
        for spin, value in ((self.certain, certain), (self.approx, max(0, total - certain))):
            spin.setAlignment(Qt.AlignCenter)
            spin.setRange(0, C.MAX_STARTING_COUNT)
            spin.setGroupSeparatorShown(True)
            spin.setValue(value)
            spin.setMinimumWidth(140)
        grid.addWidget(label("Certain"), 0, 0)
        grid.addWidget(self.certain, 0, 1)
        grid.addWidget(label("Approx"), 1, 0)
        grid.addWidget(self.approx, 1, 1)
        layout.addLayout(grid)
        self.total_label = label("", "cardTitle")
        layout.addWidget(self.total_label)
        self.problem = label("", "hint")
        self.problem.setStyleSheet(f"color: {theme.REJECT};")
        layout.addWidget(self.problem)
        save = button("Save", "primary", on_click=self._accept)
        save.setDefault(True)
        actions = connected_buttons(button("Cancel", on_click=self.reject), save)
        actions.insertStretch(0, 1)
        layout.addLayout(actions)
        self.approx.valueChanged.connect(self._check)
        self.certain.valueChanged.connect(self._check)
        self._check()

    @property
    def total(self):
        return self.certain.value() + self.approx.value()

    def _check(self):
        bad = self.total > C.MAX_STARTING_COUNT
        self.total_label.setText(f"Total · {fmt(self.total)}")
        self.problem.setText(f"Total cannot exceed {fmt(C.MAX_STARTING_COUNT)}." if bad else "")
        return not bad

    def _accept(self):
        if self._check():
            self.accept()


class VideoDialog(ThemedDialog):
    """Count the chests in a recorded gameplay video (the `video` command). Never touches the live counter."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Analyse a recording")
        self.resize(640, 520)
        self.worker = Worker()
        self.duration = None
        self.span = None                                                # (first, last) second of video to analyse
        self.eta = Eta()
        self.result_dir = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label("Count the chests in a recorded gameplay video", "cardTitle"))
        layout.addWidget(label("Runs faster than real time. Your live counter and OBS overlay are not changed; the result "
                               "files go to the video_output folder.", "hint", wrap=True))
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("Choose a video file (.mp4, .mkv, .mov ...)")
        row.addWidget(self.path, 1)
        row.addWidget(button("Browse...", on_click=self._browse))
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.state = label("", "hint")
        layout.addWidget(self.state)
        counts = QHBoxLayout()
        self.certain = CountCard("CERTAIN", "confirmed", theme.CERTAIN)
        self.approx = CountCard("APPROX", "estimated total", theme.APPROX)
        counts.addWidget(self.certain)
        counts.addWidget(self.approx)
        layout.addLayout(counts)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_LIMIT)
        layout.addWidget(self.log, 1)
        bottom = QHBoxLayout()
        self.open_results = button("Open results folder", on_click=lambda: open_path(self.result_dir))
        self.open_results.setEnabled(False)
        bottom.addWidget(self.open_results)
        bottom.addStretch(1)
        self.go = button("Start", "primary", on_click=self._toggle)
        self.go.setEnabled(False)
        bottom.addWidget(self.go)
        bottom.addWidget(button("Close", on_click=self.close))
        layout.addLayout(bottom)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(60)

    def _browse(self):
        start = str(Path.home() / "Videos")
        name, _ = QFileDialog.getOpenFileName(self, "Choose a gameplay video", start,
                                              "Videos (*.mp4 *.mkv *.mov *.avi *.flv *.ts *.webm);;All files (*)")
        if name:
            self.path.setText(name)
            self.go.setEnabled(True)

    def _toggle(self):
        if self.worker.running():
            self.worker.stop()
            self.go.setEnabled(False)
            self.state.setText("Stopping...")
            return
        self.log.clear()
        self.certain.set_value(0)
        self.approx.set_value(0)
        self.progress.setValue(0)
        self.open_results.setEnabled(False)
        self.eta = Eta()
        try:
            self.worker.start(["video", self.path.text(), "--gui"])
        except OSError as error:
            show_error(self, f"The analysis could not start ({error}).")
            return
        self.go.setText("Stop")
        restyle(self.go, "danger")
        self.state.setText("Starting...")

    def _pump(self):
        for message in self.worker.drain():
            kind = message.get("type")
            if kind == "video_start":
                self.duration = message.get("duration")
                first = message.get("start") or 0.0
                last = message.get("until") or self.duration
                self.span = (first, last) if last else None
                self.state.setText(f"Analysing {message.get('name', '')} ..."
                                   + ("" if self.span else "  (the video's length is unknown: no time estimate)"))
            elif kind == "progress":
                t = message.get("t", 0)
                if self.span and self.span[1] > self.span[0]:
                    done, total = t - self.span[0], self.span[1] - self.span[0]
                    self.progress.setRange(0, 1000)
                    self.progress.setValue(int(1000 * min(done / total, 1.0)))
                    left = describe(self.eta.update(done, total))
                    self.state.setText(f"{clock_text(t)} of {clock_text(self.span[1])} analysed   ·   "
                                       + (left or "measuring speed..."))
                else:
                    self.progress.setRange(0, 0)
                    self.state.setText(f"{clock_text(t)} analysed")
                self.certain.set_value(message.get("certain", 0))
                self.approx.set_value(message.get("approx", 0))
            elif kind == "event":
                if message.get("verdict") in ("certain", "approx"):
                    for debug_only, html in log_entries(message):
                        if not debug_only:
                            self.log.appendHtml(html)
                if message.get("certain") is not None:
                    self.certain.set_value(message["certain"], flash=True)
                    self.approx.set_value(message["approx"], flash=True)
            elif kind == "finished":
                self.progress.setRange(0, 1000)
                self.progress.setValue(1000 if not message.get("stopped_early") else self.progress.value())
                self.certain.set_value(message["certain"])
                self.approx.set_value(message["approx"])
                self.result_dir = message.get("obs_dir")
                self.open_results.setEnabled(bool(self.result_dir))
                early = " (stopped early: only the part already analysed is counted)" if message.get("stopped_early") else ""
                self.state.setText(f"Finished: {message.get('analysed', '')[:-3]} of video in {message.get('minutes', 0)} min{early}. "
                                   "APPROX is the estimated total and already includes CERTAIN.")
            elif kind in ("error", "warn"):
                for _, html in log_entries(message):
                    self.log.appendHtml(html)
                if kind == "error":
                    self.state.setText("Could not analyse this file - see the message below.")
            elif kind == "exit":
                self.go.setText("Start again")
                restyle(self.go, "primary")
                self.go.setEnabled(True)
                self.progress.setRange(0, 1000)

    def closeEvent(self, event):
        if self.worker.running():
            if not ask(self, "Stop the analysis?", "The video is still being analysed. Stop and close?", "Stop and close"):
                event.ignore()
                return
            self.worker.stop()
            deadline = time.monotonic() + 5
            while self.worker.running() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.05)
            self.worker.kill()
        self.timer.stop()
        super().closeEvent(event)


# ==================================================================
# pages
# ==================================================================

class ReviewDialog(ThemedDialog):
    """Runs `review --gui` over the captures folder: which capture, how much video, time left, then each report."""

    MODES = {"events": "around chest decisions and marks", "marks": "around marks", "session": "whole session"}

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Review captures")
        self.resize(840, 640)
        self.worker = Worker()
        self.eta = Eta()
        self.total = None
        self.failed = None
        self.finished = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        self.title = label("Reviewing captures", "cardTitle")
        layout.addWidget(self.title)
        layout.addWidget(label("Every diagnostic capture is replayed through the current detector and compared with what was "
                               "counted live and with your labels (labels.csv in each capture folder).", "hint", wrap=True))
        self.current = label("Starting...", wrap=True)
        layout.addWidget(self.current)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)                                    # busy until the total is known
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.detail = label("", "hint")
        layout.addWidget(self.detail)
        self.text = QPlainTextEdit()
        self.text.setObjectName("report")
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        row.addWidget(button("Open captures folder", on_click=self._open_folder))
        row.addStretch(1)
        row.addWidget(button("Copy report", on_click=lambda: QApplication.clipboard().setText(self.text.toPlainText())))
        self.stop = button("Stop", "danger", on_click=self._stop)
        row.addWidget(self.stop)
        row.addWidget(button("Close", on_click=self.close))
        layout.addLayout(row)
        try:
            self.worker.start(["review", "--gui"])
        except OSError as error:
            self._fail(f"The review could not start ({error}).")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(100)

    def _open_folder(self):
        cfg, _ = load_cfg()
        open_path(paths.APP_DIR / cfg["capture"]["dir"])

    def _stop(self):
        self.worker.stop()
        self.stop.setEnabled(False)
        self.current.setText("Stopping...")

    def _fail(self, text):
        self.failed = text
        self.title.setText("Review failed")
        self.title.setStyleSheet(f"color: {theme.REJECT};")
        self.current.setText(text)
        self.progress.setRange(0, 1000)
        self.stop.setEnabled(False)

    def _show_progress(self, done):
        if not self.total:
            return
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(1000 * min(done / self.total, 1.0)))
        left = describe(self.eta.update(done, self.total))
        self.detail.setText(f"{clock_text(done)} of {clock_text(self.total)} of recorded video reviewed   ·   "
                            + (left or "measuring speed..."))

    def _pump(self):
        for m in self.worker.drain():
            kind = m.get("type")
            if kind == "review_start":
                self.total = m.get("total_seconds") or 0.0
                if not m.get("captures"):
                    self._fail("No finished captures were found.")
            elif kind == "review_capture":
                mode = self.MODES.get(m.get("mode"), m.get("mode") or "")
                self.current.setText(f"Capture {m['index']} of {m['count']}:  {m['name']}   ·   {mode}   ·   "
                                     f"{clock_text(m.get('seconds', 0))} of video in {m.get('clips', '?')} clip(s)")
                self._show_progress(m.get("done", 0.0))
            elif kind == "review_progress":
                self._show_progress(m.get("done", 0.0))
            elif kind == "review_report":
                self.text.appendPlainText(m.get("text", "").rstrip() + "\n")
            elif kind == "review_finished":
                self.finished = True
                self.title.setText("Review finished")
                self.title.setStyleSheet(f"color: {theme.CERTAIN};")
                self.current.setText(f"{m.get('captures', 0)} capture(s) reviewed in {m.get('minutes', 0)} min. "
                                     "Reports are also saved as review.txt in each capture folder.")
                self.progress.setRange(0, 1000)
                self.progress.setValue(1000)
                self.detail.setText("")
            elif kind == "review_stopped":
                self.finished = True
                self.title.setText("Review stopped")
                self.current.setText("Stopped. The reports above are complete; the rest was not reviewed.")
                self.detail.setText("")
            elif kind == "error":
                self._fail(str(m.get("text", "The review failed.")))
            elif kind == "exit":
                self.stop.setEnabled(False)
                if not self.finished and not self.failed:
                    tail = "\n".join(self.worker.stderr_tail[-6:])
                    self._fail("The review stopped unexpectedly." + (f"\n{tail}" if tail else ""))

    def closeEvent(self, event):
        if self.worker.running():
            self.worker.stop()
            deadline = time.monotonic() + 5
            while self.worker.running() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.05)
            self.worker.kill()
        self.timer.stop()
        super().closeEvent(event)


class AnimatedPages(QStackedWidget):
    """Keep the incoming layout fixed and fade out a snapshot of the previous page."""

    def __init__(self):
        super().__init__()
        self._outgoing = QLabel(self)
        self._outgoing.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._outgoing.hide()
        self._fade = QGraphicsOpacityEffect(self._outgoing)
        self._outgoing.setGraphicsEffect(self._fade)
        self._entrance = QVariantAnimation(self)
        self._entrance.setDuration(160)
        self._entrance.setStartValue(1.0)
        self._entrance.setEndValue(0.0)
        self._entrance.setEasingCurve(QEasingCurve.OutCubic)
        self._entrance.valueChanged.connect(lambda value: self._fade.setOpacity(float(value)))
        self._entrance.finished.connect(self._clear_transition)

    def _clear_transition(self):
        self._outgoing.hide()
        self._outgoing.clear()

    def setCurrentIndex(self, index):
        previous = self.currentIndex()
        if index == previous or not 0 <= index < self.count():
            return
        self._entrance.stop()
        self._clear_transition()
        current = self.currentWidget()
        if current is not None and self.isVisible() and theme.animations_enabled():
            self._outgoing.setGeometry(current.geometry())
            self._outgoing.setPixmap(current.grab())
            self._fade.setOpacity(1.0)
            self._outgoing.raise_()
            self._outgoing.show()
        super().setCurrentIndex(index)
        if self._outgoing.isVisible():
            self._outgoing.raise_()
            self._entrance.start()


class Dashboard(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.screen_overlay = None
        self.screen_overlay_display = None
        self.saved_match_count = 0
        layout = page_layout(self, "Dashboard", None)
        layout.setContentsMargins(24, 24, 24, 24)
        self.findChild(QScrollArea).setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = layout.itemAt(0).widget()
        layout.removeWidget(header)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch(1)
        self.pill = label("", "pill")
        self.show_capture = button("Capture settings", "toggle")
        self.show_capture.setCheckable(True)
        self.start = button("Start counting", "primary", on_click=win.toggle_counting)
        self.start.setMinimumHeight(36)
        top.addWidget(self.start)
        layout.insertLayout(0, top)

        # error banner
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(14, 10, 10, 10)
        self.banner_text = label("", "bannerText", wrap=True)
        self.banner_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        banner_layout.addWidget(self.banner_text, 1)
        banner_layout.addWidget(button("Dismiss", on_click=self.banner.hide), 0, Qt.AlignTop)
        self.banner.hide()
        layout.addWidget(self.banner)

        # what the last update did: shown once, never in the error colours unless something needs attention
        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(14, 10, 10, 10)
        self.notice_text = label("", wrap=True)
        notice_layout.addWidget(self.notice_text, 1)
        notice_layout.addWidget(button("Dismiss", on_click=self.notice.hide), 0, Qt.AlignTop)
        self.notice.hide()
        layout.addWidget(self.notice)

        sources_layout = QVBoxLayout()
        sources_layout.setSpacing(4)
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_row.addWidget(self.pill, 0, Qt.AlignVCenter)
        self.source_chips = []
        chips = QHBoxLayout()
        chips.setSpacing(2)
        for width, position in ((140, "first"), (240, "last")):
            chip = label("", "sourceChip")
            chip.setProperty("groupPosition", position)
            chip.setMinimumHeight(28)
            chip.setTextFormat(Qt.PlainText)
            chip.setMaximumWidth(width)
            chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            chips.addWidget(chip, 0, Qt.AlignVCenter)
            self.source_chips.append(chip)
        source_row.addLayout(chips)
        source_row.addStretch(1)
        sources_layout.addLayout(source_row)
        self.metrics = label("", "metrics", wrap=True)
        self.metrics.hide()
        sources_layout.addWidget(self.metrics)
        layout.addLayout(sources_layout)

        board = QHBoxLayout()
        board.setSpacing(14)
        stage, stage_layout = card()
        stage.setObjectName("chestStage")
        recent_card, recent_layout = card("Group leaderboard", "Chests per player from matches shared to Discord.")
        self.leaders = label("Share a match to appear here.", "hint", wrap=True)
        self.leaders.setTextFormat(Qt.PlainText)
        recent_layout.addWidget(self.leaders)
        self.leader_rows = []
        for _ in range(3):
            caption = label("", "hint")
            caption.setTextFormat(Qt.PlainText)
            bar = QProgressBar()
            bar.setObjectName("leaderBar")
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setFixedHeight(5)
            caption.hide()
            bar.hide()
            recent_layout.addWidget(caption)
            recent_layout.addWidget(bar)
            self.leader_rows.append((caption, bar))
        recent_layout.addWidget(label("Recent matches", "cardTitle"))
        self.recent_matches = label("No matches saved yet.", "hint", wrap=True)
        self.recent_matches.setTextFormat(Qt.PlainText)
        recent_layout.addWidget(self.recent_matches)
        recent_layout.addStretch(1)
        recent_layout.addWidget(button("View history", on_click=lambda: win.go_to(4)), 0, Qt.AlignLeft)
        board.addWidget(recent_card, 1)
        stage_content = stage_layout
        stage_content.setSpacing(6)
        stage_content.addStretch(1)
        self.chest = ChestIllustration()
        self._shown_chest = None
        stage_content.addWidget(self.chest)
        total_title = label("TOTAL CHESTS", "countLabel")
        stage_content.addWidget(total_title, 0, Qt.AlignHCenter)
        self.total = label("0", "stageTotal")
        stage_content.addWidget(self.total, 0, Qt.AlignHCenter)
        odds_title = label("PIRATE TOKEN CHANCE", "countLabel")
        stage_content.addWidget(odds_title, 0, Qt.AlignHCenter)
        token_row = QHBoxLayout()
        self.token_mode = MaterialComboBox()
        self.token_mode.addItems(discord_scoreboard.MODES)
        self.token_mode.addItem("Auto · waiting", "Auto")
        self.token_mode.setToolTip("Choose a mode manually or use Auto to read the latest Battle Royale mode from Fortnite’s local log.")
        self.token_boost = MaterialComboBox()
        for caption, multiplier in (("Normal ×1", 1.0), ("Boost ×1.5", 1.5), ("Boost ×2", 2.0)):
            self.token_boost.addItem(caption, multiplier)
        self.token_boost.setToolTip("Apply this multiplier to every chest in the estimate.")
        self.token_odds = label("", "stageOdds")
        self.token_odds.setToolTip("Estimated chance that at least one chest in the shown total would drop a Pirate Token. "
                                   "The model assumes independent rolls at 1 in 137,500 per chest before the selected boost. "
                                   "The calculation uses the TOTAL card.")
        stage_content.addWidget(self.token_odds, 0, Qt.AlignHCenter)
        token_row.addStretch(1)
        token_row.addLayout(connected_buttons(self.token_mode, self.token_boost))
        token_row.addStretch(1)
        stage_content.addStretch(1)
        stage_content.addLayout(token_row)
        board.addWidget(stage, 1)
        self.token_boost.currentIndexChanged.connect(lambda _: self.update_token_odds())

        rail, rail_layout = card()
        counts = QHBoxLayout()
        self.certain = CountCard("CERTAIN", None, theme.CERTAIN)
        self.approx = CountCard("APPROX", None, theme.APPROX)
        self.certain.setToolTip("Chests confirmed by strong evidence")
        self.approx.setToolTip("Estimated openings only")
        counts.addWidget(self.certain, 1)
        counts.addWidget(self.approx, 1)
        rail_layout.addLayout(counts)
        rail_layout.addWidget(label("Recent openings", "cardTitle"))
        self.recent = QListWidget()
        self.recent.setObjectName("recentOpenings")
        self.recent.setSelectionMode(QListWidget.NoSelection)
        self.recent.setFocusPolicy(Qt.NoFocus)
        self.recent.setWordWrap(True)
        rail_layout.addWidget(self.recent, 1)
        self.clear_recent()
        count_actions = connected_buttons(
            button("Edit", tip="Edit the counts", on_click=win.edit_counts),
            button("Reset", tip="Set all counts to 0", on_click=win.reset_counts),
            button("Finish match", on_click=win.finish_match),
        )
        for index in range(count_actions.count()):
            count_actions.itemAt(index).widget().setProperty("compactAction", True)
        count_actions.addStretch(1)
        rail_layout.addLayout(count_actions)
        board.addWidget(rail, 1)
        layout.addLayout(board)
        self.update_token_odds()
        overlay_card, overlay_layout = card()
        overlay_header = QHBoxLayout()
        overlay_header.addWidget(label("On-screen overlay", "cardTitle"))
        overlay_header.addStretch(1)
        overlay_header.addWidget(button("Overlay settings", on_click=lambda: win.go_to(1)))
        overlay_layout.addLayout(overlay_header)
        types = QHBoxLayout()
        types.addWidget(label("Chest types:", "hint"), 0, Qt.AlignTop)
        self.types = label("", "hint", wrap=True)
        self.types.setToolTip("How the total counted chests split by type. Read from each chest's pop-up "
                              "line: CHEST, RARE CHEST or SPRITE CHEST - 'unknown type' when the line could not be read. "
                              "'Not classified' = chests counted before v0.2.2, or set with Edit counts. "
                              "OBS still shows only the two totals.")
        types.addWidget(self.types, 1)
        overlay_layout.addLayout(types)
        self.overlay_preview = ScreenOverlayCard()
        self.viewers = self.overlay_preview.caption
        overlay_layout.addWidget(self.overlay_preview)
        self.obs_line = label("", "hint", wrap=True)          # OBS WebSocket output status (only while it is on)
        self.obs_line.hide()
        overlay_layout.addWidget(self.obs_line)
        layout.addWidget(overlay_card)

        box, box_layout = card("Capture settings")
        inputs = QGridLayout()
        inputs.setColumnStretch(1, 1)
        inputs.setHorizontalSpacing(12)
        inputs.setVerticalSpacing(8)
        inputs.addWidget(label("Profile"), 0, 0)
        self.profile = MaterialComboBox()
        self.profile.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.profile.setMinimumContentsLength(10)
        self.profile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.profile.setToolTip("Each profile keeps its own counts and its own video source (for example one profile per "
                                "streamer). One profile counts at a time; OBS always shows the selected profile.")
        self.profile.activated.connect(win.switch_profile)
        profile_row = QHBoxLayout()
        profile_row.setSpacing(8)
        profile_row.addWidget(self.profile, 1)
        self.profile_new = button("New profile...", tip="A new profile with its own counts, starting at 0",
                                  on_click=win.new_profile)
        self.profile_rename = button("Rename...", tip="Change the name of the selected profile (its counts stay)",
                                     on_click=win.rename_profile)
        self.profile_delete = button("Delete...", tip="Remove the selected profile (its counts are kept in a backup "
                                                      "file)", on_click=win.delete_profile)
        profile_row.addLayout(connected_buttons(self.profile_new, self.profile_rename, self.profile_delete))
        inputs.addLayout(profile_row, 0, 1, 1, 2)

        inputs.addWidget(label("Source"), 1, 0)
        self.source = MaterialComboBox()
        self.source.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.source.setMinimumContentsLength(12)
        self.source.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        inputs.addWidget(self.source, 1, 1)
        self.refresh = button("Refresh", tip="Look for video devices and screens again", on_click=win.refresh_sources)
        inputs.addWidget(self.refresh, 1, 2)
        box_layout.addLayout(inputs)
        self.source.currentTextChanged.connect(self.update_source_summary)
        self.source.currentIndexChanged.connect(lambda _: self.clear_recent(source=1))
        self.url_row = QWidget()                            # only for "Livestream URL"
        url_layout = QVBoxLayout(self.url_row)
        url_layout.setContentsMargins(0, 0, 0, 0)
        url_layout.setSpacing(4)
        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste the live link, e.g.  https://www.twitch.tv/channel  or  "
                                    "https://www.youtube.com/watch?v=...")
        url_layout.addWidget(self.url)
        self.url.textChanged.connect(self.update_source_summary)
        self.update_source_summary()
        stab = QHBoxLayout()
        stab.addWidget(label("Stream stability"))
        self.stability = MaterialComboBox()
        for key, name, _ in settings.STREAM_STABILITY_CHOICES:
            self.stability.addItem(name, key)
        stab.addWidget(self.stability, 1)
        url_layout.addLayout(stab)
        self.stability_hint = label("", "hint", wrap=True)
        url_layout.addWidget(self.stability_hint)
        self.stability.currentIndexChanged.connect(lambda _: self.stability_hint.setText(
            next(text for key, _, text in settings.STREAM_STABILITY_CHOICES if key == self.stability.currentData())
            + " A bigger buffer cannot bring back video the platform never delivered (outages, ad breaks)."))
        url_layout.addWidget(label("Counts from the public stream as viewers get it, a few seconds behind, at 720p - no OBS "
                                   "or browser needed. Twitch and YouTube; the stream must be live and public. Video can "
                                   "arrive in bursts or pause to buffer. If the connection drops, the app reconnects by itself, "
                                   "keeps your counts and tells you which part could not be seen.", "hint", wrap=True))
        self.url_row.hide()
        box_layout.addWidget(self.url_row)
        self.source.currentIndexChanged.connect(lambda _: self.url_row.setVisible(
            (self.source.currentData() or {}).get("source") == "stream"))
        self.source.currentIndexChanged.connect(lambda _: win.update_source_hint())
        self.source.currentIndexChanged.connect(lambda _: self.position_screen_overlay())
        self.source_hint = label("", "hint", wrap=True)
        box_layout.addWidget(self.source_hint)
        box_layout.addWidget(hline())
        box_layout.addWidget(label("Diagnostics", "cardTitle"))
        self.analyse = button("Analyse a recording...", tip="Count the chests in a video file (does not change your counter)",
                              on_click=win.analyse_video)
        row = QHBoxLayout()
        row.addWidget(label("Diagnostic recording"))
        self.rec_mode = MaterialComboBox()
        for text, mode in RECORDING_CHOICES:
            self.rec_mode.addItem(text, mode)
        self.rec_mode.setToolTip("Keeps the video the counter analysed so a missed chest can be replayed and checked. "
                                 "Can be changed while counting; the counts are not affected.")
        row.addWidget(self.rec_mode, 1)
        box_layout.addLayout(row)
        self.mark = button("Mark moment   Ctrl+Alt+M", on_click=win.mark_moment)
        actions = connected_buttons(self.analyse, self.mark)
        actions.addStretch(1)
        box_layout.addLayout(actions)
        self.rec_status = label("", "hint", wrap=True)
        box_layout.addWidget(self.rec_status)
        # optional panels
        row = QHBoxLayout()
        box.hide()
        scroll = self.findChild(QScrollArea)
        scrollbar = scroll.verticalScrollBar()
        reveal = QVariantAnimation(box)
        reveal.setDuration(200)
        reveal.setEasingCurve(QEasingCurve.OutCubic)
        reveal.valueChanged.connect(lambda value: scrollbar.setValue(int(value)))

        def reveal_capture():
            if not box.isVisible():
                return
            start = scrollbar.value()
            scroll.ensureWidgetVisible(self.profile)
            end = scrollbar.value()
            if start != end and theme.animations_enabled():
                scrollbar.setValue(start)
                reveal.setStartValue(start)
                reveal.setEndValue(end)
                reveal.start()

        def show_capture(opened):
            reveal.stop()
            box.setVisible(opened)
            if opened and self.isVisible():
                QTimer.singleShot(0, reveal_capture)

        self.show_capture.toggled.connect(show_capture)
        self.show_log = button("Activity", "toggle", tip="What the counter sees and decides, live")
        self.show_log.setCheckable(True)
        self.show_preview = button("Live preview", "toggle", tip="The video the counter is analysing")
        self.show_preview.setCheckable(True)
        row.addLayout(connected_buttons(self.show_capture, self.show_log, self.show_preview))
        row.addStretch(1)
        self.detail = MaterialComboBox()
        self.detail.addItem("Normal detail", "normal")
        self.detail.addItem("Debug detail", "debug")
        self.detail.setToolTip("Debug adds every candidate, the full reason for each decision and status lines")
        self.clear = button("Clear", on_click=win.clear_log)
        self.open_logs = button("Logs folder", tip="Session logs (send these when reporting a problem)",
                                on_click=win.open_logs)
        row.addLayout(connected_buttons(self.detail, self.clear, self.open_logs))
        layout.addLayout(row)
        layout.addWidget(box)

        self.split = QSplitter(Qt.Horizontal)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_LIMIT)
        self.log.setPlaceholderText("Chests appear here as they are counted: green = certain, yellow = estimated only, "
                                    "red = looked like a chest but was not counted.")
        self.log.setMinimumHeight(220)
        self.preview = PreviewPanel("Live preview")
        self.split.addWidget(self.log)
        self.split.addWidget(self.preview)
        self.split.setCollapsible(1, False)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        layout.addWidget(self.split, 1)

        self.show_log.toggled.connect(self._panels)
        self.show_preview.toggled.connect(self._panels)
        self.detail.currentIndexChanged.connect(lambda _: win.render_log())

    def set_recent_matches(self, matches):
        self.saved_matches = tuple(matches)
        self.saved_match_count = len(matches)
        self.refresh_screen_overlay()
        lines = [f"{match.finished_at} · {match.mode}\n{match.total:,} chests"
                 for match in reversed(matches[-3:])]
        self.recent_matches.setText("\n\n".join(lines) or "No matches saved yet.")

    def set_leaders(self, ranking):
        self.leaders.setVisible(not ranking)
        if not ranking:
            self.leaders.setText("No shared rankings yet.")
        maximum = max(ranking[0].total, 1) if ranking else 1
        for index, (caption, bar) in enumerate(self.leader_rows):
            visible = index < len(ranking)
            caption.setVisible(visible)
            bar.setVisible(visible)
            if visible:
                player = ranking[index]
                text = f"{index + 1}. {player.name} · {fmt(player.total)}"
                caption.setText(caption.fontMetrics().elidedText(text, Qt.ElideMiddle, 192))
                caption.setToolTip(text)
                bar.setValue(round(1000 * player.total / maximum))

    def update_source_summary(self):
        primary = self.source.currentText() or "Scanning..."
        profile = self.profile.currentText()
        if (self.source.currentData() or {}).get("source") == "stream":
            url = QUrl(self.url.text())
            if url.host():
                primary = url.host() + url.path()
                if url.host().lower().removeprefix("www.") == "youtube.com" and url.path() == "/watch":
                    video = QUrlQuery(url).queryItemValue("v")
                    if video:
                        primary += f"?v={video}"
        for chip, text in zip(self.source_chips, (f"Profile · {profile or 'Default'}",
                                                  f"Source · {primary}")):
            chip.setText(chip.fontMetrics().elidedText(text, Qt.ElideRight, chip.maximumWidth() - 22))
            chip.setToolTip(text)

    def clear_recent(self, source=None):
        if source is None:
            self.recent.clear()
        else:
            for index in reversed(range(self.recent.count())):
                data = self.recent.item(index).data(Qt.UserRole)
                if data and data["source"] == source:
                    self.recent.takeItem(index)
        shown = self._shown_chest
        if shown is not None and (source is None or shown[0] == source):
            first = self.recent.item(0)
            remaining = first.data(Qt.UserRole) if first is not None else None
            self._shown_chest = (remaining["source"], remaining["event_id"]) if remaining else None
            self._display_chest(remaining.get("chest_type") if remaining else chest_types.NORMAL, restart=True)
        if self.recent.count():
            return
        item = QListWidgetItem("Open a chest in Fortnite to see it here.")
        item.setForeground(QColor(theme.MUTED))
        self.recent.addItem(item)

    def show_opening(self, message, source):
        if message.get("type") == "type_update":
            for index in range(self.recent.count()):
                item = self.recent.item(index)
                data = item.data(Qt.UserRole)
                if data and (data["source"], data["event_id"]) == (source, message.get("event_id")):
                    data["chest_type"] = message.get("chest_type")
                    self._describe_opening(item, data)
                    if self._shown_chest == (source, data["event_id"]):
                        self._display_chest(data["chest_type"])
                    break
            return
        verdict = message.get("verdict")
        if message.get("type") != "event" or verdict not in ("certain", "approx"):
            return
        if self.recent.item(0).data(Qt.UserRole) is None:
            self.recent.clear()
        item = QListWidgetItem()
        data = {**message, "source": source}
        self._describe_opening(item, data)
        index = 0
        emitted_at = data.get("emitted_at", 0)
        while index < self.recent.count() and self.recent.item(index).data(Qt.UserRole).get("emitted_at", 0) > emitted_at:
            index += 1
        self.recent.insertItem(index, item)
        if self.recent.count() > 20:
            self.recent.takeItem(20)
        self.recent.scrollToTop()
        if index == 0:
            self._shown_chest = (source, message.get("event_id"))
            self._display_chest(message.get("chest_type"), restart=True, animate=verdict == "certain")

    def _display_chest(self, kind, *, restart=False, animate=False):
        illustrations = [self.chest, self.overlay_preview.chest, self.win.overlay.preview.chest]
        if self.screen_overlay is not None:
            illustrations.append(self.screen_overlay.chest)
        for illustration in illustrations:
            if restart:
                illustration.reset_opening()
            illustration.set_kind(kind)
            if animate:
                illustration.celebrate()

    @staticmethod
    def _describe_opening(item, data):
        chest = chest_types.LABELS.get(data.get("chest_type"), chest_types.LABELS[chest_types.UNKNOWN])
        verdict = data["verdict"]
        item.setData(Qt.UserRole, data)
        item.setText(f"{chest}  ·  {verdict.title()}\n{data['clock']}  ·  Source {data['source']}")
        item.setForeground(QColor(theme.LOG_COLORS[verdict]))

    def update_token_odds(self):
        total = max(0, self.win.counts[1])
        boost = self.token_boost.currentData()
        chance = token_odds_text(total, boost)
        self.token_chance = chance
        self.token_odds.setText(chance)
        self.refresh_screen_overlay()

    def toggle_screen_overlay(self, enabled):
        self.win.overlay.share_screen_overlay.setEnabled(enabled)
        if not enabled:
            if self.screen_overlay is not None:
                self.screen_overlay.hide()
            return
        if self.screen_overlay is None:
            flags = (Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                     | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
            self.screen_overlay = ScreenOverlayCard(flags)
            self.screen_overlay.chest.set_kind(self.chest.kind)
            self.screen_overlay.setAttribute(Qt.WA_ShowWithoutActivating)
        self.reload_screen_overlay()
        self.screen_overlay.show()
        self.apply_screen_overlay_capture()

    def apply_screen_overlay_capture(self):
        if sys.platform == "win32" and self.screen_overlay is not None:
            hwnd = ctypes.c_void_p(int(self.screen_overlay.winId()))
            affinity = 0 if self.win.overlay.share_screen_overlay.isChecked() else 0x11  # WDA_NONE or WDA_EXCLUDEFROMCAPTURE
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity)

    def reload_screen_overlay(self):
        self.screen_overlay_display = load_cfg()[0]["display"]
        self.refresh_screen_overlay()

    def current_snapshot(self, cfg):
        try:
            profile = profiles.apply(cfg)
            identity = f"{profile['id']}@{profile['created_at'] or ''}"
            snapshot, _, _ = match_history.read_snapshot(identity, (Path(cfg["state"]["path"]),))
        except (OSError, ValueError, C.UserError):
            return ""
        return snapshot

    def match_summaries(self, snapshot=None, verified=None):
        cfg = load_cfg()[0]
        group = getattr(self.win, "group", None)
        sessions = {session.fingerprint: session for session in group._sessions} if group is not None else {}
        webhook = group._webhook if group is not None else None
        webhook_id = webhook.id if webhook else ""
        player = cfg["app"]["group_player"].strip()
        sharing = bool(cfg["app"]["group_share_matches"] and webhook)
        certain, total = self.win.counts
        boost = self.token_boost.currentData()
        fingerprint = self.win._match_fingerprint
        if snapshot is None:
            snapshot = self.current_snapshot(cfg)
        matches = getattr(self, "saved_matches", ())
        if not fingerprint:
            fingerprint = next((match.session_fingerprint for match in matches if match.snapshot == snapshot
                                and match.group_webhook_id == webhook_id), "")
        current = None
        if sharing and fingerprint:
            if verified is not None:
                current = match_stats.saved_team(verified, sessions, webhook_id)
            else:
                current = match_stats.team(sessions.get(fingerprint), player, current=(certain, total, boost))
        cumulative = match_stats.cumulative(matches, sessions, webhook_id, snapshot=snapshot, certain=certain,
                                           total=total, boost=boost, fingerprint=fingerprint, player=player, sharing=sharing, active=self.win._match_active)
        return current, cumulative

    def overlay_context(self):
        cfg = load_cfg()[0]
        path = Path(cfg["state"]["path"])
        try:
            latest = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            latest = {}
        certain, total = self.win.counts
        boost = self.token_boost.currentData()
        snapshot = self.current_snapshot(cfg)
        saved = next((match for match in getattr(self, "saved_matches", ()) if match.snapshot == snapshot), None)
        verified = (saved if saved and saved.replay and not self.win._match_active
                    and not self.win.workers_active() else None)
        if verified is None:
            personal = match_stats.personal(certain, total, boost)
            breakdown = types_text(self.win.primary_types)
        else:
            boost = verified.boost
            personal = match_stats.personal(verified.certain, verified.total, boost)
            breakdown = types_text({kind: {"approx": count} for kind, count in verified.chest_types.items()}) or "no chests yet"
        team, cumulative = self.match_summaries(snapshot, verified)
        def summary(value):
            if value is None:
                return None
            return {"certain": value.certain, "total": value.total, "log_no_drop": value.log_no_drop,
                    "boost": boost, "matches": value.matches, "players": len(value.players),
                    "names": " · ".join(value.players), "types": "", "member_totals": value.member_totals}
        own = summary(personal)
        own.update(players=1, names=cfg["app"]["group_player"], types=f"Chest types · {breakdown}")
        return {"version": 1, "profile_path": str(path.resolve()),
                "state": {"certain": certain, "approximate": total,
                          "session_started_at": latest.get("session_started_at")},
                "personal": own, "team": summary(team), "cumulative": summary(cumulative),
                "current_saved": saved is not None,
                "replay_verified": verified is not None,
                "active": self.win._match_active}

    def screen_overlay_sections(self, display):
        return overlay_layout.render_sections(display, self.overlay_context())

    def screen_overlay_text(self, display):
        return overlay_layout.format_text(display, self.screen_overlay_sections(display))

    def write_overlay_context(self, display, context):
        cfg = load_cfg()[0]
        folder = paths.APP_DIR / cfg["state"]["obs_dir"]
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
        key = (str(folder.resolve()), payload)
        if key == getattr(self, "_last_overlay_context", None):
            return
        try:
            C.atomic_write(folder / "overlay_context.json", payload + "\n")
            path = Path(cfg["state"]["path"])
            try:
                state = json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                state = context["state"]
            if display.get("use_custom_sections"):
                C.write_obs_files(state, folder, display, state_path=path)
        except (OSError, ValueError, C.UserError):
            return
        self._last_overlay_context = key

    def refresh_screen_overlay(self):
        page = getattr(self.win, "overlay", None)
        if page is not None and page.isVisible():
            page.refresh_preview()
        if self.screen_overlay_display is None:
            return
        context = self.overlay_context()
        replay_verified = context["replay_verified"]
        if replay_verified or getattr(self, "_showing_verified_counts", False):
            personal = context["personal"]
            self.certain.set_value(personal["certain"])
            self.approx.set_value(personal["total"] - personal["certain"])
            self.total.setText(fmt(personal["total"]))
            self.token_chance = match_stats.token_chance(personal["log_no_drop"])
            self.token_odds.setText(self.token_chance)
            self.types.setText(personal["types"].removeprefix("Chest types · "))
        self._showing_verified_counts = replay_verified
        sections = overlay_layout.render_sections(self.screen_overlay_display, context)
        styled = overlay_layout.render_sections(self.screen_overlay_display, context, styled=True)
        text = overlay_layout.format_text(self.screen_overlay_display, sections)
        self.write_overlay_context(self.screen_overlay_display, context)
        titles = tuple(self.screen_overlay_display[f"screen_{scope}_title"] for scope in ("personal", "team", "cumulative"))
        self.overlay_preview.set_text(text, sections=sections, titles=titles, styled_sections=styled)
        if self.screen_overlay is None or not page.show_screen_overlay.isChecked():
            return
        if not text:
            self.screen_overlay.hide()
            return
        self.screen_overlay.setWindowOpacity(self.screen_overlay_display["screen_opacity"] / 100)
        if not self.screen_overlay.isVisible():
            self.screen_overlay.show()
        if self.screen_overlay.set_text(text, sections=sections, titles=titles, styled_sections=styled):
            self.screen_overlay.adjustSize()
            self.position_screen_overlay()

    def position_screen_overlay(self):
        if self.screen_overlay is None:
            return
        screen = QGuiApplication.primaryScreen()
        monitor = (self.source.currentData() or {}).get("monitor")
        positions = getattr(self.win, "_monitor_positions", ())
        if monitor and monitor <= len(positions):
            x, y = positions[monitor - 1][:2]
            # Qt preserves Windows screen origins but can scale their dimensions.
            screen = next((item for item in QGuiApplication.screens() if item.geometry().topLeft() == QPoint(x, y)),
                          screen)
        if screen is not None:
            area = screen.availableGeometry()
            self.screen_overlay.move(area.left() + 16, area.top() + 16)

    def _panels(self):
        self.log.setVisible(self.show_log.isChecked())
        self.preview.setVisible(self.show_preview.isChecked())
        for widget in (self.detail, self.clear, self.open_logs):
            widget.setVisible(self.show_log.isChecked())
        self.split.setVisible(self.show_log.isChecked() or self.show_preview.isChecked())
        self.win.preview_visibility_changed()

    def set_pill(self, text, color):
        self.pill.setText(f"●  {text}")
        self.pill.setStyleSheet(theme.pill_style(color))

    def show_banner(self, text):
        self.banner_text.setText(text)
        self.banner.show()

    def show_notice(self, text, color):
        self.notice_text.setText(text)
        self.notice_text.setStyleSheet(f"color: {color};")
        self.notice.show()


class OverlayPage(QWidget):
    """Configure Counter.bat, as a page: what OBS shows (always the same file) and its wording."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = page_layout(self, "Overlay", "Choose what appears on your screen and in the OBS text output.")
        box, box_layout = card("On-screen overlay")
        self.show_screen_overlay = QCheckBox("Show overlay on screen")
        self.show_screen_overlay.toggled.connect(win.dashboard.toggle_screen_overlay)
        self.show_screen_overlay.toggled.connect(lambda enabled: self._save_screen_option("screen_overlay_enabled", enabled))
        box_layout.addWidget(self.show_screen_overlay)
        self.share_screen_overlay = QCheckBox("Visible when sharing your screen")
        self.share_screen_overlay.setToolTip("Include the overlay when sharing this screen on Discord.")
        self.share_screen_overlay.setEnabled(False)
        self.share_screen_overlay.toggled.connect(win.dashboard.apply_screen_overlay_capture)
        self.share_screen_overlay.toggled.connect(lambda enabled: self._save_screen_option("screen_overlay_shared", enabled))
        box_layout.addWidget(self.share_screen_overlay)
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(label("Transparency"))
        self.transparency = QSlider(Qt.Horizontal)
        self.transparency.setRange(0, 80)
        self.transparency.setSingleStep(5)
        self.transparency.setAccessibleName("Overlay transparency")
        self.transparency_value = label("0%")
        opacity_row.addWidget(self.transparency, 1)
        opacity_row.addWidget(self.transparency_value)
        box_layout.addLayout(opacity_row)
        self.transparency.valueChanged.connect(self._changed)
        self.show_token = QCheckBox("Show Pirate Token odds")
        self.show_types = QCheckBox("Show chest types")
        self.show_matches = QCheckBox("Show match count")
        self.thousands = QCheckBox("Thousands separator")
        for toggle in (self.show_token, self.show_types, self.show_matches, self.thousands):
            box_layout.addWidget(toggle)
            toggle.toggled.connect(self._changed)
        layout.addWidget(box)

        box, box_layout = card("Custom sections for screen and OBS", "Edit each section independently. Use {total}, {certain}, {approx}, "
                               "{chance}, {matches}, {players}, {names}, {member_totals} and {types}. "
                               "Wrap text in ** to make it bold. Leave a blank line to separate stats. "
                               "Team appears only for a verified shared match.")
        self.section_enabled = {}
        self.section_titles = {}
        self.section_templates = {}
        for scope, caption in (("personal", "Personal"), ("team", "Team"), ("cumulative", "All matches")):
            header = QHBoxLayout()
            enabled = QCheckBox(caption)
            title = QLineEdit()
            title.setPlaceholderText("Section title")
            header.addWidget(enabled)
            header.addWidget(title, 1)
            box_layout.addLayout(header)
            template = QPlainTextEdit()
            template.setMinimumHeight(76)
            template.setMaximumHeight(100)
            template.setPlaceholderText("One line per statistic")
            box_layout.addWidget(template)
            self.section_enabled[scope] = enabled
            self.section_titles[scope] = title
            self.section_templates[scope] = template
            enabled.toggled.connect(self._changed)
            title.textChanged.connect(self._changed)
            template.textChanged.connect(self._changed)
        layout.addWidget(box)

        reset_row = QHBoxLayout()
        reset_row.addWidget(button("Restore section defaults", on_click=self._defaults))
        reset_row.addStretch(1)
        box_layout.addLayout(reset_row)
        box, box_layout = card("Preview", "The on-screen overlay and OBS use these same sections and values.")
        self.preview = ScreenOverlayCard()
        self.preview_opacity = QGraphicsOpacityEffect(self.preview)
        self.preview.setGraphicsEffect(self.preview_opacity)
        box_layout.addWidget(self.preview)
        layout.addWidget(box)

        box, box_layout = card("OBS setup", 'In OBS: add a "Text (GDI+)" source, tick "Read from file" and choose this file. '
                                            "It never changes, whatever you pick above.")
        row = QHBoxLayout()
        self.obs_path = QLineEdit()
        self.obs_path.setReadOnly(True)
        row.addWidget(self.obs_path, 1)
        row.addLayout(connected_buttons(
            button("Copy path", on_click=self._copy_path),
            button("Open folder", on_click=lambda: open_path(Path(self.obs_path.text()).parent)),
        ))
        box_layout.addLayout(row)
        layout.addWidget(box)

        row = QHBoxLayout()
        self.saved = label("", "hint")
        row.addWidget(self.saved, 1)
        undo = button("Undo changes", "overlayUndo", on_click=self.load)
        row.addLayout(connected_buttons(undo, button("Apply", "primary", on_click=self.apply)))
        layout.addLayout(row)

        box, box_layout = card("OBS WebSocket (optional)",
                               "Instead of OBS reading the text file (it looks about once a second), the app can put the "
                               "same text straight into a Text source in OBS: every change shows at once and no file path "
                               "is needed. The text file is still written either way. Changes here apply immediately.")
        self.ws_enabled = QCheckBox("Update a Text source in OBS directly (OBS WebSocket)")
        self.ws_enabled.toggled.connect(self._ws_toggled)
        box_layout.addWidget(self.ws_enabled)
        self.ws_box = QWidget()
        grid = QGridLayout(self.ws_box)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.ws_host = QLineEdit()
        self.ws_host.setToolTip("127.0.0.1 = OBS runs on this PC. For OBS on another PC: that PC's IPv4 address.")
        self.ws_port = QSpinBox()
        self.ws_port.setAlignment(Qt.AlignCenter)
        self.ws_port.setRange(1, 65535)
        self.ws_password = QLineEdit()
        self.ws_password.setEchoMode(QLineEdit.Password)
        self.ws_source = MaterialComboBox()
        self.ws_source.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ws_source.activated.connect(self._ws_source_picked)
        grid.addWidget(label("OBS on"), 0, 0)
        grid.addWidget(self.ws_host, 0, 1)
        grid.addWidget(label("Port"), 0, 2)
        grid.addWidget(self.ws_port, 0, 3)
        grid.addWidget(label("Password"), 1, 0)
        grid.addWidget(self.ws_password, 1, 1, 1, 3)
        self.ws_forget = button("Forget password", "link", on_click=self._ws_forget)
        grid.addWidget(self.ws_forget, 1, 4)
        grid.addWidget(label("Text source"), 2, 0)
        grid.addWidget(self.ws_source, 2, 1, 1, 3)
        self.ws_refresh = button("Refresh", tip="List OBS's Text sources again", on_click=self._ws_refresh)
        grid.addWidget(self.ws_refresh, 2, 4)
        grid.setColumnStretch(1, 1)
        box_layout.addWidget(self.ws_box)
        self.ws_status = label("", "hint", wrap=True)
        box_layout.addWidget(self.ws_status)
        row = QHBoxLayout()
        row.addWidget(label('In OBS: Tools > WebSocket Server Settings > tick "Enable WebSocket server". '
                            '"Show Connect Info" shows the password. The password is kept in Windows Credential '
                            "Manager, never in a file.", "hint", wrap=True), 1)
        self.ws_direct = button("Use direct updates", tip='Untick "Read from file" on this source, so the app updates it',
                                on_click=lambda: self.win.obs_link.use_direct_updates())
        self.ws_direct.hide()
        row.addWidget(self.ws_direct, 0, Qt.AlignTop)
        self.ws_connect = button("Connect", "primary", on_click=self._ws_connect)
        row.addWidget(self.ws_connect, 0, Qt.AlignTop)
        box_layout.addLayout(row)
        layout.addWidget(box)
        layout.addStretch(1)
        self._loading = False

    def load(self):
        self._loading = True
        cfg, _ = load_cfg()
        self.display = cfg["display"]
        self.share_screen_overlay.setChecked(cfg["app"]["screen_overlay_shared"])
        self.show_screen_overlay.setChecked(cfg["app"]["screen_overlay_enabled"])
        self.obs_path.setText(str((paths.APP_DIR / cfg["state"]["obs_dir"] / "chest_counter.txt").resolve()))
        self.thousands.setChecked(self.display["use_thousands_separator"])
        self.show_token.setChecked(self.display["screen_show_token"])
        self.show_types.setChecked(self.display["screen_show_types"])
        self.show_matches.setChecked(self.display["screen_show_matches"])
        self.transparency.setValue(100 - self.display["screen_opacity"])
        for scope in self.section_enabled:
            self.section_enabled[scope].setChecked(self.display[f"screen_{scope}_enabled"])
            self.section_titles[scope].setText(self.display[f"screen_{scope}_title"])
            self.section_templates[scope].setPlainText(self.display[f"screen_{scope}_template"])
        ws = cfg["obs_websocket"]
        self.ws_enabled.blockSignals(True)
        self.ws_enabled.setChecked(ws["enabled"])
        self.ws_enabled.blockSignals(False)
        self.ws_box.setEnabled(ws["enabled"])
        self.ws_connect.setEnabled(ws["enabled"])
        self.ws_host.setText(ws["host"])
        self.ws_port.setValue(ws["port"])
        self.ws_password.clear()
        self._ws_password_hint()
        self._loading = False
        self.saved.setText("")
        self._changed()
        self.show_obs_status(self.win.obs_link.status()[1])

    def _save_screen_option(self, key, enabled):
        if not self._loading:
            self.win.save_app_option(key, enabled)

    # ---- OBS WebSocket card
    def _ws_password_hint(self):
        saved = obs_credentials.has_password()
        self.ws_password.setPlaceholderText("Saved - type a new one to replace it" if saved else
                                            "From OBS: Show Connect Info (leave empty if OBS asks for none)")
        self.ws_forget.setVisible(saved)

    def _ws_save(self, values):
        try:
            C.update_config_file(config_file(), "obs_websocket", values)
            return True
        except (C.UserError, OSError) as error:
            show_error(self, str(error))
            return False

    def _ws_toggled(self, on):
        if self._loading:
            return
        if self._ws_save({"enabled": on}):
            self.ws_box.setEnabled(on)
            self.ws_connect.setEnabled(on)
            self.win.apply_obs_settings()

    def _ws_connect(self):
        host = self.ws_host.text().strip()
        if not host or any(c.isspace() for c in host):
            show_error(self, 'Enter the PC that runs OBS: 127.0.0.1 for this PC, or that PC\'s IPv4 address (for example '
                             '192.168.1.50).')
            return
        typed = self.ws_password.text()
        if typed:
            try:
                obs_credentials.save_password(typed)
            except (OSError, ValueError) as error:
                show_error(self, f"The password could not be saved in Windows Credential Manager ({error}).")
                return
            self.ws_password.clear()
            self._ws_password_hint()
        if self._ws_save({"host": host, "port": self.ws_port.value()}):
            self.win.apply_obs_settings(reconnect=True)

    def _ws_forget(self):
        try:
            obs_credentials.forget_password()
        except OSError as error:
            show_error(self, f"The password could not be removed ({error}).")
            return
        self._ws_password_hint()
        self.win.apply_obs_settings(reconnect=True)

    def _ws_refresh(self):
        state = self.win.obs_link.status()[1]["state"]
        if state in ("live", "pick", "missing", "reads_file"):
            self.win.obs_link.refresh_sources()
        else:
            self.win.apply_obs_settings(reconnect=True)

    def _ws_source_picked(self, index):
        data = self.ws_source.itemData(index)
        if not data:
            return
        if self._ws_save({"source": data["name"], "source_uuid": data["uuid"]}):
            self.win.apply_obs_settings()

    def show_obs_status(self, status):
        name, color = OBS_STATES.get(status["state"], (status["state"], theme.MUTED))
        details = status.get("message", "")
        if status.get("obs") and status["state"] not in ("off", "connecting"):
            details += f"   ({status['obs']})"
        self.ws_status.setText(f"●  {name}" + (f" - {details}" if details else ""))
        self.ws_status.setStyleSheet(f"color: {color};")
        self.ws_direct.setVisible(status["state"] == "reads_file")
        self.ws_source.blockSignals(True)
        self.ws_source.clear()
        found = status.get("sources") or []
        current = status.get("source") or load_cfg()[0]["obs_websocket"]["source"]
        if not found:
            self.ws_source.addItem(current or "(connect to list OBS's Text sources)", None)
        else:
            if not any(s["name"] == current for s in found):
                self.ws_source.addItem("Pick a Text source ..." if not current else f"{current}   (not in OBS)", None)
            for s in found:
                kind = "FreeType 2" if s["kind"] == "text_ft2_source" else "GDI+"
                self.ws_source.addItem(f"{s['name']}   (Text {kind})", s)
                if s["name"] == current:
                    self.ws_source.setCurrentIndex(self.ws_source.count() - 1)
        self.ws_source.blockSignals(False)

    def changes(self):
        values = {"use_custom_sections": True, "use_thousands_separator": self.thousands.isChecked(),
                  "screen_show_token": self.show_token.isChecked(),
                  "screen_show_types": self.show_types.isChecked(),
                  "screen_show_matches": self.show_matches.isChecked(),
                  "screen_opacity": 100 - self.transparency.value()}
        for scope in self.section_enabled:
            values[f"screen_{scope}_enabled"] = self.section_enabled[scope].isChecked()
            values[f"screen_{scope}_title"] = self.section_titles[scope].text().strip()
            values[f"screen_{scope}_template"] = self.section_templates[scope].toPlainText().strip()
        return values

    def _changed(self):
        if self._loading:
            return
        self.transparency_value.setText(f"{self.transparency.value()}%")
        floating = self.win.dashboard.screen_overlay
        if floating is not None:
            floating.setWindowOpacity((100 - self.transparency.value()) / 100)
        self.refresh_preview()
        pending = any(self.display.get(k) != v for k, v in self.changes().items())
        self.saved.setText("Not applied yet." if pending else "")

    def refresh_preview(self):
        display = {**self.display, **self.changes()}
        context = self.win.dashboard.overlay_context()
        sections = overlay_layout.render_sections(display, context)
        styled = overlay_layout.render_sections(display, context, styled=True)
        self.preview_opacity.setOpacity(display["screen_opacity"] / 100)
        self.preview.set_text(overlay_layout.format_text(display, sections), sections=sections, styled_sections=styled,
                              titles=tuple(display[f"screen_{scope}_title"] for scope in overlay_layout.SCOPES))
    def _defaults(self):
        d = settings.DEFAULTS["display"]
        for scope in self.section_enabled:
            self.section_titles[scope].setText(d[f"screen_{scope}_title"])
            self.section_templates[scope].setPlainText(d[f"screen_{scope}_template"])

    def _copy_path(self):
        QApplication.clipboard().setText(self.obs_path.text())
        self.saved.setText("Path copied - paste it into the OBS Text source.")

    def apply(self):
        changes = self.changes()
        try:
            C.update_config_file(config_file(), "display", changes)
        except (C.UserError, OSError) as error:
            show_error(self, str(error))
            return
        self.win.dashboard.reload_screen_overlay()
        self.win.write_obs_files()
        self.load()
        self.saved.setText("Applied. " + ("The counter updates the OBS text within a second."
                                          if self.win.worker.running() else "The OBS text file was updated."))
        self.win.dashboard.reload_screen_overlay()


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = page_layout(self, "Settings", "Changes are saved in config.toml next to the app and are used the next time "
                                               "you press Start counting.")
        box, box_layout = card("Fortnite language", "The language your Fortnite is set to - the counter reads the word in the "
                                                    "chest pop-up (CHEST / BAÚ / COFFRE). \"Both\" checks English and Portuguese: "
                                                    "only useful for mixed footage. One language avoids a small risk of false "
                                                    "alarms and a little work, but does not find more chests (no difference on "
                                                    "our English test videos). Automatic language detection will come once the "
                                                    "app recognises matches.")
        self.language = MaterialComboBox()
        for text, langs in settings.LANGUAGE_CHOICES:
            self.language.addItem(text, langs)
        box_layout.addWidget(self.language)
        layout.addWidget(box)

        box, box_layout = card("Pictures analysed per second",
                               "How many pictures of your gameplay the counter checks every second. A chest pop-up is "
                               "on screen only briefly, so fewer pictures save work on this PC but can miss chests. "
                               "OBS and your stream are not affected either way.")
        self.fps = MaterialComboBox()
        self.fps.currentIndexChanged.connect(self._fps_changed)
        box_layout.addWidget(self.fps)
        self.fps_hint = label("", "hint", wrap=True)
        box_layout.addWidget(self.fps_hint)
        layout.addWidget(box)

        box, box_layout = card("Session logs", "Logs are small text files in the logs folder. They are what you send when "
                                               "reporting a problem. Save changes to apply logging preferences to the active session.")
        self.logging = QCheckBox("Keep a log of every counting session")
        self.snapshots = QCheckBox("Also save a small picture of every chest pop-up (to report missed or wrong chests)")
        box_layout.addWidget(self.logging)
        box_layout.addWidget(self.snapshots)
        self.logging.toggled.connect(self.snapshots.setEnabled)
        layout.addWidget(box)

        box, box_layout = card("Diagnostic recording (advanced)",
                               "Keeps exactly what the counter analyses, so a missed or wrong chest can be replayed and "
                               "checked later - and every future version can be tested on it. Turn it on and off on the "
                               "Dashboard, also while counting; press Ctrl+Alt+M while you play when the counter misses a chest.")
        self.lossless = QCheckBox("Lossless: the replay sees exactly what the counter saw (about twice the disk space)")
        box_layout.addWidget(self.lossless)
        row = QHBoxLayout()
        row.addLayout(connected_buttons(
            button("Open captures folder", on_click=self._open_captures),
            button("Review captures...", tip="Replay captures and compare with live counts and your labels",
                   on_click=lambda: ReviewDialog(self).exec()),
        ))
        row.addStretch(1)
        box_layout.addLayout(row)
        layout.addWidget(box)

        box, box_layout = card("Window")
        self.remember_window = QCheckBox("Remember window size and position (the app reopens the way you closed it)")
        box_layout.addWidget(self.remember_window)
        self.close_to_tray = QCheckBox("Keep counting in the system tray when the window closes")
        self.start_in_tray = QCheckBox("Start in the system tray")
        box_layout.addWidget(self.close_to_tray)
        box_layout.addWidget(self.start_in_tray)
        layout.addWidget(box)

        box, box_layout = card("Match automation", "Use the local Fortnite log to start counting at Battle Royale "
                               "warmup and save the match when you return to the lobby.")
        self.auto_match = QCheckBox("Automatically reset, start and save local Battle Royale matches")
        box_layout.addWidget(self.auto_match)
        self.auto_verify_replay = QCheckBox("Verify saved matches using Fortnite replays")
        self.auto_verify_replay.setToolTip("Reads Fortnite's local replay after the match. Enable replay recording in Fortnite.")
        box_layout.addWidget(self.auto_verify_replay)
        self.delete_verified_replay = QCheckBox("Delete local Fortnite replays after successful verification")
        self.delete_verified_replay.setToolTip("Applies to this PC for both host and guest. Saved stats are kept.")
        box_layout.addWidget(self.delete_verified_replay)
        self.auto_verify_replay.toggled.connect(self.delete_verified_replay.setEnabled)
        layout.addWidget(box)

        box, box_layout = card("Updates", "Nothing is downloaded or installed unless you choose to. Your counts, profiles and settings are kept.")
        self.update_auto = QCheckBox("Check for updates automatically when the app opens")
        box_layout.addWidget(self.update_auto)
        row = QHBoxLayout()
        row.addWidget(label("Download speed"))
        self.update_bandwidth = MaterialComboBox()
        for text, value in (("Adaptive   (recommended)", "adaptive"), ("Unlimited", "unlimited"), ("Custom limit", "custom")):
            self.update_bandwidth.addItem(text, value)
        self.update_bandwidth.currentIndexChanged.connect(self._bandwidth_changed)
        row.addWidget(self.update_bandwidth, 1)
        self.update_limit = QSpinBox()
        self.update_limit.setAlignment(Qt.AlignCenter)
        row.addWidget(self.update_limit)
        self.update_unit = MaterialComboBox()
        self.update_unit.addItem("KB/s", 1)
        self.update_unit.addItem("MB/s", 1000)
        self.update_unit.currentIndexChanged.connect(self._limit_unit_changed)
        row.addWidget(self.update_unit)
        box_layout.addLayout(row)
        self.update_hint = label("", "hint", wrap=True)
        box_layout.addWidget(self.update_hint)
        row = QHBoxLayout()
        self.check_button = button("Check for updates", on_click=win.check_for_updates)
        row.addWidget(self.check_button)
        self.update_status = label("", "hint", wrap=True)
        row.addWidget(self.update_status, 1)
        box_layout.addLayout(row)
        layout.addWidget(box)

        box, box_layout = card("Advanced", "Everything else (for example which evidence counts) is in config.toml. "
                                           "Edit it only if you know why; a typo shows a warning and uses the default.")
        row = QHBoxLayout()
        row.addLayout(connected_buttons(
            button("Open config.toml", on_click=self._open_config),
            button("Open program folder", on_click=lambda: open_path(paths.APP_DIR)),
        ))
        row.addStretch(1)
        box_layout.addLayout(row)
        layout.addWidget(box)

        row = QHBoxLayout()
        self.saved = label("", "hint")
        row.addWidget(self.saved, 1)
        row.addLayout(connected_buttons(
            button("Reset all", on_click=self.reset_all),
            button("Save", "primary", on_click=self.save),
        ))
        layout.addLayout(row)
        layout.addStretch(1)

    def load(self):
        cfg, _ = load_cfg()
        self._load_values(cfg)
        self.auto_match.setChecked(self.win._match_tail is not None)

    def _load_values(self, cfg):
        self.language.setCurrentIndex(settings.language_choice(cfg["detector"]["languages"]))
        fps = cfg["detector"]["process_fps"]
        self.fps.blockSignals(True)
        self.fps.clear()
        for value, name, _ in settings.FPS_PRESETS:
            self.fps.addItem(f"{value} per second   ({name})", value)
        if settings.fps_choice(fps) is None:
            self.fps.addItem(f"{fps} per second   (custom, from config.toml)", fps)
            self.fps.setCurrentIndex(self.fps.count() - 1)
        else:
            self.fps.setCurrentIndex(settings.fps_choice(fps))
        self.fps.blockSignals(False)
        self._fps_changed()
        self.logging.setChecked(cfg["logging"]["enabled"])
        self.snapshots.setChecked(cfg["logging"]["save_snapshots"])
        self.snapshots.setEnabled(cfg["logging"]["enabled"])
        self.lossless.setChecked(cfg["capture"]["quality"] == "lossless")
        self.remember_window.setChecked(cfg["app"]["remember_window"])
        self.close_to_tray.setChecked(cfg["app"]["close_to_tray"])
        self.start_in_tray.setChecked(cfg["app"]["start_in_tray"])
        self.auto_match.setChecked(cfg["app"]["auto_match_lifecycle"])
        self.auto_verify_replay.setChecked(cfg["app"]["auto_verify_replay"])
        self.delete_verified_replay.setChecked(cfg["app"]["delete_verified_replay"])
        self.delete_verified_replay.setEnabled(self.auto_verify_replay.isChecked())
        update = cfg["update"]
        self.update_auto.setChecked(update["check_automatically"])
        self.update_bandwidth.setCurrentIndex(max(self.update_bandwidth.findData(update["download_bandwidth"]), 0))
        limit = update["custom_limit_kb_per_second"]
        in_mb = limit >= 1000 and limit % 1000 == 0
        self.update_unit.setCurrentIndex(1 if in_mb else 0)
        self._limit_unit_changed()
        self.update_limit.setValue(limit // 1000 if in_mb else limit)
        self._bandwidth_changed()
        self.saved.setText("")

    def _bandwidth_changed(self, _index=None):
        mode = self.update_bandwidth.currentData()
        self.update_limit.setVisible(mode == "custom")
        self.update_unit.setVisible(mode == "custom")
        self.update_hint.setText({"adaptive": "About 1 MB/s while the app is counting, and no limit otherwise. It follows whether you are counting; "
                                              "it does not measure your connection.",
                                  "unlimited": "Never limited, even while counting.",
                                  "custom": "Always limited to this speed."}.get(mode, ""))

    def _limit_unit_changed(self, _index=None):
        if self.update_unit.currentData() == 1000:
            self.update_limit.setRange(1, settings.MAX_CUSTOM_LIMIT_KB // 1000)
        else:
            self.update_limit.setRange(settings.MIN_CUSTOM_LIMIT_KB, settings.MAX_CUSTOM_LIMIT_KB)

    def _fps_changed(self, _index=None):
        index = settings.fps_choice(self.fps.currentData())
        self.fps_hint.setText(settings.FPS_PRESETS[index][2] if index is not None else
                              "A value set in config.toml. 15 per second is what the counter was tested with.")

    def reset_all(self):
        if not ask(self, "Reset all settings?", "Restore the default values for this Settings page? "
                   "Counts, match history, profiles, overlay and Discord setup are kept.", "Reset settings"):
            return
        self._load_values(settings.DEFAULTS)
        self.save(message="Defaults restored.")

    def save(self, *, message="Saved."):
        langs = self.language.currentData()
        try:
            C.update_config_file(config_file(), "detector", {"languages": langs, "process_fps": self.fps.currentData()})
            C.update_config_file(config_file(), "logging", {"enabled": self.logging.isChecked(),
                                                            "save_snapshots": self.snapshots.isChecked()})
            C.update_config_file(config_file(), "capture", {"quality": "lossless" if self.lossless.isChecked() else "standard"})
            C.update_config_file(config_file(), "update", {"check_automatically": self.update_auto.isChecked(),
                                                           "download_bandwidth": self.update_bandwidth.currentData(),
                                                           "custom_limit_kb_per_second": self.update_limit.value() * self.update_unit.currentData()})
            if not self.remember_window.isChecked():
                window_file().unlink(missing_ok=True)   # off = back to the default layout, also if switched on again later
            C.update_config_file(config_file(), "app", {"remember_window": self.remember_window.isChecked(),
                                                        "auto_match_lifecycle": self.auto_match.isChecked(),
                                                        "auto_verify_replay": self.auto_verify_replay.isChecked(),
                                                        "delete_verified_replay": self.delete_verified_replay.isChecked(),
                                                        "close_to_tray": self.close_to_tray.isChecked(),
                                                        "start_in_tray": self.start_in_tray.isChecked()})
        except (C.UserError, OSError) as error:
            show_error(self, str(error))
            return
        self.saved.setText(message + (" Stop and start counting to use the new settings."
                                       if self.win.worker.running() else ""))
        self.win.set_match_automation(self.auto_match.isChecked())
        self.win.configure_tray(self.close_to_tray.isChecked() or self.start_in_tray.isChecked())

    def _open_captures(self):
        cfg, _ = load_cfg()
        folder = paths.APP_DIR / cfg["capture"]["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def _open_config(self):
        path = config_file()
        if not path.exists():
            try:
                C.update_config_file(path, "live", {})          # create it from config.example.toml, comments included
            except (C.UserError, OSError) as error:
                show_error(self, str(error))
                return
        open_path(path)


class StatsTrend(QWidget):
    """Chronological chest totals with bounded, consecutive time buckets."""

    match_activated = Signal(object)

    def __init__(self):
        super().__init__()
        self.buckets = []
        self.by_match = False
        self.visible_matches = []
        self.averages = []
        self.selected = 0
        self.setMinimumHeight(168)
        self.setMouseTracking(True)
        self.setAccessibleName("Chests over time")

    def set_matches(self, matches, days, today, *, by_match=False):
        self.by_match = by_match
        self.visible_matches = []
        self.averages = []
        self.selected = 0
        totals = Counter()
        dates = Counter()
        for match in matches:
            try:
                day = date.fromisoformat(match.finished_at[:10])
            except ValueError:
                continue
            totals[day] += match.total
            dates[day] += 1
        self.buckets = []
        if by_match:
            ordered = sorted(matches, key=lambda match: match.finished_at)
            first = max(0, len(ordered) - 30)
            self.visible_matches = ordered[first:]
            self.buckets = [(match.finished_at, match.finished_at, match.total, 1) for match in self.visible_matches]
            self.averages = [sum(match.total for match in ordered[i - 4:i + 1]) / 5 if i >= 4 else None
                             for i in range(first, len(ordered))]
        elif dates:
            first = today - timedelta(days=days - 1) if days else min(dates)
            last = today if days else max(dates)
            span = (last - first).days + 1
            step = max(1, math.ceil(span / 30))
            self.buckets = [(first + timedelta(days=offset), first + timedelta(days=min(offset + step, span) - 1), 0, 0)
                            for offset in range(0, span, step)]
            for day, total in totals.items():
                index = (day - first).days // step
                if 0 <= index < len(self.buckets):
                    a, b, count, games = self.buckets[index]
                    self.buckets[index] = (a, b, count + total, games + dates[day])
        self.setFocusPolicy(Qt.StrongFocus if self.visible_matches else Qt.NoFocus)
        self.setAccessibleDescription(
            ("Use arrow keys to select a match and Enter to open its details. " if self.visible_matches else "")
            + " ".join(self.bucket_text(bucket) for bucket in self.buckets)
        )
        self.setToolTip("")
        self.update()

    @staticmethod
    def bucket_text(bucket):
        first, last, count, games = bucket
        span = str(first) if first == last else f"{first} – {last}"
        if not games:
            return f"{span} · No saved matches"
        return f"{span} · {count:,} chests · {games:,} matches"

    def plot(self):
        return QRectF(42, 12, max(1, self.width() - 52), self.height() - 58)

    def bucket_at(self, position):
        plot = self.plot()
        if self.buckets and plot.contains(position):
            return min(len(self.buckets) - 1, int((position.x() - plot.left()) * len(self.buckets) / plot.width()))
        return None

    def mouseMoveEvent(self, event):
        index = self.bucket_at(event.position())
        detail = self.bucket_text(self.buckets[index]) if index is not None else ""
        if index is not None and self.visible_matches:
            average = self.averages[index]
            if average is not None:
                detail += f" · 5-match average {average:,.1f}"
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        self.setToolTip(detail)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        index = self.bucket_at(event.position())
        if event.button() == Qt.LeftButton and index is not None and self.visible_matches:
            self.selected = index
            self.match_activated.emit(self.visible_matches[index])
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if self.visible_matches and key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if key in (Qt.Key_Left, Qt.Key_Right):
                self.selected = min(len(self.visible_matches) - 1, max(0, self.selected + (1 if key == Qt.Key_Right else -1)))
                self.update()
            else:
                self.match_activated.emit(self.visible_matches[self.selected])
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.setFont(self.font())
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QColor(theme.MUTED))
            if not self.buckets:
                painter.drawText(self.rect(), Qt.AlignCenter, "No dated matches in this period.")
                return
            plot = self.plot()
            peak = max(1, *(bucket[2] for bucket in self.buckets),
                       *(value for value in self.averages if value is not None))
            for value, y in ((peak, plot.top()), (0, plot.bottom())):
                painter.setPen(QColor(theme.BORDER))
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                painter.setPen(QColor(theme.MUTED))
                painter.drawText(QRectF(0, y - 9, 37, 18), Qt.AlignRight | Qt.AlignVCenter, f"{value:,}")
            slot = plot.width() / len(self.buckets)
            bar = min(36, slot * 0.68)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            for i, (_, _, count, _) in enumerate(self.buckets):
                if count:
                    height = max(1, plot.height() * count / peak)
                    painter.drawRoundedRect(QRectF(plot.left() + i * slot + (slot - bar) / 2,
                                                   plot.bottom() - height, bar, height), 3, 3)
            if self.visible_matches and self.hasFocus():
                painter.setPen(QColor(theme.TEXT))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(plot.left() + self.selected * slot, plot.top(), slot, plot.height()))
            if self.averages:
                line = QPainterPath()
                started = False
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(theme.CERTAIN))
                for i, average in enumerate(self.averages):
                    if average is None:
                        continue
                    point = QPointF(plot.left() + (i + 0.5) * slot, plot.bottom() - plot.height() * average / peak)
                    if started:
                        line.lineTo(point)
                    else:
                        line.moveTo(point)
                        started = True
                    painter.drawEllipse(point, 3, 3)
                pen = QPen(QColor(theme.CERTAIN), 2)
                painter.strokePath(line, pen)
            painter.setPen(QColor(theme.MUTED))
            dates = QRectF(plot.left(), plot.bottom() + 8, plot.width(), 19)
            if self.by_match:
                painter.drawText(dates, Qt.AlignLeft, "Oldest shown")
                if len(self.buckets) > 1:
                    painter.drawText(dates, Qt.AlignRight, "Latest")
            else:
                painter.drawText(dates, Qt.AlignLeft, self.buckets[0][0].strftime("%d %b"))
                if self.buckets[0][0] != self.buckets[-1][1]:
                    painter.drawText(dates, Qt.AlignRight, self.buckets[-1][1].strftime("%d %b"))


class StatsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = page_layout(self, "Stats", "Personal results from your local match history.")
        overview, body = card("Overview")
        self.period = MaterialComboBox()
        for caption, days in (("All time", 0), ("Last 7 days", 7), ("Last 30 days", 30)):
            self.period.addItem(caption, days)
        self.period.setAccessibleName("Stats period")
        self.mode = MaterialComboBox()
        self.mode.addItem("All modes", None)
        self.mode.setAccessibleName("Game mode filter")
        filters = QHBoxLayout()
        filters.addWidget(self.period)
        filters.addWidget(self.mode)
        filters.addStretch()
        body.addLayout(filters)
        self.summary = label("", wrap=True)
        self.record = label("", "hint", wrap=True)
        for value in (self.summary, self.record):
            value.setTextFormat(Qt.PlainText)
            body.addWidget(value)
        layout.addWidget(overview)

        trend, body = card("Chests over time")
        self.trend_grouping = MaterialComboBox()
        self.trend_grouping.addItem("By date", False)
        self.trend_grouping.addItem("By match · latest 30", True)
        self.trend_grouping.setAccessibleName("Chart grouping")
        body.addWidget(self.trend_grouping, 0, Qt.AlignLeft)
        self.trend = StatsTrend()
        self.trend.match_activated.connect(self.open_match)
        body.addWidget(self.trend)
        self.trend_hint = label("Green line · 5-match average. Select a bar to open the match.", "hint", wrap=True)
        body.addWidget(self.trend_hint)
        layout.addWidget(trend)

        breakdown, self.type_rows = card("Chest types")
        layout.addWidget(breakdown)
        modes, body = card("By game mode")
        self.modes = GroupPage._table(("Mode", "Matches", "Chests", "Average", "Best"))
        self.modes.setSelectionMode(QTreeWidget.NoSelection)
        self.modes.header().setSectionResizeMode(QHeaderView.Stretch)
        body.addWidget(self.modes)
        layout.addWidget(modes)
        layout.addStretch(1)
        self.period.currentIndexChanged.connect(self.load)
        self.mode.currentIndexChanged.connect(self.load)
        self.trend_grouping.currentIndexChanged.connect(self.load)

    def load(self):
        error = ""
        try:
            matches = match_history.read_history(self.win.match_history_path())
        except (OSError, ValueError) as exc:
            matches, error = [], str(exc)
        modes = sorted({match.mode for match in matches if match.mode})
        if modes != [self.mode.itemData(index) for index in range(1, self.mode.count())]:
            selected_mode = self.mode.currentData()
            with QSignalBlocker(self.mode):
                self.mode.clear()
                self.mode.addItem("All modes", None)
                for mode in modes:
                    self.mode.addItem(mode, mode)
                self.mode.setCurrentIndex(max(0, self.mode.findData(selected_mode)))
        mode = self.mode.currentData()
        if mode is not None:
            matches = [match for match in matches if match.mode == mode]
        days, today = self.period.currentData(), datetime.now().date()
        if days:
            first_day = (today - timedelta(days=days - 1)).isoformat()
            matches = [match for match in matches if first_day <= match.finished_at[:10] <= today.isoformat()]

        by_match = self.trend_grouping.currentData()
        self.trend.set_matches(matches, days, today, by_match=by_match)
        self.trend_hint.setVisible(bool(by_match and matches))
        self.modes.clear()
        for index in range(self.type_rows.count() - 1, 0, -1):
            row = self.type_rows.takeAt(index).widget()
            row.hide()
            row.deleteLater()
        if not matches:
            self.summary.setText(error or "No saved matches for this period.")
            self.record.clear()
            self.type_rows.addWidget(label("No chests recorded.", "hint"))
            return

        summary = match_stats.cumulative(matches, {}, "")
        best = max(matches, key=lambda match: (match.total, match.finished_at))
        verified = sum(bool(match.replay) for match in matches)
        self.summary.setText(
            f"{summary.total:,} chests · {summary.matches:,} matches · {summary.total / summary.matches:,.1f} per match\n"
            f"{summary.certain:,} confirmed · {summary.total - summary.certain:,} estimated · {verified:,} replay verified\n"
            f"Estimated Pirate Token chance · {summary.chance}"
        )
        self.record.setText(f"Best match · {best.total:,} chests · {best.finished_at} · {best.mode}")

        types = Counter()
        by_mode = {}
        for match in matches:
            types.update(match.chest_types)
            by_mode.setdefault(match.mode, []).append(match.total)
        missing = summary.total - sum(types.values())
        if missing > 0:
            types["unclassified"] += missing
        type_total = sum(types.values())
        if not type_total:
            self.type_rows.addWidget(label("No chests recorded.", "hint"))
        for kind, count in types.most_common():
            if not count:
                continue
            name = discord_group.GROUP_TYPE_LABELS.get(kind, kind)
            title = label(f"{name} · {count:,} · {count / type_total:.1%}")
            title.setTextFormat(Qt.PlainText)
            row = QWidget()
            columns = QHBoxLayout(row)
            columns.setContentsMargins(0, 0, 0, 0)
            columns.addWidget(title, 2)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(round(1000 * count / type_total))
            bar.setTextVisible(False)
            bar.setFixedHeight(10)
            bar.setAccessibleName(title.text())
            columns.addWidget(bar, 3)
            self.type_rows.addWidget(row)
        for mode, totals in sorted(by_mode.items()):
            total = sum(totals)
            self.modes.addTopLevelItem(QTreeWidgetItem(
                (mode, f"{len(totals):,}", f"{total:,}", f"{total / len(totals):,.1f}", f"{max(totals):,}")
            ))


    def open_match(self, match):
        lines = [
            f"{match.finished_at} · {match.mode}",
            f"{match.total:,} chests · {match.certain:,} confirmed · {match.estimated:,} estimated",
            f"Estimated Pirate Token chance · {match_stats.personal(match.certain, match.total, match.boost).chance}",
        ]
        for kind, count in sorted(match.chest_types.items()):
            if count:
                lines.append(f"{discord_group.GROUP_TYPE_LABELS.get(kind, kind)} · {count:,}")
        direct = match.replay.get("direct")
        if isinstance(direct, dict) and all(type(direct.get(key)) is int for key in ("certain", "estimated")):
            live_total = direct["certain"] + direct["estimated"]
            lines.append(f"Live · {live_total:,} chests  /  Replay · {match.total:,} chests ({match.total - live_total:+,})")
        elif match.replay:
            lines.append("Replay verified")
        show_notice_dialog(self, "Match details", "\n".join(lines))


class GroupPage(QWidget):
    scoreboard_result = Signal(object)
    scoreboard_removed = Signal(object)
    players_reset = Signal(object)
    public_board_result = Signal(object)

    """Local match history and the shared Discord leaderboard."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.matches = []
        self._webhook = None
        self._ranks_key = None
        self._team_summary = None
        self._host_match_settings = None
        self._sessions = ()
        self._scoreboard_id = ""
        self._scoreboard_pending = False
        self._published = None
        self._public_pending = False
        self._publish_channel = None
        self._channels = ()
        self._published_channel = ""
        self._next_scoreboard_refresh = time.monotonic() + discord_group.webhook_sync_delay(None, base=0)
        self.scoreboard_result.connect(self._scoreboard_result)
        self.scoreboard_removed.connect(self._scoreboard_removed)
        self.players_reset.connect(self._players_reset)
        self.public_board_result.connect(self._public_board_result)
        layout = self.group_layout = page_layout(self, "Group", "Save your matches and contribute to one shared Discord leaderboard.")
        setup, setup_layout = card("Player setup", "Use your own player name and the webhook shared by your group.")
        form = QGridLayout()
        self.role = MaterialComboBox()
        self.role.addItem("Host", "host")
        self.role.addItem("Guest", "guest")
        self.role.setCurrentIndex(self.role.findData(win.group_role))
        self.role.currentIndexChanged.connect(self.save_role)
        form.addWidget(label("Group role"), 3, 0)
        form.addWidget(self.role, 3, 1)
        self.player = QLineEdit()
        self.player.setMaxLength(80)
        self.webhook = QLineEdit()
        self.webhook.setEchoMode(QLineEdit.Password)
        self.webhook.setPlaceholderText("Discord webhook URL")
        form.addWidget(label("Player name"), 0, 0)
        form.addWidget(self.player, 0, 1)
        form.addWidget(label("Group webhook"), 1, 0)
        form.addWidget(self.webhook, 1, 1)
        self.scoreboard = QLineEdit()
        self.scoreboard.setPlaceholderText("Shared data message link or ID")
        form.addWidget(label("Shared data message"), 2, 0)
        form.addWidget(self.scoreboard, 2, 1)
        self.webhook_name = QLineEdit()
        self.webhook_avatar = QLineEdit()
        self.webhook_name.setMaxLength(80)
        self.webhook_avatar.setPlaceholderText("HTTPS image URL, or empty for the webhook avatar")
        form.addWidget(label("Webhook display name"), 4, 0)
        form.addWidget(self.webhook_name, 4, 1)
        form.addWidget(label("Webhook avatar URL"), 5, 0)
        form.addWidget(self.webhook_avatar, 5, 1)
        setup_layout.addLayout(form)
        self.share_matches = QCheckBox("Share matches with my group")
        self.share_matches.setToolTip("Turn this off to keep new matches in your local history only.")
        self.share_matches.toggled.connect(self.save_share_matches)
        setup_layout.addWidget(self.share_matches)
        actions = connected_buttons(button("Save player setup", on_click=self.save_player_setup),
                                    button("Forget webhook", on_click=self.forget_webhook))
        actions.addStretch(1)
        setup_layout.addLayout(actions)
        self.player_status = label("", "hint", wrap=True)
        setup_layout.addWidget(self.player_status)
        layout.addWidget(setup)
        history, history_layout = card("Match history", "Finish the match from Dashboard to save your totals. "
                                      "Use Reset on the Dashboard before starting the next match. Click a match to see its details.")
        row = QHBoxLayout()
        self.edit = button("Edit...", on_click=self.edit_match)
        self.delete = button("Delete selected...", on_click=self.delete_match)
        self.compare_replay = button("Compare replay...", on_click=self.compare_saved_replay)
        self.select_matches = QCheckBox("Select all matches")
        for action in (self.edit, self.delete, self.compare_replay):
            action.setEnabled(False)
        row.addWidget(self.select_matches)
        row.addLayout(connected_buttons(self.edit, self.delete))
        row.addWidget(self.compare_replay)
        row.addStretch(1)
        history_layout.addLayout(row)
        self.stats = label("", "hint", wrap=True)
        history_layout.addWidget(self.stats)
        self.history = self._table(("Finished", "Player", "Mode", "Certain", "Approx", "Total", "Token chance",
                                    "Team players", "Team total", "Team chance"))
        self.history.itemClicked.connect(self._open_match_details)
        self.history.currentItemChanged.connect(self._selection_changed)
        self.history.itemChanged.connect(lambda *_: self.update_checked_actions())
        self.select_matches.clicked.connect(lambda checked: self.check_all(self.history, checked))
        history_layout.addWidget(self.history)
        self.feedback = label("", "hint", wrap=True)
        history_layout.addWidget(self.feedback)
        layout.addWidget(history)

        ranks, ranks_layout = card("Shared leaderboard", "Chests per player from matches shared to Discord. The overlay uses your local match history and live counter. "
                                      "Choose where the bot publishes the board and share its data message ID with Guests. Click a player to see their stats.")
        controls = QHBoxLayout()
        self.leaderboard_channel = MaterialComboBox()
        self.leaderboard_channel.setMinimumWidth(180)
        self.leaderboard_channel.setToolTip("Discord text channels where the online bot can send embeds.")
        self.leaderboard_channel.addItem("Start the bot to list channels", None)
        controls.addWidget(self.leaderboard_channel, 1)
        self.publish_board = button("Publish leaderboard", on_click=self.publish_in_channel)
        self.copy_board = button("Copy sync ID", on_click=self.copy_scoreboard_id)
        self.delete_board = button("Delete message...", on_click=self.delete_scoreboard)
        controls.addLayout(connected_buttons(self.publish_board, self.copy_board, self.delete_board))
        controls.addStretch(1)
        ranks_layout.addLayout(controls)
        self.team = label("", "hint", wrap=True)
        ranks_layout.addWidget(self.team)
        self.rank_actions = QWidget()
        rank_controls = QHBoxLayout(self.rank_actions)
        rank_controls.setContentsMargins(0, 0, 0, 0)
        self.select_players = QCheckBox("Select all players")
        self.reset_players = button("Reset selected players...", on_click=self.reset_selected_players)
        rank_controls.addWidget(self.select_players)
        rank_controls.addWidget(self.reset_players)
        rank_controls.addStretch(1)
        ranks_layout.addWidget(self.rank_actions)
        self.ranks = self._table(("Rank", "Player", "Total", "Matches", "Certain", "Approx", "Chest types"))
        self.ranks.itemClicked.connect(self._open_player_details)
        self.ranks.itemChanged.connect(lambda *_: self.update_checked_actions())
        self.select_players.clicked.connect(lambda checked: self.check_all(self.ranks, checked))
        ranks_layout.addWidget(self.ranks)
        layout.addWidget(ranks)
        layout.addStretch(1)

    def attach_discord(self, discord):
        self.bot_section, layout = card()
        header = QHBoxLayout()
        self.bot_settings = button("Bot Discord", "toggle")
        self.bot_settings.setCheckable(True)
        self.bot_settings.toggled.connect(discord.setVisible)
        self.bot_settings.toggled.connect(lambda open_: discord.load() if open_ else None)
        header.addWidget(self.bot_settings)
        header.addStretch(1)
        header.addWidget(discord.sender_status)
        header.addWidget(discord.sender_toggle)
        layout.addLayout(header)
        layout.addWidget(discord)
        discord.hide()
        self.group_layout.insertWidget(self.group_layout.count() - 1, self.bot_section)
        self.set_bot_section_role(self.win.group_role)

    def set_bot_section_role(self, role):
        if role != "host":
            self.bot_settings.setChecked(False)
        self.bot_section.setVisible(role == "host")
        self.delete_board.setVisible(role == "host")
        self.leaderboard_channel.setVisible(role == "host")
        self.rank_actions.setVisible(role == "host")
        if self._ranks_key is not None:
            self.render_ranks(self._ranks_key[0], self._team_summary)

    @staticmethod
    def _table(headers):
        table = QTreeWidget()
        table.setObjectName("matchTable")
        table.setHeaderLabels(headers)
        table.setRootIsDecorated(False)
        table.setUniformRowHeights(True)
        table.setMinimumHeight(155)
        return table

    def load(self):
        self.feedback.clear()
        self.history.clear()
        self.select_matches.setChecked(False)
        self.update_checked_actions()
        self.load_player_setup()
        self.apply_match_settings()
        self.sync_scoreboard()
        try:
            self.matches = match_history.read_history(self.win.match_history_path())
        except (OSError, ValueError) as error:
            self.matches = []
            self.win.dashboard.set_recent_matches(self.matches)
            self.stats.setText(f"Could not open match history. {error}")
            return
        self.win.dashboard.set_recent_matches(self.matches)
        if not self.matches:
            self.stats.setText("No matches saved yet.")
            return
        totals = [match.total for match in self.matches]
        self.stats.setText(f"{len(totals):,} matches · {sum(totals):,} chests · "
                           f"Best {max(totals):,} · Average {sum(totals) / len(totals):.1f}")
        for index in reversed(range(len(self.matches))):
            match = self.matches[index]
            row = QTreeWidgetItem((match.finished_at, match.player or "—", match.mode, fmt(match.certain),
                                   fmt(match.estimated), fmt(match.total), match.token_chance))
            row.setData(0, Qt.UserRole, index)
            row.setCheckState(0, Qt.Unchecked)
            self.history.addTopLevelItem(row)
        self.history.setCurrentItem(self.history.topLevelItem(0))
        self.refresh_history_teams()
    def refresh_history_teams(self):
        sessions = {session.fingerprint: session for session in self._sessions}
        webhook_id = self._webhook.id if self._webhook else ""
        for i in range(self.history.topLevelItemCount()):
            item = self.history.topLevelItem(i)
            match = self.matches[item.data(0, Qt.UserRole)]
            result = match_stats.saved_team(match, sessions, webhook_id)
            for column, text in zip((7, 8, 9), ((" · ".join(result.players), fmt(result.total), result.chance)
                                               if result is not None else ("", "", ""))):
                item.setText(column, text)

    def load_player_setup(self):
        cfg = load_cfg()[0]["app"]
        self.player.setText(cfg["group_player"])
        self.webhook_name.setText(cfg["webhook_name"])
        self.webhook_avatar.setText(cfg["webhook_avatar_url"])
        self.share_matches.blockSignals(True)
        self.share_matches.setChecked(cfg["group_share_matches"])
        self.share_matches.blockSignals(False)
        self.role.blockSignals(True)
        self.role.setCurrentIndex(self.role.findData(self.win.group_role))
        self.role.blockSignals(False)
        self._scoreboard_id = cfg["group_scoreboard_id"]
        self.scoreboard.setText(self._scoreboard_id)
        self.webhook.clear()
        try:
            self._webhook = discord_group.load_webhook()
        except (OSError, ValueError) as error:
            self._webhook = None
            self.player_status.setText(str(error))
        self.webhook.setPlaceholderText("••••••••  (saved)" if self._webhook else "Discord webhook URL")

    def save_role(self, _):
        role = self.role.currentData()
        try:
            C.update_config_file(config_file(), "app", {"group_role": role})
        except (C.UserError, OSError) as error:
            self.role.blockSignals(True)
            self.role.setCurrentIndex(self.role.findData(self.win.group_role))
            self.role.blockSignals(False)
            self.player_status.setText(f"Could not save the Group role. {error}")
            return
        self.win.set_group_role(role)
        self._next_scoreboard_refresh = 0
        self.sync_scoreboard()

    def save_share_matches(self, enabled):
        try:
            C.update_config_file(config_file(), "app", {"group_share_matches": enabled})
        except (C.UserError, OSError) as error:
            self.share_matches.blockSignals(True)
            self.share_matches.setChecked(not enabled)
            self.share_matches.blockSignals(False)
            self.player_status.setText(f"Could not save the sharing setting. {error}")
            return
        self.apply_match_settings()
        self.player_status.setText("New matches will count for the group." if enabled else
                                   "New matches stay in your local history.")

    def apply_match_settings(self):
        dashboard = self.win.dashboard
        mode, boost = dashboard.token_mode, dashboard.token_boost
        follow = self.win.group_role == "guest" and self.share_matches.isChecked()
        mode.setEnabled(not follow)
        boost.setEnabled(not follow)
        mode.setToolTip("Choose a mode manually or use Auto to read the latest Battle Royale mode from Fortnite’s local log.")
        boost.setToolTip("Apply this multiplier to every chest in the estimate.")
        if follow and self._host_match_settings is not None:
            host_mode, host_boost = self._host_match_settings
            values = (mode.findText(host_mode), boost.findData(host_boost))
            mode.setToolTip("Game mode from the Host's published leaderboard.")
            boost.setToolTip("Pirate Token multiplier from the Host's published leaderboard.")
        elif follow:
            mode.setToolTip("Waiting for Host settings. Turn off Group sharing to edit.")
            boost.setToolTip(mode.toolTip())
            values = None
        else:
            values = None
        if values is None:
            cfg = load_cfg()[0]["app"]
            values = (mode.findData("Auto") if cfg["token_mode"] == "Auto" else mode.findText(cfg["token_mode"]),
                      boost.findText(cfg["token_boost"]))
        for box, index in zip((mode, boost), values):
            blocked = box.blockSignals(True)
            box.setCurrentIndex(max(0, index))
            box.blockSignals(blocked)
        dashboard.update_token_odds()
        self.win.update_mode_tracking()

    def save_player_setup(self):
        previous = self._webhook, self._scoreboard_id
        try:
            player = discord_group.validate_player(self.player.text())
            for key, value in (("webhook_name", self.webhook_name.text().strip()),
                               ("webhook_avatar_url", self.webhook_avatar.text().strip())):
                valid, reason = settings._check("app", key, value)
                if not valid:
                    raise ValueError(f"{key} {reason}.")
            ident = discord_scoreboard.message_id(self.scoreboard.text())
            if self.webhook.text().strip():
                old = self._webhook
                self._webhook = discord_group.save_webhook(self.webhook.text())
                if old != self._webhook:
                    if old is not None and old.id != self._webhook.id and ident == self._scoreboard_id:
                        ident = ""
                    self.win.discord.stop_bot()
            C.update_config_file(config_file(), "app", {"group_player": player, "group_scoreboard_id": ident,
                                                        "webhook_name": self.webhook_name.text().strip(),
                                                        "webhook_avatar_url": self.webhook_avatar.text().strip()})
        except (C.UserError, OSError, ValueError) as error:
            self.player_status.setText(str(error))
            return
        self.load_player_setup()
        if previous != (self._webhook, self._scoreboard_id):
            self._host_match_settings = None
            self._sessions = ()
            self.apply_match_settings()
            self.render_ranks(())
        self.player_status.setText("Player setup saved." if self._webhook else "Player saved. Add the shared webhook to contribute.")
        self._next_scoreboard_refresh = 0
        self.sync_scoreboard()
        if self._webhook:
            self.win.discord.retry_group_outbox()

    def forget_webhook(self):
        if not ask(self, "Forget the group webhook?", "Remove the saved webhook from this PC?", "Forget"):
            return
        try:
            discord_group.forget_webhook()
            self.win.discord.stop_bot()
            C.update_config_file(config_file(), "app", {"group_scoreboard_id": ""})
        except (C.UserError, OSError) as error:
            self.player_status.setText(str(error))
            return
        self.load_player_setup()
        self._host_match_settings = None
        self._sessions = ()
        self.apply_match_settings()
        self.render_ranks(())
        self.player_status.setText("Webhook removed.")

    def copy_scoreboard_id(self):
        if self._scoreboard_id:
            QApplication.clipboard().setText(self._scoreboard_id)
            self.player_status.setText("Shared data message ID copied.")

    def _is_board_host(self):
        runner = self.win.discord.runner
        return (self.win.group_role == "host" and self._webhook is not None and runner is not None
                and runner.status[0] == "online"
                and getattr(runner, "webhook", None) == self._webhook)

    def refresh_leaderboard_channels(self):
        runner = self.win.discord.runner
        online = self._is_board_host()
        channels = runner.channels() if online and hasattr(runner, "channels") else ()
        if channels != self._channels or not self.leaderboard_channel.count() or not channels:
            chosen = self.leaderboard_channel.currentData()
            self._channels = channels
            self.leaderboard_channel.clear()
            if channels:
                for ident, name in channels:
                    self.leaderboard_channel.addItem(name, ident)
                selected = chosen or getattr(runner, "published_channel", "")
                self.leaderboard_channel.setCurrentIndex(max(0, self.leaderboard_channel.findData(selected)))
            else:
                self.leaderboard_channel.addItem("No accessible channels" if online else "Start the bot to list channels", None)
        published = getattr(runner, "published_channel", "") if online else ""
        if published != self._published_channel:
            self._published_channel = published
            if published and self.leaderboard_channel.findData(published) >= 0:
                self.leaderboard_channel.setCurrentIndex(self.leaderboard_channel.findData(published))
        self.leaderboard_channel.setEnabled(online and bool(channels) and not self._public_pending)

    def publish_in_channel(self):
        if not self._is_board_host() or self._public_pending or self._scoreboard_pending:
            return
        channel = self.leaderboard_channel.currentData()
        if channel is None:
            self.player_status.setText("Choose an accessible Discord channel first.")
            return
        self._publish_channel = channel
        self.sync_scoreboard(publish=True)

    def publish_public_board(self):
        channel, self._publish_channel = self._publish_channel, None
        runner = self.win.discord.runner
        if channel is None or not self._is_board_host():
            return
        mode = self.win.current_game_mode()
        settings = (mode, self.win.dashboard.token_boost.currentData()) if mode in discord_scoreboard.MODES else None
        self._public_pending = True
        self.publish_board.setEnabled(False)
        try:
            future = runner.publish_board(channel, settings=settings)
        except ValueError as error:
            self._public_pending = False
            self.player_status.setText(str(error))
            return
        def done(result):
            try:
                link, error = result.result(), ""
            except Exception:
                link, error = "", "Could not publish the board. Check View Channel, Send Messages and Embed Links."
            self.public_board_result.emit((runner, link, error))
        future.add_done_callback(done)

    def _public_board_result(self, result):
        runner, link, error = result
        self._public_pending = False
        if runner is not self.win.discord.runner:
            return
        self.player_status.setText(error or f"Leaderboard published. {link}")
        self.refresh_leaderboard_channels()

    def sync_scoreboard(self, publish=False):
        webhook = self._webhook
        ident = self._scoreboard_id
        host = self._is_board_host()
        self.publish_board.setVisible(self.win.group_role == "host")
        self.refresh_leaderboard_channels()
        self.publish_board.setEnabled(host and bool(self.leaderboard_channel.currentData())
                                      and not self._scoreboard_pending and not self._public_pending)
        self.copy_board.setEnabled(bool(ident))
        self.update_checked_actions()
        self.delete_board.setEnabled(bool(webhook and ident and host and not self._scoreboard_pending))
        if webhook is None:
            self._sessions = ()
            self.render_ranks(())
            return
        cooldown = discord_group.webhook_retry_after(webhook)
        if cooldown > 0:
            self._next_scoreboard_refresh = time.monotonic() + cooldown
            if publish:
                self.player_status.setText(f"Discord sync paused; retrying in {math.ceil(cooldown)} seconds.")
            return
        if publish and not host:
            return
        if self._scoreboard_pending or (not publish and time.monotonic() < self._next_scoreboard_refresh):
            return
        if not host and (not ident or self.win.pages.currentWidget() not in (self, self.win.dashboard)):
            return
        self._next_scoreboard_refresh = time.monotonic() + discord_group.webhook_sync_delay(webhook)
        self._scoreboard_pending = True
        previous = self._published
        dashboard = self.win.dashboard
        game_mode = self.win.current_game_mode()
        host_settings = (game_mode, dashboard.token_boost.currentData()) if game_mode in discord_scoreboard.MODES else None
        if host and hasattr(self.win.discord.runner, "set_board_settings"):
            self.win.discord.runner.set_board_settings(host_settings)

        def sync():
            try:
                created = None
                if host:
                    settings = host_settings
                    store = discord_group.GroupStore(webhook.id)
                    players, team = store.board()
                    sessions = store.sessions()
                    ranking = discord_scoreboard.rows(players)
                    if (self._is_board_host() and webhook == self._webhook and ident == self._scoreboard_id and
                            (publish or (ident and previous != (webhook.id, ident, ranking, team, settings, sessions)))):
                        created, _ = discord_scoreboard.publish(webhook, ident, ranking, team, settings=settings, sessions=sessions, data_only=True)
                else:
                    ranking, team, settings, sessions = discord_scoreboard.fetch(webhook, ident, with_sessions=True)
                    store = discord_group.GroupStore(webhook.id)
                    store.merge_sessions(sessions)
                    sessions = store.sessions()
                result = (webhook, ident, host, ranking, team, settings, created, "", sessions)
            except (OSError, ValueError, sqlite3.Error) as error:
                result = (webhook, ident, host, (), None, None, None, str(error))
            self.scoreboard_result.emit(result)

        threading.Thread(target=sync, daemon=True, name="group-scoreboard").start()

    def delete_scoreboard(self):
        webhook, ident = self._webhook, self._scoreboard_id
        if not self._is_board_host() or not webhook or not ident or self._scoreboard_pending:
            return
        if not ask(self, "Delete the leaderboard message?", "Remove the published message from Discord? "
                   "Saved matches and shared standings are kept.", "Delete message", danger=True):
            return
        if (self.win.group_role != "host" or self._scoreboard_pending
                or (webhook, ident) != (self._webhook, self._scoreboard_id)):
            return
        self._scoreboard_pending = True
        self.publish_board.setEnabled(False)
        self.delete_board.setEnabled(False)

        runner = self.win.discord.runner
        def remove():
            try:
                if runner is not None and hasattr(runner, "delete_board"):
                    runner.delete_board().result(timeout=15)
                discord_scoreboard.delete(webhook, ident)
                error = ""
            except Exception:
                error = "Could not delete the board. Check the bot and its channel permissions."
            self.scoreboard_removed.emit((webhook, ident, error))

        threading.Thread(target=remove, daemon=True, name="group-scoreboard-delete").start()

    def _scoreboard_removed(self, result):
        webhook, ident, error = result
        self._scoreboard_pending = False
        if (webhook, ident) != (self._webhook, self._scoreboard_id):
            return
        if error:
            self.player_status.setText(error)
            return
        self._published = None
        self._scoreboard_id = ""
        self.scoreboard.clear()
        try:
            C.update_config_file(config_file(), "app", {"group_scoreboard_id": ""})
        except (C.UserError, OSError) as failure:
            self.player_status.setText(f"Message deleted. Save player setup to clear the old ID. {failure}")
        else:
            self.player_status.setText("Leaderboard message deleted.")
        self.sync_scoreboard()

    def _scoreboard_result(self, result):
        webhook, ident, host, ranking, team, settings, published, error, *received_sessions = result
        self._scoreboard_pending = False
        if webhook != self._webhook or ident != self._scoreboard_id:
            self._next_scoreboard_refresh = 0
            return
        if host and self.win.group_role != "host" and not published:
            return
        if error:
            cooldown = discord_group.webhook_retry_after(webhook)
            if cooldown > 0:
                self._next_scoreboard_refresh = time.monotonic() + cooldown
            else:
                self._publish_channel = None
            self.player_status.setText(error)
            self.publish_board.setEnabled(self._is_board_host())
            return
        if published:
            self._published = (webhook.id, published, ranking, team, settings, received_sessions[0] if received_sessions else ())
            if not ident:
                self._scoreboard_id = published
                self.scoreboard.setText(published)
                try:
                    C.update_config_file(config_file(), "app", {"group_scoreboard_id": published})
                except (C.UserError, OSError) as error:
                    self.player_status.setText(f"Leaderboard published. Copy and save its message ID. {error}")
                else:
                    self.player_status.setText("Group sync ready. Share its data message ID with Guests.")
            self.copy_board.setEnabled(True)
        if received_sessions:
            self._sessions = received_sessions[0]
            self.refresh_history_teams()
            self.win.dashboard.refresh_screen_overlay()
        if not host and self.win.group_role == "guest" and settings != self._host_match_settings:
            self._host_match_settings = settings
            self.apply_match_settings()
        self.render_ranks(ranking, team)
        self.publish_public_board()

    def render_ranks(self, ranking, team=None):
        self._team_summary = team
        self.team.setText(team.text() if team is not None else "Team match summary unavailable.")
        host = self.win.group_role == "host"
        key = (ranking, host)
        if key == self._ranks_key:
            return
        checked = {row.text(1) for row in self.checked_items(self.ranks)} if ranking and host else set()
        self._ranks_key = key
        self.ranks.clear()
        for position, player in enumerate(ranking, 1):
            types = " · ".join(f"{discord_group.GROUP_TYPE_LABELS.get(kind, kind)} {fmt(count)}"
                               for kind, count in player.chest_types.items()) or "—"
            item = QTreeWidgetItem((str(position), player.name, fmt(player.total),
                                                       "—" if player.matches is None else fmt(player.matches),
                                                       "—" if player.certain is None else fmt(player.certain),
                                                       "—" if player.estimated is None else fmt(player.estimated), types))
            if host:
                item.setCheckState(0, Qt.Checked if player.name in checked else Qt.Unchecked)
            self.ranks.addTopLevelItem(item)
        self.update_checked_actions()
        self.win.dashboard.set_leaders(ranking)

    @staticmethod
    def checked_items(table):
        return [table.topLevelItem(i) for i in range(table.topLevelItemCount())
                if table.topLevelItem(i).checkState(0) == Qt.Checked]

    def check_all(self, table, checked):
        table.blockSignals(True)
        for i in range(table.topLevelItemCount()):
            table.topLevelItem(i).setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        table.blockSignals(False)
        self.update_checked_actions()

    def update_checked_actions(self):
        matches = len(self.checked_items(self.history))
        players = len(self.checked_items(self.ranks))
        self.select_matches.setChecked(0 < matches == self.history.topLevelItemCount())
        self.select_players.setChecked(0 < players == self.ranks.topLevelItemCount())
        self.delete.setEnabled(matches > 0)
        self.reset_players.setEnabled(players > 0 and self._is_board_host() and not self._scoreboard_pending)

    def _selection_changed(self, item, previous):
        self.edit.setEnabled(item is not None)
        self.compare_replay.setEnabled(item is not None)

    def reset_selected_players(self):
        names = [item.text(1) for item in self.checked_items(self.ranks)]
        webhook = self._webhook
        if not names or not self._is_board_host() or self._scoreboard_pending:
            return
        if not ask(self, "Reset selected players?", f"Reset {len(names)} selected players in the shared standings? "
                   "Their saved matches and team match records are kept.", "Reset players", danger=True):
            return
        if not self._is_board_host() or webhook != self._webhook or self._scoreboard_pending:
            return
        self._scoreboard_pending = True
        self.reset_players.setEnabled(False)

        def reset():
            try:
                discord_group.GroupStore(webhook.id).reset_players(names)
                error = ""
            except (OSError, ValueError, sqlite3.Error) as failure:
                error = str(failure)
            self.players_reset.emit((webhook, error))

        threading.Thread(target=reset, daemon=True, name="group-reset-players").start()

    def _players_reset(self, result):
        webhook, error = result
        self._scoreboard_pending = False
        if webhook != self._webhook:
            return
        self.player_status.setText(error or "Selected players reset.")
        self._next_scoreboard_refresh = 0
        self.sync_scoreboard()

    def _open_match_details(self, item, column):
        if column == 0:
            return  # The first column contains the checkbox for bulk actions.
        match = self.matches[item.data(0, Qt.UserRole)]
        lines = self._detail_lines(self.history, item, (0, 1, 2, 3, 4, 5, 6, 7, 8, 9))
        if match.replay:
            lines.append("Replay verified. Use Compare replay to see the differences.")
        show_notice_dialog(self, "Match details", "\n".join(lines))

    def _open_player_details(self, item, column):
        if column == 0 and self.win.group_role == "host":
            return  # Keep the checkbox available for bulk actions.
        lines = self._detail_lines(self.ranks, item, (0, 2, 3, 4, 5, 6))
        show_notice_dialog(self, item.text(1), "\n".join(lines))

    @staticmethod
    def _detail_lines(table, item, columns):
        header = table.headerItem()
        return [f"{header.text(column)} · {item.text(column)}"
                for column in columns if item.text(column)]

    def compare_saved_replay(self):
        current = self.history.currentItem()
        if current is None:
            return
        match = self.matches[current.data(0, Qt.UserRole)]
        try:
            if match.replay:
                comparison = replay_comparison.saved_comparison(match)
            else:
                file, _ = QFileDialog.getOpenFileName(self, "Choose the LED-N replay report", "", "JSON reports (*.json)")
                if not file:
                    return
                comparison = replay_comparison.compare_report(Path(file), match)
        except (OSError, ValueError) as error:
            show_error(self, f"Could not compare the replay. {error}")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Live and replay comparison")
        dialog.resize(660, 510)
        layout = QVBoxLayout(dialog)
        report = QPlainTextEdit(comparison)
        report.setReadOnly(True)
        layout.addWidget(report)
        layout.addWidget(button("Close", on_click=dialog.accept), 0, Qt.AlignRight)
        dialog.exec()

    def edit_match(self):
        current = self.history.currentItem()
        if current is None:
            return
        match = self.matches[current.data(0, Qt.UserRole)]
        dialog = CountsDialog(self, match.certain, match.total)
        dialog.setWindowTitle("Edit saved match")
        if dialog.exec() != QDialog.Accepted:
            return
        certain, total = dialog.certain.value(), dialog.total
        try:
            saved = match_history.edit_match(self.win.match_history_path(), match.snapshot, certain, total,
                                             token_odds_text(total, match.boost))
        except (OSError, ValueError) as error:
            show_error(self, f"Could not edit the match. {error}")
            return
        self.load()
        if saved:
            self.feedback.setText("Match updated.")

    def delete_match(self):
        snapshots = {self.matches[item.data(0, Qt.UserRole)].snapshot for item in self.checked_items(self.history)}
        if not snapshots:
            return
        if not ask(self, "Delete selected matches?", f"Delete {len(snapshots)} selected matches from this PC? "
                   "Already shared contributions remain in the leaderboard.", "Delete matches", danger=True):
            return
        try:
            removed = match_history.delete_matches(self.win.match_history_path(), snapshots)
        except (OSError, ValueError) as error:
            show_error(self, f"Could not delete the selected matches. {error}")
            return
        self.load()
        if removed:
            self.feedback.setText(f"{removed} matches deleted.")

class DiscordPage(QWidget):
    send_result = Signal(str)
    bot_changed = Signal()
    branding_result = Signal(object)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._sending = False
        self._outbox_active = threading.Lock()
        self._next_outbox_retry = 0.0
        self.runner = None
        self._configured = False
        self._branding_pending = False
        self.branding_result.connect(self._branding_done)
        self.send_result.connect(self._sent)
        self.bot_changed.connect(self.refresh_sender_status, Qt.QueuedConnection)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        setup, setup_layout = card("Bot setup", "Run the Discord bot for Group commands and match reports.")
        links = connected_buttons(
            button("Discord Developer Portal", on_click=lambda: QDesktopServices.openUrl(
                QUrl("https://discord.com/developers/applications"))),
            button("Invite bot", on_click=self.invite_bot),
            button("Discord setup guide", on_click=lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/NEVARLeVrai/fortnite-visual-chest-counter/blob/main/README.md#group-and-discord"))),
        )
        links.addStretch(1)
        setup_layout.addLayout(links)
        setup_layout.addWidget(label("1. Copy the Application ID from General Information and the token from the Bot page.\n"
                                     "2. Enable Guild Install under Installation and leave Requires OAuth2 Code Grant "
                                     "off under Bot. Click Invite bot and authorize it on a server you can manage.\n"
                                     "3. Save the webhook in Group. It determines the server and feed channel. "
                                     "Enable Message Content Intent on the Bot page. Grant View Channel and "
                                     "Read Message History in the feed. Add Send Messages for reports and Embed Links "
                                     "for styled reports.", "hint", wrap=True))
        form = QGridLayout()
        self.application_id = QLineEdit()
        self.application_id.setPlaceholderText("Discord application ID")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Bot token")
        form.addWidget(label("Bot token"), 1, 0)
        form.addWidget(self.token, 1, 1)
        form.addWidget(label("Application ID"), 2, 0)
        form.addWidget(self.application_id, 2, 1)
        setup_layout.addLayout(form)
        setup_layout.addWidget(label("The channel ID and token are stored in Windows Credential Manager.",
                                     "hint", wrap=True))
        row = connected_buttons(button("Save bot setup", on_click=self.save_setup),
                                button("Forget bot setup", on_click=self.forget_setup))
        row.addStretch(1)
        setup_layout.addLayout(row)
        self.sender_status = label("", "pill")
        self.sender_toggle = button("", on_click=self.toggle_sender)
        self.auto_start = QCheckBox("Start bot with the app")
        self.auto_start.setChecked(load_cfg()[0]["app"]["auto_start_discord_sender"])
        self.auto_start.toggled.connect(self.save_sender_start)
        setup_layout.addWidget(self.auto_start)
        self.setup_status = label("", "hint", wrap=True)
        setup_layout.addWidget(self.setup_status)
        layout.addWidget(setup)

        appearance, appearance_layout = card("Bot profile", "Apply changes to the bot's Discord profile in every server.")
        form = QGridLayout()
        self.bot_name = QLineEdit()
        self.bot_name.setMaxLength(32)
        self.bot_avatar = QLineEdit()
        self.bot_avatar.setPlaceholderText("Empty uses the bundled chest logo")
        form.addWidget(label("Bot name"), 0, 0)
        form.addWidget(self.bot_name, 0, 1)
        form.addWidget(label("Avatar PNG or JPEG"), 1, 0)
        form.addWidget(self.bot_avatar, 1, 1)
        form.addWidget(button("Browse...", on_click=self.pick_bot_avatar), 1, 2)
        self.bot_banner = QLineEdit()
        self.bot_banner.setPlaceholderText("Empty uses the bundled profile banner")
        form.addWidget(label("Banner PNG or JPEG"), 2, 0)
        form.addWidget(self.bot_banner, 2, 1)
        form.addWidget(button("Browse...", on_click=lambda: self.pick_bot_avatar(banner=True)), 2, 2)
        appearance_layout.addLayout(form)
        self.apply_branding = button("Apply bot profile", on_click=lambda: self.save_bot_preferences(apply_profile=True))
        appearance_layout.addWidget(self.apply_branding, 0, Qt.AlignRight)
        layout.addWidget(appearance)

        presence, presence_layout = card("Bot presence", "Use {total}, {players}, {matches} and {chance}. The bot updates its activity while online.")
        self.presence_enabled = QCheckBox("Show bot activity")
        presence_layout.addWidget(self.presence_enabled)
        row = QHBoxLayout()
        self.presence_activity = MaterialComboBox()
        for text, value in (("Watching", "watching"), ("Playing", "playing"), ("Listening", "listening")):
            self.presence_activity.addItem(text, value)
        row.addWidget(self.presence_activity, 1)
        row.addWidget(label("Update every"))
        self.presence_interval = QSpinBox()
        self.presence_interval.setRange(30, 300)
        self.presence_interval.setAlignment(Qt.AlignCenter)
        self.presence_interval.setSuffix(" s")
        row.addWidget(self.presence_interval)
        presence_layout.addLayout(row)
        form = QGridLayout()
        self.presence_texts = {}
        for index, (key, title) in enumerate((("personal", "One player"), ("team", "Current team"),
                                            ("group", "Group totals"), ("idle", "Idle"))):
            edit = QLineEdit()
            edit.setMaxLength(128)
            self.presence_texts[key] = edit
            form.addWidget(label(title), index, 0)
            form.addWidget(edit, index, 1)
        presence_layout.addLayout(form)
        presence_layout.addWidget(button("Save profile and presence", on_click=self.save_bot_preferences), 0, Qt.AlignRight)
        self.appearance_status = label("", "hint", wrap=True)
        presence_layout.addWidget(self.appearance_status)
        layout.addWidget(presence)

        summary, summary_layout = card("Match reports", "Send a saved match manually, or post each new match after it is saved.")
        self.auto_report = QCheckBox("Automatically send new match reports")
        self.auto_report.toggled.connect(self.save_report_mode)
        summary_layout.addWidget(self.auto_report)
        self.match = MaterialComboBox()
        self.match.setAccessibleName("Saved match")
        self.match.setPlaceholderText("No saved matches yet")
        self.match.currentIndexChanged.connect(self._show_summary)
        self.send = button("Send report", "primary", on_click=self.send_summary)
        self.send.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.match, 1)
        row.addWidget(self.send)
        summary_layout.addLayout(row)
        self.status = label("", "hint", wrap=True)
        summary_layout.addWidget(self.status)
        layout.addWidget(summary)
        self.refresh_sender_status()

    def load(self):
        cfg = load_cfg()[0]["app"]
        self.bot_name.setText(cfg["bot_name"])
        self.bot_avatar.setText(cfg["bot_avatar_path"])
        self.bot_banner.setText(cfg["bot_banner_path"])
        self.presence_enabled.setChecked(cfg["discord_presence_enabled"])
        self.presence_interval.setValue(cfg["discord_presence_interval"])
        self.presence_activity.setCurrentIndex(max(0, self.presence_activity.findData(cfg["discord_presence_activity"])))
        for key, edit in self.presence_texts.items():
            edit.setText(cfg[f"discord_presence_{key}_text"])
        self.token.clear()
        self.auto_report.blockSignals(True)
        self.auto_report.setChecked(load_cfg()[0]["app"]["auto_discord_summary"])
        self.auto_report.blockSignals(False)
        try:
            config = discord_summary.load()
            self.show_saved_setup(config)
        except (OSError, ValueError) as error:
            self.setup_status.setText(str(error))
        self.match.clear()
        try:
            for match in reversed(match_history.read_history(self.win.match_history_path())):
                self.match.addItem(f"{match.finished_at} · {match.profile} · {match.mode}", match)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
        self._show_summary()

    def pick_bot_avatar(self, *, banner=False):
        path, _ = QFileDialog.getOpenFileName(self, "Choose bot avatar", "", "Images (*.png *.jpg *.jpeg)")
        if path:
            (self.bot_banner if banner else self.bot_avatar).setText(path)

    def save_bot_preferences(self, *, apply_profile=False):
        values = {"bot_name": self.bot_name.text().strip(), "bot_avatar_path": self.bot_avatar.text().strip(),
                  "bot_banner_path": self.bot_banner.text().strip(),
                  "discord_presence_enabled": self.presence_enabled.isChecked(),
                  "discord_presence_interval": self.presence_interval.value(),
                  "discord_presence_activity": self.presence_activity.currentData()}
        values.update((f"discord_presence_{key}_text", edit.text().strip()) for key, edit in self.presence_texts.items())
        try:
            C.update_config_file(config_file(), "app", values)
        except (C.UserError, OSError) as error:
            self.appearance_status.setText(str(error))
            return
        self.appearance_status.setText("Profile and presence preferences saved.")
        if not apply_profile:
            return
        if self.runner is None or self.runner.status[0] != "online" or self._branding_pending:
            self.appearance_status.setText("Start the bot before applying its Discord profile.")
            return
        runner = self.runner
        try:
            future = runner.apply_branding(values["bot_name"], values["bot_avatar_path"], values["bot_banner_path"])
        except ValueError as error:
            self.appearance_status.setText(str(error))
            return
        self._branding_pending = True
        self.apply_branding.setEnabled(False)
        def done(result):
            try:
                text = result.result()
            except Exception as error:
                text = str(error) if isinstance(error, ValueError) else "Discord could not update the profile. Check the image and try later."
            self.branding_result.emit((runner, text))
        future.add_done_callback(done)

    def _branding_done(self, result):
        runner, text = result
        self._branding_pending = False
        if runner is self.runner:
            self.appearance_status.setText(text)
        self.refresh_sender_status()

    def _show_summary(self, *_):
        match = self.match.currentData()
        self.match.setEnabled(self.match.count() > 0)
        ready = self.runner is not None and self.runner.status[0] == "online"
        self.send.setEnabled(match is not None and ready and not self._sending)

    def save_report_mode(self, enabled):
        try:
            C.update_config_file(config_file(), "app", {"auto_discord_summary": enabled})
        except (C.UserError, OSError) as error:
            self.auto_report.blockSignals(True)
            self.auto_report.setChecked(not enabled)
            self.auto_report.blockSignals(False)
            self.status.setText(f"Could not save the report preference: {error}")
            return
        self.status.setText("Automatic reports enabled." if enabled else "Manual reports selected.")

    def invite_bot(self):
        app_id = self.application_id.text().strip()
        if not discord_summary.valid_id(app_id):
            self.setup_status.setText("Enter the Application ID from General Information first.")
            self.application_id.setFocus()
            return
        url = QUrl(f"https://discord.com/oauth2/authorize?client_id={app_id}&integration_type=0&scope=bot&permissions=0")
        if QDesktopServices.openUrl(url):
            self.setup_status.setText("In Discord, choose a server you can manage and click Authorize.")
        else:
            self.setup_status.setText("Could not open the invite link. Check your default browser.")

    def show_saved_setup(self, config):
        self._configured = config is not None
        self.application_id.setText(config.application_id if config else "")
        self.token.setPlaceholderText("••••••••  (saved)" if config else "Bot token")
        self.setup_status.setText("Bot setup saved." if config else "No bot configured.")
        self.refresh_sender_status()

    def refresh_sender_status(self):
        state, detail = self.runner.status if self.runner else ("stopped", "")
        active = state in ("online", "connecting", "stopping")
        text = {"online": "Bot online", "connecting": "Connecting", "stopping": "Stopping",
                "error": "Bot error", "stopped": "Bot stopped" if self._configured else "Setup required"}[state]
        self.sender_status.setText(f"●  {text}")
        self.sender_status.setToolTip(detail)
        self.sender_status.setStyleSheet(theme.pill_style(theme.CERTAIN if state == "online" else theme.MUTED))
        if detail and state == "error":
            self.setup_status.setText(detail)
        self.apply_branding.setEnabled(state == "online" and not self._branding_pending)
        self.sender_toggle.setText("Stop bot" if active else "Start bot")
        self.sender_toggle.setEnabled(state != "stopping" and (active or self._configured))
        self._show_summary()

    def toggle_sender(self):
        if self.runner and self.runner.status[0] in ("connecting", "online"):
            self.stop_bot()
        else:
            self.start_bot()

    def start_bot(self):
        if self.win.group_role != "host":
            return
        if self.runner and self.runner.status[0] in ("connecting", "online", "stopping"):
            return
        try:
            config = discord_summary.load()
            webhook = discord_group.load_webhook()
        except (OSError, ValueError) as error:
            self.setup_status.setText(str(error))
            return
        if config is None:
            return
        if webhook is None:
            self.setup_status.setText("Save the Group webhook before starting the bot.")
            return
        self.runner = discord_bot.BotRunner(config, webhook, self.bot_changed.emit)
        self.show_saved_setup(config)
        self.runner.start()

    def stop_bot(self, *, timeout=None):
        if self.runner:
            self.runner.stop(timeout=timeout)
        self.refresh_sender_status()

    def retry_group_outbox(self):
        try:
            webhook = discord_group.load_webhook()
        except (OSError, ValueError) as error:
            self.win.auto_discord_result.emit(f"Group delivery failed. {error}")
            return
        if webhook is None:
            return
        cooldown = discord_group.webhook_retry_after(webhook)
        if cooldown > 0:
            self._next_outbox_retry = time.monotonic() + cooldown
            return
        if not self._outbox_active.acquire(blocking=False):
            return
        self._next_outbox_retry = time.monotonic() + discord_group.webhook_sync_delay(webhook, base=30)
        history = self.win.match_history_path()

        def deliver():
            try:
                for match in match_history.read_history(history):
                    if match.group_webhook_id == webhook.id and match.player:
                        discord_group.queue_match(webhook, match, match.player)
                        if match.replay:
                            discord_group.queue_verified(webhook, match, match.player)
                sent = discord_group.flush(webhook)
                if sent:
                    self.win.auto_discord_result.emit(f"Group contributions sent to Discord ({sent}).")
            except (OSError, ValueError, sqlite3.Error) as error:
                self.win.auto_discord_result.emit(f"Group delivery failed. {error}")
            finally:
                self._outbox_active.release()

        threading.Thread(target=deliver, daemon=True, name="discord-group-outbox").start()

    def save_sender_start(self, enabled):
        self.win.save_app_option("auto_start_discord_sender", enabled)

    def save_setup(self):
        try:
            stored = discord_summary.load() if not self.token.text().strip() else None
            token = self.token.text().strip() or (stored.token if stored else "")
            config = discord_summary.BotSettings("", token, self.application_id.text().strip())
            discord_summary.save(config)
        except (OSError, ValueError) as error:
            self.setup_status.setText(str(error))
            return
        self.token.clear()
        if self.runner and self.runner.config != config:
            self.stop_bot()
            self.runner = None if self.runner.status[0] == "error" else self.runner
        self.show_saved_setup(config)

    def forget_setup(self):
        if not ask(self, "Forget Discord setup?", "Remove the saved bot setup from this PC?",
                   "Forget", danger=True):
            return
        try:
            discord_summary.forget()
        except OSError as error:
            self.setup_status.setText(str(error))
            return
        self.token.clear()
        self.stop_bot()
        if self.runner and self.runner.status[0] in ("stopped", "error"):
            self.runner = None
        self.show_saved_setup(None)
        self.setup_status.setText("Bot setup removed.")

    def send_summary(self):
        match = self.match.currentData()
        if match is None or self._sending:
            return
        config = self.runner.report_config if self.runner and self.runner.status[0] == "online" else None
        if config is None:
            self.status.setText("Start the bot to resolve the Group report channel.")
            return
        if not ask(self, "Send match summary?", f"Send to Discord channel {config.channel}?\n\n{match.summary()}", "Send"):
            return
        self._sending = True
        self.send.setEnabled(False)
        self.status.setText("Sending to Discord...")

        def deliver():
            try:
                discord_summary.send(config, match)
                result = "Summary sent."
            except (OSError, ValueError) as error:
                result = str(error)
            self.send_result.emit(result)

        threading.Thread(target=deliver, daemon=True).start()

    def _sent(self, message):
        self._sending = False
        self._show_summary()
        self.status.setText(message)


class SetupPage(QWidget):
    def __init__(self, win):
        super().__init__()
        layout = page_layout(self, "Setup", "Set up your capture, match history and optional sharing.")
        steps = (
            ("Capture Fortnite",
             "Choose your screen in Dashboard → Capture settings. For a washed-out HDR picture, "
             "select the HDR → SDR source. Check Live preview before counting.",
             "Open Dashboard", win.dashboard),
            ("Language and match detection",
             "Choose the language used in Fortnite. Turn on automatic match detection in Settings "
             "and enable replay recording in Fortnite to verify saved matches at the lobby. "
             "Replay verification is on by default. You can turn it off or enable cleanup in Settings.",
             "Open Settings", win.settings_page),
            ("On-screen overlay and OBS",
             "Choose the stats you want to show and check the preview. The floating overlay works "
             "without OBS. For OBS, use the text file shown on the Overlay page.",
             "Open Overlay", win.overlay),
            ("Group and Discord",
             "Each player runs one capture and enters their own name and the shared webhook. "
             "The host configures the bot and publishes the leaderboard. Solo counting needs no Discord setup.",
             "Open Group", win.group),
        )
        for title, description, action, page in steps:
            frame, controls = card(title, description)
            controls.addWidget(button(action, on_click=lambda _checked=False, page=page: win.go_to(win.pages.indexOf(page))),
                               0, Qt.AlignLeft)
            layout.addWidget(frame)
        layout.addStretch(1)


class HelpPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = page_layout(self, "Help & diagnostics", None)
        box, box_layout = card("Start counting in a minute")
        steps = [
            "Someone's live stream:  Video source  >  Livestream URL, paste the link (Twitch or YouTube), Start counting.",
            "A stream, video or game on a screen:  play it FULLSCREEN, pick that Screen as the video source, Start counting.",
            "Your own stream with OBS:  make a scene with only your gameplay, click Start Virtual Camera in OBS, pick "
            "\"OBS Virtual Camera\" here, Start counting.",
            "Show the numbers on stream:  in OBS add a Text (GDI+) source with \"Read from file\" pointing to the file shown "
            "on the Overlay page.",
            "Second PC, capture card or a recorded video? The guide explains every setup.",
        ]
        for step in steps:
            box_layout.addWidget(label(step, wrap=True))
        row = QHBoxLayout()
        row.addWidget(button("Open the full guide", on_click=self._guide))
        row.addStretch(1)
        box_layout.addLayout(row)
        layout.addWidget(box)

        box, box_layout = card("Diagnostics", "A short report about this PC's video devices, OBS Virtual Camera and the "
                                              "program files. It contains no account or personal data.")
        row = QHBoxLayout()
        self.run = button("Run diagnostics", "primary", on_click=self._run)
        self.copy = button("Copy report", on_click=self._copy)
        self.copy.setEnabled(False)
        row.addLayout(connected_buttons(self.run, self.copy))
        row.addLayout(connected_buttons(
            button("Open logs folder", on_click=win.open_logs),
            button("Clean logs and captures...", on_click=win.clean_diagnostics),
        ))
        row.addStretch(1)
        box_layout.addLayout(row)
        self.report = QPlainTextEdit()
        self.report.setObjectName("report")
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(260)
        self.report.setPlaceholderText("The report appears here.")
        box_layout.addWidget(self.report)
        layout.addWidget(box)

        box, box_layout = card("About")
        box_layout.addWidget(label(f"{APP_NAME} v{__version__} ({RELEASE_CHANNEL}). {TAGLINE}.", wrap=True))
        box_layout.addWidget(label("Free and open source (MIT licence). Independent and unofficial: not affiliated with, "
                                   "endorsed by or approved by Epic Games. Chest counts come from video. Optional match tracking reads the local Fortnite log. The app never "
                                   "connects to or changes the game and shows nothing that is not already on screen. "
                                   "Fair play and compatibility: see the full guide.", "hint", wrap=True))
        row = QHBoxLayout()
        row.addWidget(button("Project page", on_click=lambda: QDesktopServices.openUrl(QUrl(REPO_URL))))
        row.addStretch(1)
        box_layout.addLayout(row)
        layout.addWidget(box)
        layout.addStretch(1)
        self._thread = None
        self._result = None
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._check)

    def _guide(self):
        guide = paths.APP_DIR / "README.html"
        if guide.exists():
            open_path(guide)
        else:
            QDesktopServices.openUrl(QUrl(REPO_URL + "#readme"))

    def _run(self):
        self.run.setEnabled(False)
        self.report.setPlainText("Running diagnostics (a few seconds)...")

        def work():
            try:
                self._result = run_command(["diagnostics"], timeout=90)[1]
            except Exception as error:
                self._result = f"Diagnostics could not run: {error}"

        self._result = None
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self._poll.start(200)

    def _check(self):
        if self._result is None:
            return
        self._poll.stop()
        self.report.setPlainText(self._result.strip())
        self.run.setEnabled(True)
        self.copy.setEnabled(True)

    def _copy(self):
        QApplication.clipboard().setText(self.report.toPlainText())
        self.copy.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy.setText("Copy report"))


# ==================================================================
# main window
# ==================================================================

class UpdateDialog(ThemedDialog):
    """Not modal: counting and the rest of the app stay usable while an update is prepared, and closing this only hides it."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} update")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        self.title = label("", "cardTitle", wrap=True)
        layout.addWidget(self.title)
        self.body = label("", "hint", wrap=True)
        layout.addWidget(self.body)
        self.notes = QPlainTextEdit()                    # plain text on purpose: release notes come from outside the app
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(170)
        layout.addWidget(self.notes)
        self.stage = label("", wrap=True)
        layout.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.detail = label("", "hint", wrap=True)
        layout.addWidget(self.detail)
        self.problem = label("", wrap=True)
        self.problem.setStyleSheet(f"color: {theme.REJECT};")
        layout.addWidget(self.problem)
        layout.addStretch(1)
        row = QHBoxLayout()
        self.page = button("View on GitHub", "link", on_click=self._open_page)
        row.addWidget(self.page)
        row.addStretch(1)
        self.later = button("Later", on_click=self.hide)
        row.addWidget(self.later)
        self.cancel_button = button("Cancel", on_click=self._cancel)
        row.addWidget(self.cancel_button)
        self.primary = button("", "primary", on_click=self._primary)
        row.addWidget(self.primary)
        layout.addLayout(row)

    def _open_page(self):
        update = self.win.updates.update
        if update is not None and update.release.page_url:
            QDesktopServices.openUrl(QUrl(update.release.page_url))

    def _cancel(self):
        self.win.updates.cancel()
        self.refresh()

    def _primary(self):
        if self.win.updates.state == UM.AVAILABLE:
            self.win.download_update()
        elif self.win.updates.state == UM.READY:
            self.win.restart_and_install()

    def refresh(self):
        m = self.win.updates
        version, state = m.version_text(), m.state
        counting = bool(m.is_counting())
        self.title.setText({UM.AVAILABLE: f"Version v{version} is available", UM.PREPARING: f"Preparing version v{version}",
                            UM.READY: f"Version v{version} is ready to install", UM.INSTALLING: f"Installing version v{version}"}.get(state, ""))
        self.body.setText({UM.AVAILABLE: "Your counts, profiles and settings are kept. You can keep counting while it downloads.",
                           UM.PREPARING: "You can keep counting and close this window. This continues in the background.",
                           UM.READY: "The app will close, install the update and reopen by itself. Your counts, profiles and settings are kept."
                                     + (" Counting will stop first and your counts will be saved." if counting else ""),
                           UM.INSTALLING: m.installing_text}.get(state, ""))
        release = m.update.release if m.update is not None else None
        show_notes = state in (UM.AVAILABLE, UM.READY) and release is not None
        self.notes.setVisible(show_notes)
        if show_notes and self.notes.toPlainText() != release.notes:
            self.notes.setPlainText(release.notes)
        preparing = state == UM.PREPARING
        downloading = preparing and m.status.stage == "downloading" and m.status.total > 0
        self.stage.setVisible(preparing)
        self.stage.setText(m.stage_text() if preparing else "")
        self.progress.setVisible(preparing or state == UM.INSTALLING)
        self.progress.setRange(0, 100 if downloading else 0)             # a moving bar for steps with no meaningful percentage
        if downloading:
            self.progress.setValue(UM.percent(m.status))
        self.detail.setVisible(preparing)
        self.detail.setText(UM.progress_text(m.status) if preparing else "")
        self.problem.setVisible(bool(m.error or m.notice))
        self.problem.setText(m.error or m.notice)
        self.problem.setStyleSheet(f"color: {theme.REJECT if m.error else theme.MUTED};")
        self.page.setVisible(state in (UM.AVAILABLE, UM.READY) and release is not None and bool(release.page_url))
        self.later.setVisible(state in (UM.AVAILABLE, UM.READY))
        self.cancel_button.setVisible(preparing)
        self.cancel_button.setEnabled(not m.cancelling)
        self.primary.setVisible(state in (UM.AVAILABLE, UM.READY, UM.INSTALLING))
        self.primary.setEnabled(state in (UM.AVAILABLE, UM.READY))
        self.primary.setText({UM.AVAILABLE: "Try again" if m.error else "Download && Install", UM.READY: "Restart && Install"}.get(state, "Please wait..."))


def navigation_icon(name, color):
    icon = QPixmap(str(ASSETS / f"nav_{name}.png"))
    if not icon.isNull():
        if name != "discord":  # Preserve the official white Discord symbol.
            with QPainter(icon) as painter:
                painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
                painter.fillRect(icon.rect(), QColor(color))
        icon.setDevicePixelRatio(2)
    return icon


class NavigationIndicator(QLabel):
    def __init__(self):
        super().__init__()
        self._amount = 0.0
        self._motion = QVariantAnimation(self)
        self._motion.setDuration(150)
        self._motion.setEasingCurve(QEasingCurve.OutCubic)
        self._motion.valueChanged.connect(self._set_amount)

    def set_selected(self, selected):
        target = float(selected)
        self._motion.stop()
        if self.isVisible() and theme.animations_enabled() and self._amount != target:
            self._motion.setStartValue(self._amount)
            self._motion.setEndValue(target)
            self._motion.start()
        else:
            self._set_amount(target)

    def _set_amount(self, amount):
        self._amount = amount
        self.update()

    def paintEvent(self, event):
        if self._amount:
            color = QColor(theme.SURFACE_3)
            color.setAlphaF(self._amount)
            with QPainter(self) as painter:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(self.rect()), 16, 16)
        super().paintEvent(event)


class NavigationDestination(QPushButton):
    def __init__(self, icon, caption):
        super().__init__()
        self.setObjectName("navDestination")
        self.setFocusPolicy(Qt.TabFocus)
        self.setCheckable(True)
        self.setAccessibleName(caption)
        self.setFixedSize(80, 64)
        self._icons = (navigation_icon(icon, theme.MUTED), navigation_icon(icon, theme.TEXT))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 4)
        layout.setSpacing(3)
        self.indicator = NavigationIndicator()
        self.indicator.setAlignment(Qt.AlignCenter)
        self.indicator.setFixedSize(56, 32)
        text = label(caption, "navCaption")
        for child in (self.indicator, text):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(child, 0, Qt.AlignHCenter)
        self.toggled.connect(self._update_indicator)
        self._update_indicator(False)

    def _update_indicator(self, selected):
        self.indicator.setPixmap(self._icons[int(selected)])
        self.indicator.set_selected(selected)


class MainWindow(QMainWindow):
    auto_discord_result = Signal(str)
    replay_result = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self._tray = None
        self._tray_exit = False
        self._hidden_in_tray = False
        self.resize(1120, 800)
        self.setMinimumSize(920, 640)
        self.window_file = None                 # set by restore_layout() for the real app window only (not tests / self-test)
        self.worker = Worker()
        self._workers_pending = set()
        self._pending_resets = set()
        self._match_tail = None
        self._mode_tail = None
        self._detected_mode = None
        self._match_events = deque()
        self._next_live_share = time.monotonic() + discord_group.webhook_sync_delay(None, base=0)
        self._match_active = False
        self._match_fingerprint = ""
        self._pending_match_fingerprint = ""
        self._match_action = None
        self._match_stop_acks = set()
        self._worker_failed = False
        self.updates = UM.UpdateManager(is_counting=self.workers_active)
        self.update_dialog = None
        self._update_ticks = 0
        self._install_pending = False           # counting is being stopped so that an update can be installed
        self._result_watch_until = None         # started by the updater: wait for its verdict before saying anything
        self.mode = "stopped"
        self.counts = (0, 0)
        self.primary_counts = (0, 0)
        self.primary_types = {}
        self.entries = deque(maxlen=LOG_LIMIT)
        self.connected = None
        self.started_at = None
        self.last_status = None
        self.error_seen = False
        self.quit_when_stopped = False
        self.recording = None                   # the worker's current capture ("capture" message), None = not recording
        self.stream_state = None                # Livestream URL connection state ("stream" messages), None = not a stream
        self.marks_this_session = 0
        self.hotkey = None
        self.sources = []
        self._source_scan = None
        self._state_mtime = None
        try:
            self.preview_reader = PreviewReader(block_name(uuid.uuid4().hex[:12]))
        except Exception:
            self.preview_reader = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self._stop_timeout)
        self.match_timer = QTimer(self)
        self.match_timer.timeout.connect(self.poll_match_log)
        self.replay_result.connect(self.apply_verified_replay)
        self.auto_discord_result.connect(lambda result: self.add_log({"type": "info", "text": result}))
        self.slow = QTimer(self)
        self.slow.timeout.connect(self._tick_slow)
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._tick_preview)
        self._mark_flash = QTimer(self)                     # the "Marked" confirmation stays visible a few seconds
        self._mark_flash.setSingleShot(True)
        self._mark_flash.timeout.connect(self.update_recording_ui)
        self.obs_link = TextOutputLink().start()            # optional OBS WebSocket output; off unless configured
        self._obs_version = -1
        self._obs_logged = None                             # last OBS WebSocket state written to the activity log

        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.pages = AnimatedPages()
        self.dashboard = Dashboard(self)
        self.overlay = OverlayPage(self)
        self.settings_page = SettingsPage(self)
        self.help = HelpPage(self)
        self.group_role = load_cfg()[0]["app"]["group_role"]
        if not self.group_role:
            try:
                self.group_role = "host" if discord_summary.load() is not None else "guest"
            except (OSError, ValueError):
                self.group_role = "guest"
        self.group = GroupPage(self)
        self.discord = DiscordPage(self)
        self.group.attach_discord(self.discord)
        self.setup = SetupPage(self)
        self.stats = StatsPage(self)
        for page in (self.dashboard, self.overlay, self.settings_page, self.help, self.group, self.setup, self.stats):
            self.pages.addWidget(page)
        outer.addWidget(self._navigation_rail())
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self.pages, 1)
        self.update_link = button("", "link", on_click=self.open_update_dialog)
        self.update_link.hide()
        content.addWidget(self.update_link, 0, Qt.AlignRight)
        outer.addLayout(content, 1)
        self.setCentralWidget(root)

        self.load_home_options()
        self.dashboard._panels()
        self.fill_profiles()
        self.set_mode("stopped")
        self.read_state(force=True)
        if not C.counter_is_running(paths.APP_DIR / load_cfg()[0]["state"]["path"]):
            self.write_obs_files(quiet=True)          # a fresh unzip/update carries zero placeholders
        self.apply_obs_settings()
        self.overlay.load()
        self.set_match_automation(load_cfg()[0]["app"]["auto_match_lifecycle"])
        self.update_mode_tracking()
        self.settings_page.load()
        rec = self.dashboard.rec_mode
        rec.setCurrentIndex(max(rec.findData(load_cfg()[0]["capture"]["mode"]), 0))
        rec.currentIndexChanged.connect(lambda _: self.recording_changed())
        self.update_recording_ui()
        self.dashboard.reload_screen_overlay()
        self.group.load()
        self.refresh_sources()
        cfg, warnings = load_cfg()
        for warning in warnings:
            self.add_log({"type": "warn", "text": warning})

        self.timer.start(50)
        self.slow.start(1000)

    def save_app_option(self, key, value):
        try:
            C.update_config_file(config_file(), "app", {key: value})
        except (C.UserError, OSError) as error:
            show_error(self, f"Could not save the preference: {error}")

    def load_home_options(self):
        cfg = load_cfg()[0]["app"]
        dashboard = self.dashboard
        mode, boost = dashboard.token_mode, dashboard.token_boost
        mode.setCurrentIndex(max(0, mode.findData("Auto") if cfg["token_mode"] == "Auto"
                                 else mode.findText(cfg["token_mode"])))
        mode.currentIndexChanged.connect(self.save_game_mode)
        boost.setCurrentIndex(max(0, boost.findText(cfg["token_boost"])))
        boost.currentTextChanged.connect(lambda value: self.save_app_option("token_boost", value))
        for button, key in ((dashboard.show_log, "show_activity"), (dashboard.show_preview, "live_preview")):
            button.setChecked(cfg[key])
            button.toggled.connect(lambda value, key=key: self.save_app_option(key, value))

    def save_game_mode(self, _):
        box = self.dashboard.token_mode
        self.save_app_option("token_mode", box.currentData() or box.currentText())
        self.update_mode_tracking()

    def current_game_mode(self):
        box = self.dashboard.token_mode
        return (self._detected_mode or "Unknown") if box.currentData() == "Auto" else box.currentText()

    def set_detected_mode(self, mode):
        self._detected_mode = mode
        box = self.dashboard.token_mode
        index = box.findData("Auto")
        box.setItemText(index, f"Auto · {mode}" if mode else "Auto · waiting")

    def update_mode_tracking(self):
        if self._match_tail is not None:
            self._mode_tail = None
            return
        if self.dashboard.token_mode.currentData() != "Auto":
            self._mode_tail = None
            self.set_detected_mode(None)
            self.match_timer.stop()
        elif self._mode_tail is None:
            try:
                self._mode_tail = match_lifecycle.LogTail(match_lifecycle.local_log_path())
            except OSError:
                self.set_detected_mode(None)
                return
            self.match_timer.start(250)

    # ---- layout
    def _navigation_rail(self):
        rail = QFrame()
        rail.setObjectName("navigationRail")
        rail.setFixedWidth(96)
        column = QVBoxLayout(rail)
        column.setContentsMargins(0, 44, 0, 12)
        column.setSpacing(4)
        self.destinations = QButtonGroup(rail)
        destinations = ((self.pages.indexOf(self.setup), "settings", "Setup"), (0, "dashboard", "Dashboard"), (1, "overlay", "Overlay"), (4, "group", "Group"),
                        (self.pages.indexOf(self.stats), "stats", "Stats"), (2, "settings", "Settings"), (3, "help", "Help"))
        for index, icon, name in destinations:
            destination = NavigationDestination(icon, name)
            self.destinations.addButton(destination, index)
            column.addWidget(destination, 0, Qt.AlignHCenter)
        self.destinations.button(0).setChecked(True)
        self.destinations.idClicked.connect(self.go_to)
        column.addStretch(1)
        return rail

    def go_to(self, index):
        destination = self.destinations.button(index)
        if destination is not None and self.pages.currentIndex() != index:
            destination.setChecked(True)
            self._page_changed(index)

    def set_group_role(self, role):
        self.group_role = role
        self.group.set_bot_section_role(role)
        self.group.apply_match_settings()
        if role == "guest":
            self.discord.stop_bot()
        elif self.discord.auto_start.isChecked() and not self._install_pending and self.updates.state != UM.INSTALLING:
            self.discord.start_bot()

    def _page_changed(self, index):
        self.pages.setCurrentIndex(index)
        self.preview_visibility_changed()
        if index == 1:
            self.overlay.load()
        elif index == 2:
            self.settings_page.load()
        elif self.pages.currentWidget() is self.stats:
            self.stats.load()
        elif self.pages.currentWidget() is self.group:
            self.group.load()
            if self.group_role == "host":
                self.discord.load()

    # ---- sources
    def refresh_sources(self):
        if self._source_scan is not None and self._source_scan.is_alive():
            return
        self.dashboard.refresh.setEnabled(False)
        self.dashboard.source_hint.setText("Looking for video devices...")
        self._scan_result = None

        def scan():
            from fortnite_chest_counter.input import video_sources as sources
            devices, monitors, windows = [], [], []
            try:
                devices = [n for n, k in sources.list_video_devices() if k in ("video", "none")]
            except Exception:
                pass
            try:
                monitors = sources.list_monitors()
            except Exception:
                pass
            try:
                windows = sources.list_obs_windows()
            except Exception:
                pass
            self._scan_result = (devices, monitors, windows)

        self._source_scan = threading.Thread(target=scan, daemon=True)
        self._source_scan.start()

    def _fill_sources(self, devices, monitors, windows=()):
        self._monitor_positions = monitors
        live = self.profile_live()
        # Most used first (v0.2.2): a livestream link and a screen need no setup; OBS for streamers' own scene; then the
        # less tested inputs. (The order is only presentation: every entry stays fully supported.)
        items = [("Livestream URL   (Twitch / YouTube - paste the link)", {"source": "stream"})]
        for i, (_, _, w, h) in enumerate(monitors, 1):
            items.append((f"Screen {i}   ·   {w} x {h}", {"source": "desktop", "monitor": i}))
            items.append((f"Screen {i} HDR → SDR   ·   {w} x {h}", {"source": "desktop-hdr", "monitor": i}))
        items.append(("OBS Virtual Camera", {"source": "obs-camera"}))
        if not self.dashboard.url.text():
            self.dashboard.url.setText(live["url"])
            stab = self.dashboard.stability
            stab.setCurrentIndex(max(stab.findData(live["stream_stability"]), 0))
            stab.currentIndexChanged.emit(stab.currentIndex())        # fill the explanation
        names = [n for n in devices if n != "OBS Virtual Camera"]
        if live["source"] == "device" and live["device"] not in names and live["device"] != "OBS Virtual Camera":
            names.append(live["device"])
        for name in names:
            missing = "" if name in devices else "   (not found right now)"
            items.append((f"Capture device:  {name}{missing}   · less tested", {"source": "device", "device": name}))
        titles = [title for title, _ in windows]
        for title, minimized in windows:
            items.append((f"OBS projector:  {title}" + ("   (minimized - restore it)" if minimized else "") + "   · less tested",
                          {"source": "window", "window": title}))
        if live["source"] == "window" and live["window"] and live["window"] not in titles:
            items.append((f"OBS projector:  {live['window']}   (not open right now)   · less tested",
                          {"source": "window", "window": live["window"]}))
        dev_video = os.environ.get("FCC_DEV_VIDEO")
        if dev_video:
            items.append((f"Test video (real time):  {Path(dev_video).name}", {"source": "file", "video": dev_video}))
        current = self.dashboard.source.currentData()
        self.dashboard.source.blockSignals(True)
        self.dashboard.source.clear()
        for text, data in items:
            self.dashboard.source.addItem(text, data)
        wanted = current or {"source": live["source"], "device": live["device"], "monitor": live["monitor"],
                             "window": live["window"]}
        index = 0
        for i, (_, data) in enumerate(items):
            if data["source"] == wanted.get("source") and all(data[k] == wanted.get(k) for k in data if k != "source"):
                index = i
                break
        self.dashboard.source.setCurrentIndex(index)
        self.dashboard.source.blockSignals(False)
        self.dashboard.update_source_summary()
        self.dashboard.position_screen_overlay()
        self.dashboard.url_row.setVisible((self.dashboard.source.currentData() or {}).get("source") == "stream")
        self._obs_camera_found = "OBS Virtual Camera" in devices
        self._projectors_found = bool(windows)
        self.update_source_hint()
        self.dashboard.refresh.setEnabled(not self.workers_active())

    def update_source_hint(self):
        """One short line for the selected source: what it needs (and, for the less tested ones, that they are)."""
        kind = (self.dashboard.source.currentData() or {}).get("source")
        found = getattr(self, "_obs_camera_found", True)
        text = {
            "stream": "",
            "desktop": "Counts what is on that screen: play the stream (or the game) FULLSCREEN there. A small player inside "
                       "a web page is too small to read.",
            "desktop-hdr": "Use this for an HDR screen that looks too bright in Live preview. Keep the game or stream "
                           "fullscreen on that screen.",
            "obs-camera": "For your own OBS scene with the gameplay: click Start Virtual Camera in OBS." if found else
                          "OBS Virtual Camera was not found: open OBS Studio (28 or newer), click Start Virtual Camera, then "
                          "Refresh.",
            "device": "Reads the device directly. Less tested: not yet with a physical capture card. Most cards work with one "
                      "program at a time - if OBS uses it, pick OBS Virtual Camera instead.",
            "window": "Reads an OBS projector window (keep it open, not minimized). Less tested: not yet with a real OBS "
                      "projector.",
        }.get(kind, "")
        if kind == "obs-camera" and not getattr(self, "_projectors_found", True):
            text += (" Virtual Camera busy, or the capture card used by OBS? In OBS right-click your gameplay source > "
                     "Windowed Projector (Source), then click Refresh.")
        self.dashboard.source_hint.setText(text)

    def select_source(self, wanted):
        """Select the source list entry that matches `wanted` (a [live]-style dict). -> True if one was found."""
        box = self.dashboard.source
        for i in range(box.count()):
            data = box.itemData(i) or {}
            if data.get("source") == wanted.get("source") and all(data[k] == wanted.get(k) for k in data if k != "source"):
                box.setCurrentIndex(i)
                return True
        return False

    # ---- count profiles (profiles.py): one counts at a time, each with its own counts and video source
    def profile_live(self):
        """config.toml [live] with the active profile's remembered source on top."""
        cfg, _ = load_cfg()
        live = dict(cfg["live"])
        try:
            reg = profiles.load(cfg["profile"]["state_base"])
            remembered = profiles.active(reg).get("source") or {}
        except (OSError, profiles.ProfileError):
            remembered = {}
        if remembered.get("source"):
            live.update(remembered)
        return live

    def profile_base(self):
        return load_cfg()[0]["profile"]["state_base"]

    def fill_profiles(self):
        base = self.profile_base()
        reg = profiles.load(base)
        box = self.dashboard.profile
        box.blockSignals(True)
        box.clear()
        for p in reg["profiles"]:
            box.addItem(p["name"], p["id"])
        box.setCurrentIndex(max(box.findData(reg["active"]), 0))
        box.blockSignals(False)
        self.dashboard.update_source_summary()
        self.dashboard.profile_delete.setEnabled(reg["active"] != profiles.DEFAULT_ID and not self.workers_active())
        name = profiles.active(reg)["name"]
        self.setWindowTitle(APP_NAME if len(reg["profiles"]) == 1 else f"{APP_NAME}  -  {name}")
        return reg

    def _profile_busy(self):
        """A counter (this window's or a console one) is writing the active profile's counts right now."""
        if self.workers_active():
            return True
        return C.counter_is_running(load_cfg()[0]["state"]["path"])

    def switch_profile(self, index):
        wanted = self.dashboard.profile.itemData(index)
        cfg, _ = load_cfg()
        if wanted is None or wanted == cfg["profile"]["id"]:
            return
        if self._profile_busy():
            show_error(self, "Stop counting before switching to another profile.")
            self.fill_profiles()
            return
        try:
            profiles.set_active(cfg["profile"]["state_base"], wanted)
        except (OSError, profiles.ProfileError) as error:
            show_error(self, f"Could not switch the profile: {error}")
            self.fill_profiles()
            return
        self.profile_changed()

    def profile_changed(self):
        """After a switch / new profile: its counts, its OBS text, its video source."""
        reg = self.fill_profiles()
        self.dashboard.clear_recent()
        self.read_state(force=True)
        self.write_obs_files(quiet=True)              # OBS shows the selected profile
        live = self.profile_live()
        self.select_source({"source": live["source"], "device": live["device"], "monitor": live["monitor"],
                            "window": live["window"]})
        self.dashboard.url.setText(live["url"])
        stab = self.dashboard.stability
        stab.setCurrentIndex(max(stab.findData(live["stream_stability"]), 0))
        self.dashboard.url_row.setVisible((self.dashboard.source.currentData() or {}).get("source") == "stream")
        self.add_log({"type": "info", "text": f"Profile: {profiles.active(reg)['name']}  -  CERTAIN {fmt(self.counts[0])}"
                                              f" / APPROX {fmt(self.counts[1])}. OBS now shows this profile."})

    def new_profile(self):
        if self._profile_busy():
            show_error(self, "Stop counting before making a new profile.")
            return
        name, ok = prompt_name(self, "New profile", "Each profile keeps its own counts and video source.", "Create")
        if not ok:
            return
        try:
            profiles.create(self.profile_base(), name)
        except (OSError, profiles.ProfileError) as error:
            show_error(self, str(error))
            return
        self.profile_changed()

    def rename_profile(self):
        cfg, _ = load_cfg()
        current = cfg["profile"]
        name, ok = prompt_name(self, "Rename profile", "Enter a new name for this profile.", "Rename", current["name"])
        if not ok or name.strip() == current["name"]:
            return
        try:
            profiles.rename(current["state_base"], current["id"], name)
        except (OSError, profiles.ProfileError) as error:
            show_error(self, str(error))
            return
        self.fill_profiles()

    def delete_profile(self):
        cfg, _ = load_cfg()
        current = cfg["profile"]
        if current["id"] == profiles.DEFAULT_ID or self._profile_busy():
            return
        if not ask(self, "Delete profile?", f'Delete the profile "{current["name"]}" (CERTAIN {fmt(self.counts[0])} / '
                                            f'APPROX {fmt(self.counts[1])})?\n\nIt disappears from the list. Its counts '
                                            f"are kept in a backup file in state\\profiles\\deleted.", "Delete", danger=True):
            return
        try:
            profiles.delete(current["state_base"], current["id"])
        except (OSError, profiles.ProfileError) as error:
            show_error(self, str(error))
            return
        self.profile_changed()

    # ---- counting
    def set_match_automation(self, enabled):
        if enabled == (self._match_tail is not None):
            return
        self._match_events.clear()
        self._match_active = False
        self._match_fingerprint = ""
        self._pending_match_fingerprint = ""
        self._match_action = None
        self._match_stop_acks.clear()
        if not enabled:
            self.match_timer.stop()
            self._match_tail = None
            self.set_detected_mode(None)
            self.update_mode_tracking()
            return
        try:
            self._match_tail = match_lifecycle.LogTail(match_lifecycle.local_log_path())
        except OSError:
            self.match_automation_failed("Could not open the local Fortnite log.")
            return
        self._mode_tail = None
        self.set_detected_mode(None)
        self.match_timer.start(250)

    def match_automation_failed(self, message):
        self.set_match_automation(False)
        self.settings_page.auto_match.setChecked(False)
        try:
            C.update_config_file(config_file(), "app", {"auto_match_lifecycle": False})
        except (C.UserError, OSError):
            message += " The setting could not be turned off in config.toml."
        message += " Match automation is off for this session."
        self.dashboard.show_banner(message)
        self.add_log({"type": "warn", "text": message})

    def poll_match_log(self):
        if (self._match_action == "finish" or self.quit_when_stopped
                or self._install_pending or self.updates.state == UM.INSTALLING):
            return
        tail = self._match_tail or self._mode_tail
        if tail is None:
            return
        try:
            restarted, events = tail.poll()
        except OSError:
            if self._match_tail is not None:
                self.match_automation_failed("Could not read the local Fortnite log.")
            else:
                self.set_detected_mode(None)
            return
        if self._match_tail is None:
            if restarted:
                self.set_detected_mode(None)
            for event in events:
                if event != "finish":
                    self.set_detected_mode(event[2] if isinstance(event, tuple) and len(event) == 3 else None)
            return
        if restarted:
            self.set_detected_mode(None)
            if self._match_active:
                self.match_automation_failed("Fortnite's log restarted during a match. Finish the match manually.")
                return
            self._match_events.clear()
            self._match_action = None
            self._pending_match_fingerprint = ""
        self._match_events.extend(events)
        while self._match_events:
            event = self._match_events.popleft()
            marker, fingerprint = event[:2] if isinstance(event, tuple) else (event, "")
            detected_mode = event[2] if isinstance(event, tuple) and len(event) == 3 else None
            if not self._match_active:
                # A start followed by a lobby in unread data never gets a reset.
                self._match_action = "start" if marker == "start" else None
                self._pending_match_fingerprint = fingerprint
                if marker == "start":
                    self.set_detected_mode(detected_mode)
            elif marker == "finish":
                self._match_action = "finish"
                self._match_stop_acks.clear()
                self.stop_counting()
                return
            elif fingerprint != self._match_fingerprint:
                # The counter has crossed a match boundary without a verified finish.
                self._match_fingerprint = ""
        if (self._match_action == "start" and self._match_tail.caught_up
                and self._source_scan is None and self.dashboard.source.currentData()
                and QApplication.activeModalWidget() is None):
            if self.workers_active():
                self.stop_counting()
            else:
                self.complete_match_action()

    def complete_match_action(self):
        action, self._match_action = self._match_action, None
        if action == "start":
            fingerprint, self._pending_match_fingerprint = self._pending_match_fingerprint, ""
            if self.quit_when_stopped or self._install_pending:
                return
            try:
                started = self.start_counting(before_start=self.reset_match_source)
            except (C.UserError, OSError) as error:
                self.match_automation_failed(f"Could not reset the counter. {error}")
                return
            if not started:
                self.stop_counting()
                self.match_automation_failed("Could not start the configured source.")
                return
            self._match_active = True
            source = self.dashboard.source.currentData() or {}
            self._match_fingerprint = fingerprint if source.get("source") in ("desktop", "desktop-hdr") else ""
        elif action == "finish":
            self._match_active = False
            fingerprint, self._match_fingerprint = self._match_fingerprint, ""
            try:
                saved = self.save_current_match(session_fingerprint=fingerprint, verify_replay=bool(fingerprint))
            except (C.UserError, OSError, ValueError) as error:
                self.match_automation_failed(f"Could not save the match. {error}")
                return
            if saved:
                self.add_log({"type": "info", "text": "Battle Royale match saved to Group."})
                if self.pages.currentWidget() is self.group:
                    self.group.load()

    def reset_match_source(self):
        primary = self._cfg_for_actions()
        if not primary["state"]["persist"]:
            raise C.UserError("Enable saving counter state before recording matches.")
        configs = (primary,)
        backups = {}
        recovery = []
        with ExitStack() as guards:
            for cfg in configs:
                C.preflight(cfg, needs_video=False)
                guards.enter_context(C.SingleInstance(cfg["state"]["path"]))
            try:
                for cfg in configs:
                    path = Path(cfg["state"]["path"])
                    try:
                        prior = path.read_bytes()
                    except FileNotFoundError:
                        backups[path] = None
                    else:
                        backup = path.with_name(f"{path.name}.{uuid.uuid4().hex}.before-reset")
                        backup.write_bytes(prior)
                        backups[path] = backup
                try:
                    for cfg in configs:
                        path = Path(cfg["state"]["path"])
                        reset = C.set_counts(cfg, 0, 0, "reset")
                        if json.loads(path.read_text(encoding="utf-8")) != reset.state:
                            raise OSError("The reset counter state was not saved.")
                except (C.UserError, OSError, ValueError) as error:
                    for path, backup in backups.items():
                        try:
                            if backup is None:
                                path.unlink(missing_ok=True)
                            else:
                                os.replace(backup, path)
                        except OSError:
                            recovery.append(str(backup or path))
                    message = str(error)
                    if recovery:
                        message += " Could not restore the state automatically. Recovery files: " + ", ".join(recovery)
                    raise C.UserError(message) from error
            finally:
                for backup in backups.values():
                    if backup is not None and str(backup) not in recovery:
                        try:
                            backup.unlink(missing_ok=True)
                        except OSError:
                            pass
                self.read_state(force=True)
        self.dashboard.clear_recent()

    def workers_active(self):
        return bool(self._workers_pending) or self.worker.running()

    def toggle_counting(self):
        if self.workers_active():
            self.stop_counting()
        else:
            self.start_counting()

    @staticmethod
    def source_args(data):
        kind = data["source"]
        argv = ["--source", kind]
        for key in ("device", "monitor", "window", "video"):
            if key in data:
                argv += [f"--{key}", str(data[key])]
        if kind == "file" and os.environ.get("FCC_DEV_VIDEO_START"):
            argv += ["--start", os.environ["FCC_DEV_VIDEO_START"]]
        return argv

    def start_counting(self, *, before_start=None):
        data = self.dashboard.source.currentData()
        if not data or self.workers_active() or self._install_pending or self.updates.state == UM.INSTALLING:
            return
        cfg, _ = load_cfg()
        argv = ["live", "--gui", "--profile", cfg["profile"]["id"]]
        if self.preview_reader is not None:
            argv += ["--preview", self.preview_reader.name]
        if data["source"] == "file":                                 # developers: FCC_DEV_VIDEO (+ FCC_DEV_VIDEO_START)
            argv += self.source_args(data)
        else:
            values = {"source": data["source"]}
            if data["source"] == "device":
                values["device"] = data["device"]
            if data["source"] in ("desktop", "desktop-hdr"):
                values["monitor"] = data["monitor"]
            if data["source"] == "window":
                values["window"] = data["window"]
            if data["source"] == "stream":
                url = self.dashboard.url.text().strip()
                if not url.lower().startswith(("http://", "https://")) or any(c.isspace() for c in url):
                    self.dashboard.show_capture.setChecked(True)
                    self.dashboard.url.setFocus()
                    show_error(self, "Paste the full link of the live stream, for example\n"
                                     "https://www.twitch.tv/channel  or  https://www.youtube.com/watch?v=...")
                    return
                values["url"] = url
                values["stream_stability"] = self.dashboard.stability.currentData()
            try:
                C.update_config_file(config_file(), "live", values)   # the console version starts the same way
                profiles.remember_source(cfg["profile"]["state_base"], cfg["profile"]["id"], values)
            except (C.UserError, OSError, profiles.ProfileError) as error:
                show_error(self, f"Could not save the video source choice: {error}")
                return
        if before_start is not None:
            before_start()
        if data["source"] == "stream" and url != cfg["live"]["url"]:
            self.dashboard.clear_recent(source=1)
        self.dashboard.banner.hide()
        self.error_seen = False
        self._worker_failed = False
        self.connected = None
        self.last_status = None
        if self.preview_reader is not None:
            self.preview_reader.discard()
        try:
            self.worker.start(argv)
        except OSError as error:
            show_error(self, f"The counter could not start ({error}). Extract the whole zip again and keep every file "
                             "and folder together.")
            return
        if before_start is None:
            self._match_fingerprint = ""
        self._workers_pending.add("primary")
        whose = f" (profile {cfg['profile']['name']})" if self.dashboard.profile.count() > 1 else ""
        self.add_log({"type": "info", "text": f"Starting{whose}: {self.dashboard.source.currentText().split('   ')[0]} ..."})
        self.set_mode("connecting")
        return True

    def stop_counting(self):
        if self.workers_active() and self.mode != "stopping":
            self.set_mode("stopping")
            self.worker.stop()
            self.stop_timer.start(30000)   # a diagnostic recording may need a few seconds to finish

    def _stop_timeout(self):
        if self.mode == "stopping":
            self.worker.kill()          # counts are saved after every chest; this only skips the final summary

    def set_mode(self, mode):
        self.mode = mode
        d = self.dashboard
        text, color = {"stopped": ("Not counting", theme.MUTED), "connecting": ("Connecting", theme.INFO),
                       "counting": ("Counting", theme.CERTAIN), "stopping": ("Stopping", theme.APPROX),
                       "error": ("Not counting", theme.REJECT)}[mode]
        d.set_pill(text, color)
        running = mode in ("connecting", "counting", "stopping")
        d.start.setText({"connecting": "Cancel", "counting": "Stop counting", "stopping": "Stopping..."}.get(mode, "Start counting"))
        restyle(d.start, "danger" if mode in ("connecting", "counting") else "primary")
        d.start.setEnabled(mode != "stopping")
        d.source.setEnabled(not running)
        for widget in (d.profile, d.profile_new, d.profile_rename):
            widget.setEnabled(not running)
        d.profile_delete.setEnabled(not running and d.profile.currentData() != profiles.DEFAULT_ID)
        d.url.setEnabled(not running)
        d.stability.setEnabled(not running)
        d.refresh.setEnabled(not running)
        d.analyse.setEnabled(not running)
        if mode == "connecting":
            self.marks_this_session = 0
        if not running:
            self.recording = None
        self.update_recording_ui()
        if mode == "counting":
            self.started_at = time.monotonic()
        if not running:
            self.started_at = None
            self.stream_state = None
            d.metrics.hide()
            d.preview.view.setStyleSheet("")
            d.preview.clear()
        self.preview_visibility_changed()

    def _tick(self):
        if self._source_scan is not None and self._scan_result is not None:
            devices, monitors, windows = self._scan_result
            self._scan_result = None
            self._source_scan = None
            self._fill_sources(devices, monitors, windows)
        for message in self.worker.drain():
            self.handle(message)
        version, status = self.obs_link.status()
        if version != self._obs_version:
            self._obs_version = version
            self.obs_status_changed(status)
        self._update_ticks += 1
        if self._update_ticks % 4 == 0 and self.updates.poll():
            self.update_changed()

    # ---- updates (update_manager.py holds the state; checking and preparing run on their own threads)
    def after_start(self, installing=False):
        """Called once the window is up. Not from the constructor, so the self-test can never touch the network."""
        try:
            update_install.clear_orphaned_journal()
            if installing:                             # the updater started this copy to prove itself: its verdict comes later
                self._result_watch_until = time.monotonic() + RESULT_WATCH_SECONDS
                return
            self.show_update_result()
            if load_cfg()[0]["update"]["check_automatically"]:
                QTimer.singleShot(AUTO_CHECK_DELAY_MS, self.auto_check)
        except Exception as error:                     # updates must never stop the app from opening
            self.add_log({"type": "warn", "text": f"Could not check the last update ({type(error).__name__}: {error})."})
        if not installing and self.group_role == "host" and self.discord.auto_start.isChecked():
            self.discord.start_bot()
        if not installing:
            self.discord.retry_group_outbox()

    def auto_check(self):
        if self.updates.check(manual=False):
            self.update_changed()

    def check_for_updates(self):
        self.updates.check(manual=True)
        self.update_changed()

    def show_update_result(self):
        """What the last installation did, once. -> True if there was a result."""
        result = update_install.last_result()
        if result is None:
            if self._result_watch_until is None and update_install.transaction_left_open():
                self.dashboard.show_notice("An update did not finish. Close the app and open it again from its .exe. If this message comes back, "
                                           "extract the latest zip over the program folder. Your counts are safe.", theme.REJECT)
            return False
        self._result_watch_until = None
        notice = UM.describe_result(result, __version__)
        update_install.acknowledge_result()
        if notice:
            level, text = notice
            self.dashboard.show_notice(text, {"success": theme.CERTAIN, "rolled_back": theme.APPROX}.get(level, theme.REJECT))
            self.add_log({"type": "info" if level == "success" else "warn", "text": text})
        QTimer.singleShot(8000, update_install.cleanup_finished)
        return True

    def update_changed(self):
        m = self.updates
        text = m.indicator_text()
        self.update_link.setText(text)
        self.update_link.setVisible(bool(text))
        self.settings_page.update_status.setText(m.check_message)
        self.settings_page.check_button.setEnabled(not m.busy())
        self.dashboard.start.setEnabled(m.state != UM.INSTALLING and self.mode != "stopping")
        if self.update_dialog is not None:
            self.update_dialog.refresh()
        if m.take_open_request():
            self.open_update_dialog()
        if m.take_install_started():
            self.close()                               # the installer is waiting for this window to go

    def open_update_dialog(self):
        if self.update_dialog is None:
            self.update_dialog = UpdateDialog(self)
        self.update_dialog.refresh()
        self.update_dialog.show()
        self.update_dialog.raise_()

    def download_update(self):
        if self.updates.start_preparing(UM.bandwidth_policy(load_cfg()[0]["update"])):
            self.update_changed()

    def _install_blocker(self):
        """Something that must finish first, so nothing still runs from the program folder when the installer starts."""
        if QApplication.activeModalWidget() is not None:
            return "Close the other open window first, then try again."
        if self.help._thread is not None and self.help._thread.is_alive():
            return "Wait for the diagnostics to finish, then try again."
        return None

    def restart_and_install(self):
        m = self.updates
        if m.state != UM.READY or self._install_pending:
            return
        blocker = self._install_blocker()
        if blocker:
            m.refuse_install(blocker)
            self.update_changed()
            return
        if not self.workers_active():
            self._launch_installer()
            return
        if not ask(self, "Stop counting and install?", f"Counting will stop and your counts will be saved. The app will then close, install v{m.version_text()} "
                                                       "and reopen by itself.", "Stop counting and install"):
            return
        self._install_pending = True
        m.begin_stopping()
        self.stop_counting()
        QTimer.singleShot(INSTALL_STOP_DEADLINE_MS, self._install_stop_deadline)
        self.update_changed()

    def _install_stop_deadline(self):
        if self._install_pending:
            self._refuse_install_after_stop()

    def _refuse_install_after_stop(self):
        self._install_pending = False
        self.updates.refuse_install("Counting did not stop cleanly, so the update was not installed. Your counts are safe.")
        if not self.workers_active():
            self.set_mode("error" if self.error_seen else "stopped")
        self.update_changed()

    def _launch_installer(self):
        cfg, _ = load_cfg()
        state_folder = Path(cfg["profile"]["state_base"]).parent
        try:
            state_dir = state_folder.resolve().relative_to(paths.APP_DIR.resolve()).as_posix()
        except ValueError:
            state_dir = str(state_folder)                  # kept outside the program folder by config.toml
        data_paths = [state_folder, *(paths.APP_DIR / cfg[section][key] for section, key in (("state", "obs_dir"), ("logging", "dir"), ("capture", "dir")))]
        self.updates.install(state_dir, data_paths)
        self.update_changed()

    # ---- OBS WebSocket output (obs/link.py runs it in its own thread; this only configures and shows it)
    def apply_obs_settings(self, reconnect=False):
        cfg, _ = load_cfg()
        ws = cfg["obs_websocket"]
        overlay = (paths.APP_DIR / cfg["state"]["obs_dir"] / "chest_counter.txt").resolve()
        password = obs_credentials.load_password() if ws["enabled"] else ""
        self.obs_link.configure(ws["enabled"], ws["host"], ws["port"], password, ws["source"], ws["source_uuid"], overlay)
        if reconnect:
            self.obs_link.reconnect()

    def obs_status_changed(self, status):
        self.overlay.show_obs_status(status)
        line = self.dashboard.obs_line
        if status["state"] == "off":
            line.hide()
        else:
            name, color = OBS_STATES.get(status["state"], (status["state"], theme.MUTED))
            line.setText(f"OBS WebSocket: {status['message'] if status['state'] == 'live' else name}")
            line.setStyleSheet(f"color: {color};")
            line.show()
        previous = self._obs_logged
        if status["state"] != previous and status["state"] != "connecting":
            self._obs_logged = status["state"]
            if status["state"] != "off" or previous is not None:
                self.add_log({"type": "warn" if OBS_STATES.get(status["state"], ("", ""))[1] == theme.REJECT else "info",
                              "text": f"OBS WebSocket: {status['message']}"})
        # follow a rename in OBS / remember the source's OBS id (so a later rename is followed too)
        if status["state"] in ("live", "reads_file") and status.get("source"):
            ws = load_cfg()[0]["obs_websocket"]
            if (ws["source"], ws["source_uuid"]) != (status["source"], status.get("uuid", "")):
                try:
                    C.update_config_file(config_file(), "obs_websocket", {"source": status["source"],
                                                                          "source_uuid": status.get("uuid", "")})
                except (C.UserError, OSError):
                    pass

    def handle(self, message):
        kind = message.get("type")
        self.add_log(message)
        self.dashboard.show_opening(message, 1)
        if kind == "connected":
            self.connected = message
            if self.mode != "stopping":
                self.set_mode("counting")
            self.add_log({"type": "info", "text": "Counting. Chests are saved the moment they are counted."
                          + ("" if message.get("persist", True) else " (Saving is turned off in config.toml.)")})
        elif kind in ("counts", "event", "status", "progress"):
            if message.get("certain") is not None and message.get("approx") is not None:
                self.set_counts(message["certain"], message["approx"], flash=kind == "event")
            if kind == "status":
                self.last_status = message
        elif kind == "types":
            self.primary_types = message.get("breakdown") or {}
            self.refresh_types()
        elif kind == "reset":
            self.dashboard.clear_recent(source=1)
            self._pending_resets.discard("primary")
        elif kind == "stream":
            self.show_stream_state(message.get("state"))
        elif kind == "error":
            self.error_seen = True
            self.dashboard.show_banner(str(message.get("text", "")))
        elif kind == "mark":
            self.marks_this_session += 1
            kept = (" - the video before and after it is kept" if message.get("recorded") else
                    " - noted in the session log only (no recording was running)")
            self.add_log({"type": "info", "text": f"Marked {message.get('stamp', '')}{kept}."})
            self.dashboard.rec_status.setText(f"\u2713  Marked {message.get('stamp', '')}{kept}.")
            self.dashboard.rec_status.setStyleSheet(f"color: {theme.CERTAIN};")
            self._mark_flash.start(4000)
        elif kind == "capture":
            if message.get("state") == "recording":
                self.recording = message
                rec = self.dashboard.rec_mode                   # show what the worker really records
                index = rec.findData(message.get("mode"))
                if index >= 0 and index != rec.currentIndex():
                    rec.blockSignals(True)
                    rec.setCurrentIndex(index)
                    rec.blockSignals(False)
                self.add_log({"type": "info", "text": f"Diagnostic recording on ({RECORDING_TEXT.get(message.get('mode'), '')})"
                              f": {message.get('folder', '')}"})
            elif message.get("state") == "off":
                self.recording = None
                if message.get("mode") not in (None, "off"):            # asked for a recording, but it could not start
                    rec = self.dashboard.rec_mode
                    rec.blockSignals(True)
                    rec.setCurrentIndex(rec.findData("off"))
                    rec.blockSignals(False)
                    self.add_log({"type": "warn", "text": "The diagnostic recording could not start (see the warning above); "
                                                          "counting continues. Choose a recording mode again to retry."})
                else:
                    self.add_log({"type": "info", "text": "Diagnostic recording off."})
            elif message.get("state") == "saved":
                if self.recording and self.recording.get("folder") == message.get("folder"):
                    self.recording = None
                self.add_log({"type": "info", "text": f"Diagnostic recording saved: {message.get('clips', 0)} clip(s), "
                              f"{message.get('video_mb', 0)} MB, {message.get('events', 0)} decisions, "
                              f"{message.get('marks', 0)} marks -> {message.get('folder', '')}"})
            self.update_recording_ui()
        elif kind == "stopped":
            if self._match_action == "finish" and message.get("stop_reason") == "stopped in the app":
                self._match_stop_acks.add("primary")
            minutes = (message.get("seconds") or 0) / 60
            self.add_log({"type": "line", "text": f"Stopped ({message.get('stop_reason', '')}). Analysed "
                          f"{message.get('frames_processed', 0):,} frames in {minutes:.1f} min, "
                          f"{message.get('frames_dropped', 0):,} skipped."})
        elif kind == "exit":
            code = message.get("code")
            if code not in (0, None) and not self.error_seen:
                tail = "\n".join(self.worker.stderr_tail[-6:])
                self.dashboard.show_banner("The counter stopped unexpectedly. Your counts are safe (they are saved after "
                                           "every chest). If it keeps happening, run Help & diagnostics and send the report "
                                           "and the logs folder." + (f"\n\n{tail}" if tail else ""))
                self.error_seen = True
            self.worker_exited("primary", code)

    def worker_exited(self, role, code):
        self._workers_pending.discard(role)
        failed_finish = self._match_action == "finish" and (code != 0 or role not in self._match_stop_acks)
        early_exit = self._match_action != "finish" and not (self.quit_when_stopped or self._install_pending)
        if self._match_active and (failed_finish or early_exit):
            self.match_automation_failed("A video source did not finish counting normally. Finish the match manually.")
            self.stop_counting()
        if self._install_pending and code not in (0, None):
            self._worker_failed = True
        if role in self._pending_resets:
            self._pending_resets.discard(role)
            self._set(0, 0, "reset")
        self.read_state(force=True)
        if self.workers_active():
            return
        self.stop_timer.stop()
        self.set_mode("error" if self.error_seen else "stopped")
        if self._match_action == "finish":
            self.complete_match_action()
        if self._install_pending:
            if self._worker_failed:
                self._refuse_install_after_stop()
            else:
                self._install_pending = False
                self._launch_installer()
        if self.quit_when_stopped:
            self.close()

    def show_stream_state(self, state):
        """Livestream URL: the Dashboard pill and the preview say when the stream is not delivering video."""
        if state not in STREAM_STATES or STREAM_STATES[state][1] is None:
            return                                      # "incomplete" is a log line only
        color, pill, overlay = STREAM_STATES[state]
        self.stream_state = state
        if self.mode == "counting":
            self.dashboard.set_pill(pill, color)
        preview = self.dashboard.preview
        if overlay:
            preview.clear(overlay)
            preview.view.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: 600;")
        else:
            preview.view.setStyleSheet("")

    def set_counts(self, certain, approx, flash=False):
        self.primary_counts = (certain, approx)
        self.refresh_counts(flash)

    def refresh_counts(self, flash=False):
        certain, approx = self.primary_counts
        changed = (certain, approx) != self.counts
        self.counts = (certain, approx)
        self.dashboard.certain.set_value(certain, flash=flash and changed)
        self.dashboard.approx.set_value(max(0, approx - certain), flash=flash and changed)
        self.dashboard.total.setText(fmt(approx))
        self.dashboard.update_token_odds()

    def refresh_types(self):
        breakdown = self.primary_types
        self.dashboard.types.setText(types_text(breakdown))
        self.dashboard.refresh_screen_overlay()

    def read_state(self, force=False):
        cfg, _ = load_cfg()
        path = paths.APP_DIR / cfg["state"]["path"]
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if not force and mtime == self._state_mtime:
            return
        self._state_mtime = mtime
        counts = read_counts(path)
        self.set_counts(*(counts or (0, 0)))
        self.primary_types = read_types(path) or C.type_breakdown(C.fresh_state())
        self.refresh_types()

    def write_obs_files(self, quiet=False):
        """The OBS text files from the saved counter (e.g. after an update replaced them with zero placeholders)."""
        try:
            cfg, _ = load_cfg()
            C.preflight(cfg, needs_video=False)
            session = C.CounterSession(cfg)
            session.write_obs()
        except (C.UserError, OSError) as error:
            if not quiet:
                show_error(self, str(error))

    def publish_live_counts(self):
        if time.monotonic() < self._next_live_share:
            return
        self._next_live_share = time.monotonic() + discord_group.webhook_sync_delay(None)
        if not self._match_active or not self._match_fingerprint or not self.workers_active():
            return
        cfg = load_cfg()[0]["app"]
        if not cfg["group_share_matches"] or not cfg["group_player"].strip():
            return
        try:
            webhook = discord_group.load_webhook()
            if webhook is None:
                return
            self._next_live_share = time.monotonic() + discord_group.webhook_sync_delay(webhook)
            certain, total = self.counts
            discord_group.queue_live(webhook, self._match_fingerprint, cfg["group_player"], certain, total,
                                     self.dashboard.token_boost.currentData())
        except (OSError, ValueError, sqlite3.Error) as error:
            self.add_log({"type": "warn", "text": f"Live Group update unavailable. {error}"})
            return
        self.discord.retry_group_outbox()

    def _tick_slow(self):
        self.publish_live_counts()
        self.group.sync_scoreboard()
        if self.group._webhook is not None and time.monotonic() >= self.discord._next_outbox_retry:
            self.discord.retry_group_outbox()
        if self._result_watch_until is not None:
            if time.monotonic() > self._result_watch_until:
                self._result_watch_until = None
            else:
                self.show_update_result()
        if not self.workers_active():
            self.read_state()
        elif self.mode == "counting" and self.started_at is not None:
            elapsed = int(time.monotonic() - self.started_at)
            parts = [("\u25cf REC   " if self.recording else "")
                     + f"Counting for {elapsed // 3600}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}"]
            c = self.connected or {}
            if c.get("resolution"):
                parts.append(f"video {c['resolution'][0]} x {c['resolution'][1]}"
                             + (f" @ {c['input_fps']:g} fps" if c.get("input_fps") else ""))
            s = self.last_status
            if s:
                parts.append(f"analysing {s['fps']:.1f} pictures/s")
                if s.get("backlog_s") is not None:
                    parts.append(f"backlog {s['backlog_s']:.0f} s")
                parts.append(f"{s['ms_per_frame']:.0f} ms each")
                if s.get("dropped"):
                    parts.append(f"{fmt(s['dropped'])} skipped")
            self.dashboard.metrics.setText("   ·   ".join(parts))
            self.dashboard.metrics.show()

    # ---- activity log
    def add_log(self, message):
        for debug_only, html in log_entries(message):
            self.entries.append((debug_only, html))
            if not debug_only or self.verbosity() == "debug":
                self.dashboard.log.appendHtml(html)

    def verbosity(self):
        return self.dashboard.detail.currentData()

    def render_log(self):
        log = self.dashboard.log
        log.clear()
        debug = self.verbosity() == "debug"
        for debug_only, html in self.entries:
            if debug or not debug_only:
                log.appendHtml(html)

    def clear_log(self):
        self.entries.clear()
        self.dashboard.log.clear()
        self.dashboard.clear_recent()

    def open_logs(self):
        cfg, _ = load_cfg()
        folder = paths.APP_DIR / cfg["logging"]["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def clean_diagnostics(self):
        cfg, _ = load_cfg()
        def counting():
            return self.workers_active() or any(
                C.counter_is_running(path) for path in diagnostics_cleanup.counter_states(paths.APP_DIR, cfg))

        if counting():
            show_error(self, "Stop counting before cleaning logs and captures.")
            return
        try:
            artifacts = diagnostics_cleanup.diagnostic_artifacts(paths.APP_DIR, cfg)
        except OSError as error:
            show_error(self, f"Could not list diagnostic files.\n{error}")
            return
        if not artifacts:
            show_notice_dialog(self, "Diagnostics storage", "There are no session logs or diagnostic captures to clean.")
            return
        if not ask(self, "Clean logs and captures?", "Delete the saved session logs, snapshots and diagnostic video "
                   "recordings? Your profiles, counts and settings are kept.", "Delete", danger=True):
            return
        if counting():
            show_error(self, "Stop counting before cleaning logs and captures.")
            return
        removed, failed = diagnostics_cleanup.remove_diagnostic_artifacts(artifacts)
        message = f"Removed {removed:,} log files or recording folders."
        if failed:
            show_error(self, f"{message}\n\nCould not remove {len(failed):,}. Close any app using them and retry.")
        else:
            show_notice_dialog(self, "Diagnostics storage", message)

    # ---- preview
    def preview_visibility_changed(self):
        wanted = (not self._hidden_in_tray and self.pages.currentWidget() is self.dashboard and self.dashboard.show_preview.isChecked()
                  and self.mode in ("connecting", "counting"))
        primary = wanted and self.preview_reader is not None
        if self.preview_reader is not None:
            self.preview_reader.set_wanted(primary)
        if primary:
            self.preview_timer.start(1000 // 8)
        else:
            self.preview_timer.stop()

    def _tick_preview(self):
        if self.preview_reader is not None and self.stream_state not in ("buffering", "ad", "reconnecting", "offline"):
            self._read_preview(self.preview_reader, self.dashboard.preview)

    @staticmethod
    def _read_preview(reader, panel):
        frame = reader.read()
        if frame is not None:
            _, width, height, data = frame
            panel.show_frame(width, height, data)
        elif panel.last_frame_at is not None and time.monotonic() - panel.last_frame_at > 2:
            panel.clear("Waiting for new video frames…")

    # ---- counter actions
    def _cfg_for_actions(self):
        cfg, _ = load_cfg()
        for section, key in (("state", "path"), ("state", "obs_dir"), ("logging", "dir")):
            cfg[section][key] = str(paths.APP_DIR / cfg[section][key])
        return cfg

    def edit_counts(self):
        dialog = CountsDialog(self, *self.primary_counts)
        if dialog.exec() != QDialog.Accepted:
            return
        self._set(dialog.certain.value(), dialog.total, "set_count")

    @staticmethod
    def match_history_path():
        cfg, _ = load_cfg()
        return Path(cfg["profile"]["state_base"]).parent / "match_history.json"

    def finish_match(self):
        cfg, _ = load_cfg()
        if not cfg["state"]["persist"]:
            show_error(self, "Enable saving counter state before recording matches.")
            return
        primary = Path(cfg["state"]["path"])
        if self.workers_active() or C.counter_is_running(primary):
            show_error(self, "Stop counting, then save the match.")
            return
        if not ask(self, "Finish match?", "Save the current match to Group? "
                   "Use Reset on the Dashboard before starting the next match.", "Save match"):
            return
        try:
            saved = self.save_current_match()
        except (C.UserError, OSError, ValueError) as error:
            show_error(self, f"Could not save the match. {error}")
            return
        if self.pages.currentWidget() is self.group:
            self.group.load()
        else:
            self.go_to(4)
        self.group.feedback.setText("Match saved. Use Reset on the Dashboard before the next match." if saved else
                                    "These counts have already been saved.")

    def save_current_match(self, *, session_fingerprint="", verify_replay=False):
        cfg, _ = load_cfg()
        if not cfg["state"]["persist"]:
            raise C.UserError("Enable saving counter state before recording matches.")
        primary = Path(cfg["state"]["path"])
        state_paths = (primary,)
        player = cfg["app"]["group_player"].strip()
        webhook = None
        if player and cfg["app"]["group_share_matches"]:
            try:
                player = discord_group.validate_player(player)
                webhook = discord_group.load_webhook()
            except (OSError, ValueError) as error:
                self.add_log({"type": "warn", "text": f"Could not read Group setup. {error}"})
        with ExitStack() as guards:
            for path in state_paths:
                guards.enter_context(C.SingleInstance(path))
            profile = profiles.apply(cfg)
            identity = f"{profile['id']}@{profile['created_at'] or ''}"
            snapshot, certain, total = match_history.read_snapshot(identity, state_paths)
            if webhook and self.group_role == "guest" and self.group._host_match_settings is None:
                session_fingerprint = ""
            boost = self.dashboard.token_boost.currentData()
            types = read_types(primary) or {}
            breakdown = {kind: row["approx"] for kind, row in types.items()
                         if kind in discord_group.GROUP_TYPE_LABELS and row["approx"] > 0}
            unclassified = total - sum(breakdown.values())
            if unclassified > 0:
                breakdown["unclassified"] = breakdown.get("unclassified", 0) + unclassified
            if sum(breakdown.values()) > total:
                breakdown = {}
            match = match_history.Match(snapshot, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                                        identity, profile["name"], self.current_game_mode(),
                                        certain, total - certain, token_odds_text(total, boost), boost,
                                        player=player, chest_types=breakdown,
                                        group_webhook_id=webhook.id if webhook else "",
                                        session_fingerprint=session_fingerprint,
                                        live_events=C.load_state(primary).get("match_events", []) if primary.is_file() else [])
            saved = match_history.save_match(self.match_history_path(), match)
        if saved:
            self.dashboard.set_recent_matches(match_history.read_history(self.match_history_path()))
            if self.pages.currentWidget() is self.stats:
                self.stats.load()
        if saved and webhook is not None:
            try:
                discord_group.queue_match(webhook, match, player)
            except (OSError, ValueError, sqlite3.Error) as error:
                self.add_log({"type": "warn", "text": f"Could not queue the group contribution. {error}"})
            self.discord.retry_group_outbox()
        if (saved and verify_replay and cfg["app"]["auto_verify_replay"]
                and replay_comparison.reader_path().is_file()):
            finished_at = time.time()

            def verify():
                try:
                    result = replay_comparison.verify_local_replay(match, finished_at)
                except (OSError, ValueError, subprocess.SubprocessError):
                    result = None
                self.replay_result.emit((match, webhook, result))

            threading.Thread(target=verify, daemon=True).start()
        if saved and cfg["app"]["group_share_matches"] and cfg["app"]["auto_discord_summary"]:
            self.deliver_saved_match(match)
        return saved

    def apply_verified_replay(self, payload):
        match, webhook, result = payload
        if not load_cfg()[0]["app"]["auto_verify_replay"]:
            return
        if result is None:
            self.add_log({"type": "info", "text": "Replay could not be verified. Live counts were kept."})
            return
        path, identity, _, _, destroyed, timeline, owner = result
        types = {}
        for _, kind, broken in timeline:
            if not broken:
                label = replay_comparison.chest_type(kind)
                types[label] = types.get(label, 0) + 1
        total = sum(types.values())
        try:
            verified = match_history.verify_match(self.match_history_path(), match.snapshot, total,
                                                  token_odds_text(total, match.boost), types,
                                                  {"owner": owner, "destroyed": destroyed,
                                                   "timeline": [list(event) for event in timeline]})
            if verified is None:
                return
            if webhook is not None:
                discord_group.queue_verified(webhook, verified, match.player)
                self.discord.retry_group_outbox()
            if load_cfg()[0]["app"]["delete_verified_replay"]:
                replay_comparison.delete_consumed_replay(path, identity)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.add_log({"type": "warn", "text": f"Replay kept on disk. {error}"})
        self.dashboard.set_recent_matches(match_history.read_history(self.match_history_path()))
        if self.pages.currentWidget() is self.group:
            self.group.load()
        elif self.pages.currentWidget() is self.stats:
            self.stats.load()

    def deliver_saved_match(self, match):
        if self.group_role != "host":
            return
        runner = self.discord.runner
        if runner is None or runner.status[0] not in ("online", "connecting") or runner.report_config is None:
            return
        config = runner.report_config

        def deliver():
            try:
                discord_summary.send(config, match)
                result = "Match summary sent to Discord."
            except (OSError, ValueError) as error:
                result = f"Discord delivery failed. {error}"
            self.auto_discord_result.emit(result)

        # Discord's REST request has a timeout. Allow it to finish after the window closes.
        threading.Thread(target=deliver, daemon=False).start()

    def reset_counts(self):
        whose = "" if self.dashboard.profile.count() <= 1 else f' of the profile "{self.dashboard.profile.currentText()}"'
        if ask(self, "Reset the counter?", f"Set the counts{whose} to 0? This cannot be undone.",
               "Reset to 0", danger=True):
            self._set(0, 0, "reset")

    def _set(self, certain, approx, action):
        try:
            session = C.set_counts(self._cfg_for_actions(), certain, approx, action)
        except (C.UserError, OSError) as error:
            show_error(self, str(error))
            return
        self._match_fingerprint = ""
        self.primary_types = read_types(session.state_path) or C.type_breakdown(C.fresh_state())
        self.set_counts(session.state["certain"], session.state["approximate"], flash=True)
        self.refresh_types()
        if action == "reset":
            self.dashboard.clear_recent(source=1)
        what = "reset to 0" if action == "reset" else f"set to CERTAIN {fmt(certain)} / APPROX {fmt(approx)}"
        self.add_log({"type": "info", "text": f"Counter {what}."
                      + (" The running counter picks it up within a second." if self.worker.running() else "")})

    def mark_available(self):
        return self.mode == "counting" and self.recording is not None

    def mark_moment(self):
        """Mark button / Ctrl+Alt+M: keep the video around this moment. Only while a recording is running - without one
        there is no video to keep, and the app says so instead of pretending."""
        if self.mark_available():
            return self.worker.send("mark")
        why = ("start counting first" if self.mode != "counting" else
               "diagnostic recording is off - choose 'Only around my marks' in Dashboard > Capture settings (it keeps the 30 s before "
               "each mark)")
        self.add_log({"type": "warn", "text": f"Mark not saved: {why}."})
        self.dashboard.rec_status.setText(f"Mark not saved: {why}.")
        self.dashboard.rec_status.setStyleSheet(f"color: {theme.WARN};")
        self._mark_flash.start(5000)
        return False

    def recording_changed(self):
        """The Diagnostic recording choice: saved for next time and, while counting, applied at once (counts continue)."""
        mode = self.dashboard.rec_mode.currentData()
        try:
            C.update_config_file(config_file(), "capture", {"mode": mode})
            if mode == "off":
                # Off is an immediate stop for diagnostics, including text and PNG
                # files enabled by an earlier test. The worker watches this config.
                C.update_config_file(config_file(), "logging", {
                    "enabled": False, "log_candidates": False, "save_snapshots": False})
                self.settings_page.logging.setChecked(False)
                self.settings_page.snapshots.setChecked(False)
        except (C.UserError, OSError) as error:
            show_error(self, f"Could not save the recording choice: {error}")
            return
        if self.worker.running():
            self.worker.send(f"capture {mode}")
        self.update_recording_ui()

    def update_recording_ui(self):
        d = self.dashboard
        d.mark.setEnabled(self.mark_available())
        d.mark.setToolTip("Keep the video around this moment (the counter missed a chest or got one wrong). Also "
                          "Ctrl+Alt+M while you play." if self.mark_available() else
                          "Available while counting with a diagnostic recording running: choose a recording mode on the "
                          "left ('Only around my marks' is the smallest).")
        if self._mark_flash.isActive():
            return
        d.rec_status.setStyleSheet("")
        mode = d.rec_mode.currentData()
        if self.recording is not None:
            d.rec_status.setText(f"\u25cf  Recording {RECORDING_TEXT.get(self.recording.get('mode'), '')}   \u00b7   "
                                 f"{self.marks_this_session} mark(s) this session   \u00b7   press Ctrl+Alt+M right after a "
                                 "missed chest")
            d.rec_status.setStyleSheet(f"color: {theme.REJECT};")
        elif self.mode == "counting" and mode != "off":
            d.rec_status.setText("Starting the recording...")
        elif self.mode == "counting":
            d.rec_status.setText("Not recording. Marks need a recording: 'Only around my marks' keeps just the 30 seconds "
                                 "before and 10 after each mark.")
        elif mode != "off":
            d.rec_status.setText(f"Starts with counting: keeps {RECORDING_TEXT[mode]} (captures folder).")
        else:
            d.rec_status.setText("Off. Turn it on to replay missed chests later; you can also do it while counting.")

    def register_hotkey(self):
        """Ctrl+Alt+M anywhere (also while Fortnite has the focus) = Mark moment."""
        try:
            ok = ctypes.windll.user32.RegisterHotKey(int(self.winId()), HOTKEY_ID, 0x0002 | 0x0001 | 0x4000, ord("M"))
        except Exception:
            ok = False
        self.hotkey = bool(ok)
        if not ok:
            self.dashboard.mark.setToolTip("The counter missed a chest or got one wrong? Mark it. (Ctrl+Alt+M is used by "
                                           "another program, so only this button works.)")

    def nativeEvent(self, event_type, message):
        try:
            if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:                  # WM_HOTKEY
                    self.mark_moment()
                    return True, 0
        except Exception:
            pass
        return super().nativeEvent(event_type, message)

    def showEvent(self, event):
        super().showEvent(event)
        if self.hotkey is None:
            self.register_hotkey()

    def analyse_video(self):
        if self.worker.running():
            return
        VideoDialog(self).exec()

    # ---- window layout
    def restore_layout(self):
        """Called once by main() before the window is shown: reopen where the user left it ([app] remember_window)."""
        self.window_file = window_file()
        cfg, _ = load_cfg()
        return cfg["app"]["remember_window"] and restore_window(self, self.window_file)

    def save_layout(self):
        """On closing: keep the size, position and maximised state for next time (only while the setting is on)."""
        if self.window_file is None or not self.isVisible():
            return False
        cfg, _ = load_cfg()
        return cfg["app"]["remember_window"] and save_window(self, self.window_file)

    def configure_tray(self, enabled):
        if not enabled:
            if self._tray is not None:
                self._tray.hide()
            return False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        if self._tray is None:
            self._tray = QSystemTrayIcon(self.windowIcon(), self)
            self._tray.setToolTip(APP_NAME)
            menu = QMenu(self)
            menu.setObjectName("trayMenu")
            menu.setWindowFlag(Qt.NoDropShadowWindowHint)
            menu.addAction("Open", self.open_from_tray)
            menu.addAction("Quit", self.quit_from_tray)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(self.tray_activated)
        self._tray.show()
        return True

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_from_tray()

    def open_from_tray(self):
        self._hidden_in_tray = False
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self.preview_visibility_changed()

    def quit_from_tray(self):
        self._tray_exit = True
        try:
            self.close()
        finally:
            self._tray_exit = False

    # ---- closing
    def closeEvent(self, event):
        if (not self._tray_exit and not self.quit_when_stopped and not self._install_pending
                and self.updates.state != UM.INSTALLING and self._tray is not None and self._tray.isVisible()
                and load_cfg()[0]["app"]["close_to_tray"]):
            self.save_layout()
            self._hidden_in_tray = True
            self.hide()
            self.preview_visibility_changed()
            event.ignore()
            return
        if self.workers_active():
            if not self.quit_when_stopped:
                if not ask(self, "Stop counting?", "The counter is running. Stop counting and close the app?\n\n"
                                                   "Your counts are saved and kept for next time.", "Stop and close"):
                    event.ignore()
                    return
                self.quit_when_stopped = True
                self.match_timer.stop()
                self.dashboard.toggle_screen_overlay(False)
                self.save_layout()               # before hiding: a hidden window has no position to save
                self.stop_counting()
                self.hide()
            event.ignore()                       # closes for real when the worker has finished (see handle 'exit')
            return
        self.match_timer.stop()
        self.save_layout()
        self.dashboard.toggle_screen_overlay(False)
        if self.update_dialog is not None:
            self.update_dialog.hide()
        if self.preview_reader is not None:
            self.preview_reader.close()
        self.obs_link.stop(timeout=0.5)
        self.discord.stop_bot(timeout=2)
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)
        QApplication.quit()


# ==================================================================
# start-up
# ==================================================================

def _crash_hook(window_ref):
    def hook(kind, value, tb):
        where = None
        try:
            folder = paths.APP_DIR / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            where = folder / f"crash_app_{datetime.now():%Y%m%d_%H%M%S}.txt"
            where.write_text(f"{APP_NAME} v{__version__} (app window)\n\n" + "".join(traceback.format_exception(kind, value, tb)),
                             encoding="utf-8")
        except OSError:
            pass
        try:
            show_error(window_ref(),
                                 f"Something unexpected went wrong in the app window ({kind.__name__}: {value}).\n\n"
                                 "Counting is not affected: it runs separately and saves after every chest."
                                 + (f"\n\nDetails were saved to:\n{where}" if where else ""))
        except Exception:
            pass
    return hook


def _single_instance():
    """One app window per program folder. -> a handle to keep, or None if another window is already open."""
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k.CreateMutexW(None, False, paths.app_mutex_name())
        if handle and ctypes.get_last_error() == 183:
            return None
        return handle or True
    except Exception:
        return True


def _palette():
    palette = QPalette()
    for role, color in ((QPalette.Window, theme.BG), (QPalette.WindowText, theme.TEXT), (QPalette.Base, theme.SURFACE_2),
                        (QPalette.AlternateBase, theme.SURFACE), (QPalette.Text, theme.TEXT), (QPalette.Button, theme.SURFACE_2),
                        (QPalette.ButtonText, theme.TEXT), (QPalette.Highlight, theme.SURFACE_3), (QPalette.HighlightedText, theme.TEXT),
                        (QPalette.ToolTipBase, theme.SURFACE_3), (QPalette.ToolTipText, theme.TEXT),
                        (QPalette.PlaceholderText, theme.DIM), (QPalette.Link, theme.ACCENT)):
        palette.setColor(role, QColor(color))
    return palette


def self_test(app, report_path):
    """`--self-test REPORT.json`: build the real window without ever showing it, wait for the device scan, save a
    screenshot next to the report and exit. Proves the whole start chain (launcher -> pythonw -> Qt -> window) works on a
    PC without anything appearing on screen."""
    report_path = Path(report_path)
    result = {"ok": False, "version": __version__, "python": sys.version.split()[0], "executable": sys.executable,
              "app_dir": str(paths.APP_DIR), "packaged": paths.PACKAGED, "update_health": True}
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        result.update(pyside=PySide6.__version__, qt=qVersion(), platform=app.platformName())
        window = MainWindow()
        result["class_icon"] = set_class_icon(window)
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.show()
        deadline = time.monotonic() + 20
        while window.dashboard.source.count() == 0 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.02)
        result["sources"] = [window.dashboard.source.itemText(i) for i in range(window.dashboard.source.count())]
        result["screenshot"] = str(report_path.with_suffix(".png"))
        result["ok"] = bool(window.grab().save(result["screenshot"])) and bool(result["sources"]) and result["class_icon"]
        window.preview_reader and window.preview_reader.close()
    except Exception:
        result["error"] = traceback.format_exc()
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv=None):
    paths.setup_environment()                     # run from the program folder; bundled ffmpeg first
    paths.HIDE_CHILD_CONSOLES = True
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)    # own taskbar icon/group
    except Exception:
        pass
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    for weight in (400, 500, 700):
        QFontDatabase.addApplicationFont(str(ASSETS / f"GoogleSans-{weight}.ttf"))
    app.setPalette(_palette())
    app.setStyleSheet(theme.STYLE)
    if len(args) == 2 and args[0] == "--self-test":
        return self_test(app, args[1])
    lock = _single_instance()
    if lock is None:
        show_notice_dialog(None, APP_NAME, f"{APP_NAME} is already open.\n\nLook for it in the taskbar or system tray.")
        return 0
    window = MainWindow()
    set_class_icon(window)
    apply_window_theme(window)
    sys.excepthook = _crash_hook(lambda: window)
    window.restore_layout()
    installing = len(args) == 3 and args[0] == "--update-health"
    app_options = load_cfg()[0]["app"]
    tray_available = window.configure_tray(app_options["close_to_tray"] or app_options["start_in_tray"])
    window._hidden_in_tray = tray_available and app_options["start_in_tray"] and not installing
    if not window._hidden_in_tray:
        window.show()
    if installing:                                             # started by the updater: report only once the app is really up
        from fortnite_chest_counter.update import health
        QTimer.singleShot(UPDATE_HEALTH_DELAY_MS, lambda: health.write_report(args[1], args[2]))
    window.after_start(installing)
    return app.exec()
