"""
Chest counter: persistent state, OBS text files, session log, manual controls, a live-style
simulation on a recorded video, and the LIVE mode for OBS / capture card / desktop.

    frame source  ->  estimator.ChestEstimator  ->  counter state  ->  OBS text files + log

Windows release: "Fortnite Visual Chest Counter.exe" opens the desktop app (gui/), which runs the `live` command below as
its worker. Every command also works directly: runtime/python/python.exe -m fortnite_chest_counter <command>.
Developers: the same commands via `python -m fortnite_chest_counter ...`.

Commands
--------
    python -m fortnite_chest_counter live --source stream --url https://www.twitch.tv/CHANNEL   # a livestream link
    python -m fortnite_chest_counter live --source desktop --monitor 2 # a screen showing the stream / game fullscreen
    python -m fortnite_chest_counter live --source obs-camera          # OBS Virtual Camera feed
    python -m fortnite_chest_counter live --source device --device "Elgato HD60 X"
    python -m fortnite_chest_counter live --source desktop --monitor 1 # fallback
    python -m fortnite_chest_counter video "my gameplay.mp4"           # count chests in a recording (never touches the live counter)
    python -m fortnite_chest_counter devices                           # list video devices and monitors
    python -m fortnite_chest_counter simulate --name Fortnite-PS5-Pro --reset   # recorded video, chronological
    python -m fortnite_chest_counter show | reset | adjust --certain -1 --approx -1
    python -m fortnite_chest_counter profiles [--activate NAME]         # count profiles (one per person); any command: --profile NAME
    python -m fortnite_chest_counter diagnostics                       # support report (also saved to logs/diagnostics.txt)
    python -m fortnite_chest_counter --version
    python -m fortnite_chest_counter evaluate --name Fortnite-PS5-Pro --log logs/session_xxx.jsonl

Settings come from config.toml if present (see config.example.toml), else built-in defaults;
command-line options override both. Counts persist in the state file and are never reset
unless you run `reset` / pass --reset. The OBS files are plain UTF-8 text in obs_output/,
written atomically.
"""

import argparse
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from fortnite_chest_counter.console import CONSOLE
from fortnite_chest_counter.detection import chest_types
from fortnite_chest_counter.detection import fusion
from fortnite_chest_counter import paths
from fortnite_chest_counter import profiles
from fortnite_chest_counter import settings
from fortnite_chest_counter import overlay_layout
from fortnite_chest_counter.detection import visual as V
from fortnite_chest_counter.version import APP_NAME, RELEASE_CHANNEL, __version__

SAMPLES = Path("samples/video tests")
LAST_CHEST_NONE = "NO CHESTS YET"
SEPARATOR = " • "


class UserError(Exception):
    """An expected setup/usage problem: shown to the user as a plain message, never a traceback."""


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path, text):
    """Write UTF-8 text so a reader (OBS) never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(20):                      # Windows: replace can fail while OBS has the file open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def mmss(seconds):
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:05.2f}"


# ==================================================================
# STATE (always plain integers; formatting happens only for display)
# ==================================================================

def fresh_types():
    return {kind: {"certain": 0, "approx": 0} for kind in (*chest_types.KNOWN, chest_types.UNKNOWN)}


def fresh_state():
    return {"certain": 0, "approximate": 0, "events_logged": 0, "last_event_id": None, "last_event": None,
            "session_started_at": now_iso(), "updated_at": now_iso(), "last_processed_time": 0.0,
            "types": fresh_types(), "types_since": now_iso()}


# ---- chest types (v0.2.2): which kind of chest each COUNTED chest was. A label only - it never changes CERTAIN / APPROX.
# state["types"][kind] = {"certain": n, "approx": n} for chests the detector counted since state["types_since"]; APPROX
# includes CERTAIN, as for the totals. Everything else in the totals - counts saved before v0.2.2, Edit counts / Set
# starting count, manual corrections - is "unclassified" = total - sum of the types (derived, never stored, so the
# numbers always add up; it can be negative after a manual correction downwards). No history is ever invented.

def _clean_types(raw):
    """Saved types -> a valid dict (unknown future kinds are kept if well-formed), or None if unusable."""
    if not isinstance(raw, dict):
        return None
    out = fresh_types()
    for kind, v in raw.items():
        if isinstance(kind, str) and isinstance(v, dict) and _valid_count(v.get("certain")) and _valid_count(v.get("approx")):
            out[kind] = {"certain": v["certain"], "approx": max(v["approx"], v["certain"])}
    return out


def type_breakdown(state):
    """-> {kind: {"certain", "approx"}, ..., "unclassified": {"certain", "approx"}} with every row adding up to the totals."""
    types = state.get("types") or fresh_types()
    rows = {kind: dict(v) for kind, v in types.items()}
    rows["unclassified"] = {"certain": state["certain"] - sum(v["certain"] for v in types.values()),
                            "approx": state["approximate"] - sum(v["approx"] for v in types.values())}
    return rows


def _valid_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_state(path, warn=print):
    """Read the counter file. A missing file is a fresh start. A damaged one is set aside (never
    deleted) and replaced by a fresh state, with a message, so a bad file can never stop the app.
    Guarantees the public invariant approximate >= certain."""
    path = Path(path)
    if not path.exists():
        return fresh_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not _valid_count(raw.get("certain")) or not _valid_count(raw.get("approximate")):
            raise ValueError("'certain' and 'approximate' must be whole numbers >= 0")
    except (OSError, ValueError) as error:          # JSONDecodeError and UnicodeDecodeError are ValueErrors
        backup = path.with_name(f"{path.stem}.damaged-{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        try:
            os.replace(path, backup)
            where = f"The damaged file was kept as {backup.name}."
        except OSError:
            where = "The damaged file could not be moved aside."
        warn(f"WARNING: the saved counter ({path}) is damaged ({error}). Starting from 0. {where}")
        return fresh_state()
    state = {**fresh_state(), **raw}
    types = _clean_types(raw.get("types"))
    if types is None:                       # saved before v0.2.2 (or damaged): every saved chest stays "unclassified"
        state["types"], state["types_since"] = fresh_types(), now_iso()
    else:
        state["types"] = types
    if state["approximate"] < state["certain"]:
        warn(f"WARNING: saved APPROX ({state['approximate']}) was below CERTAIN ({state['certain']}); "
             f"APPROX is the total and includes the certain chests, so it was raised to {state['certain']}.")
        state["approximate"] = state["certain"]
    return state


def save_state(path, state):
    state["updated_at"] = now_iso()
    atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def last_chest_text(state):
    last = state.get("last_event")
    if not last:
        return LAST_CHEST_NONE
    if last.get("certain_counted"):
        return f"CHEST +1{SEPARATOR}{last['adjusted_tier']}"
    if last.get("approx_counted"):
        return f"CHEST ~+1{SEPARATOR}APPROX"
    return LAST_CHEST_NONE


def display_text(display, which, state):
    """The user's text for one counter ('certain' or 'approx'), with the numbers filled in. Never raises."""
    key = "certain_text" if which == "certain" else "approx_text"
    count = state["certain"] if which == "certain" else state["approximate"]
    return settings.render_text(display.get(key, settings.DEFAULTS["display"][key]), settings.DEFAULTS["display"][key],
                                count=count, certain=state["certain"], approx=state["approximate"],
                                thousands=display["use_thousands_separator"])


def inline_text(display, state):
    """Both numbers on one line: the combined_status_format wording, e.g. "20 certain • ~231 estimated"."""
    return settings.render_text(display["combined_status_format"], settings.DEFAULTS["display"]["combined_status_format"],
                                certain=state["certain"], approx=state["approximate"], thousands=display["use_thousands_separator"])


def display_mode(display):
    """'approx' | 'certain' | 'inline' | 'stacked' - the four things chest_counter.txt can show."""
    show = display.get("show", "both")
    if show in ("approx", "certain"):
        return show
    return "inline" if display.get("both_layout", "stacked") == "inline" else "stacked"


MODE_DESCRIPTIONS = {"approx": "estimated total only", "certain": "confirmed chests only",
                     "inline": "both numbers on one line", "stacked": "both numbers on two lines"}


def overlay_text(display, state, context=None):
    """obs_output/chest_counter.txt - THE file OBS reads. Every display mode renders here, so switching modes never
    requires changing the OBS source."""
    if display.get("use_custom_sections") and context is not None:
        context = overlay_layout.reconcile_context(context, state)
        return overlay_layout.format_text(display, overlay_layout.render_sections(display, context))
    mode = display_mode(display)
    if mode == "certain":
        return display_text(display, "certain", state)
    if mode == "approx":
        return display_text(display, "approx", state)
    if mode == "inline":
        return inline_text(display, state)
    return display_text(display, "certain", state) + "\n" + display_text(display, "approx", state)


def status_line_text(display, state):
    """obs_output/chest_status.txt (legacy, advanced): one line with both numbers, or the single number when only one is
    chosen. New setups should use chest_counter.txt."""
    return overlay_text(display, state) if display_mode(display) in ("approx", "certain") else inline_text(display, state)


def overlay_summary(display, state, obs_dir):
    """Two lines telling the user exactly what viewers see and which file OBS must read."""
    return (f"Viewers see ({MODE_DESCRIPTIONS[display_mode(display)]}): {overlay_text(display, state).replace(chr(10), '  /  ')}",
            f"OBS Text source file: {(Path(obs_dir) / 'chest_counter.txt').resolve()}")


def read_overlay_context(obs_dir, state, state_path=None):
    """Read only the current profile's bounded GUI snapshot, then advance it to worker state."""
    try:
        with (Path(obs_dir) / "overlay_context.json").open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return None
        context = json.loads(raw)
        if not overlay_layout.valid_context(context):
            return None
        if state_path is not None:
            profile = context.get("profile_path")
            if not isinstance(profile, str) or Path(profile).resolve() != Path(state_path).resolve():
                return None
        return overlay_layout.reconcile_context(context, state)
    except (OSError, TypeError, ValueError, RecursionError):
        return None


def write_obs_files(state, obs_dir, display, *, state_path=None):
    """The OBS text files. File names never change (OBS sources point at them); only their text is customisable."""
    obs_dir = Path(obs_dir)
    atomic_write(obs_dir / "certain_chests.txt", display_text(display, "certain", state) + "\n")
    atomic_write(obs_dir / "approximate_chests.txt", display_text(display, "approx", state) + "\n")
    atomic_write(obs_dir / "chest_status.txt", status_line_text(display, state) + "\n")
    context = read_overlay_context(obs_dir, state, state_path) if display.get("use_custom_sections") else None
    atomic_write(obs_dir / "chest_counter.txt", overlay_text(display, state, context) + "\n")
    atomic_write(obs_dir / "last_chest.txt", last_chest_text(state) + "\n")


def write_png(path, rgb):
    """Minimal RGB PNG writer (no image library needed)."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class SessionLog:
    def __init__(self, log_cfg):
        self.directory = Path(log_cfg["dir"])
        self.path = None
        self.enabled = False
        self.snapshot_dir = None
        self.configure(log_cfg)

    def configure(self, log_cfg):
        """Apply logging switches while retaining this run's path and directory."""
        self.enabled = False
        self.snapshot_dir = None
        self.log_candidates = log_cfg["log_candidates"]
        if not log_cfg["enabled"]:
            return
        if self.path is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / f"session_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        self.enabled = True
        if log_cfg.get("save_snapshots"):
            self.snapshot_dir = self.directory / "snapshots" / self.path.stem

    def save_snapshot(self, name, rgb):
        """Popup-region image for one event, so a human can later check what was (not) counted. -> file name or None."""
        if not self.enabled or self.snapshot_dir is None or rgb is None:
            return None
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            write_png(self.snapshot_dir / name, np.ascontiguousarray(rgb, dtype=np.uint8))
            return name
        except OSError:
            return None

    def write(self, record, counted=True):
        if not self.enabled or (not counted and not self.log_candidates):
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o)) + "\n")


