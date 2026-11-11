"""Render the shared on-screen and OBS sections from one validated context."""

from html import escape
import math
import re

from fortnite_chest_counter import match_stats, settings
from fortnite_chest_counter.detection import chest_types


SCOPES = ("personal", "team", "cumulative")


def _count(value, default=0):
    return value if type(value) is int and 0 <= value <= 9_000_000_000_000_000 else default


def _boost(value):
    return float(value) if type(value) in (int, float) and value in (1, 1.5, 2) else 1.0


def _summary(value):
    if not isinstance(value, dict):
        return None
    total = _count(value.get("total"))
    certain = min(_count(value.get("certain")), total)
    boost = _boost(value.get("boost"))
    log_no_drop = value.get("log_no_drop")
    if type(log_no_drop) not in (int, float) or not math.isfinite(log_no_drop) or log_no_drop > 0:
        log_no_drop = match_stats.chest_exposure(total, boost)
    rows = value.get("member_totals")
    member_totals = []
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            name, count = row
            if isinstance(name, str) and name.strip():
                member_totals.append((" ".join(name.splitlines()).strip(), _count(count)))
    return {
        "member_totals": tuple(member_totals),
        "certain": certain, "total": total, "log_no_drop": float(log_no_drop), "boost": boost,
        "matches": _count(value.get("matches")), "players": _count(value.get("players")),
        "names": value.get("names") if isinstance(value.get("names"), str) else "",
        "types": value.get("types") if isinstance(value.get("types"), str) else "",
    }


def _personal(state, *, boost=1.0, active=False):
    state = state if isinstance(state, dict) else {}
    total = max(_count(state.get("approximate")), _count(state.get("certain")))
    return {"certain": _count(state.get("certain")), "total": total,
            "log_no_drop": match_stats.chest_exposure(total, boost), "boost": boost,
            "matches": int(bool(total or active)), "players": 1, "names": "", "types": "", "member_totals": ()}


def valid_context(value):
    return (isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
            and isinstance(value.get("state"), dict) and isinstance(value.get("personal"), dict)
            and type(value["state"].get("certain")) is int and value["state"]["certain"] >= 0
            and type(value["state"].get("approximate")) is int
            and value["state"]["approximate"] >= value["state"]["certain"]
            and "session_started_at" in value["state"]
            and (value["state"]["session_started_at"] is None
                 or isinstance(value["state"]["session_started_at"], str)))


def reconcile_context(context, state):
    """Advance a GUI snapshot to worker counts without adding the same current match twice."""
    if not isinstance(state, dict):
        state = {}
    if not valid_context(context):
        personal = _personal(state)
        return {"version": 1, "state": dict(state), "personal": personal, "team": None,
                "cumulative": dict(personal), "current_saved": False, "active": False}
    personal = _summary(context["personal"])
    team = _summary(context.get("team"))
    cumulative = _summary(context.get("cumulative")) or dict(personal)
    old = context["state"]
    total = max(_count(state.get("approximate")), _count(state.get("certain")))
    certain = _count(state.get("certain"))
    same_match = old.get("session_started_at") == state.get("session_started_at")
    rate = (personal["log_no_drop"] / personal["total"] if personal["total"] > 0
            else match_stats.chest_exposure(1, personal["boost"]))
    if same_match:
        total_delta = total - old["approximate"]
        certain_delta = certain - old["certain"]
        if context.get("replay_verified") is True and (total_delta or certain_delta):
            personal = _personal(state, boost=personal["boost"], active=context.get("active") is True)
            team = None
            context = {**context, "replay_verified": False}
            total_delta = certain_delta = 0
        live_summaries = (personal, team) if context.get("current_saved") is True else (personal, team, cumulative)
        for summary in live_summaries:
            if summary is None:
                continue
            summary["total"] = max(0, summary["total"] + total_delta)
            summary["certain"] = min(summary["total"], max(0, summary["certain"] + certain_delta))
            summary["log_no_drop"] = min(0.0, summary["log_no_drop"] + total_delta * rate)
        if team is not None:
            own = personal["names"].strip().casefold()
            team["member_totals"] = tuple((name, personal["total"] if name.casefold() == own else count)
                                          for name, count in team["member_totals"])
        previous_matches = personal["matches"]
        personal["matches"] = max(previous_matches, int(total > 0 or context.get("active") is True))
        if context.get("current_saved") is not True:
            cumulative["matches"] += personal["matches"] - previous_matches
    else:
        # A saved previous match already belongs to the cumulative result. An unsaved
        # current contribution must be removed before incorporating the new counter.
        if context.get("current_saved") is not True:
            previous = team if team is not None else personal
            cumulative["total"] = max(0, cumulative["total"] - previous["total"])
            cumulative["certain"] = max(0, cumulative["certain"] - previous["certain"])
            cumulative["log_no_drop"] = min(0.0, cumulative["log_no_drop"] - previous["log_no_drop"])
            cumulative["matches"] = max(0, cumulative["matches"] - previous["matches"])
        personal = _personal(state, boost=personal["boost"], active=context.get("active") is True)
        cumulative["total"] += personal["total"]
        cumulative["certain"] = min(cumulative["total"], cumulative["certain"] + personal["certain"])
        cumulative["log_no_drop"] += personal["log_no_drop"]
        cumulative["matches"] += personal["matches"]
        team = None
        context = {**context, "replay_verified": False}
    return {**context, "version": 1, "state": dict(state), "personal": personal, "team": team,
            "cumulative": cumulative, "current_saved": context.get("current_saved") is True and same_match,
            "active": context.get("active") is True}


