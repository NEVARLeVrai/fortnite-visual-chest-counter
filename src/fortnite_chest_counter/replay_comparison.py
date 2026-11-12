"""Compare a saved live match with a local LED-N report."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
import time

from fortnite_chest_counter import paths


class ReplayAssociationError(ValueError):
    """Available replay files cannot be linked safely to the saved match."""


class ReplayReaderUnavailable(ValueError):
    """The local replay reader cannot start."""


class ReplayUnreadable(ValueError):
    """A candidate exists but could not be decoded."""


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
                elif fingerprint or abs((started - local_start).total_seconds()) > 3:
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
        raise ReplayAssociationError("Expected one replay for the selected match and player. Check the match time and Epic display name.")
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


def verified_chest_types(timeline):
    if not isinstance(timeline, (list, tuple)) or any(
        not isinstance(event, (list, tuple)) or len(event) != 3
        or not isinstance(event[1], str) or type(event[2]) is not bool for event in timeline
    ):
        return None
    return Counter(chest_type(kind) for _, kind, destroyed in timeline if not destroyed)


UNKNOWN_MINIMUM = 3
UNKNOWN_PERCENT = 25
REPLAY_DELTA_MINIMUM = 5


def matches_quality(match, quality):
    if quality == "unverified":
        return not match.replay
    direct = match.replay.get("direct") if match.replay else {
        "certain": match.certain, "estimated": match.estimated, "chest_types": match.chest_types,
    }
    if not isinstance(direct, dict):
        return False
    counts = direct.get("certain"), direct.get("estimated")
    if any(type(count) is not int or count < 0 for count in counts):
        return False
    live_total = sum(counts)
    if quality == "unknown":
        types = direct.get("chest_types")
        unknown = types.get("unknown", 0) if isinstance(types, dict) else 0
        return (type(unknown) is int and UNKNOWN_MINIMUM <= unknown <= live_total
                and unknown * 100 >= live_total * UNKNOWN_PERCENT)
    if quality == "delta" and match.replay:
        types = verified_chest_types(match.replay.get("timeline"))
        return types is not None and abs(sum(types.values()) - live_total) >= REPLAY_DELTA_MINIMUM
    return False


def saved_recap(match, type_labels):
    if not match.replay:
        return []
    direct = match.replay.get("direct")
    timeline = match.replay.get("timeline")
    if (not isinstance(direct, dict)
            or any(type(direct.get(key)) is not int or direct[key] < 0 for key in ("certain", "estimated"))):
        return ["Replay verified"]
    live_total = direct["certain"] + direct["estimated"]
    replay_types = verified_chest_types(timeline)
    if replay_types is None:
        return ["Replay verified", f"Live {live_total:,} → Saved {match.total:,} chests ({match.total - live_total:+,})"]

    verified_total = sum(replay_types.values())
    lines = [f"Live {live_total:,} → Replay {verified_total:,} chests ({verified_total - live_total:+,})"]
    if (match.certain, match.estimated) != (verified_total, 0):
        lines.append(f"Saved counts · {match.total:,} chests ({match.certain:,} confirmed, {match.estimated:,} estimated)")
    live_types = direct.get("chest_types")
    if isinstance(live_types, dict) and all(isinstance(k, str) and type(n) is int and n >= 0
                                            for k, n in live_types.items()):
        changes = [f"{type_labels.get(kind, kind)}: {live_types.get(kind, 0):,} → {replay_types[kind]:,}"
                   for kind in sorted(live_types.keys() | replay_types.keys())
                   if live_types.get(kind, 0) != replay_types[kind]]
        if changes:
            lines.append("Chest type changes")
            lines.extend(changes)
    return lines


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


def eligible_replay(path):
    if not _REPLAY_NAME.fullmatch(path.name):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _read_local_candidate(reader, path, identity, match, *, timeout):
    with TemporaryDirectory(prefix="fcc-replay-") as temp:
        work = Path(temp)
        demos = work / "demos"
        demos.mkdir()
        shutil.copyfile(path, demos / path.name)
        if replay_identity(path) != identity:
            raise FileNotFoundError("Replay is still changing.")
        subprocess.run(["dotnet", str(reader.resolve()), "--once", "--lang", "en",
                        "--demos", str(demos), "--data", str(work / "report")],
                       check=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stamp = path.stem.removeprefix("UnsavedReplay-")
        local_start = datetime.strptime(stamp, "%Y.%m.%d-%H.%M.%S").astimezone()
        replay, _, total, kinds, _, destroyed, timeline = read_report(
            work / "report" / "data.json", match, local_start=local_start)
        return path, identity, total, kinds, destroyed, timeline, replay["PlayerName"]


def verify_local_replay(match, finished_at, *, wait_seconds=30):
    if not match.session_fingerprint:
        raise ReplayAssociationError("Match ID unavailable for safe verification")
    reader, root = reader_path(), demos_path()
    if not reader.is_file() or shutil.which("dotnet") is None:
        raise ReplayReaderUnavailable("Replay reader unavailable")
    if root is None:
        return None
    previous = set()
    checked = {}
    unreadable = set()
    deadline = time.monotonic() + wait_seconds
    parse_deadline = deadline + 90
    while time.monotonic() < deadline:
        time.sleep(2)
        if not root.is_dir():
            continue
        candidates = set()
        for path in root.iterdir():
            try:
                if not eligible_replay(path):
                    continue
                identity = replay_identity(path)
            except FileNotFoundError:
                continue
            if identity[1] and abs(identity[2] / 1e9 - finished_at) <= 90:
                candidates.add((path, identity))
        stable = candidates == previous and bool(candidates)
        previous = candidates
        if not stable:
            continue
        for path, identity in sorted(candidates):
            key = (path, identity)
            if key in checked:
                continue
            remaining = parse_deadline - time.monotonic()
            if remaining <= 0:
                raise ReplayAssociationError("Replay checks timed out")
            try:
                checked[key] = _read_local_candidate(reader, path, identity, match, timeout=remaining)
            except FileNotFoundError:
                try:
                    if replay_identity(path) == identity:
                        checked[key] = None
                        unreadable.add(key)
                except FileNotFoundError:
                    continue
            except ReplayAssociationError:
                checked[key] = None
            except (OSError, ValueError, subprocess.SubprocessError):
                checked[key] = None
                unreadable.add(key)
        if not candidates.issubset(checked):
            continue
        matched = [checked[key] for key in candidates if checked[key] is not None]
        if len(matched) > 1:
            raise ReplayAssociationError("Multiple replays match this result")
        if matched:
            return matched[0]
    if unreadable:
        raise ReplayUnreadable("Replay unreadable")
    if checked:
        raise ReplayAssociationError("No replay could be linked safely")
    return None
