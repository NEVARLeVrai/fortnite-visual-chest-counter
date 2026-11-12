"""
Diagnostic capture: record what the counter analyses, so a live miss becomes a repeatable test case.

OFF by default ([capture] mode = "off"); then nothing here runs at all. When on, the live loop hands every analysed frame
(1280x720 RGB at the detector's cadence - exactly the detector's input) to `Capture.offer`, which only puts it in a small
queue. A recorder thread pipes the frames into a separate FFmpeg encoder. Counting never waits for the recording: if the
encoder falls behind, the RECORDING skips frames (noted in the timeline), never the detector.

Modes (switchable while counting from the app: each switch starts a new capture folder)
  marks    keep only the video around marks ("the counter missed a chest" - Ctrl+Alt+M / Mark moment in the app, M in
           the console). A rolling buffer on disk: everything else is deleted ~40 s after it was recorded. Smallest.
  events   also keep the video around every chest decision (counted or not; not around generic button prompts).
  session  keep the whole session.

One folder per counting session: captures/<YYYYmmdd_HHMMSS>/
  capture.json   app version, detector settings, source, resolution, frame rate, codec, mode, times
  video/         10-second segments (seg_00000.mkv ...) and how they join into clips (clips.json; one .ffconcat
                 playlist per clip, readable by FFmpeg and by `review`)
  timeline.csv   recording frame number -> live time t (seconds since counting started)
  events.jsonl   every detection decision (counted or not), with live time and clip/recording time
  marks.jsonl    every mark, with live time and clip/recording time
  labels.csv     one row per decision and mark for a person to fill in ("truth" column) - used by `review`
"""

import csv
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

from fortnite_chest_counter import paths

MODES = ("off", "marks", "events", "session")
SEGMENT_SECONDS = 10
MISSED_CHEST_WINDOW = (6, 4)
QUEUE_FRAMES = 30                          # ~2 s of 720p frames (~80 MB) between the counter and the encoder
MIN_FREE_BYTES = 2 * 1024 ** 3             # do not start below 2 GB free; stop recording below 1 GB
# Measured (docs/guides/diagnostic-capture.md): the 7-px pop-up text is so small that any lossy format moves its match score a little.
#   standard  intra-only full-colour MJPEG, best quality: ~10 GB/hour; replay = live for most decisions, borderline ones
#             (within ~0.03 of a tier threshold) can differ
#   lossless  FFV1, exact RGB: ~22 GB/hour; the replay sees exactly what the detector saw
QUALITIES = {"standard": (["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p"], "MJPEG 4:4:4 q2"),
             "lossless": (["-c:v", "ffv1", "-level", "3", "-slices", "4", "-pix_fmt", "bgr0"], "FFV1 lossless RGB")}
CODEC_ARGS, CODEC_NAME = QUALITIES["standard"]
LABEL_FIELDS = ["kind", "id", "live_time", "clip", "clip_time", "decision", "tier", "evidence", "truth", "note"]
TRUTH_HELP = ("truth: chest | not_chest | missed_chest | sprite_chest | reward | other  (leave empty if unsure)")


def mmss(seconds):
    minutes, rest = divmod(max(float(seconds), 0.0), 60)
    return f"{int(minutes):02d}:{rest:05.2f}"


