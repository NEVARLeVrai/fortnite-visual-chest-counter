"""Durable shared Group contributions sent through a single Discord webhook."""

from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
import json
import math
import random
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from urllib.parse import urlsplit
from uuid import uuid4

from fortnite_chest_counter import match_stats, paths, replay_comparison, settings
from fortnite_chest_counter.detection.chest_types import LABELS
from fortnite_chest_counter.discord_summary import _NoRedirects
from fortnite_chest_counter.obs import credentials


_TARGET = "Fortnite Visual Chest Counter/Group Webhook"
_WEBHOOK = re.compile(r"https://discord\.com/api/(?:v10/)?webhooks/([1-9][0-9]{14,21})/([\w.-]{16,300})", re.ASCII)
_EVENT_ID = re.compile(r"[a-f0-9]{32}")
_PREFIX = "||FCC1 "
_LIVE_PREFIX = "||FCCL1 "
_SESSION_PREFIX = "FCC session v1 "
_FINGERPRINT = re.compile(r"[a-f0-9]{64}")
PIRATE_TOKEN_RATE = match_stats.RATE
_FLUSH_LOCK = threading.Lock()
_COOLDOWN_LOCK = threading.Lock()
_WEBHOOK_COOLDOWNS = {}
_CLIENT_STAGGER = random.uniform(0.0, 2.0)

