"""Recognize local Battle Royale boundaries in newly appended Fortnite log data."""

from datetime import datetime
from hashlib import sha256
import os
import re
from pathlib import Path


_STAMP = rb"\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d:\d{3})\]\[ *(\d{1,3})\]"
_PREFIX = rb"(?:" + _STAMP + rb")?"
_SESSION = re.compile(_STAMP + rb'LogCsvProfiler: Display: Metadata set : sessionid="([0-9a-fA-F]{32})"')
_PLAYLIST = re.compile(_STAMP + rb'LogCsvProfiler: Display: Metadata set : playlistid="([A-Za-z0-9_]+)"')
_BR_PLAYLIST = re.compile(rb"Playlist_(?:Default|NoBuildBR_?|Habanero)(Solo|Duo|Trio|Squads?)", re.IGNORECASE)
_SESSION_LINES = 32
_SESSION_WINDOW_MS = 10


def _timestamp(value):
    return datetime.strptime(value.decode("ascii"), "%Y.%m.%d-%H.%M.%S:%f")

_START = re.compile(_PREFIX + rb"LogBattleRoyaleGamePhaseLogic: HandleGamePhaseChanged\. "
                    rb"OldPhase = EAthenaGamePhase::Setup\. NewPhase = EAthenaGamePhase::Warmup")
_FRONTEND = re.compile(_PREFIX + rb"LogWorld: Bringing World /Game/Maps/Frontend\.Frontend up for play "
                       rb"\(max tick rate \d+\) at \d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d")


def local_log_path():
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "FortniteGame" / "Saved" / "Logs" / "FortniteGame.log"


class LogTail:
    """Read at most one chunk per poll and carry only the current bounded line."""

    READ_SIZE = 256 * 1024
    LINE_LIMIT = 4096
    ANCHOR_SIZE = 128

    def __init__(self, path):
        self.path = path
        self._reset()
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                self._identity = (stat.st_dev, stat.st_ino)
                self._offset = stat.st_size
                self._anchor = self._read_anchor(stream)
                self._skip_line = bool(self._anchor and not self._anchor.endswith(b"\n"))
        except FileNotFoundError:
            pass

    def _reset(self):
        self.caught_up = True
        self._identity = None
        self._offset = 0
        self._anchor = b""
        self._partial = b""
        self._skip_line = False
        self._line_number = 0
        self._session = None
        self._playlist = None

    def _read_anchor(self, stream):
        size = min(self.ANCHOR_SIZE, self._offset)
        stream.seek(self._offset - size)
        return stream.read(size)

    def poll(self):
        """Return a discontinuity flag followed by the ordered markers from this chunk."""
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                restarted = self._identity is not None and (
                    identity != self._identity or stat.st_size < self._offset
                    or self._read_anchor(stream) != self._anchor
                )
                if restarted:
                    self._reset()
                self._identity = identity
                stream.seek(self._offset)
                chunk = stream.read(self.READ_SIZE)
                self._offset += len(chunk)
                self.caught_up = self._offset >= stat.st_size
                self._anchor = (self._anchor + chunk)[-self.ANCHOR_SIZE:]
        except FileNotFoundError:
            restarted = self._identity is not None
            self._reset()
            return restarted, []
        if not chunk:
            return restarted, []

        lines = (self._partial + chunk).split(b"\n")
        partial = lines.pop()
        if self._skip_line and lines:
            lines.pop(0)
            self._line_number += 1
            self._skip_line = False
        self._skip_line = self._skip_line or len(partial) > self.LINE_LIMIT
        self._partial = b"" if self._skip_line else partial
        events = []
        for line in lines:
            self._line_number += 1
            if self._session and self._line_number - self._session[0] > _SESSION_LINES:
                self._session = None
            if self._playlist and self._line_number - self._playlist[0] > _SESSION_LINES:
                self._playlist = None
            if len(line) > self.LINE_LIMIT:
                continue
            line = line.rstrip(b"\r")
            start = _START.fullmatch(line)
            session = _SESSION.fullmatch(line)
            playlist = _PLAYLIST.fullmatch(line)
            if start:
                fingerprint = self._start_metadata(start, self._session)
                mode = self._start_metadata(start, self._playlist)
                self._session = self._playlist = None
                if mode:
                    events.append(("start", fingerprint or "", mode))
                else:
                    events.append(("start", fingerprint) if fingerprint else "start")
            elif _FRONTEND.fullmatch(line):
                self._session = self._playlist = None
                events.append("finish")
            elif session:
                stamp, frame, session_id = session.groups()
                self._session = self._line_number, stamp, int(frame), sha256(session_id.lower()).hexdigest()
            elif playlist:
                stamp, frame, playlist_id = playlist.groups()
                match = _BR_PLAYLIST.fullmatch(playlist_id)
                mode = match[1].decode("ascii").capitalize().removesuffix("s") if match else None
                self._playlist = self._line_number, stamp, int(frame), mode
        return restarted, events

    @staticmethod
    def _start_metadata(start, candidate):
        stamp, frame = start.groups()
        if not candidate or stamp is None:
            return None
        _, prior_stamp, prior_frame, value = candidate
        if int(frame) != prior_frame:
            return None
        try:
            elapsed = _timestamp(stamp) - _timestamp(prior_stamp)
        except ValueError:
            return None
        if 0 <= elapsed.total_seconds() * 1000 <= _SESSION_WINDOW_MS:
            return value
        return None
