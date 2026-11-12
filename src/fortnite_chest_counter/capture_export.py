"""Export one finished missed-chest report without the rest of its recording."""

import csv
import json
import math
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zipfile import ZipFile

from fortnite_chest_counter.capture import MISSED_CHEST_WINDOW


_SEGMENT = re.compile(r"seg_[0-9]+\.mkv")
_MARK = re.compile(r"M[0-9]+")


def _file(folder, relative):
    file = folder / relative
    if file.is_symlink() or not file.resolve().is_relative_to(folder) or not file.is_file():
        raise ValueError(f"Capture file unavailable: {Path(relative).name}")
    return file


def _rows(folder, name):
    with _file(folder, name).open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row


def _info(folder):
    data = json.loads(_file(folder, "capture.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("ended_at"):
        raise ValueError("Stop the diagnostic recording before exporting.")
    return data


def reports(folder):
    folder = folder.resolve()
    _info(folder)
    return [row for row in _rows(folder, "marks.jsonl")
            if row.get("label") == "missed chest" and isinstance(row.get("clip"), str)
            and isinstance(row.get("stamp"), str) and _MARK.fullmatch(str(row.get("mark_id", "")))
            and type(row.get("recording_time")) in (int, float) and math.isfinite(row["recording_time"])]


def export_report(folder, mark_id, output):
    folder = folder.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip destination.")
    info = _info(folder)
    mark = next((row for row in reports(folder) if row["mark_id"] == mark_id), None)
    if mark is None:
        raise ValueError("This report has no saved video.")
    rec = mark["recording_time"]
    before, after = MISSED_CHEST_WINDOW
    segments = {}
    with _file(folder, "video/segments.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) != 3 or not _SEGMENT.fullmatch(row[0]):
                continue
            start, end = map(float, row[1:])
            if start < rec + after and end > rec - before:
                path = folder / "video" / row[0]
                if path.exists():
                    segments[row[0]] = (start, end, _file(folder, Path("video") / row[0]))
    segments = sorted(segments.values())
    containing = next((i for i, (start, end, _) in enumerate(segments) if start <= rec < end), None)
    if containing is None:
        raise ValueError("The video at this report has been removed.")

    mark_offset = sum(end - start for start, end, _ in segments[:containing]) + rec - segments[containing][0]
    events = []
    event_fields = ("event_id", "time", "recording_time", "adjusted_tier", "popup_score", "popup_template",
                    "interaction_score", "certain_counted", "approx_counted", "reason")
    for row in _rows(folder, "events.jsonl"):
        event_time = row.get("recording_time")
        if type(event_time) in (int, float) and any(start <= event_time < end for start, end, _ in segments):
            events.append({field: row[field] for field in event_fields if field in row})
    app = info.get("app") if isinstance(info.get("app"), dict) else {}
    metrics = mark.get("diagnostics") if isinstance(mark.get("diagnostics"), dict) else {}
    manifest = {
        "version": app.get("version"),
        "source_type": app.get("source_type"),
        "input_resolution": app.get("input_resolution"),
        "capture_fps": info.get("fps"),
        "mark": mark_id,
        "seconds_into_clip": round(mark_offset, 3),
        "recording_time": rec,
        "diagnostics": {name: metrics[name] for name in
                        ("hud_scale", "target_fps", "measured_fps", "skipped_frames") if name in metrics},
    }
    with NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
    try:
        with ZipFile(temp, "w") as archive:
            for _, _, path in segments:
                archive.write(path, f"video/{path.name}")
            archive.writestr("video/report.ffconcat", "ffconcat version 1.0\n" +
                             "".join(f"file {path.name}\n" for _, _, path in segments))
            archive.writestr("report.json", json.dumps(manifest, indent=2))
            archive.writestr("decisions.jsonl", "".join(json.dumps(row) + "\n" for row in events))
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
