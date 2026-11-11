"""
User settings: a human-editable config.toml, with safe defaults and clear warnings.

Precedence:  built-in defaults  ->  config file  ->  command-line overrides

  * no config file            -> defaults (not an error)
  * a field is missing        -> the default for just that field
  * a value is invalid        -> warning, default for just that field
  * a key/section is unknown  -> warning (it is probably a typo), ignored
  * the file is not valid TOML-> warning, ALL defaults

Only user-facing behaviour is configurable here; fragile detector internals stay constants in
the code. Also home of the single count-formatting helper used by every user-facing number.
"""

import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")

LEGACY_SECTION_TEMPLATES = {
    "personal": ("Chests · {total}\nConfirmed · {certain}    Estimated · {approx}\nPirate Token · {chance}\n{types}",),
    "team": ("{member_totals}\nChests · {total}\nPirate Token · {chance}",
             "Chests · {total}    Players · {players}\nPirate Token · {chance}"),
    "cumulative": ("Chests · {total}\nMatches · {matches}\nPirate Token · {chance}",),
}

DEFAULTS = {
    "detector": {
        "process_fps": 15,                      # detector cadence; 15 is what the detectors were developed at
        "languages": ["en"],              # chest-word templates to use
        "certain_min_tier": "HIGH",             # "HIGH": CERTAIN+HIGH popups are certain; "CERTAIN": only CERTAIN
        "count_supported_medium_as_approx": True,       # MEDIUM popup + prompt + chest-like object
        "count_interaction_object_as_approx": True,     # prompt + chest-like object without a popup
        "medium_counts_alone_languages": ["en", "fr"],  # languages whose MEDIUM popups count as approx by themselves
    },
    "display": {
        "show": "both",                         # what obs_output/chest_counter.txt shows: both | certain | approx
        "both_layout": "inline",               # show = "both": "stacked" (two lines) or "inline" (one line = combined_status_format)
        "certain_text": "CERTAIN OPENED CHESTS: {count}",
        "approx_text": "APPROX. OPENED CHESTS: {count}",
        "combined_status_format": "{certain} certain • ~{approx} estimated",
        "use_thousands_separator": True,
        "screen_token_text": "Pirate Token · #",
        "screen_show_token": True,
        "screen_show_total": True,              # legacy setting retained for older config files
        "screen_total_text": "Total: #",
        "screen_show_types": True,
        "screen_show_matches": True,
        "screen_opacity": 100,
        "use_custom_sections": True,
        "screen_personal_enabled": True,
        "screen_team_enabled": True,
        "screen_cumulative_enabled": True,
        "screen_personal_title": "PERSONAL · CURRENT MATCH",
        "screen_team_title": "TEAM · CURRENT MATCH",
        "screen_cumulative_title": "MATCH HISTORY + LIVE",
        "screen_personal_template": "**Chests · {total}**\nConfirmed · {certain}    Estimated · {approx}\n{types}\nPirate Token · {chance}",
        "screen_team_template": "**Chests · {total}**\n{member_totals}\nPirate Token · {chance}",
        "screen_cumulative_template": "**Chests · {total}**\nMatches · {matches}\nPirate Token · {chance}",
        "screen_team_text": "Team · # chests · {chance}",
        "screen_cumulative_text": "All matches · # chests · {chance}",
        # v0.1.0 names, still understood: a config that sets only these gets "<label>: {count}" as its text.
        "certain_label": "CERTAIN OPENED CHESTS",
        "approximate_label": "APPROX. OPENED CHESTS",
    },
    "console": {
        "verbosity": "normal",                  # minimal | normal | debug
        "colors": True,                         # coloured live window (automatically off when not a console)
    },
    "state": {
        "persist": True,
        "path": "state/chest_state.json",
        "obs_dir": "obs_output",
    },
    "logging": {
        "enabled": False,
        "log_candidates": False,                # opt in to events that were NOT counted
        "save_snapshots": False,               # opt in to small popup PNGs for reporting misses
        "dir": "logs",
    },
    "capture": {
        "mode": "off",                          # off | marks (video around marks) | events (+ around detections) | session
        "before_seconds": 10,                   # video kept before / after each detection decision
        "after_seconds": 10,
        "mark_before_seconds": 30,              # video kept before / after a mark ("the counter missed a chest")
        "mark_after_seconds": 10,
        "quality": "standard",                  # standard (~10 GB/hour of video) | lossless (exact replay, ~22 GB/hour)
        "dir": "captures",
    },
    "live": {
        "source": "desktop-hdr",                     # stream | desktop | desktop-hdr | obs-camera | device | window | file
        "device": "OBS Virtual Camera",         # DirectShow device name for --source device
        "monitor": 1,                           # monitor number for --source desktop
        "window": "",                           # window title for --source window (an OBS "Windowed Projector (Source)")
        "url": "",                              # livestream URL for --source stream (Twitch / YouTube)
        "stream_quality": "720p",               # --source stream: 720p (recommended) | 1080p | 480p
        "stream_stability": "maximum",          # --source stream: maximum (recommended) | balanced | low_latency
        "status_interval_seconds": 5,
    },
    "obs_websocket": {
        "enabled": False,                       # also put the overlay text straight into an OBS Text source (OBS WebSocket 5)
        "host": "127.0.0.1",                    # the PC running OBS (this PC by default)
        "port": 4455,                           # OBS: Tools -> WebSocket Server Settings
        "source": "",                           # the OBS Text source to update (its name)
        "source_uuid": "",                      # its OBS id, so a rename in OBS is followed (set by the app)
    },
    "app": {
        "screen_overlay_enabled": True,
        "screen_overlay_shared": True,
        "token_mode": "Auto",
        "token_boost": "Normal ×1",
        "live_preview": False,
        "show_activity": False,
        "bot_name": "Chest Counter",
        "bot_avatar_path": "",
        "bot_banner_path": "",
        "webhook_name": "Chest Counter",
        "webhook_avatar_url": "https://raw.githubusercontent.com/NEVARLeVrai/fortnite-visual-chest-counter/main/docs/previews/logo.png",
        "discord_presence_enabled": True,
        "discord_presence_rotate": False,
        "discord_presence_interval": 30,
        "discord_presence_activity": "watching",
        "discord_presence_personal_text": "In match · {total} chests · Token {chance}",
        "discord_presence_team_text": "{players} players · {total} chests · Token {chance}",
        "discord_presence_token_text": "Token chance · {chance}",
        "discord_presence_group_text": "Group history · {total} chests · {matches} matches · Token {chance}",
        "discord_presence_idle_text": "In lobby",
        "group_player": "",
        "group_scoreboard_id": "",
        "group_share_matches": False,
        "group_role": "host",
        "remember_window": True,                # the desktop app reopens with the size, position and maximised state it closed with
        "close_to_tray": True,
        "start_in_tray": False,
        "auto_match_lifecycle": True,
        "auto_verify_replay": True,
        "delete_verified_replay": False,
        "auto_discord_summary": True,
        "auto_start_discord_sender": True,
    },
    "update": {
        "check_automatically": True,            # look for a new version shortly after the app opens (nothing is downloaded by itself)
        "download_bandwidth": "adaptive",       # adaptive | unlimited | custom
        "custom_limit_kb_per_second": 1000,     # used when download_bandwidth = "custom"
    },
}