class CounterSession:
    """Counts + OBS files + log for one run; shared by `simulate` and `live`."""

    def __init__(self, cfg, reset=False):
        self.cfg = cfg
        self.persist = cfg["state"]["persist"]
        self.state_path, self.obs_dir = Path(cfg["state"]["path"]), Path(cfg["state"]["obs_dir"])
        self.display = cfg["display"]
        self.state = fresh_state() if reset or not self.persist else load_state(self.state_path)
        self.loaded_existing = (not reset) and self.persist and self.state_path.exists()
        self.log = SessionLog(cfg["logging"])
        self.latencies = []
        self.recent = {}                      # event time -> (event id, chest type, certain, approx): for type updates
        self._warned = set()
        self._reset_pending_ack = False
        self._mtime = self._state_mtime()

    def _state_mtime(self):
        try:
            return self.state_path.stat().st_mtime_ns
        except OSError:
            return None

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            CONSOLE.warn(message)

    def fmt(self, n):
        return settings.format_count(n, self.display["use_thousands_separator"])

    def save(self):
        if self.persist:
            try:
                save_state(self.state_path, self.state)
            except OSError as error:                 # never let a locked/full disk kill a live session
                self._warn_once("state", f"could not save {self.state_path} ({error}); will retry on the next chest")
                return
            self._mtime = self._state_mtime()
        if self._reset_pending_ack:
            self._reset_pending_ack = False
            CONSOLE.emit("reset")

    def write_obs(self):
        try:
            write_obs_files(self.state, self.obs_dir, self.display, state_path=self.state_path)
        except OSError as error:
            self._warn_once("obs", f"could not update the OBS text files in {self.obs_dir} ({error}); will retry on the next chest")

    def reset_from_app(self, estimator):
        """Reset on the worker's counting thread so a simultaneous chest cannot overwrite the reset."""
        estimator.flush()                       # discard pre-reset detections without rearming visual onset detectors
        estimator.take_type_updates()
        previous = {"certain": self.state["certain"], "approximate": self.state["approximate"]}
        fingerprint = self.state.get("match_fingerprint")
        self.state.clear()
        self.state.update(fresh_state())
        if fingerprint:
            self.state["match_fingerprint"] = fingerprint
        self.recent.clear()
        self._reset_pending_ack = True
        self.save()
        self.write_obs()
        self.log.write({"event_id": "MANUAL", "source": "manual", "action": "reset",
                        "previous": previous, "logged_at": now_iso()})
        self.announce()

    def sync_external_changes(self):
        """`Reset Counter` / `adjust` run as separate programs and edit the state file. If that happened
        while we are running, adopt their numbers instead of overwriting them on the next save."""
        if not self.persist or self._state_mtime() == self._mtime:
            return False
        before = (self.state["certain"], self.state["approximate"])
        new = load_state(self.state_path, warn=lambda m: self._warn_once("sync", m))
        self.state.clear()
        self.state.update(new)
        self._mtime = self._state_mtime()
        self.write_obs()
        self.log.write({"event": "external_change", "previous": {"certain": before[0], "approximate": before[1]},
                        "counts_after": {"certain": self.state["certain"], "approximate": self.state["approximate"]},
                        "logged_at": now_iso()})
        return (self.state["certain"], self.state["approximate"]) != before

    def apply(self, finished, now_t, quiet=False):
        """Count and log finished estimator events; refresh files when a count changed."""
        applied = []
        # Reset / Set Starting Count may have rewritten the counter file a moment ago (they run as separate programs).
        # Adopt that BEFORE counting, so a chest found in the same second is added on top instead of overwriting it.
        if finished and self.sync_external_changes() and not quiet:
            CONSOLE.info(f"Counter changed by Reset / Set Starting Count -> CERTAIN {self.fmt(self.state['certain'])} | "
                         f"APPROX {self.fmt(self.state['approximate'])}")
        for ev in finished:
            st = self.state
            st["events_logged"] += 1
            event_id = f"E{st['events_logged']:04d}"
            # PUBLIC INVARIANT: APPROX is the best estimate of the TOTAL and contains the CERTAIN chests.
            # A certain chest therefore always adds exactly 1 to BOTH counts, an approx-only chest adds 1 to APPROX.
            certain_inc = int(bool(ev["certain_counted"]))
            approx_inc = int(bool(ev["approx_counted"]) or bool(certain_inc))
            ev["approx_counted"] = bool(approx_inc)
            st["certain"] += certain_inc
            st["approximate"] += approx_inc
            if st["approximate"] < st["certain"]:                       # cannot happen; repair rather than ever show it
                st["approximate"] = st["certain"]
            kind = ev.get("chest_type") or chest_types.UNKNOWN
            if certain_inc or approx_inc:                               # the label of a counted chest (never a count)
                row = st.setdefault("types", fresh_types()).setdefault(kind, {"certain": 0, "approx": 0})
                row["certain"] += certain_inc
                row["approx"] += approx_inc
                self.recent[round(ev["time"], 3)] = (event_id, kind, certain_inc, approx_inc)
                if len(self.recent) > 200:
                    self.recent.pop(next(iter(self.recent)))
            ev["event_id"], ev["stamp"] = event_id, mmss(ev["time"])
            snapshot = ev.pop("_snapshot", None)
            verdict = "CERTAIN" if certain_inc else "APPROX" if approx_inc else "notcounted"
            ev["snapshot"] = self.log.save_snapshot(
                f"{event_id}_{ev['stamp'].replace(':', 'm').replace('.', 's')}_{ev['adjusted_tier']}_{verdict}.png", snapshot)
            write_s = 0.0
            if certain_inc or approx_inc:
                timeline = st.setdefault("match_events", [])
                timeline.append({"id": event_id, "at": round(time.time() - max(0, now_t - ev["time"]), 3),
                                 "type": kind, "certain": bool(certain_inc)})
                if len(timeline) > 512:
                    del timeline[0]
                st["last_event_id"] = event_id
                st["last_event"] = {k: ev[k] for k in ("event_id", "stamp", "source", "adjusted_tier",
                                                       "certain_counted", "approx_counted")}
                t0 = time.perf_counter()
                self.write_obs()
                write_s = time.perf_counter() - t0
            ev["latency_s"] = round(now_t - ev["time"] + write_s, 3)       # event start -> OBS text updated
            if certain_inc or approx_inc:
                self.latencies.append(ev["latency_s"])
            st["last_processed_time"] = now_t
            self.save()
            self.log.write({**ev, "certain_increment": certain_inc, "approx_increment": approx_inc,
                            "counts_after": {"certain": st["certain"], "approximate": st["approximate"]},
                            "obs_write_s": round(write_s, 4), "logged_at": now_iso()},
                           counted=bool(certain_inc or approx_inc))
            if not quiet:
                CONSOLE.event(ev, self.fmt(st["certain"]), self.fmt(st["approximate"]), st["certain"], st["approximate"])
            applied.append(ev)
        if applied:
            self.announce_types()
        return applied

    def apply_type_updates(self, updates):
        """The detector read a counted chest's type better after counting it (estimator.take_type_updates): move that
        chest from its old type to the new one. The totals never change."""
        changed = False
        for u in updates or ():
            known = self.recent.get(round(u["time"], 3))
            if known is None:
                continue
            event_id, old, certain_inc, approx_inc = known
            new = u["chest_type"]
            if new == old:
                continue
            types = self.state.setdefault("types", fresh_types())
            src, dst = types.setdefault(old, {"certain": 0, "approx": 0}), types.setdefault(new, {"certain": 0, "approx": 0})
            if src["approx"] < approx_inc or src["certain"] < certain_inc:
                continue                                                 # a Reset happened since: nothing to move
            src["certain"] -= certain_inc
            src["approx"] -= approx_inc
            dst["certain"] += certain_inc
            dst["approx"] += approx_inc
            self.recent[round(u["time"], 3)] = (event_id, new, certain_inc, approx_inc)
            for entry in reversed(self.state.get("match_events", [])):
                if entry["id"] == event_id:
                    entry["type"] = new
                    break
            self.log.write({"event": "type_update", "event_id": event_id, "from": old, "to": new,
                            "confidence": u.get("type_confidence"), "evidence": u.get("type_evidence"),
                            "logged_at": now_iso()})
            CONSOLE.emit("type_update", event_id=event_id, previous=old, chest_type=new)
            changed = True
        if changed:
            self.save()
            self.announce_types()
        return changed

    def announce_types(self):
        """Machine mode: the chest-type breakdown of the counted chests (display only; OBS shows the totals)."""
        CONSOLE.emit("types", breakdown=type_breakdown(self.state), since=self.state.get("types_since"))

    def announce(self):
        """Machine mode: tell the desktop app the current numbers and exactly what the OBS file shows."""
        CONSOLE.emit("counts", certain=self.state["certain"], approx=self.state["approximate"],
                     certain_text=self.fmt(self.state["certain"]), approx_text=self.fmt(self.state["approximate"]),
                     overlay=overlay_text(self.display, self.state))
        self.announce_types()


# ==================================================================
# CONFIG PLUMBING
# ==================================================================

def config_overrides(args):
    return {("detector", "process_fps"): getattr(args, "process_fps", None),
                 ("state", "path"): getattr(args, "state", None), ("state", "obs_dir"): getattr(args, "obs_dir", None),
                 ("capture", "mode"): getattr(args, "capture_mode", None),
                 ("logging", "dir"): getattr(args, "log_dir", None), ("live", "source"): getattr(args, "source", None),
                 ("live", "device"): getattr(args, "device", None), ("live", "monitor"): getattr(args, "monitor", None),
                 ("live", "window"): getattr(args, "window", None), ("live", "url"): getattr(args, "url", None)}


def config_path(args):
    return Path(getattr(args, "config", None) or settings.DEFAULT_CONFIG_PATH)


class DisplayConfigWatcher:
    """Reload display, console and logging switches while counting. Detector settings still need a restart."""

    def __init__(self, args):
        self.args, self.path = args, config_path(args)
        self.mtime = self._mtime()

    def _mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def check(self, session):
        """Return True when live display, console and logging settings were reloaded."""
        mtime = self._mtime()
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        cfg, _, warnings = settings.load_config(getattr(self.args, "config", None), config_overrides(self.args))
        for w in warnings:
            CONSOLE.warn(w)
        logging_options = {key: cfg["logging"][key] for key in ("enabled", "log_candidates", "save_snapshots")}
        session.cfg["logging"].update(logging_options)
        try:
            session.log.configure(logging_options)
        except OSError as error:
            session._warn_once("logging", f"could not enable session logging ({error})")
        from fortnite_chest_counter import estimator
        estimator.SAVE_SNAPSHOTS = session.log.enabled and session.log.snapshot_dir is not None
        session.display = cfg["display"]
        CONSOLE.configure(cfg["console"]["verbosity"], cfg["console"]["colors"])
        session.write_obs()
        CONSOLE.info("Overlay settings changed - " + overlay_summary(session.display, session.state, session.obs_dir)[0])
        session.announce()
        return True


def make_config(args):
    overrides = config_overrides(args)
    cfg, origin, warnings = settings.load_config(getattr(args, "config", None), overrides)
    CONSOLE.configure(cfg["console"]["verbosity"], cfg["console"]["colors"])
    for w in warnings:
        CONSOLE.warn(w)
    use_profiles(cfg, args)
    from fortnite_chest_counter import estimator
    estimator.configure(cfg["detector"])
    estimator.SAVE_SNAPSHOTS = cfg["logging"]["enabled"] and cfg["logging"]["save_snapshots"]
    return cfg, origin


