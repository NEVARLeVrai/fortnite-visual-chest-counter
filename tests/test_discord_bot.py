import asyncio
import json
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import discord
import pytest

from fortnite_chest_counter import discord_bot, discord_group, discord_summary, match_history


WEBHOOK = discord_group.Webhook("123456789012345678", "fixture-secret-token")
CONFIG = discord_summary.BotSettings("234567890123456789", "fixture.bot.token")


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_durable_webhook_retry_and_authenticated_reset(tmp_path, monkeypatch):
    store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    match = SimpleNamespace(snapshot="local-1", total=18)
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise URLError("confirmation lost")
            return FakeResponse()

    monkeypatch.setattr(discord_group, "build_opener", lambda *_: Opener())
    assert store.queue(match, "Rin | West")
    assert not store.queue(match, "wrong player")
    with pytest.raises(ValueError, match="remains pending"):
        store.flush(WEBHOOK)
    reopened = discord_group.GroupStore(WEBHOOK.id, store.path)
    assert reopened.flush(WEBHOOK) == 1
    assert reopened.flush(WEBHOOK) == 0
    assert requests[0].data == requests[1].data
    assert requests[0].full_url == f"{WEBHOOK.url}?wait=true"
    body = json.loads(requests[0].data)
    assert body["allowed_mentions"] == {"parse": []}
    channel = SimpleNamespace(id=int(CONFIG.channel))

    def message(id, content, author=WEBHOOK.id):
        return SimpleNamespace(id=id, channel=channel, content=content, webhook_id=int(author))

    assert not reopened.ingest(message(100, body["content"], author=CONFIG.channel))
    assert reopened.ingest(message(101, body["content"]))
    assert not reopened.ingest(message(102, body["content"]))
    assert reopened.cursor(channel.id) == 102
    assert reopened.adjust("RIN | WEST", -3) == ("Rin | West", 15)
    assert reopened.adjust("rin | west", None) == ("Rin | West", 0)
    assert not reopened.ingest(message(103, body["content"]))
    assert reopened.ingest(message(104, '||FCC1 ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","rin | west",6]||'))
    assert reopened.count("Rin | West")[1] == 6
    assert reopened.count(None) == ("Everyone", 6)


def test_report_embed_and_fallback(monkeypatch):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise HTTPError(request.full_url, 403, "Missing permissions", None, None)
            return FakeResponse()

    monkeypatch.setattr(discord_summary, "build_opener", lambda *_: Opener())
    match = match_history.Match("snapshot", "2026-11-10 10:00", "default", "Capture", "Solo", 4, 3, "1%")
    discord_summary.send(CONFIG, match)
    embed, = requests[0]["embeds"]
    assert [(field["name"], field["value"]) for field in embed["fields"]] == [
        ("Total chests", "7"), ("Certain", "4"), ("Estimated", "3"), ("Estimated Pirate Token chance", "1%")
    ]
    assert requests[1] == {"content": match.summary(), "allowed_mentions": {"parse": []}}


def test_group_breakdowns_and_zero_matches_survive_retry_and_corrections(tmp_path):
    store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    channel = SimpleNamespace(id=int(CONFIG.channel))

    def event(message_id, count, *details):
        payload = json.dumps([f"{message_id:032x}", "Rin", count, *details])
        return SimpleNamespace(webhook_id=int(WEBHOOK.id), channel=channel, id=message_id,
                               content=f"||FCC1 {payload}||")

    assert store.ingest(event(1, 8, 5, {"chest": 3, "rare": 2, "unclassified": 3}))
    assert store.ingest(event(2, 7))
    assert store.ingest(event(3, 0, 0, {}))
    assert not store.ingest(event(4, 2, 0, {"sprite": 3}))
    assert store.adjust("Rin", -2) == ("Rin", 13)
    row, = store.leaderboard()
    assert (row.recorded, row.adjustment, row.certain, row.confirmed_total, row.matches) == (15, -2, 5, 8, 3)
    assert row.chest_types == {"chest": 3, "rare": 2, "unclassified": 3}
    assert store.adjust("Rin", None) == ("Rin", 0)
    assert store.ingest(event(5, 0, 0, {}))
    everyone = store.stats()
    assert (everyone.total, everyone.matches, everyone.certain, everyone.chest_types) == (0, 1, 0, {})