_TIERS = ("CERTAIN", "HIGH")
LIVE_SOURCES = ("obs-camera", "device", "desktop", "desktop-hdr", "window", "stream", "file")
STREAM_QUALITIES = ("720p", "1080p", "480p")
# Livestream URL "Stream stability" (input/video_sources.STREAM_STABILITY holds the Streamlink values): key, name, text.
STREAM_STABILITY_CHOICES = (
    ("maximum", "Maximum stability   (recommended)",
     "Lowest buffering risk. Keeps the biggest safety buffer, so short network hiccups do not interrupt counting - "
     "counts appear later (the most delay of the three). Best for long sessions."),
    ("balanced", "Balanced",
     "Moderate buffering risk. A middle way between delay and buffering protection (Streamlink's standard setting)."),
    ("low_latency", "Low latency   (higher buffering risk)",
     "Follows the live stream most closely, but may buffer or reconnect more often on an unstable connection. Not "
     "recommended when complete counts matter."),
)
BANDWIDTH_MODES = ("adaptive", "unlimited", "custom")
MIN_CUSTOM_LIMIT_KB, MAX_CUSTOM_LIMIT_KB = 50, 1_000_000
SHOW_MODES = ("both", "certain", "approx")
BOTH_LAYOUTS = ("stacked", "inline")
VERBOSITY_LEVELS = ("minimal", "normal", "debug")
CAPTURE_MODES = ("off", "marks", "events", "session")
CAPTURE_QUALITIES = ("standard", "lossless")
# The app's language choice -> [detector] languages. "Both" is not automatic detection: it checks both words all the time.
# (Real "Auto" - identify the language per match - needs match recognition first; see docs/ROADMAP.md.)
LANGUAGE_CHOICES = (
    ("English", ["en"]),
    ("Portuguese (Brazil)", ["pt"]),
    ("Both (English + Portuguese (Brazil))", ["en", "pt"]),
    ("French", ["fr"]),
)