def use_profiles(cfg, args):
    """Count into the active profile (profiles.py), or the one named by --profile. An explicit --state file is used
    exactly as given (tests, research, two counters side by side)."""
    choice = getattr(args, "profile", None)
    if getattr(args, "state", None):
        if choice:
            raise UserError("--profile and --state cannot be used together.")
        return
    try:
        profiles.apply(cfg, choice, warn=CONSOLE.warn)
    except profiles.ProfileError as error:
        raise UserError(str(error))


# ==================================================================
# FRAME SOURCE FOR RECORDED VIDEO
# ==================================================================

class Mp4FrameSource:
    """Yields (seconds, 720p RGB frame) in order at `fps`. A live source provides the same thing."""

    def __init__(self, path, fps, start=0.0, until=None):
        self.path, self.fps, self.start, self.until = Path(path), fps, start, until

    def frames(self):
        cmd = ["ffmpeg", "-v", "error"]
        if self.start:
            cmd += ["-ss", f"{self.start:.3f}"]
        cmd += ["-i", str(self.path)]
        if self.until is not None:
            cmd += ["-t", f"{self.until - self.start:.3f}"]
        cmd += ["-vf", f"{V.scale_filter()},fps={self.fps}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=10 ** 8, stdin=paths.child_stdin(),
                                   creationflags=paths.child_process_flags())
        size = V.NORMALIZED_WIDTH * V.NORMALIZED_HEIGHT * 3
        i = 0
        while True:
            raw = process.stdout.read(size)
            if len(raw) < size:
                break
            yield self.start + i / self.fps, np.frombuffer(raw, np.uint8).reshape(V.NORMALIZED_HEIGHT, V.NORMALIZED_WIDTH, 3)
            i += 1


def hd_video_for(name):
    return SAMPLES / f"{name}-HD-No-Audio.mp4"


# ==================================================================
# EVALUATION AGAINST VERIFIED KNOWLEDGE
# ==================================================================

def stamp_to_seconds(stamp):
    minutes, rest = stamp.split(":")
    return int(minutes) * 60 + float(rest)


def known_items(name):
    """-> list of (time, before, after, kind) from verified files. kind: chest | false | non-chest."""
    items = []
    if name == "Fortnite-PS5-Pro":
        for stamp, real in fusion.load_verified_labels(SAMPLES / f"{name}-verified-predictions.txt").items():
            items.append((stamp_to_seconds(stamp), 1.5, 1.5, "chest" if real else "false"))
        for stamp, real in fusion.load_verified_labels(SAMPLES / f"{name}-verified-missed-candidates.txt").items():
            # these times are interaction-prompt times; the event can sit a few seconds either side
            items.append((stamp_to_seconds(stamp), 3.0, 4.0, "chest" if real else "non-chest"))
    else:
        from fortnite_chest_counter.detection import audio as detector
        for t in detector.load_ground_truth(detector.GROUND_TRUTH_FILE):
            items.append((float(t), 1.5, 1.5, "chest"))
    return items


def evaluate(events, name):
    items = known_items(name)
    chests = [k for k in items if k[3] == "chest"]
    print(f"\n=== EVALUATION against verified knowledge ({len(chests)} known real chests, "
          f"{sum(k[3] != 'chest' for k in items)} verified non-chests) ===")
    for e in events:
        match = [k for k in items if k[0] - k[1] <= e["time"] <= k[0] + k[2]]
        k = min(match, key=lambda k: abs(k[0] - e["time"])) if match else None
        e["_known"] = k
        e["_class"] = {"chest": "real chest", "false": "verified-false popup", "non-chest": "verified non-chest"}[k[3]] if k else "unknown"
    certain = [e for e in events if e["certain_counted"]]
    approx = [e for e in events if e["approx_counted"]]

    def breakdown(group):
        out = {}
        for e in group:
            out[e["_class"]] = out.get(e["_class"], 0) + 1
        return out

    real_certain = sum(e["_class"] == "real chest" for e in certain)
    print(f"CERTAIN count: {len(certain)}   by class: {breakdown(certain)}   precision {real_certain}/{len(certain)}"
          if certain else "CERTAIN count: 0")
    print(f"APPROX  count: {len(approx)}   by class: {breakdown(approx)}")
    print(f"Known real chests: {len(chests)}   approx error: {len(approx) - len(chests):+d}   "
          f"(unknown-class counted events could be real undiscovered chests)")
    duplicates, represented = 0, 0
    print("\nKnown real chests in the combined event stream:")
    for k in sorted(chests):
        hits = [e for e in events if e["_known"] is k]
        duplicates += max(len(hits) - 1, 0)
        if hits:
            represented += 1
            e = hits[0]
            print(f"  {mmss(k[0])}  represented by {e['event_id']} {e['source']:18s} tier {e['adjusted_tier']:>6s}  "
                  f"certain={'yes' if e['certain_counted'] else 'no ':3s} approx={'yes' if e['approx_counted'] else 'no'}")
        else:
            print(f"  {mmss(k[0])}  NOT represented by any event")
    print(f"Represented: {represented}/{len(chests)}; certain-counted real: {real_certain}/{len(chests)}; "
          f"approx-counted real: {sum(e['_class'] == 'real chest' for e in approx)}/{len(chests)}; "
          f"duplicate events on the same chest: {duplicates}")
    wrong = [e for e in approx if e["_class"] in ("verified-false popup", "verified non-chest")]
    unknown = [e for e in approx if e["_class"] == "unknown"]
    print(f"Counted but verified wrong: {len(wrong)}; counted with no verification yet: {len(unknown)}"
          + (" -> " + ", ".join(f"{e['stamp']}({e['source']})" for e in unknown) if unknown else ""))


# ==================================================================
# COMMANDS
# ==================================================================

def cmd_simulate(args):
    from fortnite_chest_counter import estimator

    cfg, origin = make_config(args)
    session = CounterSession(cfg, reset=args.reset)
    video = Path(args.video) if args.video else hd_video_for(args.name)
    state = session.state
    if not args.reset and not args.start and (state["certain"] or state["approximate"]):
        raise SystemExit(f"{session.state_path} already holds {session.fmt(state['certain'])} certain / "
                         f"{session.fmt(state['approximate'])} approx. Use --reset to start over, "
                         f"or --start SECONDS to continue a previous run.")
    start = args.start or 0.0
    session.save()
    session.write_obs()
    fps = cfg["detector"]["process_fps"]
    print(f"Simulating {video.name} from {start:.0f}s{' to ' + str(args.until) + 's' if args.until else ''} "
          f"({'real time' if args.realtime else 'fast'}); config {origin}; state {session.state_path}; "
          f"OBS files {session.obs_dir}; log {session.log.path}")

    est = estimator.ChestEstimator()
    source = Mp4FrameSource(video, fps, start, args.until)
    wall0, events, last_t = time.time(), [], start
    for t, frame in source.frames():
        last_t = t
        events += session.apply(est.process_frame(t, frame), t)
        session.apply_type_updates(est.take_type_updates())
        if args.realtime:
            ahead = (t - start) - (time.time() - wall0)
            if ahead > 0:
                time.sleep(ahead)
    events += session.apply(est.flush(), last_t)
    session.apply_type_updates(est.take_type_updates())
    session.save()
    session.write_obs()
    print(f"\nFinished at {mmss(last_t)} in {time.time() - wall0:.0f}s wall time. CERTAIN {session.fmt(state['certain'])}, "
          f"APPROX {session.fmt(state['approximate'])}, {len(events)} events logged")
    if args.evaluate and args.name and not args.until and not args.start:
        evaluate(events, args.name)


def cmd_video(args):
    """Count chests in a recorded gameplay video. Uses the same detector as live mode but never touches the live
    counter: nothing is saved to state/, and the OBS-style text files go to video_output/ instead of obs_output/."""
    from fortnite_chest_counter import estimator

    banner()
    video = Path(args.video)
    if not video.is_file():
        raise UserError(f"Video file not found: {video}\nChoose an existing video file (.mp4, .mkv, .mov ...).")
    cfg, origin = make_config(args)
    cfg["state"]["persist"] = False                     # a recording must never change the live counter
    cfg["state"]["obs_dir"] = args.obs_dir or "video_output"
    preflight(cfg)
    session = CounterSession(cfg, reset=True)
    state = session.state
    fps = cfg["detector"]["process_fps"]
    start = args.start or 0.0
    gui = getattr(args, "gui", False)
    control = AppControl() if gui else None                 # the desktop app's Stop button
    if gui:
        CONSOLE.emit("video_start", name=video.name, duration=video_duration(video), start=start, until=args.until)
    else:
        print(f"Analysing {video.name} (settings: {origin}). This runs faster than real time; leave the window open.")
    est = estimator.ChestEstimator()
    source = Mp4FrameSource(video, fps, start, args.until)
    wall0, last_t, frames, next_report, next_progress = time.time(), start, 0, start + 60.0, 0.0
    stopped_early = False
    try:
        for t, frame in source.frames():
            last_t, frames = t, frames + 1
            session.apply(est.process_frame(t, frame), t)
            session.apply_type_updates(est.take_type_updates())
            if t >= next_report:
                CONSOLE.status(f"{mmss(t)} of video analysed | CERTAIN {session.fmt(state['certain'])} | APPROX {session.fmt(state['approximate'])}")
                next_report += 60.0
            if gui and time.monotonic() >= next_progress:
                CONSOLE.emit("progress", t=round(t, 1), certain=state["certain"], approx=state["approximate"])
                next_progress = time.monotonic() + 0.5
            if control is not None and control.requested.is_set():
                stopped_early = True
                break
        session.apply(est.flush(), last_t)
        session.apply_type_updates(est.take_type_updates())
    except KeyboardInterrupt:
        stopped_early = True
        print("\nStopped early; the numbers below cover only the part already analysed.")
    if frames == 0:
        raise UserError("No picture could be read from this file. Is it a video that your PC can play? "
                        "(Details are printed just above if FFmpeg reported a problem.)")
    session.write_obs()
    if gui:
        CONSOLE.emit("finished", certain=state["certain"], approx=state["approximate"], analysed=mmss(last_t),
                     analysed_s=round(last_t - start, 1), minutes=round((time.time() - wall0) / 60, 1),
                     stopped_early=stopped_early, obs_dir=str(session.obs_dir.resolve()))
        return
    CONSOLE.line(f"\nFinished: {mmss(last_t)} of video analysed in {(time.time() - wall0) / 60:.1f} min.\n", "bold")
    show_counts(session)
    print("\n  APPROX is the estimated TOTAL and already includes the CERTAIN chests (do not add them together).")
    print(f"  Text files saved in: {session.obs_dir.resolve()}")


def video_duration(path):
    """Length of a video file in seconds, from FFmpeg's own report (no ffprobe needed), or None."""
    import re
    try:
        report = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, timeout=30,
                                stdin=paths.child_stdin(),
                                creationflags=paths.child_process_flags()).stderr.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", report)
    return int(found[1]) * 3600 + int(found[2]) * 60 + float(found[3]) if found else None


# ---- friendly-message building blocks -------------------------------------------------

OBS_STEPS = """  1. Open OBS Studio
  2. Click "Start Virtual Camera" (bottom-right "Controls" panel)
  3. Run {app} again"""


def banner():
    CONSOLE.line(f"{APP_NAME} v{__version__} ({RELEASE_CHANNEL})", "bold")


def preflight(cfg, needs_video=True):
    """First-run setup + check that the install is complete. Raises UserError with a fix."""
    for folder in (Path(cfg["state"]["path"]).parent, Path(cfg["state"]["obs_dir"]), Path(cfg["logging"]["dir"])):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise UserError(f"Cannot create the folder '{folder}' ({error}).\n"
                            f"Move the {APP_NAME} folder somewhere you can write to (for example your Desktop or Documents), "
                            f"not Program Files or a zip file.")
    missing = paths.missing_templates()
    if missing:
        raise UserError("The installation is incomplete: some program files are missing "
                        f"({', '.join(m.name for m in missing)}).\nExtract the whole zip again (do not run it from inside the zip) "
                        "and keep every file and folder together.")
    if needs_video and not paths.ffmpeg_executable():
        raise UserError("The video helper (ffmpeg) was not found.\nExtract the whole zip again and keep the 'runtime' folder "
                        "next to the program. If you deleted or moved it, re-download the app.")