def test_guild_registration_and_runtime_permission_check(tmp_path, monkeypatch):
    async def verify():
        guild = SimpleNamespace(id=123)
        channel = SimpleNamespace(id=int(CONFIG.channel), guild=guild)
        feed = SimpleNamespace(id=345, guild=guild)
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        bot = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        bot._connection.application_id = 456
        monkeypatch.setattr(discord_bot.discord, "TextChannel", SimpleNamespace)
        upserts = []

        async def fetch_channel(id):
            return {channel.id: channel, feed.id: feed}[id]

        async def fetch_webhook(*, prefer_auth):
            assert not prefer_auth
            return SimpleNamespace(channel_id=feed.id, guild_id=guild.id)

        async def upsert(app, guild_id, payload):
            upserts.append((app, guild_id, payload))

        monkeypatch.setattr(bot, "fetch_channel", fetch_channel)
        monkeypatch.setattr(discord_bot.discord.Webhook, "partial",
                            lambda *_args, **_kwargs: SimpleNamespace(fetch=fetch_webhook))
        monkeypatch.setattr(bot.http, "upsert_guild_command", upsert)
        await bot.setup_hook()
        assert {(app, guild_id) for app, guild_id, _ in upserts} == {(456, 123)}
        assert {payload["name"] for _, _, payload in upserts} == {"chests", "leaderboard", "match", "add", "reset"}
        assert not bot.tree.get_commands()
        assert bot.feed_channel is feed
        assert bot.intents.message_content and not bot.intents.members

        async def history(*, limit, after, before, oldest_first):
            assert (limit, after.id, before, oldest_first) == (None, 0, None, True)
            content = '||FCC1 ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","Rin",8,4,{"rare":2}]||'
            yield SimpleNamespace(id=100, channel=feed, webhook_id=int(WEBHOOK.id), content=content)
            yield SimpleNamespace(id=101, channel=feed, webhook_id=777, content=content)

        feed.history = history
        await bot.on_ready()
        assert store.stats().total == 8
        assert store.cursor(feed.id) == 101
        assert store.cursor(channel.id) == 0

        calls = []

        async def send_message(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(store, "adjust", lambda *_: pytest.fail("Unauthorized write"))
        response = SimpleNamespace(send_message=send_message, is_done=lambda: False)
        add = bot.tree.get_command("add", guild=discord.Object(guild.id))
        reset = bot.tree.get_command("reset", guild=discord.Object(guild.id))
        await add.callback(SimpleNamespace(guild_id=123, permissions=discord.Permissions.none(), response=response),
                           "Rin", 5)
        await reset.callback(SimpleNamespace(guild_id=789, permissions=discord.Permissions.all(), response=response),
                             "Rin")
        assert "Manage Server" in calls[0][0][0]
        assert "configured server" in calls[1][0][0]
        assert all(options["ephemeral"] for _, options in calls)

    asyncio.run(verify())


def test_gateway_runner_closes_on_stop(monkeypatch):
    online = Event()
    closed = Event()

    class FakeBot:
        failure = None
        feed_channel = None

        def __init__(self, config, store, status, webhook):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.set()

        async def start(self, token):
            assert token == CONFIG.token
            self.status("online", "Connected")
            online.set()
            await asyncio.Future()

    monkeypatch.setattr(discord_bot, "GuildBot", FakeBot)
    runner = discord_bot.BotRunner(CONFIG, None, lambda: None)
    runner.start()
    assert online.wait(2)
    assert runner.status == ("online", "Connected")
    runner.stop(timeout=2)
    assert closed.is_set()
    assert runner.status[0] == "stopped"


def test_qt_autostart_tray_hide_and_real_close(tmp_path, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication
    from fortnite_chest_counter.gui import app as gui

    qt = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui.paths, "APP_DIR", tmp_path)
    monkeypatch.setattr(gui.MainWindow, "refresh_sources", lambda *_: None)
    monkeypatch.setattr(gui.MainWindow, "show_update_result", lambda *_: None)
    monkeypatch.setattr(gui.update_install, "clear_orphaned_journal", lambda: None)
    monkeypatch.setattr(gui.discord_summary, "load", lambda: CONFIG)
    monkeypatch.setattr(gui.discord_group, "load_webhook", lambda: WEBHOOK)
    monkeypatch.setattr(gui.discord_group, "flush", lambda *_: 0)
    gui.C.update_config_file(gui.config_file(), "app", {"auto_start_discord_sender": True, "close_to_tray": True})
    gui.C.update_config_file(gui.config_file(), "update", {"check_automatically": False})
    calls = []

    class Runner:
        def __init__(self, config, webhook, changed):
            self.config = config
            self.webhook = webhook
            self.report_config = config
            self.status = ("connecting", "Connecting")
            self.changed = changed

        def start(self):
            calls.append("start")
            self.status = ("online", "Connected")
            self.changed()

        def stop(self, *, timeout=None):
            calls.append("stop")
            self.status = ("stopped", "")

    monkeypatch.setattr(gui.discord_bot, "BotRunner", Runner)
    win = gui.MainWindow()
    try:
        assert calls == []
        win.after_start(installing=True)
        assert calls == []
        win.after_start()
        qt.processEvents()
        assert calls == ["start"]
        assert "Bot online" in win.discord.sender_status.text()
        win._tray = SimpleNamespace(isVisible=lambda: True, hide=lambda: None)
        hide = QCloseEvent()
        win.closeEvent(hide)
        assert not hide.isAccepted() and win._hidden_in_tray
        assert calls == ["start"]
        win._tray_exit = True
        close = QCloseEvent()
        win.closeEvent(close)
        assert close.isAccepted() and calls == ["start", "stop"]
    finally:
        win.deleteLater()
        qt.processEvents()



def test_team_summary_counts_first_submission_per_session_and_player(tmp_path, monkeypatch):
    store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    channel = SimpleNamespace(id=int(CONFIG.channel))
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse()

    monkeypatch.setattr(discord_group, "build_opener", lambda *_: Opener())

    def submit(snapshot, session, player, total, boost=1.0):
        assert store.queue(SimpleNamespace(snapshot=snapshot, session_fingerprint=session, total=total, boost=boost), player)
        assert store.flush(WEBHOOK) == 1
        body = requests[-1]
        message = SimpleNamespace(id=len(requests), channel=channel, webhook_id=int(WEBHOOK.id), content=body["content"],
                                  embeds=[discord.Embed.from_dict(body["embeds"][0])])
        assert store.ingest(message)
        return message

    first, second = "a" * 64, "b" * 64
    submit("one", first, "Rin", 10)
    solo_chance = 1 - (1 - 1 / 137_500) ** 10
    assert store.team_summary() == discord_group.TeamSummary(1, 1, 10, 13750, f"{solo_chance:.6%}")
    submit("two", first, "Sam", 4, 2.0)
    chance = 1 - (1 - 1 / 137_500) ** 10 * (1 - 2 / 137_500) ** 4
    shared = discord_group.TeamSummary(1, 2, 14, round(1 / chance), f"{chance:.6%}")
    assert store.team_summary() == shared
    submit("three", first, "rIN", 100, 1.5)
    assert store.team_summary() == shared
    assert (store.stats("Rin").matches, store.stats("Rin").total) == (1, 10)
    submit("four", "", "Rin", 200)
    submit("five", "1" * 32, "Sam", 40)
    assert store.team_summary() == shared
    embed = discord_bot.public_leaderboard(store, None)
    assert "**Synchronized totals** · 14 chests · 1 match" in embed["description"]
    assert f"{chance:.6%}" in embed["description"]
    assert (store.stats("Rin").matches, store.stats("Rin").total) == (2, 210)
    assert store.adjust("Sam", None) == ("Sam", 0)
    assert store.team_summary() == shared

    message = submit("six", second, "Sam", 0)
    assert store.team_summary() == discord_group.TeamSummary(2, 1, 0, chance_percent="0.000000%")
    message.id += 1
    assert not store.ingest(message)
    assert store.team_summary() == discord_group.TeamSummary(2, 1, 0, chance_percent="0.000000%")


def test_qt_guest_startup_and_role_switch(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from fortnite_chest_counter.gui import app as gui

    qt = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui.paths, "APP_DIR", tmp_path)
    monkeypatch.setattr(gui.MainWindow, "refresh_sources", lambda *_: None)
    monkeypatch.setattr(gui.MainWindow, "show_update_result", lambda *_: None)
    monkeypatch.setattr(gui.update_install, "clear_orphaned_journal", lambda: None)
    monkeypatch.setattr(gui.discord_summary, "load", lambda: None)
    monkeypatch.setattr(gui.discord_group, "load_webhook", lambda: WEBHOOK)
    monkeypatch.setattr(gui.discord_group, "flush", lambda *_: 0)
    gui.C.update_config_file(gui.config_file(), "app", {"group_role": "guest"})
    gui.C.update_config_file(gui.config_file(), "update", {"check_automatically": False})
    calls = []

    class Runner:
        def __init__(self, config, webhook, changed):
            self.webhook = webhook
            self.status = ("stopped", "")

        def start(self):
            calls.append("start")
            self.status = ("online", "Connected")

        def stop(self, *, timeout=None):
            if self.status[0] == "online":
                calls.append("stop")
            self.status = ("stopped", "")

    monkeypatch.setattr(gui.discord_bot, "BotRunner", Runner)
    win = gui.MainWindow()
    win.timer.stop()
    win.slow.stop()
    try:
        assert win.group.role.currentData() == "guest"
        assert win.group.publish_board.isHidden()
        assert win.group.bot_section.isHidden()
        assert not win.group.share_matches.isChecked()
        monkeypatch.setattr(gui.discord_summary, "load", lambda: CONFIG)
        win.after_start()
        win.discord.start_bot()
        assert calls == []
        win.group.role.setCurrentIndex(win.group.role.findData("host"))
        assert calls == ["start"]
        assert not win.group.publish_board.isHidden()
        assert not win.group.bot_section.isHidden()
        assert win.discord.isHidden()
        assert gui.load_cfg()[0]["app"]["group_role"] == "host"
        win.group.role.setCurrentIndex(win.group.role.findData("guest"))
        assert calls == ["start", "stop"]
        assert win.group.publish_board.isHidden()
        assert win.group.bot_section.isHidden()
        assert gui.load_cfg()[0]["app"]["group_role"] == "guest"
        win.discord.start_bot()
        assert calls == ["start", "stop"]
    finally:
        win.close()
        qt.processEvents()

    reopened = gui.MainWindow()
    try:
        assert reopened.group.role.currentData() == "guest"
        assert reopened.group.publish_board.isHidden()
        assert reopened.group.bot_section.isHidden()
        reopened.after_start()
        assert calls == ["start", "stop"]
    finally:
        reopened.close()
        qt.processEvents()

def test_leaderboard_command_reuses_rich_embed_and_checks_publish_permission(tmp_path, monkeypatch):
    from fortnite_chest_counter import discord_scoreboard

    async def verify():
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        players = [discord_group.Player("Rin", 8, certain=5, confirmed_total=8, matches=2,
                                        chest_types={"rare": 3})]
        team = discord_group.TeamSummary(1, 1, 8, 17188, "0.005818%")
        monkeypatch.setattr(store, "board", lambda: (players, team))
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        calls = []
        published = []

        async def send(*args, **kwargs):
            calls.append((args, kwargs))

        async def defer(**kwargs):
            calls.append(("defer", kwargs))

        async def edit_original(**kwargs):
            calls.append(("edit_original", kwargs))

        async def publish(channel_id):
            published.append(channel_id)
            return "https://discord.com/channels/123/456/789"

        guild = discord.Object(123)
        discord_bot.register_commands(tree, guild, store, lambda: True, publish)
        command = tree.get_command("leaderboard", guild=guild)
        interaction = SimpleNamespace(
            guild_id=123, channel_id=456, permissions=discord.Permissions.none(),
            app_permissions=discord.Permissions.all(),
            response=SimpleNamespace(defer=defer, send_message=send, is_done=lambda: False),
            followup=SimpleNamespace(send=send), edit_original_response=edit_original,
        )
        await command.callback(interaction)
        expected = discord_scoreboard.make_public_embed(players, team)
        actual = calls[-1][1]["embed"].to_dict()
        assert actual["title"] == expected["title"]
        assert actual["fields"] == expected["fields"]
        assert "Pirate Token chance · **0.005818%**" in actual["fields"][0]["value"]
        assert "Rare chest" not in actual["fields"][0]["value"]
        assert actual["fields"][-1]["name"] == "Latest synchronized match"
        assert "Results received from 1 player" in actual["fields"][-1]["value"]
        monkeypatch.setattr(store, "stats", lambda player: players[0])
        await tree.get_command("chests", guild=guild).callback(interaction, player="Rin")
        assert "Pirate Token chance · **0.005818%**" in calls[-1][1]["embed"].description
        assert actual["description"] == expected["description"]
        assert calls[0] == ("defer", {"thinking": True, "ephemeral": False})

        fingerprint = "a" * 64
        finished = discord_group.SessionPlayer("Rin", 5, 8, 1, finished=True, updated_at=100)
        live = discord_group.SessionPlayer("Echo", 2, 10, 1, updated_at=200)
        previous = discord_group.SessionPlayer("Other", 2, 3, 2, finished=True, updated_at=90)
        monkeypatch.setattr(store, "sessions", lambda: (
            discord_group.TeamSession(fingerprint, (finished, live)),
            discord_group.TeamSession("b" * 64, (live,)),
            discord_group.TeamSession("c" * 64, (previous,)),
        ))
        interaction.response.is_done = lambda: True
        match = tree.get_command("match", guild=guild)
        calls.clear()
        await match.callback(interaction)
        assert calls[0] == ("defer", {"thinking": True})
        assert calls[1][0] == "edit_original"
        history = calls[-1][1]["embed"].description
        assert "2** synchronized matches" in history
        assert "All shared matches" in history
        assert "**11** chests · 7 confirmed · 4 estimated" in history
        assert "0.010181%" in history
        assert fingerprint[:12] in history and "bbbb" not in history
        await match.callback(interaction, id=fingerprint[:12])
        summary = calls[-1][1]["embed"].description
        assert "**8** chests · 5 confirmed · 3 estimated" in summary
        assert "0.005818%" in summary and "Echo" not in summary
        await match.callback(interaction, page=2)
        assert calls[-1] == ("edit_original", {"content": "Choose a page from 1 to 1."})
        interaction.app_permissions = discord.Permissions.none()
        await match.callback(interaction)
        assert calls[-1][1]["content"].startswith("**Shared match history**")
        interaction.app_permissions = discord.Permissions.all()

        def unavailable():
            raise TimeoutError

        monkeypatch.setattr(store, "sessions", unavailable)
        await match.callback(interaction)
        assert calls[-1] == ("edit_original", {"content": "Could not read shared match history. Try again in a moment."})
        interaction.response.is_done = lambda: False

        calls.clear()
        await command.callback(interaction, publish=True)
        assert published == []
        assert "Manage Server" in calls[0][0][0]
        assert calls[0][1]["ephemeral"]

        calls.clear()
        interaction.permissions = discord.Permissions.all()
        await command.callback(interaction, publish=True)
        assert published == [456]
        assert calls[0] == ("defer", {"thinking": True, "ephemeral": True})
        assert calls[-1][1]["ephemeral"]
        await client.close()

    asyncio.run(verify())


def test_published_board_moves_updates_resumes_and_deletes(tmp_path, monkeypatch):
    async def verify():
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        players = [discord_group.Player("Rin", 8, certain=5, confirmed_total=8, matches=2)]
        monkeypatch.setattr(store, "board", lambda: (players, discord_group.TeamSummary()))
        guild = SimpleNamespace(id=123, me=object())
        messages = {}
        edits = []
        sent = []
        deleted = []

        class Message:
            def __init__(self, channel, ident):
                self.channel = channel
                self.id = ident

            async def edit(self, **kwargs):
                if self.id not in messages:
                    raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing")
                edits.append(self.id)
                messages[self.id] = kwargs["embed"].to_dict()
                return self

            async def delete(self):
                if self.channel.deny_delete:
                    raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
                if self.id not in messages:
                    raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing")
                deleted.append(self.id)
                del messages[self.id]

        class Channel:
            def __init__(self, ident, name):
                self.id, self.name, self.guild = ident, name, guild
                self.deny_delete = False
                self.permissions = discord.Permissions.all()

            def permissions_for(self, member):
                assert member is guild.me
                return self.permissions

            def get_partial_message(self, ident):
                return Message(self, ident)

            async def send(self, **kwargs):
                ident = 1000 + len(sent)
                sent.append((self.id, ident))
                assert kwargs["allowed_mentions"].to_dict()["parse"] == []
                messages[ident] = kwargs["embed"].to_dict()
                return Message(self, ident)

        first, second = Channel(234, "first"), Channel(345, "second")
        guild.text_channels = [first, second]
        channels = {channel.id: channel for channel in guild.text_channels}
        monkeypatch.setattr(discord_bot.discord, "TextChannel", Channel)
        bot = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        bot._connection.application_id = 456
        bot.feed_channel = first
        monkeypatch.setattr(bot, "get_channel", channels.get)
        monkeypatch.setattr(bot, "get_guild", lambda ident: guild if ident == guild.id else None)
        assert bot.available_channels() == (("234", "#first"), ("345", "#second"))

        assert await bot.publish_board() == ""
        assert sent == []
        assert await bot.publish_board(first.id, settings=("Duo", 1.5)) == "https://discord.com/channels/123/234/1000"
        assert store.published_board(456, 123) == ("234", "1000")
        assert bot.published_channel == "234"
        await bot.publish_board(first.id)
        assert len(sent) == 1 and edits == [1000]
        await bot.publish_board()
        assert edits == [1000]

        players[0] = discord_group.Player("Rin", 12, certain=9, confirmed_total=12, matches=3)
        await bot.refresh_board.coro(bot)
        assert edits == [1000]
        bot._feed_ready = True
        await bot.refresh_board.coro(bot)
        assert edits == [1000, 1000]
        assert messages[1000]["fields"][0]["value"].startswith("**12** chests")
        await bot.publish_board(second.id)
        assert sent[-1] == (345, 1001)
        assert deleted == [1000]
        assert store.published_board(456, 123) == ("345", "1001")
        assert bot.published_channel == "345"

        restarted = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        restarted._connection.application_id = 456
        restarted.feed_channel = first
        monkeypatch.setattr(restarted, "get_channel", channels.get)
        monkeypatch.setattr(restarted, "get_guild", lambda *_: guild)
        await restarted.publish_board()
        assert sent == [(234, 1000), (345, 1001)]
        assert edits[-1] == 1001
        assert restarted.published_channel == "345"

        second.deny_delete = True
        with pytest.raises(discord.Forbidden):
            await restarted.delete_board()
        assert store.published_board(456, 123) == ("345", "1001")
        second.deny_delete = False
        await restarted.delete_board()
        assert store.published_board(456, 123) is None
        assert restarted.published_channel == ""
        assert deleted == [1000, 1001]
        await bot.close()
        await restarted.close()

    asyncio.run(verify())


def test_edited_live_message_bypasses_history_cursor_and_authenticates_webhook(tmp_path):
    async def verify():
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        bot = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        channel_id = 345
        fingerprint = "a" * 64
        calls = []
        author = WEBHOOK.id
        import time
        stamp = time.time()

        async def fetch_message(ident):
            calls.append(ident)
            return SimpleNamespace(id=ident, channel=SimpleNamespace(id=channel_id),
                                   webhook_id=int(author), content=content)

        bot.feed_channel = SimpleNamespace(id=channel_id, fetch_message=fetch_message)
        store.advance_cursor(channel_id, 9999)
        content = '||FCCL1 ' + json.dumps([fingerprint, "Rin", 1, 2, 3, 1.0, stamp]) + '||'
        payload = SimpleNamespace(channel_id=channel_id, message_id=100, data={"content": content})
        await bot.on_raw_message_edit(payload)
        session, = store.sessions()
        assert session.total == 3 and session.certain == 2
        assert store.cursor(channel_id) == 9999
        content = '||FCCL1 ' + json.dumps([fingerprint, "Rin", 2, 3, 5, 1.0, stamp + 1]) + '||'
        payload.data = {"content": content}
        await bot.on_raw_message_edit(payload)
        assert store.sessions()[0].total == 5
        assert store.cursor(channel_id) == 9999
        content = '||FCCL1 ' + json.dumps([fingerprint, "Rin", 3, 4, 7, 1.0, stamp + 2]) + '||'
        await bot._refresh_live_messages()
        assert store.sessions()[0].total == 7
        assert store.cursor(channel_id) == 9999
        author = "987654321012345678"
        content = '||FCCL1 ' + json.dumps([fingerprint, "Rin", 4, 5, 99, 1.0, stamp + 3]) + '||'
        payload.data = {"content": content}
        await bot.on_raw_message_edit(payload)
        assert store.sessions()[0].total == 7
        payload.data = {"content": "unrelated webhook message"}
        await bot.on_raw_message_edit(payload)
        assert calls == [100, 100, 100, 100]
        payload.channel_id = 999
        await bot.on_raw_message_edit(payload)
        assert calls == [100, 100, 100, 100]
        await bot.close()

    asyncio.run(verify())


def test_bot_presence_uses_fresh_shared_session_and_throttles_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot.paths, "APP_DIR", tmp_path)

    async def verify():
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        bot = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        bot._connection.user = SimpleNamespace(id=456)
        clock = [1000.0, 100.0]
        monkeypatch.setattr(discord_bot.time, "time", lambda: clock[0])
        monkeypatch.setattr(discord_bot.time, "monotonic", lambda: clock[1])
        current = []
        saved = []
        cfg = dict(discord_bot.settings.DEFAULTS["app"])
        monkeypatch.setattr(discord_bot.settings, "load_config", lambda *_: ({"app": cfg}, "", []))
        monkeypatch.setattr(store, "sessions", lambda: tuple(current))
        monkeypatch.setattr(store, "board", lambda: (saved, discord_group.TeamSummary(1) if saved else discord_group.TeamSummary()))
        calls = []

        async def change_presence(**kwargs):
            calls.append((kwargs["status"], kwargs["activity"].type, kwargs["activity"].name))

        monkeypatch.setattr(bot, "change_presence", change_presence)
        await bot.update_presence()
        assert calls == [(discord.Status.online, discord.ActivityType.watching, "In lobby")]
        current.append(discord_group.TeamSession("a" * 64, (
            discord_group.SessionPlayer("Rin", 4, 8, 1.0, updated_at=1000.0),
            discord_group.SessionPlayer("Sam", 3, 6, 1.0, updated_at=1000.0),
        )))
        clock[1] = 120.0
        await bot.update_presence()
        assert len(calls) == 1
        saved.append(discord_group.Player("Rin", 14, matches=1))
        clock[1] = 130.0
        await bot.update_presence()
        assert calls[-1][2] == "2 players · 14 chests · Token 0.010181%"

        clock[1] = 160.0
        await bot.update_presence()
        assert len(calls) == 2
        assert calls[-1][2] == "2 players · 14 chests · Token 0.010181%"
        current[0] = discord_group.TeamSession("a" * 64, (
            discord_group.SessionPlayer("Rin", 4, 8, 1.0, True, updated_at=1000.0),
            discord_group.SessionPlayer("Sam", 3, 6, 1.0, True, updated_at=1000.0),
        ))
        clock[1] = 220.0
        await bot.update_presence()
        assert calls[-1][2] == "In lobby · Group history · 14 chests · 1 matches · Token 0.010181%"

        clock[0], clock[1] = 1201.0, 250.0
        current[0] = discord_group.TeamSession("a" * 64, (
            discord_group.SessionPlayer("Rin", 4, 8, 1.0, updated_at=1000.0),
            discord_group.SessionPlayer("Sam", 3, 6, 1.0, updated_at=1000.0),
        ))
        await bot.update_presence()
        assert len(calls) == 3

        async def fail_publication():
            raise OSError("board unavailable")

        monkeypatch.setattr(bot, "publish_board", fail_publication)
        bot._feed_ready = True
        current.append(discord_group.TeamSession("b" * 64, (
            discord_group.SessionPlayer("Rin", 1, 3, 1.0, updated_at=1201.0),
        )))
        await bot.refresh_board.coro(bot)
        assert calls[-1][2] == "In match · 3 chests · Token 0.002182%"
        assert "Could not update" in bot.publication_status

        clock[1] = 280.0
        await bot.update_presence()
        assert len(calls) == 4
        await bot.close()

    asyncio.run(verify())


