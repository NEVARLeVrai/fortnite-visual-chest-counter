"""Compare a saved live match with a local LED-N report."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from fortnite_chest_counter import paths


def read_report(path, match, *, local_start=None):
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = report.get("Matches") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Choose the data.json exported by LED-N.")
    finished = datetime.strptime(match.finished_at, "%Y-%m-%d %H:%M").astimezone()
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("IsReplayOwner") is not True or row.get("IsBattleRoyale") is not True:
            continue
        player = row.get("PlayerName")
        if not isinstance(player, str) or not player.strip():
            continue
        if local_start is None and match.player and player.strip().casefold() != match.player.strip().casefold():
            continue
        try:
            started = datetime.fromisoformat(row["MatchStartUtc"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if local_start is not None:
                session_id = row.get("SessionId")
                fingerprint = getattr(match, "session_fingerprint", "")
                if fingerprint and isinstance(session_id, str) and re.fullmatch(r"[0-9a-fA-F]{32}", session_id):
                    if sha256(session_id.lower().encode("ascii")).hexdigest() != fingerprint:
                        continue
                elif abs((started - local_start).total_seconds()) > 3:
                    continue
            duration = row["DurationSeconds"]
            if type(duration) not in (int, float) or not 0 < duration <= 7200:
                continue
            end = started + timedelta(seconds=duration)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if abs((end - finished).total_seconds()) <= 120:
            candidates.append((row, end))
    if len(candidates) != 1:
        raise ValueError("Expected one replay for the selected match and player. Check the match time and Epic display name.")
    replay, end = candidates[0]
    events = replay.get("Chests")
    total = replay.get("ChestsOpened")
    if type(total) is not int or not isinstance(events, list) or total != len(events):
        raise ValueError("The report does not include a complete list of chest events.")
    kinds = Counter()
    opened = destroyed = 0
    timeline = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("The report contains an invalid chest event.")
        seconds, kind, broken = (event.get(key) for key in ("TimeSeconds", "Kind", "Destroyed"))
        if (type(seconds) not in (int, float) or not 0 <= seconds <= 7200
                or not isinstance(kind, str) or not kind.strip() or type(broken) is not bool):
            raise ValueError("The report contains an invalid chest event.")
        kinds[kind] += 1
        destroyed += broken
        opened += not broken
        timeline.append((seconds, kind, broken))
    return replay, end, total, kinds, opened, destroyed, timeline


def compare_report(path, match):
    replay, end, total, kinds, opened, destroyed, timeline = read_report(path, match)
    direct = match.replay.get("direct", {})
    certain = direct.get("certain", match.certain)
    estimated = direct.get("estimated", match.estimated)
    live_total = certain + estimated
    live_types = ", ".join(f"{kind} {count}" for kind, count in sorted(direct.get("chest_types", match.chest_types).items()))
    replay_types = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    lines = [
        f"{replay['PlayerName']} · replay finished {end.astimezone():%Y-%m-%d %H:%M:%S}",
        f"Live: {live_total} chests ({certain} confirmed, {estimated} estimated)",
        f"Live chest types: {live_types or 'unavailable'}",
        f"Replay: {total} chests ({opened} opened, {destroyed} destroyed)",
        f"Replay chest types: {replay_types or 'none'}",
        f"Replay minus live: {total - live_total:+d}",
    ]
    other = replay.get("OtherInteractions")
    if isinstance(other, dict):
        items = [f"{kind} {count}" for kind, count in other.items()
                 if isinstance(kind, str) and type(count) is int and count > 0]
        if items:
            lines.append("Other interactions: " + ", ".join(items))
    treasures = replay.get("TreasureFinds")
    if isinstance(treasures, list):
        lines.append(f"Treasures: {len(treasures)}")
    return _format_timelines(lines, match.live_events, timeline)


def saved_comparison(match):
    direct = match.replay["direct"]
    timeline = match.replay["timeline"]
    live_total = direct["certain"] + direct["estimated"]
    lines = [
        match.replay["owner"],
        f"Live: {live_total} chests ({direct['certain']} confirmed, {direct['estimated']} estimated)",
        f"Replay: {len(timeline)} chest events ({match.replay['destroyed']} destroyed)",
        f"Verified openings: {match.total} ({match.total - live_total:+d} vs live)",
    ]
    return _format_timelines(lines, match.live_events, timeline)


def _format_timelines(lines, live_events, timeline):
    if live_events:
        lines.extend(("", "Live timeline · local time"))
        for event in live_events:
            at = datetime.fromtimestamp(event["at"]).astimezone()
            status = "confirmed" if event["certain"] else "estimated"
            lines.append(f"{at:%H:%M:%S.%f}"[:-3] + f"  {event['type']} · {status}")
    lines.extend(("", "Replay timeline · timestamps relative to the replay"))
    for seconds, kind, broken in sorted(timeline):
        minutes, remainder = divmod(round(seconds * 10), 600)
        lines.append(f"{minutes:02d}:{remainder / 10:04.1f}  {kind} · {'destroyed' if broken else 'opened'}")
    return "\n".join(lines)

_REPLAY_NAME = re.compile(r"UnsavedReplay-\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.replay", re.I)


def chest_type(kind):
    return {
        "chest": "chest",
        "rare chest": "rare",
        "buried chest": "buried",
        "sprite chest (rare)": "sprite",
        "sprite poster chest": "poster",
        "coffre": "chest",
        "coffre rare": "rare",
        "coffre enterre": "buried",
        "coffre d'esprit (rare)": "sprite",
        "coffre des affiches d'esprit": "poster",
    }.get(kind.casefold().replace("é", "e"), "unknown")


def reader_path():
    return Path(os.environ.get("FCC_REPLAY_READER") or paths.APP_DIR / "runtime/replay-reader/ChestCounter.dll")


def demos_path():
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "FortniteGame/Saved/Demos" if local else None


def replay_identity(path):
    stat = path.stat(follow_symlinks=False)
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def delete_consumed_replay(path, identity):
    root = demos_path()
    if (root is None or path.parent != root or not _REPLAY_NAME.fullmatch(path.name)
            or path.is_symlink() or path.resolve().parent != root.resolve()
            or replay_identity(path) != identity):
        return False
    path.unlink()
    return True


def verify_local_replay(match, finished_at):
    reader, root = reader_path(), demos_path()
    if root is None or not reader.is_file():
        return None
    previous = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(2)
        if not root.is_dir():
            continue
        candidates = []
        for path in root.iterdir():
            if not _REPLAY_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                identity = replay_identity(path)
            except FileNotFoundError:
                continue
            if identity[1] and abs(identity[2] / 1e9 - finished_at) <= 90:
                candidates.append((path, identity))
        if len(candidates) == 1 and candidates[0] == previous:
            path, identity = candidates[0]
            with TemporaryDirectory(prefix="fcc-replay-") as temp:
                work = Path(temp)
                demos = work / "demos"
                demos.mkdir()
                shutil.copyfile(path, demos / path.name)
                if replay_identity(path) != identity:
                    previous = None
                    continue
                subprocess.run(["dotnet", str(reader.resolve()), "--once", "--lang", "en",
                                "--demos", str(demos), "--data", str(work / "report")],
                               check=True, timeout=90, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                report = work / "report" / "data.json"
                # A Group alias is not an Epic identity. Match the game-owned
                # replay against the session ID, or the filename time if the ID is absent.
                stamp = path.stem.removeprefix("UnsavedReplay-")
                local_start = datetime.strptime(stamp, "%Y.%m.%d-%H.%M.%S").astimezone()
                replay, _, total, kinds, _, destroyed, timeline = read_report(
                    report, match, local_start=local_start)
                return path, identity, total, kinds, destroyed, timeline, replay["PlayerName"]
        previous = candidates[0] if len(candidates) == 1 else None
    return None
