"""Discord Gateway lifecycle and slash commands for local Group totals."""

import asyncio
import math
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import time

import discord
from discord import app_commands
from discord.ext import tasks

from fortnite_chest_counter import discord_scoreboard, match_stats, paths, settings

from fortnite_chest_counter.discord_group import GroupStore, TeamSession


def completed_matches(totals):
    return [TeamSession(session.fingerprint, players) for session in totals.sessions()
            if (players := tuple(player for player in session.players if player.finished))]


def synchronized_totals(matches):
    exposure = math.fsum(match_stats.chest_exposure(player.total, player.boost)
                         for item in matches for player in item.players)
    return match_stats.Summary(sum(item.certain for item in matches), sum(item.total for item in matches),
                               exposure, matches=len(matches))


def public_leaderboard(totals, settings):
    players, team = totals.board()
    history = synchronized_totals(completed_matches(totals))
    return discord_scoreboard.make_public_embed(players, team, settings=settings, history=history)


def register_commands(tree, guild, totals, ready, publish_board=None, board_settings=None):
    async def reply(interaction, title, detail):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        if interaction.app_permissions.embed_links:
            await send(embed=discord.Embed(title=title, description=detail, color=0x5865F2))
        else:
            await send(f"**{title}**\n{detail}"[:2000])

    async def error(interaction, message):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(message, ephemeral=True)

    async def check(interaction, *, manage_guild=False):
        if interaction.guild_id != guild.id:
            await error(interaction, "These commands are only available in the configured server.")
            return False
        if manage_guild and not interaction.permissions.manage_guild:
            await error(interaction, "You need Manage Server to publish the leaderboard or change chest totals.")
            return False
        if not ready():
            await error(interaction, "The bot is catching up with the Group feed. Try again in a moment.")
            return False
        return True

    async def autocomplete(interaction, current):
        if interaction.guild_id != guild.id or not ready():
            return []
        try:
            rows = await asyncio.to_thread(totals.leaderboard)
            return [
                app_commands.Choice(name=row.name, value=row.name)
                for row in rows
                if current.casefold() in row.name.casefold() and len(row.name) <= 100
            ][:25]
        except (OSError, ValueError):
            return []

    @tree.command(guild=guild, description="Show the Group chest totals")
    @app_commands.describe(player="Leave empty to count everyone")
    @app_commands.autocomplete(player=autocomplete)
    async def chests(interaction: discord.Interaction, player: str | None = None):
        if not await check(interaction):
            return
        try:
            row = await asyncio.to_thread(totals.stats, player)
        except ValueError as exc:
            await error(interaction, str(exc))
            return
        details = [f"**{discord.utils.escape_markdown(row.name)}** · {row.total:,} chests · {row.matches:,} submissions"]
        if player is None:
            team = await asyncio.to_thread(totals.team_summary)
            details.append(f"Team matches: {team.matches:,}")
        details.append(f"Pirate Token chance · **{row.token_chance}**")
        if row.certain is not None:
            details.append(f"{row.certain:,} confirmed · {row.confirmed_total - row.certain:,} estimated")
        types = discord_scoreboard.type_breakdown(row.chest_types)
        if types:
            details.append(types)
        if row.adjustment:
            details.append(f"Manual adjustment: {row.adjustment:+,}")
        await reply(interaction, "Chests opened", "\n".join(details))

    async def finished_matches():
        matches = await asyncio.to_thread(completed_matches, totals)
        return sorted(matches,
                      key=lambda match: (match_time(match), match.fingerprint), reverse=True)

    def match_time(match):
        return max(player.updated_at for player in match.players)

    def match_stamp(match):
        stamp = int(match_time(match))
        return f"<t:{stamp}:f>" if stamp else "Date unavailable"

    def match_totals(certain, total, chance):
        return (f"**{total:,}** chests · {certain:,} confirmed · {total - certain:,} estimated\n"
                f"Estimated Pirate Token chance · **{chance}**")

    async def edit_reply(interaction, title, detail):
        if interaction.app_permissions.embed_links:
            await interaction.edit_original_response(
                embed=discord.Embed(title=title, description=detail, color=0x5865F2),
                allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.edit_original_response(
                content=f"**{title}**\n{detail}"[:2000], allowed_mentions=discord.AllowedMentions.none())

    @tree.command(guild=guild, description="Show statistics for finished shared matches")
    @app_commands.describe(player="Leave empty for the group, or choose a player")
    @app_commands.autocomplete(player=autocomplete)
    async def stats(interaction: discord.Interaction, player: str | None = None):
        if not await check(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            matches = await asyncio.wait_for(finished_matches(), timeout=8)
        except (TimeoutError, OSError, sqlite3.Error, ValueError):
            await interaction.edit_original_response(
                content="Could not read shared match history. Try again in a moment.")
            return
        title = "Group stats"
        if player is not None:
            name = player.strip().casefold()
            matches = [TeamSession(match.fingerprint, (member,)) for match in matches
                       for member in match.players if member.name.casefold() == name]
            if matches:
                title = f"Stats · {matches[0].players[0].name}"
        if not matches:
            detail = "No finished shared matches for that player." if player is not None else "No finished shared matches yet."
            await edit_reply(interaction, title, detail)
            return

        summary = synchronized_totals(matches)
        best = max(matches, key=lambda match: (match.total, match_time(match)))
        detail = (f"**{summary.matches:,}** synchronized matches\n"
                  f"{match_totals(summary.certain, summary.total, summary.chance)}\n"
                  f"Average per match · **{summary.total / summary.matches:,.1f}** chests\n"
                  f"Best match · **{best.total:,}** chests · {match_stamp(best)}")
        await edit_reply(interaction, title, detail)

    async def match_choices(interaction, current):
        if interaction.guild_id != guild.id or not ready():
            return []
        try:
            matches = await finished_matches()
        except (OSError, sqlite3.Error, ValueError):
            return []
        prefix = current.strip().casefold()
        return [app_commands.Choice(
            name=f"{match.fingerprint[:12]} · {match.total:,} chests · {len(match.players)} results",
            value=match.fingerprint)
            for match in matches if match.fingerprint.startswith(prefix)][:25]

    @tree.command(guild=guild, description="Browse shared match history or view a specific match")
    @app_commands.describe(id="Match ID from the history, or pick a suggestion",
                           page="Page number, starting at 1")
    @app_commands.autocomplete(id=match_choices)
    async def match(interaction: discord.Interaction, id: str | None = None,
                    page: app_commands.Range[int, 1] = 1):
        if not await check(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            matches = await asyncio.wait_for(finished_matches(), timeout=8)
        except (TimeoutError, OSError, sqlite3.Error, ValueError):
            await interaction.edit_original_response(
                content="Could not read shared match history. Try again in a moment.")
            return
        if id is None:
            title = "Shared match history"
            match_label = "match" if len(matches) == 1 else "matches"
            intro = f"**{len(matches):,}** synchronized {match_label} · Use `/match id:` to open a summary."
            if matches and page == 1:
                summary = synchronized_totals(matches)
                overall = match_totals(summary.certain, summary.total, summary.chance)
                intro += f"\n\n**All shared matches**\n{overall}"
            rows = [f"`{item.fingerprint[:12]}` · {match_stamp(item)} · **{item.total:,}** chests · "
                    f"{len(item.players)} results" for item in matches]
        else:
            prefix = id.strip().casefold()
            found = [item for item in matches if prefix and item.fingerprint.startswith(prefix)]
            if len(found) != 1:
                await interaction.edit_original_response(
                    content="Use a unique match ID from `/match` or choose a suggestion.")
                return
            item = found[0]
            title = f"Match {item.fingerprint[:12]}"
            result_label = "result" if len(item.players) == 1 else "results"
            intro = f"{match_stamp(item)} · {len(item.players)} {result_label} received\n{match_totals(item.certain, item.total, item.chance_percent)}"
            rows = [f"**{discord.utils.escape_markdown(player.name)}** · {player.total:,} chests · "
                    f"{player.certain:,} confirmed · {player.total - player.certain:,} estimated"
                    for player in item.players]
        page_size = 8
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        if page > pages:
            await interaction.edit_original_response(content=f"Choose a page from 1 to {pages:,}.")
            return
        first = (page - 1) * page_size
        body = "\n".join(rows[first:first + page_size]) or "No matches shared yet."
        detail = f"{intro}\n\n{body}\n\nPage {page:,} / {pages:,}"
        await edit_reply(interaction, title, detail)

    @tree.command(guild=guild, description="Show the full leaderboard or publish it in this channel")
    @app_commands.describe(publish="Publish the auto-updating board here (Manage Server required)")
    async def leaderboard(interaction: discord.Interaction, publish: bool = False):
        if not await check(interaction, manage_guild=publish):
            return
        await interaction.response.defer(thinking=True, ephemeral=publish)
        if publish:
            if publish_board is None:
                await error(interaction, "Publishing is unavailable. Restart the bot and try again.")
                return
            link = await publish_board(interaction.channel_id)
            await interaction.followup.send(f"Leaderboard published: {link}", ephemeral=True)
            return
        settings = board_settings() if board_settings is not None else None
        embed = await asyncio.to_thread(public_leaderboard, totals, settings)
        if interaction.app_permissions.embed_links:
            await interaction.followup.send(embed=discord.Embed.from_dict(embed),
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            lines = [embed["title"], embed["description"]]
            lines.extend(f'{field["name"]}\n{field["value"]}' for field in embed["fields"])
            await interaction.followup.send("\n\n".join(lines)[:2000],
                                            allowed_mentions=discord.AllowedMentions.none())

    async def adjust(interaction, player, amount):
        if not await check(interaction, manage_guild=True):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            name, total = await asyncio.to_thread(totals.adjust, player, amount)
        except ValueError as exc:
            await error(interaction, str(exc))
            return
        title = "Chest total reset" if amount is None else "Chest total updated"
        await reply(interaction, title, f"**{discord.utils.escape_markdown(name)}** · {total:,}")

    @tree.command(guild=guild, description="Correct a player's total with a positive or negative count")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(player=autocomplete)
    async def add(interaction: discord.Interaction, player: str, amount: int):
        await adjust(interaction, player, amount)

    @tree.command(guild=guild, description="Reset a player's chest total")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(player=autocomplete)
    async def reset(interaction: discord.Interaction, player: str):
        await adjust(interaction, player, None)

    @tree.error
    async def on_command_error(interaction, _):
        await error(interaction, "The Discord command could not finish. Check the app and try again.")


class _SetupError(Exception):
    pass


class GuildBot(discord.Client):
    def __init__(self, config, totals, status, webhook=None):
        shared = webhook is not None
        super().__init__(intents=discord.Intents(guilds=True, guild_messages=shared, message_content=shared),
                         allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.totals = totals
        self._status_changed = status
        self.webhook = webhook
        self.tree = app_commands.CommandTree(self, fallback_to_global=False)
        self.feed_channel = None
        self._feed_lock = asyncio.Lock()
        self._feed_ready = False
        self._connection_epoch = 0
        self.failure = None
        self._publication_lock = asyncio.Lock()
        self._last_board = None
        self.board_settings = None
        self.publication_status = ""
        self.published_channel = ""
        self._presence_text = None
        self._presence_type = None
        self._presence_updated = None

    async def setup_hook(self):
        if self.config.application_id and int(self.config.application_id) != self.application_id:
            raise _SetupError("The bot token does not match the saved Application ID.")
        if self.webhook:
            try:
                partial = discord.Webhook.partial(int(self.webhook.id), self.webhook.token, client=self)
                webhook = await partial.fetch(prefer_auth=False)
            except (discord.NotFound, discord.Forbidden):
                raise _SetupError("Discord rejected the saved Group webhook. Check the webhook in Group.") from None
            if webhook.channel_id is None or webhook.guild_id is None:
                raise _SetupError("Use an incoming webhook attached to a Discord server text channel.")
            try:
                self.feed_channel = await self.fetch_channel(webhook.channel_id)
            except (discord.NotFound, discord.Forbidden):
                raise _SetupError("The bot cannot see the Group feed channel. Check View Channel.") from None
            if not isinstance(self.feed_channel, discord.TextChannel):
                raise _SetupError("Use a Group webhook attached to a Discord text channel.")

        if self.feed_channel is None:
            raise _SetupError("Save the Group webhook before starting the bot.")
        guild = discord.Object(self.feed_channel.guild.id)
        register_commands(self.tree, guild, self.totals, lambda: self._feed_ready, self.publish_board,
                          lambda: self.board_settings)
        for command in self.tree.get_commands(guild=guild):
            # Guild POST upserts this name/type. Bulk sync would delete unrelated commands.
            await self.http.upsert_guild_command(self.application_id, guild.id, command.to_dict(self.tree))

    async def on_ready(self):
        epoch = self._connection_epoch
        if self.feed_channel is not None:
            cached_channel = self.get_channel(self.feed_channel.id)
            if cached_channel is not None and cached_channel.guild.me is not None:
                permissions = cached_channel.permissions_for(cached_channel.guild.me)
                if not permissions.read_message_history:
                    self.failure = "Grant the bot Read Message History in the Group feed channel."
                    await self.close()
                    return
            await self._catch_up()
            if not self.failure:
                await self._refresh_live_messages()
        if not self.failure and epoch == self._connection_epoch:
            self._feed_ready = True
            try:
                target = await asyncio.to_thread(self.totals.published_board, self.application_id, self.feed_channel.guild.id)
                self.published_channel = target[0] if target else ""
            except (OSError, sqlite3.Error):
                self.publication_status = "Could not load the published leaderboard. Check local state."
            self._status_changed("online", f"Connected as {self.user}.")
            if not self.refresh_board.is_running():
                self.refresh_board.start()

    def available_channels(self):
        if self.feed_channel is None:
            return ()
        guild = self.get_guild(self.feed_channel.guild.id)
        if guild is None or guild.me is None:
            return ()
        result = []
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.send_messages and permissions.embed_links:
                result.append((str(channel.id), f"#{channel.name}"))
        return tuple(result)

    async def on_guild_channel_create(self, channel):
        self._channels_changed(channel.guild.id)

    async def on_guild_channel_update(self, before, after):
        self._channels_changed(after.guild.id)

    async def on_guild_channel_delete(self, channel):
        self._channels_changed(channel.guild.id)

    async def on_guild_role_update(self, before, after):
        self._channels_changed(after.guild.id)

    async def on_guild_role_delete(self, role):
        self._channels_changed(role.guild.id)

    def _channels_changed(self, guild_id):
        if self._feed_ready and self.feed_channel and self.feed_channel.guild.id == guild_id:
            self._status_changed("online", self.publication_status or f"Connected as {self.user}.")

    async def publish_board(self, channel_id=None, *, settings=None):
        async with self._publication_lock:
            guild_id = self.feed_channel.guild.id
            prior = await asyncio.to_thread(self.totals.published_board, self.application_id, guild_id)
            if channel_id is None and prior is None:
                if self.published_channel:
                    self.published_channel = ""
                    self._channels_changed(guild_id)
                return ""
            target = str(channel_id) if channel_id is not None else prior[0]
            channel = self.get_channel(int(target)) or await self.fetch_channel(int(target))
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
                raise ValueError("Choose a text channel in the configured server.")
            permissions = channel.permissions_for(channel.guild.me)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                raise ValueError("Grant View Channel, Send Messages and Embed Links in the leaderboard channel.")
            if settings is not None:
                self.board_settings = settings
            data = await asyncio.to_thread(public_leaderboard, self.totals, self.board_settings)
            same_channel = prior is not None and prior[0] == target
            if channel_id is None and self._last_board == (prior, data):
                return f"https://discord.com/channels/{guild_id}/{target}/{prior[1]}"
            embed = discord.Embed.from_dict(data)
            if same_channel:
                message = channel.get_partial_message(int(prior[1]))
                try:
                    await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    await asyncio.to_thread(self.totals.forget_published_board, self.application_id, guild_id)
                    self._last_board = None
                    self.published_channel = ""
                    self._channels_changed(guild_id)
                    if channel_id is None:
                        return ""
                    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            else:
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            try:
                await asyncio.to_thread(self.totals.save_published_board, self.application_id, guild_id, target, message.id)
            except Exception:
                if prior is None or str(message.id) != prior[1]:
                    await message.delete()
                raise
            self._last_board = ((target, str(message.id)), data)
            self.published_channel = target
            self.publication_status = ""
            if prior and prior[0] != target:
                try:
                    old_channel = self.get_channel(int(prior[0])) or await self.fetch_channel(int(prior[0]))
                    await old_channel.get_partial_message(int(prior[1])).delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    self.publication_status = "New board published. The previous message could not be removed."
            self._channels_changed(guild_id)
            return f"https://discord.com/channels/{guild_id}/{target}/{message.id}"

    async def delete_board(self):
        async with self._publication_lock:
            guild_id = self.feed_channel.guild.id
            prior = await asyncio.to_thread(self.totals.published_board, self.application_id, guild_id)
            if prior is not None:
                try:
                    channel = self.get_channel(int(prior[0])) or await self.fetch_channel(int(prior[0]))
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
                        raise ValueError("The published leaderboard is outside the configured server.")
                    await channel.get_partial_message(int(prior[1])).delete()
                except discord.NotFound:
                    pass
                await asyncio.to_thread(self.totals.forget_published_board, self.application_id, guild_id)
            self._last_board = None
            self.published_channel = ""
            self.publication_status = ""
            self._channels_changed(guild_id)

    async def apply_branding(self, name="Chest Counter", avatar_path="", banner_path=""):
        if self.user is None:
            raise ValueError("Wait until the bot is online before applying its profile.")
        name = name.strip()
        if not 2 <= len(name) <= 32 or not name.isprintable() or any(char in name for char in "@#:`"):
            raise ValueError("Use a bot name of 2 to 32 characters without @, #, colon or backticks.")
        assets = Path(__file__).parent / "gui/assets"

        def read_image(selected, bundled, label):
            path = Path(selected) if selected else assets / bundled
            if not selected and not path.is_file():
                path = assets / "icon_256.png"
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                raise ValueError(f"Choose a PNG or JPEG {label}.")
            try:
                with path.open("rb") as stream:
                    data = stream.read(2 * 1024 * 1024 + 1)
            except OSError:
                raise ValueError(f"Could not read the bot {label}. Choose an existing image file.") from None
            if len(data) > 2 * 1024 * 1024:
                raise ValueError(f"Choose a bot {label} smaller than 2 MB.")
            if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
                raise ValueError(f"The {label} must contain a PNG or JPEG image.")
            return data

        avatar = await asyncio.to_thread(read_image, avatar_path, "bot_avatar.png", "avatar")
        banner = await asyncio.to_thread(read_image, banner_path, "bot_banner.png", "banner")
        values = {"avatar": avatar, "banner": banner}
        if self.user.name != name:
            values["username"] = name
        await self.user.edit(**values)
        return "Bot name, avatar and banner updated."

    async def update_presence(self):
        if self.user is None:
            return
        config = settings.load_config(paths.APP_DIR / "config.toml")[0]["app"]
        monotonic = time.monotonic()
        if not config.get("discord_presence_enabled", True):
            if self._presence_text != "":
                await self.change_presence(status=discord.Status.online, activity=None)
                self._presence_text = ""
                self._presence_type = None
                self._presence_updated = monotonic
            return
        interval = max(30, min(300, config.get("discord_presence_interval", 30)))
        if self._presence_updated is not None and monotonic - self._presence_updated < interval:
            return
        now = time.time()
        sessions = await asyncio.to_thread(self.totals.sessions)
        active = [(player.updated_at, session) for session in sessions for player in session.players
                  if not player.finished and 0 <= now - player.updated_at <= 120]
        latest = max(active, key=lambda entry: entry[0])[1] if active else None
        def render(template, *, players=0, total=0, matches=0, chance="0.000000%"):
            text = template
            for key, value in (("players", players), ("total", total), ("matches", matches), ("chance", chance)):
                text = text.replace("{" + key + "}", f"{value:,}" if type(value) is int else value)
            return text.replace("\n", " ").strip()[:128]

        defaults = settings.DEFAULTS["app"]
        if latest is not None:
            count = len(latest.players)
            key = "discord_presence_personal_text" if count == 1 else "discord_presence_team_text"
            values = {"players": count, "total": latest.total, "chance": latest.chance_percent}
            text = render(config.get(key, defaults[key]), **values)
        else:
            players, team = await asyncio.to_thread(self.totals.board)
            saved = bool(players) or team.matches > 0
            key = "discord_presence_idle_text"
            idle = render(config.get(key, defaults[key]))
            text = idle
            if saved:
                key = "discord_presence_group_text"
                chance = match_stats.token_chance(math.fsum(player.log_no_drop for player in players))
                history = render(config.get(key, defaults[key]), players=len(players), chance=chance,
                                 total=sum(player.total for player in players), matches=team.matches)
                text = " · ".join(part for part in (idle, history) if part)[:128]
        activity_type = getattr(discord.ActivityType, config.get("discord_presence_activity", "watching"),
                                discord.ActivityType.watching)
        if text == self._presence_text and activity_type == self._presence_type:
            return
        await self.change_presence(status=discord.Status.online,
                                   activity=discord.Activity(type=activity_type, name=text))
        self._presence_text = text
        self._presence_type = activity_type
        self._presence_updated = monotonic

    @tasks.loop(seconds=15)
    async def refresh_board(self):
        if not self._feed_ready:
            return
        try:
            await self.publish_board()
        except (discord.HTTPException, OSError, ValueError, sqlite3.Error):
            self.publication_status = "Could not update the published board. Check channel permissions."
        try:
            await self.update_presence()
        except (discord.DiscordException, OSError, ValueError, sqlite3.Error):
            # Discord or the local store may be temporarily unavailable. Retry on the next refresh.
            pass

    async def close(self):
        self.refresh_board.cancel()
        await super().close()

    async def on_resumed(self):
        await self.on_ready()

    async def on_disconnect(self):
        self._connection_epoch += 1
        self._feed_ready = False
        self._status_changed("connecting", "Reconnecting to Discord…")

    async def on_message(self, message):
        if (self.feed_channel and message.channel.id == self.feed_channel.id
                and str(message.webhook_id) == self.totals.webhook_id):
            await self._catch_up(through=message)

    async def _refresh_live_messages(self):
        async with self._feed_lock:
            try:
                identifiers = await asyncio.to_thread(self.totals.live_messages, self.feed_channel.id)
                for ident in identifiers:
                    try:
                        message = await self.feed_channel.fetch_message(ident)
                    except discord.NotFound:
                        continue
                    if str(message.webhook_id) == self.totals.webhook_id:
                        await asyncio.to_thread(self.totals.ingest, message)
            except (discord.HTTPException, OSError, ValueError, sqlite3.Error):
                self.failure = "Could not recover live Group updates. Check the feed channel and local state."
                self._feed_ready = False
                await self.close()

    async def on_raw_message_edit(self, payload):
        if self.feed_channel is None or payload.channel_id != self.feed_channel.id:
            return
        content = payload.data.get("content")
        if isinstance(content, str) and not content.startswith("||FCCL1 "):
            return
        async with self._feed_lock:
            try:
                message = await self.feed_channel.fetch_message(payload.message_id)
                if (str(message.webhook_id) == self.totals.webhook_id
                        and isinstance(message.content, str) and message.content.startswith("||FCCL1 ")):
                    await asyncio.to_thread(self.totals.ingest, message)
            except discord.NotFound:
                return
            except (discord.HTTPException, OSError, ValueError, sqlite3.Error):
                self._status_changed("online", "Could not read an edited Group update. Check the feed channel.")

    async def _catch_up(self, through=None):
        # Serialize history and live delivery. Never move the cursor past a
        # message before processing the older messages in the same channel.
        async with self._feed_lock:
            try:
                last = await asyncio.to_thread(self.totals.cursor, self.feed_channel.id)
                if through is not None and through.id <= last:
                    return
                async for message in self.feed_channel.history(limit=None, after=discord.Object(last),
                                                               before=through, oldest_first=True):
                    if str(message.webhook_id) == self.totals.webhook_id:
                        await asyncio.to_thread(self.totals.ingest, message)
                    last = message.id
                if through is not None:
                    await asyncio.to_thread(self.totals.ingest, through)
                elif last:
                    await asyncio.to_thread(self.totals.advance_cursor, self.feed_channel.id, last)
            except discord.Forbidden:
                self.failure = "Grant the bot Read Message History in the Group feed channel."
            except Exception:
                self.failure = "Could not read shared Group contributions. Check the feed channel and local state."
            if self.failure:
                self._feed_ready = False
                await self.close()


class BotRunner:
    """Own one cancellable Gateway session on a background thread."""

    def __init__(self, config, webhook, changed):
        self.config = config
        self.webhook = webhook
        self._totals = GroupStore(webhook.id if webhook else "")
        self._changed = changed
        self._lock = threading.Lock()
        self._status = ("connecting", "Connecting to Discord…")
        self._session = None
        self._client = None
        self._channels = ()
        self._published_channel = ""
        self.report_config = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="discord-gateway")

    @property
    def status(self):
        with self._lock:
            return self._status

    def start(self):
        self._thread.start()

    def channels(self):
        with self._lock:
            return self._channels if self._status[0] == "online" else ()

    @property
    def published_channel(self):
        with self._lock:
            return self._published_channel if self._status[0] == "online" else ""

    def _submit(self, method, *args, **kwargs):
        with self._lock:
            if self._session is None or self._client is None or self._status[0] != "online":
                raise ValueError("Start the bot before changing Discord settings.")
            coroutine = getattr(self._client, method)(*args, **kwargs)
            try:
                return asyncio.run_coroutine_threadsafe(coroutine, self._session[0])
            except RuntimeError:
                coroutine.close()
                raise ValueError("The bot is stopping. Start it again before changing Discord settings.") from None

    def set_board_settings(self, settings):
        with self._lock:
            if self._session is None or self._client is None or self._status[0] != "online":
                return
            try:
                self._session[0].call_soon_threadsafe(setattr, self._client, "board_settings", settings)
            except RuntimeError:
                return

    def publish_board(self, channel_id, settings=None):
        return self._submit("publish_board", channel_id, settings=settings)

    def delete_board(self):
        return self._submit("delete_board")

    def apply_branding(self, name="Chest Counter", avatar_path="", banner_path=""):
        return self._submit("apply_branding", name, avatar_path, banner_path)

    def stop(self, *, timeout=None):
        with self._lock:
            if self._status[0] not in ("stopped", "error", "stopping"):
                self._status = ("stopping", "Disconnecting from Discord…")
                if self._session is not None:
                    loop, task = self._session
                    loop.call_soon_threadsafe(task.cancel)
        if timeout is not None:
            self._thread.join(timeout)

    def _update(self, state, detail, channel_id=None):
        with self._lock:
            if self._status[0] == "stopping":
                return
            self._status = (state, detail)
            if state == "online" and self._client is not None:
                channels = getattr(self._client, "available_channels", None)
                self._channels = channels() if channels is not None else ()
                self._published_channel = getattr(self._client, "published_channel", "")
            if channel_id is not None:
                self.report_config = replace(self.config, channel=str(channel_id))
        self._changed()

    async def _connect(self):
        def status_changed(state, detail):
            self._update(state, detail, bot.feed_channel.id if bot.feed_channel is not None else None)

        async with GuildBot(self.config, self._totals, status_changed, self.webhook) as bot:
            with self._lock:
                if self._status[0] == "stopping":
                    return
                self._client = bot
                self._session = asyncio.get_running_loop(), asyncio.create_task(bot.start(self.config.token))
            try:
                await self._session[1]
                if bot.failure:
                    raise _SetupError(bot.failure)
            finally:
                with self._lock:
                    self._session = None
                    self._client = None

    def _run(self):
        result = ("stopped", "The bot is off.")
        try:
            asyncio.run(self._connect())
        except asyncio.CancelledError:
            pass
        except discord.LoginFailure:
            result = ("error", "Discord rejected the bot token. Check the saved setup.")
        except discord.PrivilegedIntentsRequired:
            result = ("error", "Enable Message Content Intent on the Discord Developer Portal's Bot page.")
        except _SetupError as exc:
            result = ("error", str(exc))
        except discord.Forbidden:
            result = ("error", "Discord denied command registration. Check the bot's server installation.")
        except Exception:
            result = ("error", "Could not connect to Discord. Check your connection and saved setup.")
        finally:
            with self._lock:
                if self._status[0] == "stopping":
                    result = ("stopped", "The bot is off.")
                self._status = result
            self._changed()