def test_apply_bot_branding_is_explicit_and_uses_packaged_icon(tmp_path):
    async def verify():
        bot = discord_bot.GuildBot(CONFIG, discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db"),
                                   lambda *_: None, WEBHOOK)
        calls = []

        async def edit(**kwargs):
            calls.append(kwargs)

        user = SimpleNamespace(id=456, name="Previous bot name", edit=edit)
        bot._connection.user = user
        assert calls == []
        assert await bot.apply_branding() == "Bot name, avatar and banner updated."
        assert calls[0]["username"] == "Chest Counter"
        assert calls[0]["avatar"].startswith(b"\x89PNG\r\n\x1a\n")
        user.name = "Chest Counter"
        await bot.apply_branding()
        assert set(calls[1]) == {"avatar", "banner"}
        await bot.close()

    asyncio.run(verify())


def test_custom_bot_branding_validates_before_discord_request(tmp_path):
    async def verify():
        bot = discord_bot.GuildBot(CONFIG, discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db"),
                                   lambda *_: None, WEBHOOK)
        calls = []

        async def edit(**kwargs):
            calls.append(kwargs)

        bot._connection.user = SimpleNamespace(id=456, name="Old bot", edit=edit)
        avatar = tmp_path / "custom.png"
        avatar.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        await bot.apply_branding("My Squad", str(avatar))
        assert calls[0]["username"] == "My Squad"
        assert calls[0]["avatar"] == avatar.read_bytes()
        assert calls[0]["banner"].startswith(b"\x89PNG\r\n\x1a\n")
        for name in ("A", "x" * 33, "Bad\nName", "bad@name"):
            with pytest.raises(ValueError, match="bot name"):
                await bot.apply_branding(name, str(avatar))
        with pytest.raises(ValueError, match="existing image"):
            await bot.apply_branding("My Squad", str(tmp_path / "missing.png"))
        wrong = tmp_path / "wrong.png"
        wrong.write_text("not an image")
        with pytest.raises(ValueError, match="PNG or JPEG image"):
            await bot.apply_branding("My Squad", str(wrong))
        huge = tmp_path / "huge.jpg"
        huge.write_bytes(b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024))
        with pytest.raises(ValueError, match="smaller than 2 MB"):
            await bot.apply_branding("My Squad", str(huge))
        assert len(calls) == 1
        await bot.close()

    asyncio.run(verify())