# The app's "Pictures analysed per second" choice -> [detector] process_fps (config.toml still accepts any 5-30).
# Measured (docs/releases/RELEASE_NOTES_v0.2.1.md): on the two labelled test recordings (22 chests) 15 found 21, 10 the same 21 plus
# 2 extra estimated counts, 5 found 18. Counter CPU on the development PC: 15 ~45 %, 10 ~44 %, 5 ~25 % of one core.
FPS_PRESETS = (
    (15, "Recommended", "What the counter was built and tested with. On our test PC the counter used about half of one "
                        "CPU core."),
    (10, "Balanced", "Fewer pictures to check. On our test PC this saved hardly any CPU, and on our test recordings it "
                     "found the same chests but added a few extra estimated (APPROX) counts."),
    (5, "Performance", "About half the CPU of 15 per second on our test PC, but it misses chests: on our test recordings "
                       "it found 18 of the 21 chests that 15 per second found."),
)


def fps_choice(fps):
    """Index in FPS_PRESETS for a process_fps value, or None for a custom value from config.toml."""
    return next((i for i, (value, _, _) in enumerate(FPS_PRESETS) if value == fps), None)


def language_choice(languages):
    """Index in LANGUAGE_CHOICES for a [detector] languages list (any order); unknown combinations -> Both."""
    wanted = set(languages or [])
    return next((i for i, (_, langs) in enumerate(LANGUAGE_CHOICES) if set(langs) == wanted), 2)
TEXT_TEMPLATES = ("certain_text", "approx_text", "combined_status_format")
# Placeholders every display text may use. {count} = this text's own number (CERTAIN for certain_text, APPROX for
# approx_text); {certain} and {approx} (or {approximate}) = either number, anywhere.
_SAMPLE = {"count": "1", "certain": "1", "approx": "2", "approximate": "2"}


def format_count(value, thousands=True):
    """The ONE place counts become text: 0, 999, 1,000, 1,234,567 (or raw integers if disabled)."""
    value = int(value)
    return f"{value:,}" if thousands else str(value)


def render_text(template, fallback, count=0, certain=0, approx=0, thousands=True):
    """Fill a user's display text. A broken template never raises: the default text is used instead."""
    values = {"count": format_count(count, thousands), "certain": format_count(certain, thousands),
              "approx": format_count(approx, thousands), "approximate": format_count(approx, thousands)}
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return fallback.format(**values)