def disable_quick_edit():
    """Clicking inside a Windows console pauses the program until a key is pressed. That would freeze counting,
    so switch that behaviour off for the live window."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


def console_keys():
    """Keys pressed in the console window since the last call (lower case). Q = stop (no Ctrl+C needed, no 'terminate
    batch job?' prompt), M = mark this moment for the diagnostic capture."""
    keys = set()
    try:
        import msvcrt
        while msvcrt.kbhit():
            keys.add(msvcrt.getwch().lower())
    except Exception:
        pass
    return keys


def quit_requested():
    """True if the user pressed Q in the console window."""
    return "q" in console_keys()


def _kernel32():
    """kernel32 with typed signatures and per-call error codes (Windows only)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype, k.CreateMutexW.argtypes = wintypes.HANDLE, (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    k.OpenMutexW.restype, k.OpenMutexW.argtypes = wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k


def instance_mutex_name(state_path):
    """One counter per counter file: the lock name is derived from the state file's full path."""
    import hashlib
    key = str(Path(state_path).resolve()).lower().encode("utf-8")
    return "Local\\FortniteVisualChestCounter-" + hashlib.sha1(key).hexdigest()[:16]


def counter_is_running(state_path):
    """True while a live counter for this counter file is running (on this Windows session)."""
    try:
        k = _kernel32()
        handle = k.OpenMutexW(0x00100000, False, instance_mutex_name(state_path))       # SYNCHRONIZE
        if handle:
            k.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


class ConsoleCloseGuard:
    """
    Closing the console window with X is a NORMAL way to stop the counter. Windows then sends CTRL_CLOSE_EVENT to the
    console's processes and ends them ~5 s later (log-off / shutdown work the same way). This registers a real console
    control handler (SetConsoleCtrlHandler, not Python's KeyboardInterrupt) that turns the event into a normal stop:
    the frame source is told to stop, the live loop persists counts / OBS files / session_end and releases the lock,
    and only then does the handler return. Bounded: the handler waits at most 4.5 s, inside Windows' grace period.
    If Windows kills the process anyway, nothing breaks: counts are saved after every chest and the lock is a kernel
    object that dies with the process. Ctrl+C is left to Python (KeyboardInterrupt).
    """
    EVENTS = {1: "Ctrl+Break", 2: "window closed (X)", 5: "Windows log-off", 6: "Windows shutdown"}
    WAIT_SECONDS = 4.5

    def __init__(self):
        import threading
        self.requested, self.done = threading.Event(), threading.Event()
        self.reason = None
        self.on_request = None              # set by the live loop: unblocks the frame source
        self._handler = None

    def __enter__(self):
        try:
            import ctypes
            from ctypes import wintypes

            def handler(kind):
                if kind not in self.EVENTS:
                    return False            # Ctrl+C -> Python's own handler -> KeyboardInterrupt
                self.reason = self.EVENTS[kind]
                self.requested.set()
                if self.on_request:
                    try:
                        self.on_request()
                    except Exception:
                        pass
                self.done.wait(self.WAIT_SECONDS)
                return True

            self._handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, True)
        except Exception:
            self._handler = None
        return self

    def __exit__(self, *exc):
        self.done.set()


class AppControl:
    """
    `--gui`: the desktop app runs this command as a worker without a console and controls it through stdin. A line "stop",
    or the end of stdin (the app closed, crashed or was ended in Task Manager - Windows then closes the pipe), means:
    stop and save, exactly like pressing Q in the console. Same interface as ConsoleCloseGuard (`requested`, `reason`,
    `on_request`), but a normal stop: final events are still reported and nothing is rushed.
    """

    def __init__(self, stream=None):
        import threading
        self.requested = threading.Event()
        self.reason = "stopped in the app"
        self.on_request = None
        self._marks = deque()                   # "mark [label]" lines: the app's Mark button / hotkey
        self._captures = deque()                # "capture off|marks|events|session": the app's Recording choice
        self._reset = threading.Event()
        self._stream = stream if stream is not None else sys.stdin
        threading.Thread(target=self._read, daemon=True, name="app-control").start()

    def take_marks(self):
        marks = []
        while self._marks:
            marks.append(self._marks.popleft())
        return marks

    def take_capture_requests(self):
        modes = []
        while self._captures:
            modes.append(self._captures.popleft())
        return modes

    def take_reset(self):
        if not self._reset.is_set():
            return False
        self._reset.clear()
        return True

    def _read(self):
        try:
            for line in self._stream:
                command, _, rest = line.strip().partition(" ")
                if command.lower() in ("stop", "quit"):
                    break
                if command.lower() == "mark":
                    self._marks.append(rest.strip() or "mark")
                if command.lower() == "reset":
                    self._reset.set()
                if command.lower() == "capture" and rest.strip().lower() in ("off", "marks", "events", "session"):
                    self._captures.append(rest.strip().lower())
            else:
                self.reason = "the app closed"
        except Exception:
            self.reason = "the app closed"
        self.request()

    def request(self):
        self.requested.set()
        if self.on_request:
            try:
                self.on_request()
            except Exception:
                pass

    def attach(self, stop_source):
        """Make a stop request reach `stop_source` (also one that arrived before the source existed)."""
        self.on_request = stop_source
        if self.requested.is_set():
            stop_source()