def test_custom_presence_templates_activity_and_disable(tmp_path, monkeypatch):
    async def verify():
        store = discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
        bot = discord_bot.GuildBot(CONFIG, store, lambda *_: None, WEBHOOK)
        bot._connection.user = SimpleNamespace(id=456)
        clock = [100.0]
        config = {"discord_presence_enabled": True,
                  "discord_presence_interval": 10, "discord_presence_activity": "playing",
                  "discord_presence_team_text": "{players} players / {total} opened / {chance}"}
        monkeypatch.setattr(discord_bot.settings, "load_config", lambda *_: ({"app": config}, "", []))
        monkeypatch.setattr(discord_bot.time, "time", lambda: 1000.0)
        monkeypatch.setattr(discord_bot.time, "monotonic", lambda: clock[0])
        session = discord_group.TeamSession("a" * 64, (
            discord_group.SessionPlayer("Rin", 2, 3, 1.0, updated_at=1000.0),
            discord_group.SessionPlayer("Sam", 2, 4, 1.0, updated_at=1000.0),
        ))
        monkeypatch.setattr(store, "sessions", lambda: (session,))
        monkeypatch.setattr(store, "board", lambda: ([], discord_group.TeamSummary()))
        calls = []

        async def change_presence(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(bot, "change_presence", change_presence)
        await bot.update_presence()
        assert calls[0]["activity"].type == discord.ActivityType.playing
        assert calls[0]["activity"].name == "2 players / 7 opened / 0.005091%"
        clock[0] = 129.0
        config["discord_presence_activity"] = "listening"
        await bot.update_presence()
        assert len(calls) == 1
        clock[0] = 130.0
        await bot.update_presence()
        assert calls[1]["activity"].type == discord.ActivityType.listening
        assert calls[1]["activity"].name == calls[0]["activity"].name
        clock[0] = 160.0
        config["discord_presence_team_text"] = "x" * 200 + "{unknown}"
        await bot.update_presence()
        assert calls[-1]["activity"].name == "x" * 128
        config["discord_presence_enabled"] = False
        await bot.update_presence()
        assert calls[-1]["activity"] is None
        await bot.update_presence()
        assert len(calls) == 4
        await bot.close()

    asyncio.run(verify())


def test_custom_bot_banner_validates_before_discord_request(tmp_path):
    async def verify():
        bot = discord_bot.GuildBot(CONFIG, discord_group.GroupStore(WEBHOOK.id, tmp_path / "group.db"),
                                   lambda *_: None, WEBHOOK)
        calls = []

        async def edit(**kwargs):
            calls.append(kwargs)

        bot._connection.user = SimpleNamespace(id=456, name="Chest Counter", edit=edit)
        banner = tmp_path / "banner.jpg"
        banner.write_bytes(b"\xff\xd8\xfffixture")
        await bot.apply_branding(banner_path=str(banner))
        assert calls[0]["banner"] == banner.read_bytes()
        assert calls[0]["avatar"].startswith(b"\x89PNG\r\n\x1a\n")
        assert "username" not in calls[0]
        banner.write_text("invalid image")
        with pytest.raises(ValueError, match="banner must contain"):
            await bot.apply_branding(banner_path=str(banner))
        assert len(calls) == 1
        await bot.close()

    asyncio.run(verify())

@pytest.mark.parametrize("verified_first", [False, True])
def test_replay_updates_one_group_contribution(tmp_path, monkeypatch, verified_first):
    from dataclasses import replace

    host = discord_group.GroupStore(WEBHOOK.id, tmp_path / "host.db")
    guest = discord_group.GroupStore(WEBHOOK.id, tmp_path / "guest.db")
    match = match_history.Match("match", "2026-11-10 10:00", "default", "Capture", "Duo",
                                4, 3, "1%", session_fingerprint="a" * 64)
    messages = []

    def publish(webhook, event, name, count, certain, types, fingerprint, boost, *, verified=False):
        footer = SimpleNamespace(text=discord_group._SESSION_PREFIX + json.dumps([fingerprint, boost]))
        embed = SimpleNamespace(title=discord_group._VERIFIED_TITLE if verified else "", footer=footer)
        payload = json.dumps([event, name, count, certain, types])
        messages.append(SimpleNamespace(id=len(messages)+1, channel=SimpleNamespace(id=int(CONFIG.channel)),
                                        webhook_id=int(webhook.id), content=f"||FCC1 {payload}||", embeds=[embed]))

    monkeypatch.setattr(discord_group, "_publish", publish)
    assert guest.queue(match, "Rin")
    assert guest.flush(WEBHOOK) == 1
    guest.queue_verified(replace(match, certain=9, estimated=0, chest_types={"buried": 2}), "Rin")
    assert guest.flush(WEBHOOK) == 1
    viewer = discord_group.GroupStore(WEBHOOK.id, tmp_path / "viewer.db")
    if verified_first:
        messages.reverse()
    assert host.ingest(messages[0])
    first_snapshot = host.sessions()
    viewer.merge_sessions(first_snapshot)
    assert host.ingest(messages[1]) is not verified_first
    assert not host.ingest(messages[0])
    row, = host.leaderboard()
    assert (row.matches, row.recorded, row.certain, row.chest_types) == (1, 9, 9, {"buried": 2})
    viewer.merge_sessions(host.sessions())
    viewer.merge_sessions(first_snapshot)
    session, = viewer.sessions()
    assert session.total == 9 and session.certain == 9
    assert session.chance_percent == row.token_chance == host.team_summary().chance_percent