def _check(section, key, value):
    """Return (ok, problem) for one value."""
    default = DEFAULTS[section][key]
    if isinstance(default, bool):
        return (isinstance(value, bool), "must be true or false")
    if key == "process_fps":
        return (isinstance(value, int) and not isinstance(value, bool) and 5 <= value <= 30, "must be a whole number from 5 to 30")
    if (section == "live" and key in ("window", "url")
            or section == "app" and key in ("group_player", "group_scoreboard_id")):
        return (isinstance(value, str), "must be text in quotes")
    if section == "app" and key == "group_role":
        return (value in ("", "host", "guest"), 'must be "host" or "guest"')
    if section == "live" and key == "stream_quality":
        return (value in STREAM_QUALITIES, f"must be one of {list(STREAM_QUALITIES)}")
    if section == "live" and key == "stream_stability":
        keys = [k for k, _, _ in STREAM_STABILITY_CHOICES]
        return (value in keys, f"must be one of {keys}")
    if section == "obs_websocket":
        if key == "port":
            return (isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535,
                    "must be a whole number from 1 to 65535")
        if key == "host":
            return (isinstance(value, str) and value.strip() != "" and not any(c.isspace() for c in value),
                    "must be a PC name or IP address, e.g. \"127.0.0.1\"")
        if key in ("source", "source_uuid"):
            return (isinstance(value, str), "must be text in quotes")
    if section == "update" and key == "download_bandwidth":
        return (value in BANDWIDTH_MODES, f"must be one of {list(BANDWIDTH_MODES)}")
    if section == "update" and key == "custom_limit_kb_per_second":
        return (isinstance(value, int) and not isinstance(value, bool) and MIN_CUSTOM_LIMIT_KB <= value <= MAX_CUSTOM_LIMIT_KB,
                f"must be a whole number from {MIN_CUSTOM_LIMIT_KB} to {MAX_CUSTOM_LIMIT_KB}")
    if section == "display" and key.startswith("screen_") and key.endswith(("_text", "_title", "_template")):
        return (isinstance(value, str) and len(value) <= 4096 and "\x00" not in value, "must be text up to 4096 characters")
    if section == "app" and key in ("bot_avatar_path", "bot_banner_path"):
        return (isinstance(value, str) and "\x00" not in value, "must be a file path or empty")
    if section == "app" and key.startswith("discord_presence_") and key.endswith("_text"):
        return (isinstance(value, str) and len(value) <= 128 and "\x00" not in value, "must be text up to 128 characters")
    if section == "app" and key == "discord_presence_interval":
        return (type(value) is int and 30 <= value <= 300, "must be a whole number from 30 to 300")
    if section == "app" and key == "discord_presence_activity":
        return (value in ("watching", "playing", "listening"), "must be watching, playing or listening")
    if section == "app" and key in ("bot_name", "webhook_name"):
        limit = 32 if key == "bot_name" else 80
        return (isinstance(value, str) and 2 <= len(value) <= limit and value.isprintable(),
                f"must be a printable name of 2 to {limit} characters")
    if section == "app" and key == "webhook_avatar_url":
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        return (value == "" or parsed is not None and len(value) <= 2048 and parsed.scheme == "https"
                and bool(parsed.hostname) and not parsed.username and not parsed.password,
                "must be an HTTPS image URL or empty")
    if section == "display" and key == "screen_opacity":
        return (type(value) is int and 20 <= value <= 100, "must be a whole number from 20 to 100")
    if key == "certain_min_tier":
        return (value in _TIERS, f"must be one of {list(_TIERS)}")
    if key == "source":
        return (value in LIVE_SOURCES, f"must be one of {list(LIVE_SOURCES)}")
    if section == "capture" and key.endswith("_seconds"):
        return (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 120,
                "must be a whole number of seconds from 0 to 120")
    if section == "capture" and key == "mode":
        return (value in CAPTURE_MODES, f"must be one of {list(CAPTURE_MODES)}")
    if section == "capture" and key == "quality":
        return (value in CAPTURE_QUALITIES, f"must be one of {list(CAPTURE_QUALITIES)}")
    if key in ("monitor", "status_interval_seconds"):
        return (isinstance(value, int) and not isinstance(value, bool) and value >= 1, "must be a whole number >= 1")
    if isinstance(default, list):
        return (isinstance(value, list) and all(isinstance(v, str) for v in value), "must be a list of strings")
    if key == "show":
        return (value in SHOW_MODES, f"must be one of {list(SHOW_MODES)}")
    if key == "both_layout":
        return (value in BOTH_LAYOUTS, f"must be one of {list(BOTH_LAYOUTS)}")
    if key == "verbosity":
        return (value in VERBOSITY_LEVELS, f"must be one of {list(VERBOSITY_LEVELS)}")
    if key in TEXT_TEMPLATES:
        if not isinstance(value, str):
            return False, "must be text in quotes"
        try:
            value.format(**_SAMPLE)
        except (KeyError, IndexError, ValueError):
            return False, ("may only use the placeholders {count}, {certain} and {approx} "
                           "(write {{ and }} for a literal brace)")
        return True, ""
    if isinstance(default, str):
        return (isinstance(value, str) and value.strip() != "", "must be non-empty text")
    return True, ""


