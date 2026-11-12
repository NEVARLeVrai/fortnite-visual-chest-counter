"""Local, manually recorded match results."""

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json

from fortnite_chest_counter.counter import atomic_write


@dataclass(frozen=True)
class Match:
    snapshot: str
    finished_at: str
    profile_id: str
    profile: str
    mode: str
    certain: int
    estimated: int
    token_chance: str
    boost: float = 1.0
    player: str = ""
    chest_types: dict[str, int] = field(default_factory=dict)
    group_webhook_id: str = ""
    session_fingerprint: str = ""

    replay: dict = field(default_factory=dict)
    live_events: list = field(default_factory=list)
    recovered_partial: bool = False
    capture_config: dict = field(default_factory=dict)

    @property
    def total(self):
        return self.certain + self.estimated

    def summary(self):
        return (
            f"Fortnite | {self.mode} | {self.player or self.profile} | {self.finished_at}\n"
            f"Total: {self.total:,} chests ({self.certain:,} certain, {self.estimated:,} estimated)\n"
            f"Estimated Pirate Token chance: {self.token_chance}"
        )


def read_snapshot(profile_id, state_paths):
    certain = total = 0
    parts = []
    for index, path in enumerate(state_paths):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"certain": 0, "approximate": 0}
        if not isinstance(state, dict):
            raise ValueError("A counter file does not contain saved counts.")
        confirmed, opened = state.get("certain"), state.get("approximate")
        if type(confirmed) is not int or type(opened) is not int or not 0 <= confirmed <= opened:
            raise ValueError("A counter file contains invalid counts.")
        certain += confirmed
        total += opened
        if index == 0 or opened:
            parts.append((str(path), state.get("session_started_at"), state.get("last_event_id"), confirmed, opened))
    key = json.dumps([profile_id, parts], ensure_ascii=False)
    return sha256(key.encode("utf-8")).hexdigest(), certain, total


def read_history(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as error:
        raise ValueError("The saved match history could not be read.") from error
    try:
        if data["version"] != 1 or not isinstance(data["matches"], list):
            raise ValueError
        rows = [Match(**item) for item in data["matches"]]
        if any(type(row.certain) is not int or type(row.estimated) is not int or row.certain < 0 or row.estimated < 0
               or type(row.boost) not in (int, float) or not 0 < row.boost <= 2
               or not all(isinstance(value, str) for value in
                          (row.snapshot, row.finished_at, row.profile_id, row.profile, row.mode, row.token_chance,
                           row.player, row.group_webhook_id))
               or type(row.recovered_partial) is not bool or not isinstance(row.capture_config, dict)
               or not isinstance(row.replay, dict) or not isinstance(row.chest_types, dict)
               or any(not isinstance(kind, str) or type(count) is not int or count < 0
                      for kind, count in row.chest_types.items())
               or sum(row.chest_types.values()) > row.total
               for row in rows):
            raise ValueError
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("The saved match history could not be read.") from error


def save_match(path, match):
    rows = read_history(path)
    if any(row.snapshot == match.snapshot for row in rows):
        return False
    _write_history(path, (*rows, match))
    return True


def verify_match(path, snapshot, total, token_chance, chest_types, replay):
    rows = read_history(path)
    for index, row in enumerate(rows):
        if row.snapshot == snapshot and not row.replay:
            direct = {"certain": row.certain, "estimated": row.estimated, "chest_types": row.chest_types}
            verified = replace(row, certain=total, estimated=0, token_chance=token_chance,
                               chest_types=chest_types, replay={**replay, "direct": direct}, recovered_partial=False)
            rows[index] = verified
            _write_history(path, rows)
            return verified
    return None


def edit_match(path, snapshot, certain, total, token_chance):
    rows = read_history(path)
    for index, row in enumerate(rows):
        if row.snapshot == snapshot:
            rows[index] = replace(row, certain=certain, estimated=total - certain, token_chance=token_chance,
                                  chest_types=row.chest_types if total == row.total else {})
            _write_history(path, rows)
            return True
    return False


def delete_match(path, snapshot):
    return bool(delete_matches(path, {snapshot}))


def delete_matches(path, snapshots):
    rows = read_history(path)
    remaining = [row for row in rows if row.snapshot not in snapshots]
    removed = len(rows) - len(remaining)
    if removed:
        _write_history(path, remaining)
    return removed


def _write_history(path, rows):
    records = []
    for row in rows:
        record = asdict(row)
        if not row.recovered_partial:
            record.pop("recovered_partial")
        if not row.capture_config:
            record.pop("capture_config")
        records.append(record)
    atomic_write(path, json.dumps({"version": 1, "matches": records},
                                  ensure_ascii=False, indent=2) + "\n")


def leaderboard(rows):
    profiles = {}
    for row in rows:
        _, matches, total, certain = profiles.get(row.profile_id, (row.profile, 0, 0, 0))
        profiles[row.profile_id] = (row.profile, matches + 1, total + row.total, certain + row.certain)
    return sorted(profiles.values(), key=lambda item: (-item[2], -item[3], item[0].casefold()))
