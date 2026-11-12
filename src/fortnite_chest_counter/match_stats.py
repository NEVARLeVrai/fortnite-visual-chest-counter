"""Compose personal and team match totals without counting the same session twice."""

from dataclasses import dataclass
import math


RATE = 137_500


def chest_exposure(total, boost):
    return total * math.log1p(-boost / RATE)


def token_chance(log_no_drop):
    return f"{-math.expm1(log_no_drop) if log_no_drop else 0.0:.6%}"


@dataclass(frozen=True)
class Summary:
    certain: int = 0
    total: int = 0
    log_no_drop: float = 0.0
    players: tuple[str, ...] = ()
    matches: int = 0
    member_totals: tuple[tuple[str, int], ...] = ()

    @property
    def chance(self):
        return token_chance(self.log_no_drop)

    def __add__(self, other):
        return Summary(self.certain + other.certain, self.total + other.total,
                       self.log_no_drop + other.log_no_drop, matches=self.matches + other.matches)


def personal(certain, total, boost):
    return Summary(certain, total, chest_exposure(total, boost), matches=1)


def team(session, player, *, current=None):
    if session is None:
        return None
    members = {member.name.casefold(): member for member in session.players}
    own = player.casefold()
    if not own or (own not in members and current is None):
        return None
    names = {key: member.name for key, member in members.items()}
    if current is not None:
        names[own] = player
    if len(names) < 2:
        return None
    result = Summary()
    for key, member in members.items():
        if key != own or current is None:
            result += personal(member.certain, member.total, member.boost)
    if current is not None:
        result += personal(*current)
    member_totals = tuple((name, current[1] if key == own and current is not None else members[key].total)
                          for key, name in names.items())
    return Summary(result.certain, result.total, result.log_no_drop, tuple(names.values()), 1, member_totals)


def member_freshness(member, now):
    if member.finished:
        return "Final result"
    if not member.updated_at:
        return "Update time unavailable"
    seconds = max(0, int(now - member.updated_at))
    if seconds < 5:
        return "Live · updated just now"
    age = f"{seconds // 5 * 5}s" if seconds < 60 else f"{seconds // 60}m"
    return f"Last update {age} ago" if seconds >= 45 else f"Live · updated {age} ago"


def saved_team(match, sessions, webhook_id):
    if not match.session_fingerprint or not match.group_webhook_id or match.group_webhook_id != webhook_id:
        return None
    return team(sessions.get(match.session_fingerprint), match.player,
                current=(match.certain, match.total, match.boost))


def cumulative(matches, sessions, webhook_id, *, snapshot="", certain=0, total=0, boost=1.0,
               fingerprint="", player="", sharing=False, active=False):
    result = Summary()
    seen = set()
    snapshots = set()
    for match in matches:
        snapshots.add(match.snapshot)
        linked = saved_team(match, sessions, webhook_id)
        key = (match.group_webhook_id, match.session_fingerprint)
        if linked is not None:
            if key not in seen:
                result += linked
                seen.add(key)
        else:
            result += personal(match.certain, match.total, match.boost)
    if snapshot and snapshot not in snapshots and (total > 0 or active):
        linked = team(sessions.get(fingerprint), player, current=(certain, total, boost)) if sharing and fingerprint else None
        key = (webhook_id, fingerprint)
        if linked is not None:
            if key not in seen:
                result += linked
        else:
            result += personal(certain, total, boost)
    return result


def capture_label(config):
    kind = config.get("kind")
    source = {"desktop": "Screen · SDR", "desktop-hdr": "Screen · HDR → SDR", "window": "Window",
              "device": "Capture device", "stream": "Stream", "file": "Video file"}.get(kind) if isinstance(kind, str) else None
    if not source:
        return "Unknown configuration"
    hud, fps = config.get("hud_scale"), config.get("process_fps")
    if type(hud) in (int, float) and math.isfinite(hud):
        source += f" · HUD {hud:.0%}"
    if type(fps) in (int, float) and math.isfinite(fps):
        source += f" · {fps:g} fps"
    return source


def capture_comparison(matches):
    results = {}
    for match in matches:
        key = capture_label(match.capture_config)
        totals = results.setdefault(key, [0, 0, 0])
        totals[0] += 1
        direct = match.replay.get("direct") if match.replay else {"chest_types": match.chest_types}
        types = direct.get("chest_types") if isinstance(direct, dict) else None
        if isinstance(types, dict) and all(type(count) is int and count >= 0 for count in types.values()):
            totals[1] += sum(count for kind, count in types.items() if kind != "unclassified")
            totals[2] += types.get("unknown", 0)
    return dict(sorted(results.items()))