class Capture:
    def __init__(self, base_dir, mode, fps, width, height, before=10, after=10, mark_before=30, mark_after=10,
                 info=None, ffmpeg="ffmpeg", codec_args=None, codec_name=None, quality="standard"):
        if mode not in ("marks", "events", "session"):
            raise ValueError(f"capture mode must be 'marks', 'events' or 'session', not {mode!r}")
        if codec_args is None:
            codec_args, codec_name = QUALITIES[quality]
        self.mode, self.fps, self.width, self.height = mode, fps, width, height
        self.before, self.after, self.mark_before, self.mark_after = before, after, mark_before, mark_after
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for n in range(1, 100):                    # recordings switched within one second get _2, _3 ...
            self.folder = Path(base_dir).resolve() / (stamp if n == 1 else f"{stamp}_{n}")
            self.video_dir = self.folder / "video"
            try:
                self.video_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise OSError(f"too many recordings started in the same second in {base_dir}")
        free = shutil.disk_usage(self.folder).free
        if free < MIN_FREE_BYTES:
            shutil.rmtree(self.folder, ignore_errors=True)
            raise OSError(f"only {free / 1024 ** 3:.1f} GB free on that drive (recording needs at least 2 GB)")
        self.frame_times = []                  # live t of every recorded frame (index = recording frame number)
        self.keep = []                         # (start, end) in recording seconds that must be kept
        self.events, self.marks = [], []
        self.dropped = 0
        self.stopped_reason = None
        self.deleted = set()
        self._queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self._lock = threading.Lock()
        self._timeline = open(self.folder / "timeline.csv", "w", encoding="utf-8", newline="\n")
        self._timeline.write("frame,live_t\n")
        self.info = {"app": info or {}, "mode": mode, "fps": fps, "width": width, "height": height, "codec": codec_name or CODEC_NAME,
                     "segment_seconds": SEGMENT_SECONDS, "before": before, "after": after, "mark_before": mark_before,
                     "mark_after": mark_after, "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write_info()
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_threads", "1", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
               "-threads", "2", *(codec_args or CODEC_ARGS),
               "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
               "-segment_format", "matroska", "-segment_list", str(self.video_dir / "segments.csv"),
               "-segment_list_type", "csv", str(self.video_dir / "seg_%05d.mkv")]
        self._encoder_log = open(self.folder / "encoder.log", "wb")
        # Diagnostic recording yields to counting and the game when the CPU is busy.
        flags = paths.child_process_flags() | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        self._encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._encoder_log,
                                         creationflags=flags)
        self._thread = threading.Thread(target=self._write, daemon=True, name="capture-writer")
        self._thread.start()

    # ------------------------------------------------------------------ called from the live loop (must be cheap)
    def offer(self, t, frame):
        """Queue one analysed frame. Never blocks: a full queue means the encoder is behind -> this frame is not recorded."""
        if self.stopped_reason:
            return
        try:
            self._queue.put_nowait((t, frame))
        except queue.Full:
            self.dropped += 1

    def recording_time(self, live_t):
        """Recording seconds for a live time (nearest recorded frame)."""
        with self._lock:
            times = self.frame_times
            if not times:
                return 0.0
            i = min(bisect_left(times, live_t), len(times) - 1)
            if i > 0 and abs(times[i - 1] - live_t) < abs(times[i] - live_t):
                i -= 1
            return i / self.fps

    def add_events(self, events):
        """Detection decisions (counted or not) from CounterSession.apply. Events mode keeps the video around each."""
        for ev in events:
            rec = self.recording_time(ev["time"])
            row = {k: ev.get(k) for k in ("event_id", "time", "stamp", "source", "adjusted_tier", "popup_score",
                                          "popup_template", "interaction_score", "certain_counted", "approx_counted",
                                          "scaled_popup", "reason")}
            row["recording_time"] = round(rec, 3)
            self.events.append(row)
            # Generic button prompts (pickups, ammo boxes ...) are frequent and not chest decisions: logged, video not kept.
            # "marks" mode keeps video only around marks.
            if self.mode == "events" and not (ev.get("source") == "interaction_object" and not ev.get("approx_counted")):
                self.keep.append((rec - self.before, rec + self.after))
            self._append("events.jsonl", row)

    def add_mark(self, live_t, label="mark", diagnostics=None):
        rec = self.recording_time(live_t)
        row = {"mark_id": f"M{len(self.marks) + 1:03d}", "time": round(live_t, 3), "stamp": mmss(live_t),
               "recording_time": round(rec, 3), "label": label,
               "logged_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if diagnostics is not None:
            row["diagnostics"] = diagnostics
        self.marks.append(row)
        self._append("marks.jsonl", row)
        return row

    # ------------------------------------------------------------------ recorder thread
    def _write(self):
        next_cleanup = time.monotonic() + SEGMENT_SECONDS
        while True:
            item = self._queue.get()
            if item is None:
                break
            t, frame = item
            try:
                self._encoder.stdin.write(frame.data if frame.flags.c_contiguous else frame.tobytes())
            except (OSError, ValueError):
                self.stopped_reason = "the recorder stopped unexpectedly (see encoder.log)"
                break
            with self._lock:
                self._timeline.write(f"{len(self.frame_times)},{t:.3f}\n")
                self.frame_times.append(t)
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + SEGMENT_SECONDS
                try:
                    self._cleanup()
                    low = shutil.disk_usage(self.folder).free < MIN_FREE_BYTES // 2
                except Exception:                  # housekeeping must never stop the recording
                    low = False
                if low:
                    self.stopped_reason = "less than 1 GB free on the drive"
                    break

    def _segments(self):
        """Finished segments: [(file name, start s, end s)] from FFmpeg's list."""
        try:
            rows = list(csv.reader((self.video_dir / "segments.csv").read_text(encoding="utf-8").splitlines()))
        except (OSError, ValueError):
            return []
        segments = []
        for r in rows:                             # FFmpeg may be writing the last line right now: skip a partial one
            try:
                segments.append((r[0], float(r[1]), float(r[2])))
            except (IndexError, ValueError):
                pass
        return segments

    def _keep_ranges(self):
        ranges = list(self.keep)
        for mark in tuple(self.marks):
            rec = self.recording_time(mark["time"])
            before, after = (MISSED_CHEST_WINDOW if mark["label"] == "missed chest"
                             else (self.mark_before, self.mark_after))
            ranges.append((rec - before, rec + after))
        return ranges

    def _cleanup(self, final=False):
        """Marks / events mode: delete finished segments nothing interesting can still claim."""
        if self.mode == "session":
            return
        now = len(self.frame_times) / self.fps
        horizon = now - max(self.before + 8.0, self.mark_before) - SEGMENT_SECONDS     # events finish up to ~6 s late
        ranges = self._keep_ranges()
        for name, start, end in self._segments():
            if name in self.deleted or (not final and end > horizon) or any(a < end and b > start for a, b in ranges):
                continue
            try:
                (self.video_dir / name).unlink()
                self.deleted.add(name)
            except OSError:
                pass

    # ------------------------------------------------------------------ end of session
    def close(self, timeout=20.0):
        """Finish the recording: flush the encoder, drop unneeded video, write clips + labels. -> summary dict."""
        try:                                       # bounded: a dead writer leaves a full queue behind
            self._queue.put(None, timeout=timeout if self._thread.is_alive() else 0.01)
        except queue.Full:
            pass
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.stopped_reason = self.stopped_reason or "the recorder could not finish before the stop timeout"
            self._encoder.kill()                  # unblock a writer waiting on a full FFmpeg pipe before closing it
            self._encoder.wait(timeout=3)
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise TimeoutError("the recording writer did not stop after FFmpeg exited")
        try:
            self._encoder.stdin.close()
        except OSError:
            pass
        try:
            self._encoder.wait(timeout)
        except subprocess.TimeoutExpired:
            self._encoder.kill()
            self._encoder.wait(timeout=3)
        self._timeline.close()
        self._encoder_log.close()
        for row in self.events + self.marks:          # exact now that every recorded frame is known
            row["recording_time"] = round(self.recording_time(row["time"]), 3)
        self._cleanup(final=True)
        clips = self._clips()
        (self.video_dir / "clips.json").write_text(json.dumps(clips, indent=2), encoding="utf-8")
        for row in self.events + self.marks:
            row["clip"], row["clip_time"] = self.locate(clips, row["recording_time"])
        self._rewrite("events.jsonl", self.events)
        self._rewrite("marks.jsonl", self.marks)
        self._write_labels()
        size = sum(f.stat().st_size for f in self.video_dir.glob("*.mkv"))
        self.info.update(ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                         frames_recorded=len(self.frame_times), frames_not_recorded=self.dropped,
                         stopped_early=self.stopped_reason, clips=len(clips), video_mb=round(size / 1e6, 1),
                         events=len(self.events), marks=len(self.marks))
        self._write_info()
        return {"folder": str(self.folder), "clips": len(clips), "video_mb": round(size / 1e6, 1),
                "events": len(self.events), "marks": len(self.marks), "frames_not_recorded": self.dropped,
                "stopped_early": self.stopped_reason}

    def _clips(self):
        """Join kept segments that follow each other into clips (an .ffconcat playlist each; no re-encoding)."""
        kept = [s for s in self._segments() if s[0] not in self.deleted and (self.video_dir / s[0]).exists()]
        runs, clips = [], []
        for seg in kept:
            if runs and abs(runs[-1][-1][2] - seg[1]) < 0.5 / self.fps:
                runs[-1].append(seg)
            else:
                runs.append([seg])
        for n, run in enumerate(runs, 1):
            name = "session.ffconcat" if self.mode == "session" else f"clip_{n:03d}.ffconcat"
            (self.video_dir / name).write_text("ffconcat version 1.0\n" + "".join(f"file {s[0]}\n" for s in run),
                                              encoding="utf-8")
            clips.append({"file": f"video/{name}", "start": run[0][1], "end": run[-1][2],
                          "live_start": self._live_at(run[0][1]), "segments": [s[0] for s in run]})
        return clips

    def _live_at(self, rec_seconds):
        i = min(int(round(rec_seconds * self.fps)), len(self.frame_times) - 1)
        return round(self.frame_times[i], 3) if i >= 0 and self.frame_times else None

    @staticmethod
    def locate(clips, rec):
        for clip in clips:
            if clip["start"] - 1e-6 <= rec < clip["end"] + 1e-6:
                return clip["file"], round(rec - clip["start"], 3)
        return None, None

    def _write_labels(self):
        rows = []
        for ev in self.events:
            decision = "certain" if ev.get("certain_counted") else "approx" if ev.get("approx_counted") else "not counted"
            evidence = (f"popup {ev['popup_score']:.2f} {ev['popup_template']}" if ev.get("popup_score") is not None
                        else f"prompt {ev.get('interaction_score') or 0:.2f}")
            if ev.get("scaled_popup"):
                evidence += f", large HUD: blurred {ev['scaled_popup']['blur']:.2f} at x{ev['scaled_popup']['scale']}"
            rows.append({"_t": ev["time"], "kind": "event", "id": ev.get("event_id") or "",
                         "live_time": mmss(ev["time"]), "clip": ev["clip"] or "",
                         "clip_time": "" if ev["clip_time"] is None else mmss(ev["clip_time"]), "decision": decision,
                         "tier": ev.get("adjusted_tier") or "", "evidence": evidence, "truth": "", "note": ""})
        for m in self.marks:
            rows.append({"_t": m["time"], "kind": "mark", "id": m["mark_id"], "live_time": m["stamp"], "clip": m["clip"] or "",
                         "clip_time": "" if m["clip_time"] is None else mmss(m["clip_time"]), "decision": m["label"],
                         "tier": "", "evidence": "", "truth": "", "note": ""})
        rows.sort(key=lambda r: r["_t"])                     # by time, not by text ("100:00" < "99:00" as text)
        with open(self.folder / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, LABEL_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        (self.folder / "README.txt").write_text(
            "Diagnostic capture of one counting session.\n\n"
            "labels.csv lists every detection decision and every mark. Watch the clip at clip_time and fill in the\n"
            f"'truth' column ({TRUTH_HELP}).\n"
            "A mark is the moment the hotkey was pressed, usually a few seconds AFTER the chest: change its live_time\n"
            "to when the chest actually opened. Add a row (kind=missed) for any other chest that has no row.\n"
            "Then run the review (app: Settings > Review captures, or the command `review <this folder>`): it replays\n"
            "the recording through the current detector and compares it with the live decisions and your labels.\n"
            "Clips: video/clip_*.ffconcat (FFmpeg playlists of the 10-second .mkv segments next to them).\n",
            encoding="utf-8")

    # ------------------------------------------------------------------ files
    def _append(self, name, row):
        with open(self.folder / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _rewrite(self, name, rows):
        with open(self.folder / name, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)

    def _write_info(self):
        (self.folder / "capture.json").write_text(json.dumps(self.info, indent=2, default=str), encoding="utf-8")


def read_labels(folder):
    """labels.csv rows that have a 'truth' (Excel-saved files included)."""
    path = Path(folder) / "labels.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("truth") or "").strip()]


def find_captures(path):
    """A capture folder, or every capture folder below `path`."""
    path = Path(path)
    if (path / "capture.json").exists():
        return [path]
    return sorted(p.parent for p in path.glob("*/capture.json"))