def load_config(path=None, overrides=None):
    """
    -> (config dict, description of where it came from, list of warning strings).
    `overrides` is {(section, key): value} from the command line; only given values are applied.
    """
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    warnings = []
    explicit = path is not None
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    origin = "built-in defaults"

    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))   # utf-8-sig: Notepad/PowerShell may add a BOM
            origin = str(path)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as error:
            warnings.append(f"{path}: cannot be read ({error}); using ALL built-in defaults")
            data = {}
        explicit_keys = {(sec, key) for sec, vals in data.items() if isinstance(vals, dict) for key in vals}
        for section, values in data.items():
            if section not in DEFAULTS or not isinstance(values, dict):
                warnings.append(f"{path}: unknown section [{section}] ignored (typo?)")
                continue
            for key, value in values.items():
                if key not in DEFAULTS[section]:
                    warnings.append(f"{path}: unknown key '{key}' in [{section}] ignored (typo?)")
                    continue
                ok, problem = _check(section, key, value)
                if ok:
                    cfg[section][key] = value
                else:
                    warnings.append(f"{path}: [{section}] {key} = {value!r} {problem}; using default {DEFAULTS[section][key]!r}")
        # v0.1.0 configs: a custom certain_label / approximate_label still changes the overlay text.
        for label_key, text_key in (("certain_label", "certain_text"), ("approximate_label", "approx_text")):
            if (("display", label_key) in explicit_keys and ("display", text_key) not in explicit_keys
                    and cfg["display"][label_key] != DEFAULTS["display"][label_key]):
                label = cfg["display"][label_key].replace("{", "{{").replace("}", "}}")
                cfg["display"][text_key] = label + ": {count}"
    elif explicit:
        warnings.append(f"{path}: config file not found; using built-in defaults")

    for (section, key), value in (overrides or {}).items():
        if value is None:
            continue
        ok, problem = _check(section, key, value)
        if ok:
            cfg[section][key] = value
        else:
            warnings.append(f"command line: {key} = {value!r} {problem}; keeping {cfg[section][key]!r}")
    if cfg["display"]["screen_cumulative_title"] == "ALL MATCHES":
        cfg["display"]["screen_cumulative_title"] = DEFAULTS["display"]["screen_cumulative_title"]
    for scope, previous in LEGACY_SECTION_TEMPLATES.items():
        key = f"screen_{scope}_template"
        if cfg["display"][key].replace("\\n", "\n") in previous:
            cfg["display"][key] = DEFAULTS["display"][key]
    return cfg, origin, warnings