def _types_text(state, thousands):
    raw = state.get("types")
    if not isinstance(raw, dict):
        return None
    kinds = []
    classified = 0
    for kind, row in raw.items():
        if not isinstance(row, dict):
            continue
        count = _count(row.get("approx"))
        classified += count
        if count:
            kinds.append(f"{chest_types.LABELS.get(kind, kind)} {settings.format_count(count, thousands)}")
    rest = max(0, _count(state.get("approximate")) - classified)
    if rest:
        kinds.append(f"not classified {settings.format_count(rest, thousands)}")
    return "Chest types · " + (" · ".join(kinds) or "no chests yet")


def render_sections(display, context, *, styled=False):
    """Render templates as plain text, or as escaped rich text for the Qt overlay."""
    context = context if isinstance(context, dict) else {}
    state = context.get("state") if isinstance(context.get("state"), dict) else {}
    personal = _summary(context.get("personal")) or _personal(state)
    summaries = (personal, _summary(context.get("team")), _summary(context.get("cumulative")) or dict(personal))
    thousands = display.get("use_thousands_separator", True)
    hidden = set()
    for flag, token in (("screen_show_token", "chance"), ("screen_show_matches", "matches"),
                        ("screen_show_types", "types")):
        if not display.get(flag, settings.DEFAULTS["display"][flag]):
            hidden.add(token)
    rendered = []
    for scope, summary in zip(SCOPES, summaries):
        if summary is None or not display.get(f"screen_{scope}_enabled", True):
            rendered.append("")
            continue
        values = {name: settings.format_count(summary[name], thousands)
                  for name in ("certain", "total", "matches", "players")}
        values["approx"] = settings.format_count(summary["total"] - summary["certain"], thousands)
        values["chance"] = match_stats.token_chance(summary["log_no_drop"])
        values.update(names=summary["names"], types=summary["types"])
        if scope == "personal" and not context.get("replay_verified"):
            current_types = _types_text(state, thousands)
            if current_types is not None:
                values["types"] = current_types
        template = display.get(f"screen_{scope}_template", settings.DEFAULTS["display"][f"screen_{scope}_template"])
        template = template if isinstance(template, str) else ""
        values["member_totals"] = "\n".join(
            f"{name} · {settings.format_count(count, thousands)}" for name, count in summary["member_totals"])
        lines = []
        for line in template.replace("\\n", "\n").splitlines():
            if any("{" + token + "}" in line for token in hidden) or any("{" + token + "}" in line and not values[token]
                           for token in ("types", "member_totals")):
                continue
            parts = re.split(r"\*\*(.+?)\*\*", line)
            for index, part in enumerate(parts):
                part = re.sub(r"\{(\w+)\}", lambda match: values.get(match[1], match[0]), part)
                if styled:
                    part = re.sub(r" {2,}", lambda match: "&nbsp;" * len(match[0]), escape(part))
                    if index % 2:
                        part = f"<b>{part}</b>"
                parts[index] = part
            lines.append("".join(parts).rstrip())
        text = "\n".join(lines).strip()
        rendered.append(text.replace("\n", "<br>") if styled else text)
    return tuple(rendered)


def format_text(display, sections):
    """Use the same section titles in plain text output as in the graphical overlay."""
    parts = []
    for scope, text in zip(SCOPES, sections):
        if not text:
            continue
        title = display.get(f"screen_{scope}_title", settings.DEFAULTS["display"][f"screen_{scope}_title"])
        title = title.strip() if isinstance(title, str) else ""
        parts.append(f"{title}\n{text}" if title else text)
    return "\n\n".join(parts)