class SingleInstance:
    """
    Two live counters sharing one counter file would fight, so a second one is refused. The lock is a Windows named
    mutex: the kernel releases it automatically when the process ends, however it ends (X, crash, Task Manager, power
    loss), so it can never be left behind and block a later start. (v0.1.0 used a live.lock file; a leftover one is
    ignored and removed.)
    """

    def __init__(self, state_path):
        self.state_path = Path(state_path)
        self.name = instance_mutex_name(state_path)
        self.handle = None

    def __enter__(self):
        import ctypes
        try:
            k = _kernel32()
        except Exception:
            return self                      # not Windows: no lock
        handle = k.CreateMutexW(None, False, self.name)
        if handle and ctypes.get_last_error() == 183:                                       # ERROR_ALREADY_EXISTS
            k.CloseHandle(handle)
            raise UserError(f"{APP_NAME} is already running in another window.\n"
                            "Close this window and use the one that is already open (only one can run at a time).")
        self.handle = handle
        # Leftovers of a session Windows killed before it could finish: a v0.1.0 lock file, a half-written save.
        # (The real counter file is always complete: saves are atomic, so the .tmp is simply discarded.)
        for leftover in (self.state_path.with_name("live.lock"), self.state_path.with_name(self.state_path.name + ".tmp")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        return self

    def __exit__(self, *exc):
        if self.handle:
            try:
                _kernel32().CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def explain_source_failure(kind, label, error_text):
    """Turn an ffmpeg/source failure into steps a streamer can follow."""
    from fortnite_chest_counter.input import video_sources as sources

    if kind == "obs-camera":
        try:
            names = [n for n, k in sources.list_video_devices() if k in ("video", "none")]
        except Exception:
            names = []
        if "OBS Virtual Camera" not in names:
            return ("OBS Virtual Camera was not found.\n\n" + OBS_STEPS.format(app=APP_NAME) +
                    "\n\nStill not working? Update OBS Studio to version 28 or newer.")
        return ("OBS Virtual Camera exists but is not sending video.\n\n"
                "  1. In OBS, click \"Start Virtual Camera\" (it must show as running)\n"
                "  2. Check the OBS preview is showing your game (the ChestDetectorFeed scene)\n"
                f"  3. Run {APP_NAME} again")
    if kind == "device":
        return (f"Could not open the video device ({label}).\n\n"
                "  - Pick the device again in the app's Video source list (Refresh lists them), or run the\n"
                "    diagnostics to see the exact device names\n"
                "  - Many capture cards can only be used by ONE program at a time. If OBS is already using it, "
                "use OBS Virtual Camera (or a screen showing OBS's projector) instead.")
    if kind == "desktop-hdr":
        return ("Could not capture this HDR screen. Refresh the source list and select it again. "
                f"HDR capture needs FFmpeg with 16-bit gfxcapture support.\n\nDetails: {error_text}")
    if kind == "desktop":
        return ("Could not capture the screen. Pick the screen again in the Video source list (Refresh), or count the "
                "stream from its link (Livestream URL) instead.")
    if kind == "stream":
        if "No module named streamlink" in error_text:
            return "The livestream reader (Streamlink) is missing. Extract the whole zip again."
        return (f"Could not open the livestream ({label}).\n\n"
                "  - Is the stream live right now? Offline channels have no video.\n"
                "  - Use the full link: https://www.twitch.tv/CHANNEL, or a YouTube https://www.youtube.com/watch?v=... "
                "(or https://www.youtube.com/@CHANNEL/live)\n"
                "  - Private, members-only, subscriber-only and age-restricted streams cannot be read.\n\n"
                f"Details: {error_text}")
    if kind == "window":
        if "is minimized" in error_text:
            return ("The OBS projector window is minimized, so there is no picture to count.\n\n"
                    "  - Restore it (click it in the taskbar). It may stay behind other windows, but not minimized.\n"
                    "  - Then press Start counting again.")
        if "is not open" in error_text:
            return ("The OBS projector window you picked is not open.\n\n" + sources.WINDOW_STEPS +
                    "\n\nA projector closes with OBS. Tip: OBS > Settings > General > Projectors > \"Save projectors on exit\" "
                    "reopens it every time.")
        return (f"Could not capture the OBS projector window ({label}).\n\n" + sources.WINDOW_STEPS +
                f"\n\nDetails: {error_text}")
    return f"Could not open the video source ({label}). Details: {error_text}"


def friendly_rate_note(res, kind=None):
    if not res:
        return
    if abs(res[0] / res[1] - 16 / 9) > 0.05:
        CONSOLE.warn(f"the video is {res[0]}x{res[1]}, which is not 16:9. The picture will be stretched and the chest "
              f"detection may not work. Make the ChestDetectorFeed scene 16:9 (for example 1920x1080).")
    elif res[0] < 1280:
        hint = (" Make the OBS projector window bigger (at least 1280x720; any shape - black bars are cut off)."
                if kind == "window" else "")
        CONSOLE.warn(f"the video is only {res[0]}x{res[1]}. Detection works best at 1280x720 or higher.{hint}")


def cmd_live(args):
    from fortnite_chest_counter import estimator
    from fortnite_chest_counter.input import video_sources as sources

    banner()
    cfg, origin = make_config(args)
    live = cfg["live"]
    kind, fps = live["source"], cfg["detector"]["process_fps"]
    preflight(cfg)
    if getattr(args, "gui", False):
        # desktop-app worker: no console; the app's Stop button / closing the app arrives through stdin
        control = AppControl()
        with SingleInstance(cfg["state"]["path"]):
            _run_live(args, cfg, origin, live, kind, fps, estimator, sources, control=control)
        return
    disable_quick_edit()
    # exit order: the lock is removed first, then the close guard lets Windows end the process
    with ConsoleCloseGuard() as guard, SingleInstance(cfg["state"]["path"]):
        _run_live(args, cfg, origin, live, kind, fps, estimator, sources, guard)


STATUS_LOG_SECONDS = 300            # a status record in the session log every 5 min (survives a hard kill)


def _open_preview(args):
    """`--preview NAME` (desktop app): the shared-memory picture for its optional live preview. Never fatal."""
    name = getattr(args, "preview", None)
    if not name:
        return None
    try:
        from fortnite_chest_counter.preview import PreviewWriter
        return PreviewWriter(name)
    except Exception as error:                              # the preview is optional; counting is not
        CONSOLE.warn(f"live preview unavailable ({error}); counting is not affected")
        return None


def _open_capture(cfg, kind, label, res, in_fps, fps, mode=None):
    """[capture] mode marks/events/session (or `mode`): start the diagnostic recording. Never fatal - counting goes on."""
    c = dict(cfg["capture"])
    if mode is not None:
        c["mode"] = mode
    if c["mode"] == "off":
        return None
    try:
        from fortnite_chest_counter.capture import Capture
        from fortnite_chest_counter.input import video_sources as sources
        capture = Capture(c["dir"], c["mode"], fps, sources.WIDTH, sources.HEIGHT, c["before_seconds"], c["after_seconds"],
                          c["mark_before_seconds"], c["mark_after_seconds"], ffmpeg=paths.ffmpeg_executable() or "ffmpeg",
                          quality=c["quality"],
                          info={"version": __version__, "source": label, "source_type": kind, "input_resolution": res,
                                "input_fps": in_fps, "detector": cfg["detector"], "profile": profile_label(cfg)})
    except Exception as error:
        CONSOLE.warn(f"diagnostic recording could not start ({error}); counting is not affected")
        return None
    what = {"marks": "video around your marks", "events": "video around every detection and mark"}.get(c["mode"],
                                                                                                      "the whole session")
    CONSOLE.emit("capture", state="recording", mode=c["mode"], folder=str(capture.folder.resolve()))
    if not CONSOLE.machine:
        CONSOLE.info(f"Diagnostic recording ON ({what}) -> {capture.folder}. Press M to mark a chest the counter missed.")
    return capture


def _mark(session, capture, t, label, diagnostics=None):
    """A moment the user flagged (usually: 'the counter missed a chest just now')."""
    row = capture.add_mark(t, label, diagnostics) if capture is not None else {"time": round(t, 3), "stamp": mmss(t), "label": label}
    session.log.write({"event": "mark", **row, "recorded": capture is not None, "logged_at": now_iso()})
    CONSOLE.emit("mark", stamp=row["stamp"], label=label, recorded=capture is not None, mark_id=row.get("mark_id"))
    if not CONSOLE.machine:
        CONSOLE.info(f"Marked {row['stamp']}" + (" - kept in the diagnostic recording" if capture is not None else
                                                 " - noted in the session log (turn on [capture] to keep the video)"))


def _switch_capture(capture, mode, cfg, kind, label, res, in_fps, fps, session, closing):
    """The app changed the Recording choice while counting. The old recording finishes in a background thread (the
    encoder flush must never hold up counting); the new one starts at once, in its own folder. -> the new capture."""
    if mode == (capture.mode if capture is not None else "off"):
        return capture
    if capture is not None:
        import threading
        thread = threading.Thread(target=_close_capture, args=(capture, None, session, 20.0), daemon=True,
                                  name="capture-close")
        thread.start()
        closing.append(thread)
    new = _open_capture(cfg, kind, label, res, in_fps, fps, mode=mode) if mode != "off" else None
    if new is None:
        CONSOLE.emit("capture", state="off", mode=mode)
    return new


def _close_capture(capture, flushed, session, timeout):
    if capture is None:
        return
    try:
        if flushed:
            capture.add_events(flushed)
        summary = capture.close(timeout=timeout)
    except Exception as error:
        CONSOLE.warn(f"the diagnostic recording could not be finished cleanly ({error})")
        return
    session.log.write({"event": "capture", **summary, "logged_at": now_iso()})
    CONSOLE.emit("capture", state="saved", **summary)
    if not CONSOLE.machine:
        try:
            CONSOLE.info(f"Diagnostic recording saved: {summary['clips']} clip(s), {summary['video_mb']} MB, "
                         f"{summary['events']} decisions, {summary['marks']} marks -> {summary['folder']}")
        except (OSError, ValueError):
            pass


def _run_live(args, cfg, origin, live, kind, fps, estimator, sources, guard=None, control=None):
    session = CounterSession(cfg, reset=args.reset)
    state = session.state
    try:
        source = sources.make_source(kind, fps, device=live["device"], monitor=live["monitor"], window=live["window"],
                                     url=live["url"], quality=live["stream_quality"], stability=live["stream_stability"],
                                     video=args.video, start=args.start or 0.0, duration=args.duration)
    except ValueError as error:
        raise UserError(str(error))
    if control is not None:
        control.attach(source.request_stop)
    CONSOLE.info(f"Connecting to {source.label} ...")
    try:
        info = source.open(timeout=15.0)
    except (RuntimeError, OSError) as error:
        if control is not None and control.requested.is_set():
            return                                          # the app cancelled while connecting: nothing to report
        raise UserError(explain_source_failure(kind, source.label, str(error))) from error
    res, in_fps = info["input_resolution"], info["input_fps"]
    CONSOLE.info((f"Connected: video {res[0]}x{res[1]} @ {in_fps} fps" if res else "Connected.")
                 + f" -> analysed at {fps} frames per second")
    friendly_rate_note(res, kind)
    local_hud_scale = paths.local_fortnite_hud_scale() if kind in ("desktop", "desktop-hdr", "window") else None
    if control is not None:
        CONSOLE.emit("connected", source=source.label, kind=kind, resolution=res, input_fps=in_fps, process_fps=fps,
                     hud_scale=local_hud_scale, settings=origin, continuing=session.loaded_existing, persist=session.persist,
                     obs_file=str((session.obs_dir / "chest_counter.txt").resolve()),
                     log=str(session.log.path) if session.log.path else None)
        session.announce()
    else:
        print(f"Settings: {origin}")
        print("Counter: " + (f"continuing from {session.fmt(state['certain'])} certain / {session.fmt(state['approximate'])} approx"
                             if session.loaded_existing else "starting at 0 / 0")
              + ("" if session.persist else " (saving is turned off in config.toml)"))
        viewers, obs_file = overlay_summary(session.display, state, session.obs_dir)
        CONSOLE.line(viewers, "bold")
        CONSOLE.line(f"{obs_file}   (change what viewers see: the app's Overlay page, or the configure command)")
        CONSOLE.line("This window always shows both numbers for you; viewers only see the file above.", "dim")
        print("Counting has started. Counts are kept when you stop and restart.")
        print("To stop: press Q in this window (or Ctrl+C, or just close the window). Nothing is lost.")
        CONSOLE.line(f"Window: {CONSOLE.verbosity} detail, colours {'on' if CONSOLE.colors else 'off'} ([console] in config.toml)\n", "dim")

    est = estimator.ChestEstimator(hud_scale=local_hud_scale)
    if local_hud_scale is not None:
        CONSOLE.info(f"Fortnite HUD: {local_hud_scale:.0%} (local settings); "
                     f"first popup search at {est.preferred_hud_scale:.0%} of captured size.")
    preview = _open_preview(args)
    preview_from_capture = preview is not None and isinstance(source, sources.LiveSource)
    if preview_from_capture:
        source.set_preview(preview)
    capture = _open_capture(cfg, kind, source.label, res, in_fps, fps)
    closing_threads = []                                    # recordings switched off mid-session, finishing in background
    session.save()
    session.write_obs()
    session.log.write({"event": "session_start", "app_version": __version__, "source_type": kind, "source": source.label,
                       "input_resolution": res, "input_fps": in_fps, "normalized": [sources.WIDTH, sources.HEIGHT],
                       "process_fps": fps, "session_started": now_iso(), "config": origin,
                       "fortnite_hud_scale": local_hud_scale, "preferred_popup_scale": est.preferred_hud_scale,
                       "ui": "app" if control is not None else "console", "profile": profile_label(cfg)})
    interval = live["status_interval_seconds"]
    recent = deque(maxlen=300)                              # bounded: a 12-hour stream must not grow memory
    n_frames, total_s, max_s = 0, 0.0, 0.0
    last_status, cpu0, wall0 = time.monotonic(), time.process_time(), time.monotonic()
    last_sync = last_status_log = time.monotonic()
    watcher = DisplayConfigWatcher(args)
    processed_at_status = processed_at_log = 0
    det_fps = None
    stop_reason = "stream ended"
    capture_warned = False
    if guard is not None:
        guard.on_request = source.request_stop

    def apply_pending_reset():
        if control is not None and control.take_reset():
            session.reset_from_app(est)

    if control is not None:
        source.on_idle = apply_pending_reset
    try:
        for t, frame in source.frames():
            if guard is not None and guard.requested.is_set():
                break
            if control is not None and control.requested.is_set():
                stop_reason = control.reason
                break
            if args.duration and t >= args.duration:          # applies to every source type
                stop_reason = "duration reached"
                break
            apply_pending_reset()
            t0 = time.perf_counter()
            applied = session.apply(est.process_frame(t, frame), t)
            session.apply_type_updates(est.take_type_updates())
            dt = time.perf_counter() - t0
            recent.append(dt)
            n_frames, total_s, max_s = n_frames + 1, total_s + dt, max(max_s, dt)
            if preview is not None and not preview_from_capture:
                preview.offer(t, frame)                     # sources without an independent frame reader
            if capture is not None:                         # after counting; never blocks (see capture.py)
                capture.offer(t, frame)
                if applied:
                    capture.add_events(applied)
                if capture.stopped_reason and not capture_warned:
                    capture_warned = True
                    CONSOLE.warn(f"diagnostic recording stopped: {capture.stopped_reason}. Counting continues.")
            now_m = time.monotonic()
            if control is not None:
                for mode in control.take_capture_requests():
                    capture = _switch_capture(capture, mode, cfg, kind, source.label, res, in_fps, fps, session,
                                               closing_threads)
                    capture_warned = False
            marks = control.take_marks() if control is not None else []
            if control is None:
                keys = console_keys()
                if "q" in keys:
                    stop_reason = "Q pressed"
                    break
                marks += ["mark"] if "m" in keys else []
            for label in marks:
                diagnostics = None
                if label == "missed chest" and capture is not None:
                    diagnostics = {"hud_scale": local_hud_scale, "target_fps": fps,
                                   "measured_fps": round(det_fps, 1) if det_fps is not None else None,
                                   "skipped_frames": source.frames_dropped}
                _mark(session, capture, t, label, diagnostics)
            if now_m - last_sync >= 1.0:
                last_sync = now_m
                for level, note in getattr(source, "take_notices", list)():     # e.g. the projector window was minimized
                    (CONSOLE.warn if level == "warn" else CONSOLE.info)(note)
                if session.sync_external_changes():
                    CONSOLE.info(f"Counter changed by Reset / Set Starting Count -> CERTAIN {session.fmt(state['certain'])} | "
                                 f"APPROX {session.fmt(state['approximate'])}")
                    session.announce()
                watcher.check(session)
            if now_m - last_status >= interval:
                span = now_m - last_status
                det_fps = (source.frames_processed - processed_at_status) / span
                backlog = source.backlog_seconds() if hasattr(source, "backlog_seconds") else None   # livestream only
                CONSOLE.status(f"{det_fps:4.1f} fps (source {in_fps or '?'}) | dropped {session.fmt(source.frames_dropped)} "
                               f"| {1000 * np.mean(recent):.0f} ms/frame | CERTAIN {session.fmt(state['certain'])} "
                               f"| APPROX {session.fmt(state['approximate'])}"
                               + (f" | backlog {backlog:.0f} s" if backlog is not None else ""),
                               fps=round(det_fps, 1), source_fps=in_fps, dropped=source.frames_dropped,
                               ms_per_frame=round(1000 * float(np.mean(recent)), 1),
                               certain=state["certain"], approx=state["approximate"],
                               backlog_s=round(backlog, 1) if backlog is not None else None)
                last_status, processed_at_status = now_m, source.frames_processed
            if now_m - last_status_log >= STATUS_LOG_SECONDS:
                span = now_m - last_status_log
                session.log.write({"event": "status", "t": round(t, 1), "logged_at": now_iso(),
                                   "detector_fps": round((source.frames_processed - processed_at_log) / span, 2),
                                   "frames_dropped_total": source.frames_dropped,
                                   "avg_frame_ms_recent": round(1000 * float(np.mean(recent)), 1),
                                   "max_frame_ms_so_far": round(1000 * max_s, 1),
                                   "backlog_s": (round(source.backlog_seconds(), 1)
                                                 if hasattr(source, "backlog_seconds") else None),
                                   "certain": state["certain"], "approximate": state["approximate"]})
                last_status_log, processed_at_log = now_m, source.frames_processed
        else:
            if guard is not None:
                # Closing the window sends the close event to every process in it; FFmpeg usually exits first, so
                # the video "ends" a moment before our own handler runs. Give it a moment to tell the two apart.
                guard.requested.wait(0.5)
            if control is not None and control.requested.is_set():
                stop_reason = control.reason                # the app's Stop ended the frame source
            if stop_reason == "stream ended" and kind in ("obs-camera", "device", "desktop", "desktop-hdr", "window", "stream") \
                    and not (guard is not None and guard.requested.is_set()):
                stop_reason = "video source stopped"
                if kind == "window":
                    CONSOLE.warn(f"{getattr(source, 'ended_reason', None) or 'the projector window stopped sending pictures'}"
                                 ", so counting stopped. Open it again in OBS (right-click the source > Windowed Projector "
                                 "(Source)) and start counting again.")
                else:
                    CONSOLE.warn("the video stopped coming in (was OBS or the Virtual Camera closed?).")
    except KeyboardInterrupt:
        stop_reason = "Ctrl+C"
    except OSError:
        if not (guard is not None and guard.requested.is_set()):
            raise                                           # a real error; only a vanished console is expected here
    finally:
        closing = guard is not None and guard.requested.is_set()
        if closing:
            stop_reason = guard.reason or "window closed (X)"
        # Persist first (the console may already be gone when the window was closed), print last.
        apply_pending_reset()
        session.sync_external_changes()
        flushed = session.apply(est.flush(), source.elapsed(), quiet=closing)
        session.apply_type_updates(est.take_type_updates())
        session.save()
        session.write_obs()
        if preview_from_capture:
            source.set_preview(None)
        source.close(wait=not closing)
        for thread in closing_threads:                     # recordings switched off mid-session: let them finish
            thread.join(3.0 if closing else 30.0)
        _close_capture(capture, flushed, session, timeout=3.0 if closing else 20.0)
        wall = max(time.monotonic() - wall0, 1e-9)
        stats = {"event": "session_end", "stop_reason": stop_reason, "seconds": round(wall, 1),
                 "frames_processed": source.frames_processed, "frames_dropped": source.frames_dropped,
                 "detector_fps": round(source.frames_processed / wall, 2),
                 "avg_frame_ms": round(1000 * total_s / n_frames, 1) if n_frames else None,
                 "max_frame_ms": round(1000 * max_s, 1) if n_frames else None,
                 "cpu_percent_of_one_core": round(100 * (time.process_time() - cpu0) / wall, 1),
                 "counted_event_latency_s": {"avg": round(float(np.mean(session.latencies)), 2) if session.latencies else None,
                                             "max": round(float(np.max(session.latencies)), 2) if session.latencies else None},
                 "certain": state["certain"], "approximate": state["approximate"]}
        session.log.write(stats)
        if preview is not None:
            preview.close()
        if control is not None:                            # (no `return` in a finally: it would swallow a crash)
            session.announce()
            CONSOLE.emit("stopped", **{k: v for k, v in stats.items() if k != "event"},
                         log=str(session.log.path) if session.log.path else None)
        else:
            try:
                print(f"\nStopped ({stop_reason}). Analysed {stats['frames_processed']:,} frames in "
                      f"{stats['seconds'] / 60:.1f} min, {stats['frames_dropped']:,} dropped. (Full details: {session.log.path or 'logging is off'})")
                CONSOLE.line(f"{display_text(session.display, 'certain', state)} | {display_text(session.display, 'approx', state)}", "bold")
            except (OSError, ValueError):
                pass                                        # no console any more (window closed)


def cmd_devices(args):
    from fortnite_chest_counter.input import video_sources as sources

    banner()
    if not paths.ffmpeg_executable():
        raise UserError("The video helper (ffmpeg) was not found; see the README.")
    print("Video devices (usable with: live --source device --device \"NAME\"):")
    found = False
    for name, kind in sources.list_video_devices():
        if kind in ("video", "none"):
            found = True
            note = "   <- start 'Start Virtual Camera' in OBS to use the normal mode" if name == "OBS Virtual Camera" else ""
            print(f"  {name}{note}")
    if not found:
        print("  (none found)")
    print("\nMonitors (usable with: live --source desktop --monitor N):")
    for i, (x, y, w, h) in enumerate(sources.list_monitors(), 1):
        print(f"  {i}: {w}x{h} at ({x},{y})")


def profile_label(cfg):
    """The profile name recorded in logs and captures (None when an explicit --state file is used)."""
    return (cfg.get("profile") or {}).get("name")


def show_counts(session):
    CONSOLE.line(f"  {display_text(session.display, 'certain', session.state)}", "green")
    CONSOLE.line(f"  {display_text(session.display, 'approx', session.state)}", "yellow")


def cmd_show(args):
    banner()
    cfg, _ = make_config(args)
    preflight(cfg, needs_video=False)
    session = CounterSession(cfg)
    session.write_obs()
    print(f"\nCurrent counter{' - profile ' + profile_label(cfg) if profile_label(cfg) else ''}:")
    show_counts(session)
    print("\n  (CERTAIN is part of APPROX: APPROX is the estimated total.)")
    for line in overlay_summary(session.display, session.state, session.obs_dir):
        CONSOLE.line(f"  {line}")
    if args.json:
        print(json.dumps(session.state, indent=2, ensure_ascii=False))


def cmd_reset(args):
    banner()
    cfg, _ = make_config(args)
    preflight(cfg, needs_video=False)
    old = CounterSession(cfg)
    session = CounterSession(cfg, reset=True)
    session.save()
    session.write_obs()
    session.log.write({"event_id": "MANUAL", "source": "manual", "action": "reset", "previous": {
        "certain": old.state["certain"], "approximate": old.state["approximate"]}, "logged_at": now_iso()})
    print(f"\nWas: CERTAIN {old.fmt(old.state['certain'])} / APPROX {old.fmt(old.state['approximate'])}")
    print("\nCounter reset:")
    print(f"  CERTAIN: {session.fmt(0)}")
    print(f"  APPROX: {session.fmt(0)}")
    if counter_is_running(cfg["state"]["path"]):
        print("\nIf the counter window is open, it picks up the new numbers within a second.")


def cmd_adjust(args):
    banner()
    cfg, _ = make_config(args)
    preflight(cfg, needs_video=False)
    session = CounterSession(cfg)
    st = session.state
    st["certain"] = max(st["certain"] + args.certain, 0)
    st["approximate"] = max(st["approximate"] + args.approx, 0)
    st["approximate"] = max(st["approximate"], st["certain"])      # APPROX is the total and includes CERTAIN
    st["events_logged"] += 1
    session.save()
    session.write_obs()
    session.log.write({"event_id": f"E{st['events_logged']:04d}", "source": "manual", "action": "adjust",
                       "certain_increment": args.certain, "approx_increment": args.approx,
                       "counts_after": {"certain": st["certain"], "approximate": st["approximate"]}, "logged_at": now_iso()})
    print("Counter adjusted:")
    show_counts(session)


# ---- configure: choose what viewers see without editing files (the app's Overlay page uses the same helpers) -----------------------------

def toml_string(text):
    """A TOML basic string for any text (quotes, backslashes, new lines, emoji)."""
    escaped = (str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
               .replace("\t", "\\t"))
    return f'"{escaped}"'


def update_config_file(path, section, values):
    """Set `key = value` lines in one [section] of config.toml, keeping every other line and comment. Creates the
    file from config.example.toml (or empty) if it does not exist. The result is checked by loading it; if that would
    produce a warning for a value we wrote, the original file is restored and UserError is raised."""
    import re
    path = Path(path)
    original = path.read_bytes() if path.exists() else None
    if original is not None:
        lines = original.decode("utf-8-sig").splitlines()
    else:
        example = path.with_name("config.example.toml")
        lines = example.read_text(encoding="utf-8-sig").splitlines() if example.exists() else []
    header = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    start = next((i for i, l in enumerate(lines) if (m := header.match(l)) and m.group(1).strip() == section), None)
    if start is None:
        lines += ["", f"[{section}]"]
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
    for key, value in values.items():
        text = toml_string(value) if isinstance(value, str) else ("true" if value is True else "false" if value is False else str(value))
        found = next((i for i in range(start + 1, end) if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i])), None)
        if found is not None:
            lines[found] = f"{key} = {text}"
        else:
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1                              # keep the blank line before the next section
            lines.insert(insert, f"{key} = {text}")
            end += 1
    atomic_write(path, "\n".join(lines) + "\n")
    cfg, _, warnings = settings.load_config(path)
    bad = [w for w in warnings if any(f"] {k} =" in w for k in values)]
    if bad or any(cfg[section][k] != v for k, v in values.items()):
        if original is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, original.decode("utf-8-sig"))
        raise UserError("That text could not be saved (" + ("; ".join(bad) or "it did not read back the same") + "). Nothing was changed.")
    return path