GROUP_TYPE_LABELS = {**LABELS, "unclassified": "Not classified", "buried": "Buried chest", "poster": "Spirit poster chest"}
_WEBHOOK_NAME = "Chest Counter"
_WEBHOOK_AVATAR = ("https://raw.githubusercontent.com/NEVARLeVrai/fortnite-visual-chest-counter/"
                   "main/docs/previews/logo.png")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_revisions (
    webhook TEXT NOT NULL, event TEXT NOT NULL, incoming INTEGER NOT NULL,
    PRIMARY KEY (webhook, event, incoming)
);
CREATE TABLE IF NOT EXISTS session_states (
    webhook TEXT NOT NULL, fingerprint TEXT NOT NULL, player TEXT NOT NULL, name TEXT NOT NULL,
    revision INTEGER NOT NULL, certain INTEGER NOT NULL, total INTEGER NOT NULL, boost REAL NOT NULL,
    finished INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(webhook, fingerprint, player)
);
CREATE TABLE IF NOT EXISTS live_receipts (
    webhook TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL,
    fingerprint TEXT NOT NULL, player TEXT NOT NULL, PRIMARY KEY(webhook, channel, message)
);
CREATE TABLE IF NOT EXISTS live_outbox (
    webhook TEXT NOT NULL, fingerprint TEXT NOT NULL, player TEXT NOT NULL, name TEXT NOT NULL,
    revision INTEGER NOT NULL, certain INTEGER NOT NULL, total INTEGER NOT NULL, boost REAL NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', PRIMARY KEY(webhook, fingerprint, player)
);
CREATE TABLE IF NOT EXISTS contributions (
    webhook TEXT NOT NULL, event TEXT NOT NULL, player TEXT NOT NULL, name TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY(webhook, event)
);
CREATE TABLE IF NOT EXISTS corrections (
    webhook TEXT NOT NULL, player TEXT NOT NULL, amount INTEGER NOT NULL, reset_row INTEGER NOT NULL,
    PRIMARY KEY(webhook, player)
);
CREATE TABLE IF NOT EXISTS cursors (
    webhook TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(webhook, channel)
);
CREATE TABLE IF NOT EXISTS outbox (
    webhook TEXT NOT NULL, snapshot TEXT NOT NULL, event TEXT NOT NULL, name TEXT NOT NULL,
    count INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(webhook, snapshot)
);
CREATE TABLE IF NOT EXISTS event_details (
    webhook TEXT NOT NULL, event TEXT NOT NULL, certain INTEGER, chest_types TEXT NOT NULL,
    PRIMARY KEY(webhook, event)
);
CREATE TABLE IF NOT EXISTS outbox_failures (
    webhook TEXT NOT NULL, event TEXT NOT NULL, PRIMARY KEY(webhook, event)
);
CREATE TABLE IF NOT EXISTS replay_outbox_details (
    webhook TEXT NOT NULL, event TEXT NOT NULL, certain INTEGER NOT NULL, chest_types TEXT NOT NULL,
    PRIMARY KEY(webhook, event)
);
CREATE TABLE IF NOT EXISTS leaderboard_publications (
    webhook TEXT NOT NULL, bot TEXT NOT NULL, guild TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL,
    PRIMARY KEY(webhook, bot, guild)
);
CREATE TABLE IF NOT EXISTS event_sessions (
    webhook TEXT NOT NULL, event TEXT NOT NULL, fingerprint TEXT NOT NULL, boost REAL NOT NULL,
    PRIMARY KEY(webhook, event)
);
"""


@dataclass(frozen=True)
class Webhook:
    id: str
    token: str = field(repr=False)

    @property
    def url(self):
        return f"https://discord.com/api/v10/webhooks/{self.id}/{self.token}"


class WebhookRateLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"Discord is rate limiting Group sync. Retry in {math.ceil(self.retry_after)} seconds. "
                         "Pending results are kept.")


def _webhook_key(webhook):
    return str(getattr(webhook, "id", webhook))


def webhook_retry_after(webhook):
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        return max(0.0, _WEBHOOK_COOLDOWNS.get(_webhook_key(webhook), 0.0) - now,
                   _WEBHOOK_COOLDOWNS.get("*", 0.0) - now)


def webhook_sync_delay(webhook, base=5.0):
    """Spread clients across a bounded interval and respect a server-requested cooldown."""
    return max(webhook_retry_after(webhook), max(0.0, base) + _CLIENT_STAGGER + random.uniform(0.0, 1.0))


def check_webhook_cooldown(webhook):
    remaining = webhook_retry_after(webhook)
    if remaining > 0:
        raise WebhookRateLimited(remaining)


def _retry_number(value):
    if type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 64:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return max(0.5, seconds)


def _set_webhook_cooldown(webhook, seconds, *, global_limit=False):
    delay = seconds + random.uniform(0.1, 0.5)
    until = time.monotonic() + delay
    with _COOLDOWN_LOCK:
        key = "*" if global_limit else _webhook_key(webhook)
        _WEBHOOK_COOLDOWNS[key] = max(_WEBHOOK_COOLDOWNS.get(key, 0.0), until)
    return webhook_retry_after(webhook)


def webhook_rate_limited(webhook, error):
    """Read only bounded Discord error metadata. No request is retried or slept here."""
    headers = getattr(error, "headers", None)
    delays = []
    if headers is not None:
        for key in ("Retry-After", "X-RateLimit-Reset-After"):
            delay = _retry_number(headers.get(key))
            if delay is not None:
                delays.append(delay)
    global_limit = bool(headers is not None and (
        str(headers.get("X-RateLimit-Global", "")).lower() == "true"
        or headers.get("X-RateLimit-Scope") == "global"))
    try:
        body = error.read(8192)
        if len(body) <= 8192:
            data = json.loads(body)
            if isinstance(data, dict):
                delay = _retry_number(data.get("retry_after"))
                if delay is not None:
                    delays.append(delay)
                global_limit = global_limit or data.get("global") is True
    except (AttributeError, OSError, TypeError, ValueError, RecursionError):
        pass
    finally:
        error.close()
    delay = max(delays, default=1.0)
    return WebhookRateLimited(_set_webhook_cooldown(webhook, delay, global_limit=global_limit))


def webhook_response(webhook, response):
    headers = getattr(response, "headers", None)
    if headers is not None and headers.get("X-RateLimit-Remaining") == "0":
        delay = _retry_number(headers.get("X-RateLimit-Reset-After"))
        if delay is not None:
            _set_webhook_cooldown(webhook, delay)


def webhook_identity():
    """Read the configured sender for new messages without changing the webhook itself."""
    cfg, _, _ = settings.load_config(paths.APP_DIR / "config.toml")
    options = cfg["app"]
    name = options.get("webhook_name", _WEBHOOK_NAME)
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or not name.isprintable():
        name = _WEBHOOK_NAME
    identity = {"username": name}
    avatar = options.get("webhook_avatar_url", _WEBHOOK_AVATAR)
    if isinstance(avatar, str) and avatar and len(avatar) <= 2048:
        try:
            location = urlsplit(avatar)
            if location.scheme == "https" and location.hostname:
                identity["avatar_url"] = avatar
        except ValueError:
            pass
    return identity


def _parse_webhook(url):
    match = _WEBHOOK.fullmatch(url.strip())
    if not match:
        raise ValueError("Paste a valid Discord webhook URL.")
    return Webhook(*match.groups())


def load_webhook():
    url = credentials.load_password(_TARGET)
    return _parse_webhook(url) if url else None


def save_webhook(url):
    webhook = _parse_webhook(url)
    credentials.save_password(webhook.url, _TARGET, description="Shared Discord Group webhook")
    return webhook


def forget_webhook():
    credentials.forget_password(_TARGET)


def validate_player(player):
    player = " ".join(player.split())
    if not player or len(player) > 80 or not all(char.isprintable() for char in player):
        raise ValueError("Enter a Group player name of 1 to 80 characters.")
    return player


@dataclass(frozen=True)
class Player:
    name: str
    total: int
    certain: int | None = None
    chest_types: dict[str, int] = field(default_factory=dict)
    recorded: int = 0
    adjustment: int = 0
    confirmed_total: int = 0
    matches: int = 0
    recorded_log_no_drop: float = 0.0

    @property
    def log_no_drop(self):
        # Manual additions have no saved boost. Reductions have no chest identity,
        # so scale the recorded exposure by the fraction of chests retained.
        base = match_stats.chest_exposure(1, 1)
        recorded = self.recorded_log_no_drop or self.recorded * base
        if self.total < self.recorded:
            return recorded * max(0, self.total) / self.recorded
        return recorded + (self.total - self.recorded) * base


    @property
    def token_chance(self):
        return match_stats.token_chance(self.log_no_drop)


def _combine(name, players, adjustment=0):
    kinds = Counter()
    recorded = confirmed_total = certain = matches = 0
    has_details = False
    recorded_odds = []
    for player in players:
        recorded_odds.append(player.recorded_log_no_drop)
        recorded += player.recorded
        confirmed_total += player.confirmed_total
        certain += player.certain or 0
        has_details |= player.certain is not None
        matches += player.matches
        kinds.update(player.chest_types)
        adjustment += player.adjustment
    return Player(name, recorded + adjustment, certain if has_details else None, dict(kinds),
                  recorded, adjustment, confirmed_total, matches, math.fsum(recorded_odds))


@dataclass(frozen=True)
class TeamSummary:
    matches: int = 0
    players: int = 0
    chests: int = 0
    one_in: int | None = None
    chance_percent: str | None = None

    def text(self):
        total = f"Team matches: {self.matches:,}"
        if not self.matches:
            return total
        chance = self.chance_percent or (f"about 1 in {self.one_in:,}" if self.one_in is not None else "zero")
        if self.chance_percent and self.one_in is not None:
            chance += f" (about 1 in {self.one_in:,})"
        return (f"{total}\nLatest received team match: {self.chests:,} chests · "
                f"{self.players:,} submitted players\nEstimated Pirate Token chance from submitted chests: {chance}")


@dataclass(frozen=True)
class SessionPlayer:
    name: str
    certain: int
    total: int
    boost: float
    finished: bool = False
    revision: int = 0
    updated_at: float = 0.0


@dataclass(frozen=True)
class TeamSession:
    fingerprint: str
    players: tuple[SessionPlayer, ...]

    @property
    def total(self):
        return sum(player.total for player in self.players)

    @property
    def certain(self):
        return sum(player.certain for player in self.players)

    @property
    def complete(self):
        return bool(self.players) and all(player.finished for player in self.players)

    @property
    def chance_percent(self):
        no_drop = math.fsum(match_stats.chest_exposure(player.total, player.boost)
                            for player in self.players)
        return match_stats.token_chance(no_drop)


def validate_sessions(values):
    """Validate all rows before a snapshot can change cached session state."""
    if not isinstance(values, (tuple, list)) or len(values) > 100_000:
        raise ValueError("Invalid shared session snapshot.")
    result = []
    seen = set()
    for session in values:
        if (not isinstance(session, TeamSession) or not isinstance(session.fingerprint, str)
                or not _FINGERPRINT.fullmatch(session.fingerprint)):
            raise ValueError("Invalid shared session fingerprint.")
        if session.fingerprint in seen or not isinstance(session.players, (tuple, list)) or len(session.players) > 100:
            raise ValueError("Invalid shared session players.")
        seen.add(session.fingerprint)
        players = set()
        for player in session.players:
            if (not isinstance(player, SessionPlayer) or not isinstance(player.name, str)
                    or validate_player(player.name) != player.name
                    or player.name.casefold() in players
                    or type(player.total) is not int or not 0 <= player.total <= 1_000_000_000
                    or type(player.certain) is not int or not 0 <= player.certain <= player.total
                    or _validate_session(session.fingerprint, player.boost) is None
                    or type(player.finished) is not bool or type(player.revision) is not int
                    or not 0 <= player.revision <= 9_000_000_000_000_000_000
                    or type(player.updated_at) not in (int, float) or not math.isfinite(player.updated_at)
                    or not 0 <= player.updated_at <= 100_000_000_000):
                raise ValueError("Invalid shared player state.")
            players.add(player.name.casefold())
        result.append(TeamSession(session.fingerprint, tuple(session.players)))
    return tuple(result)


class GroupStore:
    def __init__(self, webhook_id, path=None):
        self.webhook_id = webhook_id
        self.path = Path(path) if path is not None else paths.APP_DIR / "state" / "discord-group.sqlite3"

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
            with db:
                yield db

    def _players(self, db):
        rows = db.execute("""
            SELECT m.player, m.name, m.count, d.certain, d.chest_types,
                   COALESCE(c.amount, 0), m.rowid > COALESCE(c.reset_row, 0), COALESCE(s.boost, 1.0), s.fingerprint
            FROM contributions AS m
            LEFT JOIN corrections AS c ON c.webhook = m.webhook AND c.player = m.player
            LEFT JOIN event_details AS d ON d.webhook = m.webhook AND d.event = m.event
            LEFT JOIN event_sessions AS s ON s.webhook = m.webhook AND s.event = m.event
            WHERE m.webhook = ?
            ORDER BY m.rowid
        """, (self.webhook_id,))
        groups = {}
        identities = {}
        seen = set()
        for key, name, count, certain, chest_types, amount, included, boost, fingerprint in rows:
            identities[key] = name, amount
            if fingerprint is not None:
                identity = key, fingerprint
                if identity in seen:
                    continue
                seen.add(identity)
            if included:
                groups.setdefault(key, []).append(Player(name, count, certain, json.loads(chest_types or "{}"),
                                                          count, confirmed_total=count if certain is not None else 0,
                                                          matches=1, recorded_log_no_drop=match_stats.chest_exposure(count, boost)))
        return {key: _combine(name, groups.get(key, ()), amount) for key, (name, amount) in identities.items()}

    def _leaderboard(self, db):
        players = (player for player in self._players(db).values() if player.matches or player.adjustment)
        return sorted(players, key=lambda player: (-player.total, player.name.casefold()))

    def leaderboard(self):
        with self.connection() as db:
            return self._leaderboard(db)

    def _team_summary(self, db):
        rows = db.execute("""
            WITH first_submissions AS (
                SELECT s.fingerprint, MIN(m.rowid) AS received
                FROM contributions AS m
                JOIN event_sessions AS s ON s.webhook = m.webhook AND s.event = m.event
                WHERE m.webhook = ?
                GROUP BY s.fingerprint, m.player
            )
            SELECT f.fingerprint, m.count, s.boost
            FROM first_submissions AS f
            JOIN contributions AS m ON m.rowid = f.received
            JOIN event_sessions AS s ON s.webhook = m.webhook AND s.event = m.event
            ORDER BY f.received
        """, (self.webhook_id,))
        sessions = {}
        latest = None
        for latest, count, boost in rows:
            sessions.setdefault(latest, []).append((count, boost))
        if latest is None:
            return TeamSummary()
        submitted = sessions[latest]
        chests = sum(count for count, _ in submitted)
        log_no_drop = math.fsum(match_stats.chest_exposure(count, boost)
                                for count, boost in submitted)
        chance = -math.expm1(log_no_drop) if log_no_drop else 0.0
        return TeamSummary(len(sessions), len(submitted), chests, round(1 / chance) if chance else None,
                           f"{chance:.6%}")

    def team_summary(self):
        with self.connection() as db:
            return self._team_summary(db)

    def board(self):
        with self.connection() as db:
            db.execute("BEGIN")
            ranking = self._leaderboard(db)
            return ranking, self._team_summary(db) if ranking else TeamSummary()

    def count(self, name):
        player = self.stats(name)
        return player.name, player.total

    def stats(self, name=None):
        with self.connection() as db:
            if name is None:
                return _combine("Everyone", self._players(db).values())
            return self._player(db, name)

    def _player(self, db, name):
        player = self._players(db).get(name.strip().casefold())
        if player is None:
            raise ValueError("Player not found in shared Group matches.")
        return player

    def adjust(self, name, amount):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            player = self._player(db, name)
            key = player.name.casefold()
            if amount is None:
                reset_row, = db.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM contributions WHERE webhook=? AND player=?", (self.webhook_id, key)
                ).fetchone()
                db.execute("INSERT OR REPLACE INTO corrections VALUES (?, ?, 0, ?)", (self.webhook_id, key, reset_row))
                return player.name, 0
            if player.total + amount < 0:
                raise ValueError("A player's total cannot be negative.")
            db.execute("""
                INSERT INTO corrections VALUES (?, ?, ?, 0)
                ON CONFLICT(webhook, player) DO UPDATE SET amount = amount + excluded.amount
            """, (self.webhook_id, key, amount))
            return player.name, player.total + amount

    def reset_players(self, names):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for name in names:
                player = self._player(db, name)
                key = player.name.casefold()
                row, = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM contributions WHERE webhook=? AND player=?",
                                  (self.webhook_id, key)).fetchone()
                db.execute("INSERT OR REPLACE INTO corrections VALUES (?, ?, 0, ?)", (self.webhook_id, key, row))

    def _save_player_state(self, db, fingerprint, player):
        db.execute("""
            INSERT INTO session_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(webhook, fingerprint, player) DO UPDATE SET
                name=excluded.name, revision=excluded.revision, certain=excluded.certain,
                total=excluded.total, boost=excluded.boost, finished=excluded.finished,
                updated_at=COALESCE(NULLIF(excluded.updated_at, 0), session_states.updated_at)
            WHERE (session_states.finished=0
                   AND (excluded.finished=1 OR excluded.revision > session_states.revision))
               OR (session_states.finished=1 AND excluded.finished=1
                   AND excluded.revision > session_states.revision)
        """, (self.webhook_id, fingerprint, player.name.casefold(), player.name, player.revision,
              player.certain, player.total, player.boost, int(player.finished), player.updated_at))

    def sessions(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Backfill verified results received before live sessions were introduced.
            records = db.execute("""
                SELECT s.fingerprint, m.name, COALESCE(d.certain, 0), m.count, s.boost, COALESCE(r.incoming, 0)
                FROM contributions AS m
                JOIN event_sessions AS s ON s.webhook=m.webhook AND s.event=m.event
                LEFT JOIN event_details AS d ON d.webhook=m.webhook AND d.event=m.event
                LEFT JOIN replay_revisions AS r ON r.webhook=m.webhook AND r.event=m.event AND r.incoming=1
                WHERE m.webhook=? ORDER BY m.rowid
            """, (self.webhook_id,))
            first = set()
            for fingerprint, name, certain, total, boost, revision in records:
                key = fingerprint, name.casefold()
                if key in first:
                    continue
                first.add(key)
                self._save_player_state(db, fingerprint, SessionPlayer(name, certain, total, boost, True, revision))
            groups = {}
            for fingerprint, name, revision, certain, total, boost, finished, updated_at in db.execute(
                    "SELECT fingerprint, name, revision, certain, total, boost, finished, updated_at "
                    "FROM session_states WHERE webhook=? ORDER BY updated_at, fingerprint, player",
                    (self.webhook_id,)):
                groups.setdefault(fingerprint, []).append(
                    SessionPlayer(name, certain, total, boost, bool(finished), revision, updated_at))
            return tuple(TeamSession(fingerprint, tuple(players)) for fingerprint, players in groups.items())

    def merge_sessions(self, sessions):
        sessions = validate_sessions(sessions)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for session in sessions:
                for player in session.players:
                    self._save_player_state(db, session.fingerprint, player)

    def queue_live(self, fingerprint, player, certain, total, boost):
        name = validate_player(player)
        state = SessionPlayer(name, certain, total, boost, updated_at=time.time())
        validate_sessions((TeamSession(fingerprint, (state,)),))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision, certain, total, boost, finished, updated_at FROM session_states "
                                 "WHERE webhook=? AND fingerprint=? AND player=?",
                                 (self.webhook_id, fingerprint, name.casefold())).fetchone()
            if current and (current[4] or (current[1:4] == (certain, total, boost) and time.time() - current[5] < 20)):
                return False
            revision = current[0] + 1 if current else 1
            state = SessionPlayer(name, certain, total, boost, revision=revision, updated_at=state.updated_at)
            self._save_player_state(db, fingerprint, state)
            db.execute("""
                INSERT INTO live_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '')
                ON CONFLICT(webhook, fingerprint, player) DO UPDATE SET
                    name=excluded.name, revision=excluded.revision, certain=excluded.certain,
                    total=excluded.total, boost=excluded.boost, sent=0
            """, (self.webhook_id, fingerprint, name.casefold(), name, revision, certain, total, boost))
            return True

    def live_messages(self, channel):
        """Recover edits made while the Gateway was disconnected."""
        with self.connection() as db:
            rows = db.execute("""
                SELECT r.message FROM live_receipts AS r
                JOIN session_states AS s ON s.webhook=r.webhook AND s.fingerprint=r.fingerprint AND s.player=r.player
                WHERE r.webhook=? AND r.channel=? AND s.finished=0 AND s.updated_at >= ?
                ORDER BY s.updated_at DESC LIMIT 1000
            """, (self.webhook_id, str(channel), time.time() - 48 * 3600)).fetchall()
            return tuple(int(row[0]) for row in rows)

    def published_board(self, bot, guild):
        with self.connection() as db:
            return db.execute("SELECT channel, message FROM leaderboard_publications "
                              "WHERE webhook=? AND bot=? AND guild=?",
                              (self.webhook_id, str(bot), str(guild))).fetchone()

    def save_published_board(self, bot, guild, channel, message):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO leaderboard_publications VALUES (?, ?, ?, ?, ?)",
                       (self.webhook_id, str(bot), str(guild), str(channel), str(message)))

    def forget_published_board(self, bot, guild):
        with self.connection() as db:
            db.execute("DELETE FROM leaderboard_publications WHERE webhook=? AND bot=? AND guild=?",
                       (self.webhook_id, str(bot), str(guild)))

    def cursor(self, channel):
        with self.connection() as db:
            row = db.execute("SELECT message FROM cursors WHERE webhook=? AND channel=?",
                             (self.webhook_id, str(channel))).fetchone()
            return int(row[0]) if row else 0

    def advance_cursor(self, channel, message):
        with self.connection() as db:
            db.execute("""INSERT INTO cursors VALUES (?, ?, ?)
                          ON CONFLICT(webhook, channel) DO UPDATE SET message = excluded.message
                          WHERE length(excluded.message) > length(cursors.message)
                             OR (length(excluded.message) = length(cursors.message)
                                 AND excluded.message > cursors.message)""",
                       (self.webhook_id, str(channel), str(message)))

    def ingest(self, message):
        if str(message.webhook_id) != self.webhook_id:
            return False
        contribution = _parse_event(message.content)
        verified = any(getattr(embed, "title", None) == _VERIFIED_TITLE
                       for embed in getattr(message, "embeds", ()))
        live = _parse_live(message.content)
        channel = str(message.channel.id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("SELECT message FROM cursors WHERE webhook=? AND channel=?",
                                (self.webhook_id, channel)).fetchone()
            if cursor and int(cursor[0]) >= message.id and live is None and not verified:
                return False
            inserted = False
            if live:
                fingerprint, player = live
                db.execute("INSERT OR REPLACE INTO live_receipts VALUES (?, ?, ?, ?, ?)",
                           (self.webhook_id, channel, str(message.id), fingerprint, player.name.casefold()))
                before = db.total_changes
                self._save_player_state(db, fingerprint, player)
                inserted = db.total_changes > before
            if contribution:
                event, name, count, certain, chest_types = contribution
                result = db.execute("INSERT OR IGNORE INTO contributions VALUES (?, ?, ?, ?, ?)",
                                    (self.webhook_id, event, name.casefold(), name, count))
                inserted = bool(result.rowcount)
                session = _message_session(message)
                if verified and not inserted:
                    existing = db.execute(
                        "SELECT c.player, s.fingerprint, s.boost FROM contributions c "
                        "LEFT JOIN event_sessions s ON s.webhook=c.webhook AND s.event=c.event "
                        "WHERE c.webhook=? AND c.event=?", (self.webhook_id, event)).fetchone()
                    if existing == (name.casefold(), *(session or (None, None))):
                        inserted = bool(db.execute("INSERT OR IGNORE INTO replay_revisions VALUES (?, ?, 1)",
                                                   (self.webhook_id, event)).rowcount)
                        if inserted:
                            db.execute("UPDATE contributions SET count=? WHERE webhook=? AND event=?",
                                       (count, self.webhook_id, event))
                if inserted:
                    if verified:
                        db.execute("INSERT OR IGNORE INTO replay_revisions VALUES (?, ?, 1)",
                                   (self.webhook_id, event))
                    db.execute("INSERT OR REPLACE INTO event_details VALUES (?, ?, ?, ?)",
                               (self.webhook_id, event, certain, json.dumps(chest_types)))
                    self._save_session(db, event, session)
                    if session:
                        self._save_player_state(db, session[0],
                                                SessionPlayer(name, certain or 0, count, session[1], True,
                                                              revision=int(verified), updated_at=time.time()))
            db.execute("""
                INSERT INTO cursors VALUES (?, ?, ?)
                ON CONFLICT(webhook, channel) DO UPDATE SET message=excluded.message
                WHERE length(excluded.message) > length(cursors.message)
                   OR (length(excluded.message)=length(cursors.message) AND excluded.message > cursors.message)
            """, (self.webhook_id, channel, str(message.id)))
            return inserted

    def _save_session(self, db, event, session):
        if session:
            db.execute("INSERT OR IGNORE INTO event_sessions VALUES (?, ?, ?, ?)",
                       (self.webhook_id, event, *session))

    def queue(self, match, player):
        with self.connection() as db:
            return self._queue(db, match, validate_player(player))

    def _queue(self, db, match, player):
        if not 0 <= match.total <= 1_000_000_000:
            raise ValueError("A Group contribution must contain 0 to 1,000,000,000 chests.")
        event = uuid4().hex
        certain, types = _validate_breakdown(match.total, getattr(match, "certain", None),
                                            getattr(match, "chest_types", {}))
        result = db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?, 0)",
                            (self.webhook_id, match.snapshot, event, player, match.total))
        if result.rowcount:
            db.execute("INSERT INTO event_details VALUES (?, ?, ?, ?)",
                       (self.webhook_id, event, certain, json.dumps(types)))
            session = _validate_session(getattr(match, "session_fingerprint", ""), getattr(match, "boost", None))
            self._save_session(db, event, session)
            if session:
                self._save_player_state(db, session[0],
                                        SessionPlayer(player, certain or 0, match.total,
                                                      session[1], True, updated_at=time.time()))
                db.execute("UPDATE live_outbox SET sent=1 WHERE webhook=? AND fingerprint=? AND player=?",
                           (self.webhook_id, session[0], player.casefold()))
        return bool(result.rowcount)

    def queue_verified(self, match, player):
        player = validate_player(player)
        verified_types = replay_comparison.saved_verified_chest_types(match.replay)
        if verified_types is not None:
            match = replace(match, certain=sum(verified_types.values()), estimated=0,
                            chest_types=dict(verified_types))
        if not 0 <= match.total <= 1_000_000_000:
            raise ValueError("A Group contribution must contain 0 to 1,000,000,000 chests.")
        certain, types = _validate_breakdown(match.total, match.certain, match.chest_types)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT event, name, count FROM outbox WHERE webhook=? AND snapshot=?",
                             (self.webhook_id, match.snapshot)).fetchone()
            if row is None:
                self._queue(db, match, player)
                row = db.execute("SELECT event, name, count FROM outbox WHERE webhook=? AND snapshot=?",
                                 (self.webhook_id, match.snapshot)).fetchone()
            event, original_player, queued_total = row
            if original_player != player:
                raise ValueError("The original contribution belongs to a different player.")
            revision = db.execute("INSERT OR IGNORE INTO replay_revisions VALUES (?, ?, 0)",
                                  (self.webhook_id, event))
            if revision.rowcount:
                db.execute("UPDATE outbox SET count=?, sent=0 WHERE webhook=? AND event=?",
                           (match.total, self.webhook_id, event))
                db.execute("DELETE FROM outbox_failures WHERE webhook=? AND event=?", (self.webhook_id, event))
            if revision.rowcount or queued_total == match.total:
                # Incoming live messages can still update event_details on the host.
                db.execute("INSERT OR IGNORE INTO replay_outbox_details VALUES (?, ?, ?, ?)",
                           (self.webhook_id, event, certain, json.dumps(types)))

    def delivery_states(self):
        if not self.path.is_file():
            return {}
        with self.connection() as db:
            rows = db.execute("""
                SELECT o.snapshot, o.sent, f.event IS NOT NULL, r.event IS NOT NULL
                FROM outbox o LEFT JOIN outbox_failures f ON f.webhook=o.webhook AND f.event=o.event
                LEFT JOIN replay_revisions r ON r.webhook=o.webhook AND r.event=o.event AND r.incoming=0
                WHERE o.webhook=?
            """, (self.webhook_id,))
            return {snapshot: ("Sent" if sent else "Retry needed" if failed else "Pending", bool(verified))
                    for snapshot, sent, failed, verified in rows}

    def flush(self, webhook, *, snapshot=None):
        with _FLUSH_LOCK, self.connection() as db:
            rows = db.execute("""
                SELECT o.event, o.name, o.count, COALESCE(v.certain, d.certain),
                    COALESCE(v.chest_types, d.chest_types), s.fingerprint, s.boost, v.event IS NOT NULL,
                    EXISTS(SELECT 1 FROM replay_revisions r WHERE r.webhook=o.webhook AND r.event=o.event AND r.incoming=0)
                FROM outbox AS o LEFT JOIN event_details AS d ON d.webhook = o.webhook AND d.event = o.event
                LEFT JOIN event_sessions AS s ON s.webhook = o.webhook AND s.event = o.event
                LEFT JOIN replay_outbox_details AS v ON v.webhook = o.webhook AND v.event = o.event
                WHERE o.webhook=? AND o.sent=0 AND (? IS NULL OR o.snapshot=?) ORDER BY o.rowid
            """, (self.webhook_id, snapshot, snapshot)).fetchall()
            for event, name, count, certain, chest_types, fingerprint, boost, has_replay_details, verified in rows:
                session = (fingerprint, boost) if fingerprint else ()
                if verified and not has_replay_details:
                    # The legacy outbox kept the verified total, but incoming live results
                    # could overwrite its type breakdown.
                    certain, types = count, {}
                else:
                    types = json.loads(chest_types or "{}")
                try:
                    _publish(webhook, event, name, count, certain, types, *session,
                             **({"verified": True} if verified else {}))
                except (OSError, ValueError):
                    db.execute("""INSERT OR IGNORE INTO outbox_failures
                        SELECT ?, ? WHERE ? =
                        EXISTS(SELECT 1 FROM replay_revisions WHERE webhook=? AND event=? AND incoming=0)
                    """, (self.webhook_id, event, verified, self.webhook_id, event))
                    db.commit()
                    raise
                db.execute("""UPDATE outbox SET sent=1 WHERE webhook=? AND event=? AND ? =
                    EXISTS(SELECT 1 FROM replay_revisions r WHERE r.webhook=outbox.webhook
                           AND r.event=outbox.event AND r.incoming=0)""", (self.webhook_id, event, verified))
                db.execute("DELETE FROM outbox_failures WHERE webhook=? AND event=?", (self.webhook_id, event))
                db.commit()
            if snapshot is not None:
                return len(rows)
            live_rows = db.execute(
                "SELECT fingerprint, player, name, revision, certain, total, boost, message FROM live_outbox "
                "WHERE webhook=? AND sent=0 ORDER BY rowid", (self.webhook_id,)).fetchall()
            for fingerprint, key, name, revision, certain, total, boost, message in live_rows:
                ident = _publish_live(webhook, fingerprint, name, revision, certain, total, boost, message)
                db.execute("UPDATE live_outbox SET message=? WHERE webhook=? AND fingerprint=? AND player=?",
                           (ident, self.webhook_id, fingerprint, key))
                db.execute("UPDATE live_outbox SET sent=1 WHERE webhook=? AND fingerprint=? AND player=? AND revision=?",
                           (self.webhook_id, fingerprint, key, revision))
                db.commit()
            return len(rows) + len(live_rows)


def _parse_event(content):
    if not content.startswith(_PREFIX) or not content.endswith("||"):
        return None
    try:
        fields = json.loads(content[len(_PREFIX):-2])
        if not isinstance(fields, list) or len(fields) not in (3, 5):
            return None
        event, name, count = fields[:3]
        if (not isinstance(event, str) or not _EVENT_ID.fullmatch(event)
                or type(count) is not int or not 0 <= count <= 1_000_000_000
                or not isinstance(name, str) or validate_player(name) != name):
            return None
        certain, chest_types = _validate_breakdown(count, *(fields[3:] or [None, {}]))
        return event, name, count, certain, chest_types
    except (TypeError, ValueError, RecursionError):
        return None


def _validate_breakdown(count, certain, chest_types):
    if certain is not None and (type(certain) is not int or not 0 <= certain <= count):
        raise ValueError("The certain count must be between zero and the Group contribution total.")
    if (not isinstance(chest_types, dict)
            or any(key not in GROUP_TYPE_LABELS or type(value) is not int or value < 0
                   for key, value in chest_types.items())
            or sum(chest_types.values()) > count):
        raise ValueError("The chest types must be recognized and fit the Group contribution total.")
    return certain, chest_types


def _validate_session(fingerprint, boost):
    if (isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint)
            and type(boost) in (int, float) and boost in (1, 1.5, 2)):
        return fingerprint, boost
    return None


def _message_session(message):
    embeds = getattr(message, "embeds", ())
    if len(embeds) != 1:
        return None
    footer = getattr(getattr(embeds[0], "footer", None), "text", None)
    if not isinstance(footer, str) or not footer.startswith(_SESSION_PREFIX):
        return None
    try:
        fingerprint, boost = json.loads(footer[len(_SESSION_PREFIX):])
        return _validate_session(fingerprint, boost)
    except (TypeError, ValueError, RecursionError):
        return None


_VERIFIED_TITLE = "Replay-verified chest contribution"


def _publish(webhook, event, name, count, certain, chest_types, fingerprint=None, boost=None, *, verified=False):
    check_webhook_cooldown(webhook)
    payload = json.dumps([event, name, count, certain, chest_types], ensure_ascii=True,
                         separators=(",", ":")).replace("|", "\\u007c")
    embed = {"title": _VERIFIED_TITLE if verified else "Group chest contribution", "author": {"name": name},
             "description": f"{count:,} chests", "color": 0x5865F2}
    session = _validate_session(fingerprint, boost)
    if session:
        # Leave the contribution content readable by earlier versions.
        embed["footer"] = {"text": _SESSION_PREFIX + json.dumps(session, separators=(",", ":"))}
    body = json.dumps({
        **webhook_identity(),
        "content": f"{_PREFIX}{payload}||",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    request = Request(f"{webhook.url}?wait=true", data=body,
                      headers={"Content-Type": "application/json", "User-Agent": "FortniteVisualChestCounter"})
    try:
        with build_opener(_NoRedirects()).open(request, timeout=12) as response:
            webhook_response(webhook, response)
    except HTTPError as error:
        if error.code == 429:
            raise webhook_rate_limited(webhook, error) from None
        raise ValueError(f"Discord rejected the Group webhook request (HTTP {error.code}).") from None
    except (OSError, URLError):
        raise ValueError("Discord did not confirm the Group contribution. It remains pending.") from None


def _parse_live(content):
    if not isinstance(content, str) or len(content) > 1900 or not content.startswith(_LIVE_PREFIX) or not content.endswith("||"):
        return None
    try:
        fingerprint, name, revision, certain, total, boost, updated_at = json.loads(content[len(_LIVE_PREFIX):-2])
        player = SessionPlayer(name, certain, total, boost, revision=revision, updated_at=updated_at)
        validate_sessions((TeamSession(fingerprint, (player,)),))
        return fingerprint, player
    except (TypeError, ValueError, RecursionError):
        return None


def _publish_live(webhook, fingerprint, name, revision, certain, total, boost, message=""):
    check_webhook_cooldown(webhook)
    values = [fingerprint, name, revision, certain, total, boost, time.time()]
    content = json.dumps(values, ensure_ascii=True, separators=(",", ":")).replace("|", "\\u007c")
    payload = {"content": f"{_LIVE_PREFIX}{content}||", "allowed_mentions": {"parse": []}}
    if not message:
        payload.update(webhook_identity())
    body = json.dumps(payload).encode("utf-8")
    suffix = f"/messages/{message}" if message else "?wait=true"
    request = Request(webhook.url + suffix, data=body, method="PATCH" if message else "POST",
                      headers={"Content-Type": "application/json", "User-Agent": "FortniteVisualChestCounter"})
    try:
        with build_opener(_NoRedirects()).open(request, timeout=12) as response:
            webhook_response(webhook, response)
            result = json.load(response)
            ident = result["id"]
            if not isinstance(ident, str) or not ident.isdigit() or not 1 <= len(ident) <= 22:
                raise ValueError("Discord did not return a live update message ID.")
            return ident
    except HTTPError as error:
        if error.code == 429:
            raise webhook_rate_limited(webhook, error) from None
        if error.code == 404 and message:
            return _publish_live(webhook, fingerprint, name, revision, certain, total, boost)
        raise ValueError(f"Discord rejected the live Group update (HTTP {error.code}).") from None
    except (OSError, URLError, KeyError, TypeError, ValueError):
        raise ValueError("Discord did not confirm the live Group update. It remains pending.") from None


def queue_live(webhook, fingerprint, player, certain, total, boost):
    return GroupStore(webhook.id).queue_live(fingerprint, player, certain, total, boost)


def queue_match(webhook, match, player):
    return GroupStore(webhook.id).queue(match, player)


def queue_verified(webhook, match, player):
    GroupStore(webhook.id).queue_verified(match, player)


def flush(webhook, *, snapshot=None):
    store = GroupStore(webhook.id)
    return store.flush(webhook, snapshot=snapshot) if store.path.is_file() else 0

def has_pending_verified(webhook):
    store = GroupStore(webhook.id)
    if not store.path.is_file():
        return False
    with store.connection() as db:
        return db.execute("""
            SELECT 1 FROM outbox AS o JOIN replay_revisions AS r
                ON r.webhook=o.webhook AND r.event=o.event AND r.incoming=0
            WHERE o.webhook=? AND o.sent=0 LIMIT 1
        """, (webhook.id,)).fetchone() is not None