def text_from_hash(text):
    """'Chests opened: #' -> 'Chests opened: {count}'. Braces are kept literal; no # -> the number goes at the end."""
    literal = str(text).replace("{", "{{").replace("}", "}}")
    if "#" not in literal:
        literal = literal.rstrip() + " #"
    return literal.replace("#", "{count}")


def hash_from_text(template):
    """The reverse, to show a user their current text: 'Chests: {count}' -> 'Chests: #'."""
    return str(template).replace("{count}", "#").replace("{{", "{").replace("}}", "}")


CONFIGURE_CHOICES = {"1": "approx", "2": "certain", "3": "inline", "4": "stacked"}
MODE_SETTINGS = {"approx": {"show": "approx"}, "certain": {"show": "certain"},
                 "inline": {"show": "both", "both_layout": "inline"}, "stacked": {"show": "both", "both_layout": "stacked"}}


def line_from_markers(text):
    """One-line wording: '#C confirmed • #A total' -> '{certain} confirmed • {approx} total' (braces kept literal)."""
    import re
    literal = str(text).replace("{", "{{").replace("}", "}}")
    literal = re.sub("#a", "{approx}", re.sub("#c", "{certain}", literal, flags=re.I), flags=re.I)
    if "{approx}" not in literal and "{certain}" not in literal:
        literal = "{certain} / {approx} " + literal.strip()
    return literal


def markers_from_line(template):
    return (str(template).replace("{certain}", "#C").replace("{approximate}", "#A").replace("{approx}", "#A")
            .replace("{{", "{").replace("}}", "}"))


def _ask(question, valid=None):
    """input() that returns '' on Enter/EOF and re-asks until the answer is in `valid` (if given)."""
    while True:
        try:
            answer = input(question).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if not answer or valid is None or answer in valid:
            return answer
        CONSOLE.warn(f"'{answer}' is not one of {', '.join(sorted(valid))}. Try again.")


def cmd_configure(args):
    """Choose what viewers see (the single OBS file chest_counter.txt) and its wording, then save config.toml.
    Asks if no options are given."""
    banner()
    cfg, _ = make_config(args)
    preflight(cfg, needs_video=False)
    session = CounterSession(cfg)
    display = cfg["display"]
    path = config_path(args)
    defaults = settings.DEFAULTS["display"]
    mode, texts = display_mode(display), {}

    if args.show or args.layout or args.approx_text is not None or args.certain_text is not None or args.line_text is not None:
        if args.show == "both" or (args.show is None and args.layout):
            mode = args.layout or ("inline" if display_mode(display) == "inline" else "stacked")
        elif args.show:
            mode = args.show
        if args.approx_text is not None:
            texts["approx_text"] = text_from_hash(args.approx_text)
        if args.certain_text is not None:
            texts["certain_text"] = text_from_hash(args.certain_text)
        if args.line_text is not None:
            texts["combined_status_format"] = line_from_markers(args.line_text)
        confirm = False
    else:
        state = dict(session.state)
        examples = {m: overlay_text({**display, **MODE_SETTINGS[m]}, state).replace("\n", "  /  ") for m in MODE_SETTINGS}
        CONSOLE.line("\nWhat should OBS show?   (OBS always reads obs_output\\chest_counter.txt - you never change that)", "bold")
        for key, m in CONFIGURE_CHOICES.items():
            CONSOLE.line(f"  {key}) {MODE_DESCRIPTIONS[m]:<27s} e.g. {examples[m]}")
        current = {v: k for k, v in CONFIGURE_CHOICES.items()}[mode]
        answer = _ask(f"Type 1-4 and press Enter (just Enter = keep {current}, {MODE_DESCRIPTIONS[mode]}): ", set(CONFIGURE_CHOICES))
        mode = CONFIGURE_CHOICES.get(answer, mode)
        CONSOLE.line('\nNow the wording (just Enter = keep it, "default" = original text).')
        if mode == "inline":
            CONSOLE.line("Put #C where the confirmed number goes and #A where the estimated total goes.  e.g.  #C confirmed • #A total")
            typed = _ask(f"One-line text (now: {markers_from_line(display['combined_status_format'])}): ")
            if typed.lower() == "default":
                texts["combined_status_format"] = defaults["combined_status_format"]
            elif typed:
                texts["combined_status_format"] = line_from_markers(typed)
        else:
            CONSOLE.line("Put # where the number goes.  e.g.  Chests opened: #   |   Chest Hunt: #/500   |   # chests")
            for which in {"approx": ["approx"], "certain": ["certain"], "stacked": ["certain", "approx"]}[mode]:
                key = f"{which}_text"
                label = "estimated total" if which == "approx" else "confirmed chests"
                typed = _ask(f"Text for the {label} (now: {hash_from_text(display[key])}): ")
                if typed.lower() == "default":
                    texts[key] = defaults[key]
                elif typed:
                    texts[key] = text_from_hash(typed)
        confirm = True

    changes = {**MODE_SETTINGS[mode], **texts}
    preview = {**display, **changes}
    if not any(display.get(k) != v for k, v in changes.items()):
        CONSOLE.line("\nNothing changed.")
    else:
        CONSOLE.line("\nPreview - OBS will show:", "bold")
        for line in overlay_text(preview, session.state).split("\n"):
            CONSOLE.line(f"    {line}", "green")
        if confirm and _ask("Press Enter to save it, or type n and Enter to cancel: ").lower().startswith("n"):
            CONSOLE.line("Cancelled - nothing was changed.")
            return
        update_config_file(path, "display", changes)
        cfg, _ = make_config(args)
        session.display = cfg["display"]
        CONSOLE.line(f"Saved in {path.resolve()}.", "green")
    session.write_obs()
    viewers, obs_file = overlay_summary(session.display, session.state, session.obs_dir)
    CONSOLE.line(viewers, "bold")
    CONSOLE.line(obs_file)
    CONSOLE.line("In OBS, your Text (GDI+) source must have \"Read from file\" ticked and point to that file.")
    if counter_is_running(cfg["state"]["path"]):
        CONSOLE.info("The counter window is open: it applies this within a second.")


MAX_STARTING_COUNT = 1_000_000


def parse_count(text):
    """'150', '1,200', ' 75 ' -> int. Negative, decimal, empty, non-numeric or > MAX_STARTING_COUNT -> (None, why)."""
    raw = str(text).strip()
    cleaned = raw.replace(",", "").replace("_", "").replace(" ", "")
    if not cleaned:
        return None, "nothing was typed"
    if cleaned.startswith("-"):
        return None, "it cannot be negative"
    if "." in cleaned:
        return None, "please type a whole number (no decimals)"
    if not (cleaned.isascii() and cleaned.isdigit()):
        return None, "please type a number, like 150"
    value = int(cleaned)
    if value > MAX_STARTING_COUNT:
        return None, f"that is more than {MAX_STARTING_COUNT:,}; please check the number"
    return value, ""


def cmd_set_count(args):
    """Start from the chests a streamer already opened: APPROX = that total, CERTAIN = 0 (the detector did not see them).
    Same save path as reset/adjust, so a running counter picks it up through its normal file sync."""
    banner()
    cfg, _ = make_config(args)
    preflight(cfg, needs_video=False)
    session = CounterSession(cfg)
    st = session.state
    CONSOLE.line(f"\nCurrent counter: CERTAIN {session.fmt(st['certain'])} | APPROX {session.fmt(st['approximate'])}")
    if args.count is not None:
        total, why = parse_count(args.count)
        if total is None:
            raise UserError(f"'{args.count}' is not a valid number of chests: {why}. Nothing was changed.")
    else:
        CONSOLE.line("Type the number of chests you have already opened and press Enter (just Enter = cancel).")
        while True:
            try:
                answer = input("How many chests have you already opened? ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if not answer.strip():
                CONSOLE.line("\nNothing was changed.")
                return
            total, why = parse_count(answer)
            if total is not None:
                break
            CONSOLE.warn(f"'{answer.strip()}' was not accepted: {why}. Nothing was changed - try again.")
    certain = args.certain
    if certain < 0 or certain > total:
        raise UserError(f"--certain must be between 0 and {total:,} (CERTAIN is part of APPROX). Nothing was changed.")
    previous = {"certain": st["certain"], "approximate": st["approximate"]}
    st["certain"], st["approximate"] = certain, total
    st["events_logged"] += 1
    session.save()
    session.write_obs()
    session.log.write({"event_id": f"E{st['events_logged']:04d}", "source": "manual", "action": "set_starting_count",
                       "previous": previous, "counts_after": {"certain": certain, "approximate": total}, "logged_at": now_iso()})
    CONSOLE.line("\nStarting count set:", "bold")
    show_counts(session)
    CONSOLE.line(f"\nNew chests are added on top of {session.fmt(total)}. (CERTAIN starts at {session.fmt(certain)}: "
                 "chests opened before the counter ran were not seen by it.)")
    if counter_is_running(cfg["state"]["path"]):
        CONSOLE.info("The counter window is open: it picks up the new numbers within a second.")


def set_counts(cfg, certain, approx, action="set_count", *, match_fingerprint=None):
    """The desktop app's Reset / Edit counts. Same file, OBS files and log record as the reset / set-count commands, so
    a running counter adopts the new numbers within a second (CounterSession.sync_external_changes). -> the session."""
    for value in (certain, approx):
        if not _valid_count(value) or value > MAX_STARTING_COUNT:
            raise UserError(f"Counts must be whole numbers from 0 to {MAX_STARTING_COUNT:,}. Nothing was changed.")
    if certain > approx:
        raise UserError("CERTAIN cannot be higher than APPROX: APPROX is the total and includes the certain chests. "
                        "Nothing was changed.")
    preflight(cfg, needs_video=False)
    old = CounterSession(cfg)
    previous = {"certain": old.state["certain"], "approximate": old.state["approximate"]}
    session = CounterSession(cfg, reset=(action == "reset"))
    st = session.state
    st["certain"], st["approximate"] = certain, approx
    if match_fingerprint is not None:
        st["match_fingerprint"] = match_fingerprint
    if action != "reset":
        st["events_logged"] += 1
    session.save()
    session.write_obs()
    record = {"event_id": "MANUAL" if action == "reset" else f"E{st['events_logged']:04d}", "source": "manual",
              "action": action, "previous": previous, "logged_at": now_iso()}
    if action != "reset":
        record["counts_after"] = {"certain": certain, "approximate": approx}
    session.log.write(record)
    return session


def short_path(path):
    """Paths in support reports: hide the Windows user name."""
    text = str(path)
    home = str(Path.home())
    return text.replace(home, "%USERPROFILE%") if home and home in text else text


def cmd_diagnostics(args):
    import platform
    from fortnite_chest_counter.input import video_sources as sources

    lines = []

    def out(text=""):
        lines.append(text)
        print(text)

    out(f"=== {APP_NAME} diagnostics ===")
    out(f"App version:      {__version__} ({RELEASE_CHANNEL}), {'packaged' if paths.PACKAGED else 'running from source'}")
    out(f"Windows:          {platform.platform()}")
    out(f"Program folder:   {short_path(paths.APP_DIR)}")
    cfg, origin, warnings = settings.load_config(getattr(args, "config", None))
    out(f"Settings:         {origin}" + (f"  ({len(warnings)} warning(s))" if warnings else ""))
    for w in warnings:
        out(f"  ! {w}")
    try:
        reg = profiles.load(cfg["state"]["path"], warn=lambda m: out(f"  ! {m}"))
        out(f"Profiles:         {len(reg['profiles'])} (active: {profiles.active(reg)['name']})")
    except Exception as error:
        out(f"Profiles:         could not be read ({error})")
    det = cfg["detector"]
    out(f"Detector:         {det['process_fps']} fps | languages {det['languages']} | certain_min_tier {det['certain_min_tier']} | "
        f"medium_counts_alone {det['medium_counts_alone_languages']}")
    out(f"Live source:      {cfg['live']['source']}" + (f"  (window {cfg['live']['window']!r})"
                                                          if cfg['live']['source'] == "window" else
                                                          f"  ({cfg['live']['url']}, {cfg['live']['stream_quality']})"
                                                          if cfg['live']['source'] == "stream" else ""))
    try:
        sl = subprocess.run([sys.executable, "-m", "streamlink", "--version"], capture_output=True, text=True, timeout=30,
                            stdin=paths.child_stdin(), creationflags=paths.child_process_flags())
        out("Streamlink:       " + ((sl.stdout or sl.stderr).strip().splitlines() or ["?"])[-1][:80])
    except Exception as error:
        out(f"Streamlink:       could not run ({error})")
    ff = paths.ffmpeg_executable()
    if ff:
        try:
            version_line = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10,
                                          stdin=paths.child_stdin(),
                                          creationflags=paths.child_process_flags()).stdout.splitlines()[0]
        except Exception as error:
            version_line = f"could not run ({error})"
        out(f"FFmpeg:           {short_path(ff)}  [{version_line}]")
    else:
        out("FFmpeg:           NOT FOUND")
    missing = paths.missing_templates()
    out("Templates:        " + ("all present" if not missing else "MISSING: " + ", ".join(m.name for m in missing)))
    for label, folder in (("state", Path(cfg["state"]["path"]).parent), ("obs_output", Path(cfg["state"]["obs_dir"])),
                          ("logs", Path(cfg["logging"]["dir"]))):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            status = "writable"
        except OSError as error:
            status = f"NOT writable ({error})"
        out(f"Folder {label + ':':11s} {status}")
    try:
        state = load_state(cfg["state"]["path"], warn=lambda m: out(f"  ! {m}"))
        out(f"Counter:          CERTAIN {state['certain']} / APPROX {state['approximate']}")
    except Exception as error:
        out(f"Counter:          unreadable ({error})")
    if ff:
        try:
            devices = [(n, k) for n, k in sources.list_video_devices() if k in ("video", "none")]
        except Exception as error:
            devices = []
            out(f"Video devices:    could not list ({error})")
        else:
            out("Video devices:    " + (", ".join(n for n, _ in devices) if devices else "(none found)"))
        if any(n == "OBS Virtual Camera" for n, _ in devices):
            try:
                src = sources.make_source("obs-camera", det["process_fps"])
                info = src.open(timeout=8.0)
                src.close()
                res = info["input_resolution"]
                out(f"OBS Virtual Camera: running, sending video {res[0]}x{res[1]} @ {info['input_fps']} fps" if res
                    else "OBS Virtual Camera: running, sending video")
            except Exception:
                out("OBS Virtual Camera: found but NOT sending video (click 'Start Virtual Camera' in OBS)")
        else:
            out("OBS Virtual Camera: NOT found (open OBS Studio 28+ and click 'Start Virtual Camera')")
        try:
            out("Monitors:         " + ", ".join(f"{w}x{h}" for _, _, w, h in sources.list_monitors()))
        except Exception:
            pass
    try:
        windows = sources.list_obs_windows()
        out("OBS windows:      " + (", ".join(repr(t) + (" (minimized)" if m else "") for t, m in windows)
                                    if windows else "(no projector or other OBS window open)"))
    except Exception as error:
        out(f"OBS windows:      could not list ({error})")
    _diagnose_obs_websocket(cfg["obs_websocket"], out)
    out("\n(Copy everything above into your support message. It contains no account or personal data;"
        " device names are listed as Windows reports them.)")
    try:
        target = Path(cfg["logging"]["dir"]) / "diagnostics.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"A copy was saved to {target}")
    except OSError:
        pass


def _diagnose_obs_websocket(ws, out):
    """Support report: the optional OBS WebSocket output - settings, and a short live check. Never the password."""
    if not ws["enabled"]:
        out("OBS WebSocket:    off (OBS reads the text file)")
        return
    from fortnite_chest_counter.obs import client as obs
    from fortnite_chest_counter.obs import credentials
    out(f"OBS WebSocket:    on -> {ws['host']}:{ws['port']}, Text source {ws['source']!r}, "
        f"password saved: {'yes' if credentials.has_password() else 'no'}")
    try:
        client = obs.ObsClient(ws["host"], ws["port"], credentials.load_password(), timeout=3.0).connect()
    except obs.ObsError as error:
        out(f"                  NOT connected: {error}")
        return
    try:
        found = obs.list_text_sources(client)
        target = obs.find_source(found, ws["source"], ws["source_uuid"])
        if target is None:
            detail = f"Text source NOT found ({len(found)} Text source(s) in OBS)"
        else:
            on, _ = obs.reads_file(client, target)
            detail = "Text source found" + (", but it has 'Read from file' ticked" if on else ", updated directly")
        out(f"                  connected to {client.version_text}; {detail}")
    except obs.ObsError as error:
        out(f"                  connected, but: {error}")
    finally:
        client.close()


def cmd_review(args):
    """Replay diagnostic captures through the current detector (review.py). --gui: JSON progress for the app."""
    from fortnite_chest_counter import review
    banner()
    folder = Path(args.folder) if args.folder else Path(settings.load_config(getattr(args, "config", None))[0]["capture"]["dir"])
    gui = getattr(args, "gui", False)
    control = AppControl() if gui else None
    started = time.monotonic()
    try:
        captures = review.run(folder, current_settings=args.current_settings,
                              out=(lambda text: None) if gui else (lambda text: CONSOLE.line(text)),
                              events=(lambda kind, **data: CONSOLE.emit(f"review_{kind}", **data)) if gui else None,
                              should_stop=(lambda: control.requested.is_set()) if gui else None)
    except FileNotFoundError as error:
        raise UserError(f"{error}.\nTurn on diagnostic recording (Dashboard > Recording, or [capture] mode in config.toml) "
                        "and count a session first.")
    except review.Stopped:
        CONSOLE.emit("review_stopped", minutes=round((time.monotonic() - started) / 60, 1))
        return
    CONSOLE.emit("review_finished", captures=len(captures), minutes=round((time.monotonic() - started) / 60, 1))


def cmd_profiles(args):
    """List the count profiles (and optionally make one active - the desktop app shows the same list)."""
    banner()
    cfg, _, warnings = settings.load_config(getattr(args, "config", None), config_overrides(args))
    for w in warnings:
        CONSOLE.warn(w)
    base = cfg["state"]["path"]
    try:
        if args.activate:
            entry = profiles.find(profiles.load(base, warn=CONSOLE.warn), args.activate)
            if counter_is_running(profiles.profile_state_path(base, profiles.load(base)["active"])):
                raise UserError("A counter is running for the active profile. Stop it before switching profiles.")
            profiles.set_active(base, entry["id"])
            CONSOLE.info(f'Active profile: {entry["name"]}')
        reg = profiles.load(base, warn=CONSOLE.warn)
    except profiles.ProfileError as error:
        raise UserError(str(error))
    display = cfg["display"]
    print("\nProfiles (the active one is counted and shown in OBS):")
    for p in reg["profiles"]:
        state = load_state(profiles.profile_state_path(base, p["id"]), warn=CONSOLE.warn)
        mark = "*" if p["id"] == reg["active"] else " "
        print(f"  {mark} {p['name']:<{profiles.MAX_NAME}}  CERTAIN {settings.format_count(state['certain'], display['use_thousands_separator'])}"
              f" | APPROX {settings.format_count(state['approximate'], display['use_thousands_separator'])}")


def cmd_evaluate(args):
    events = [json.loads(line) for line in Path(args.log).read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [e for e in events if e.get("source") not in ("manual", None) and "time" in e]
    evaluate(events, args.name)


def report_crash(error, log_dir="logs"):
    """Unexpected bug: keep the full traceback in logs/ for a bug report, show the user something calm."""
    import platform
    import traceback

    where = None
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        where = log_dir / f"crash_{datetime.now():%Y%m%d_%H%M%S}.txt"
        where.write_text(f"{APP_NAME} v{__version__} on {platform.platform()}\n\n"
                         + "".join(traceback.format_exception(type(error), error, error.__traceback__)), encoding="utf-8")
    except OSError:
        pass
    CONSOLE.line("")
    CONSOLE.error(f"Something unexpected went wrong ({type(error).__name__}: {error}).")
    print("Your counter is safe - it is saved after every chest.")
    if where:
        print(f"Technical details were saved to: {where}")
    print("If it keeps happening, send that file (and the diagnostics report: Help & diagnostics in the app) when you "
          "report the problem.")


def build_parser():
    parser = argparse.ArgumentParser(prog="Fortnite Visual Chest Counter", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} v{__version__} ({RELEASE_CHANNEL})")
    sub = parser.add_subparsers(dest="command")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="config file (default: config.toml if it exists)")
    common.add_argument("--state", help="override [state] path")
    common.add_argument("--obs-dir", help="override [state] obs_dir")
    common.add_argument("--log-dir", help="override [logging] dir")
    common.add_argument("--process-fps", type=int, help="override [detector] process_fps")
    common.add_argument("--profile", help="use this count profile (name) instead of the active one")

    p = sub.add_parser("simulate", parents=[common], help="(development) run a recorded video through the live pipeline")
    p.add_argument("--name", help="recording name, e.g. Fortnite-PS5-Pro")
    p.add_argument("--video", help="explicit HD video path (overrides --name)")
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--until", type=float)
    p.add_argument("--realtime", action="store_true", help="pace processing to video time (default: as fast as possible)")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--no-evaluate", dest="evaluate", action="store_false")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("live", parents=[common], help="count chests from a live video source (the default)")
    p.add_argument("--source", choices=settings.LIVE_SOURCES,
                   help="override [live] source")
    p.add_argument("--url", help="livestream URL for --source stream (Twitch / YouTube)")
    p.add_argument("--device", help="DirectShow device name for --source device")
    p.add_argument("--window", help="exact window title for --source window (an OBS Windowed Projector)")
    p.add_argument("--monitor", type=int, help="monitor number for --source desktop or desktop-hdr")
    p.add_argument("--video", help="video file for --source file (played in real time; for testing)")
    p.add_argument("--start", type=float, help="start offset in seconds for --source file")
    p.add_argument("--duration", type=float, help="stop after this many seconds (default: run until Ctrl+C)")
    p.add_argument("--reset", action="store_true", help="zero the counts first (counts are otherwise kept)")
    p.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)        # desktop-app worker: JSON lines, stdin stop
    p.add_argument("--preview", help=argparse.SUPPRESS)                         # desktop-app preview block name
    p.add_argument("--capture-mode", choices=settings.CAPTURE_MODES, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("video", parents=[common], help="count chests in a recorded gameplay video file")
    p.add_argument("video", help="path to the video file")
    p.add_argument("--start", type=float, help="start at this many seconds")
    p.add_argument("--until", type=float, help="stop at this many seconds")
    p.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_video)

    sub.add_parser("devices", help="list video devices and monitors").set_defaults(func=cmd_devices)
    sub.add_parser("diagnostics", parents=[common], help="print a support report").set_defaults(func=cmd_diagnostics)
    p = sub.add_parser("show", parents=[common], help="print the current counts")
    p.add_argument("--json", action="store_true", help="also print the raw saved state")
    p.set_defaults(func=cmd_show)
    sub.add_parser("reset", parents=[common], help="set both counts to 0").set_defaults(func=cmd_reset)
    p = sub.add_parser("configure", parents=[common], help="choose what viewers see in OBS and its wording (asks if no options)")
    p.add_argument("--show", choices=list(settings.SHOW_MODES), help="what chest_counter.txt shows")
    p.add_argument("--layout", choices=list(settings.BOTH_LAYOUTS), help='with --show both: "inline" (one line) or "stacked" (two lines)')
    p.add_argument("--line-text", help='one-line text for "inline": #C = confirmed, #A = estimated (e.g. "#C confirmed • #A total")')
    p.add_argument("--approx-text", help='text for the estimated total, # = the number (e.g. "Chests opened: #")')
    p.add_argument("--certain-text", help="text for the confirmed chests, # = the number")
    p.set_defaults(func=cmd_configure)
    p = sub.add_parser("set-count", parents=[common],
                       help="start from the chests you already opened (sets APPROX; CERTAIN starts at 0)")
    p.add_argument("count", nargs="?", help="chests already opened (asked for if left out)")
    p.add_argument("--certain", type=int, default=0, help="advanced: also set CERTAIN (must not exceed the count)")
    p.set_defaults(func=cmd_set_count)
    p = sub.add_parser("adjust", parents=[common], help="manually correct the counts")
    p.add_argument("--certain", type=int, default=0)
    p.add_argument("--approx", type=int, default=0)
    p.set_defaults(func=cmd_adjust)
    p = sub.add_parser("review", parents=[common], help="replay diagnostic captures through the current detector")
    p.add_argument("folder", nargs="?", help="a capture folder, or a folder of them (default: [capture] dir)")
    p.add_argument("--current-settings", action="store_true",
                   help="use config.toml's detector settings instead of the ones recorded with each capture")
    p.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_review)
    p = sub.add_parser("profiles", parents=[common], help="list the count profiles (one per person)")
    p.add_argument("--activate", metavar="NAME", help="make this profile the active one")
    p.set_defaults(func=cmd_profiles)
    p = sub.add_parser("evaluate", help="(development) score a session log against verified knowledge")
    p.add_argument("--name", required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(func=cmd_evaluate)
    return parser


def main(argv=None):
    paths.setup_environment()                 # run from the program folder; prefer the bundled ffmpeg
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:                              # double-clicking the .exe = the normal live mode
        argv = ["live"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "gui", False):            # the desktop app's worker: JSON lines out, no console window of its own
        CONSOLE.machine = True
        paths.HIDE_CHILD_CONSOLES = True
    CONSOLE.configure()
    try:
        args.func(args)
        return 0
    except UserError as error:
        CONSOLE.line("")
        CONSOLE.error(str(error))
        return 1
    except KeyboardInterrupt:
        return 0
    except SystemExit:
        raise
    except Exception as error:                # a bug, not a setup problem
        report_crash(error, getattr(args, "log_dir", None) or "logs")
        return 2


if __name__ == "__main__":
    sys.exit(main())
