"""BR Stats Discord bot - lobbies and matchmaking backed by the BR Stats website.

The website owns players, MMR, balancing and match records. This bot runs
lobbies, posts balanced teams and takes a screenshot of the final scoreboard
(/submit). The website reads the screenshot (round score, champions, names,
stats) and holds it as a *pending match*. The bot posts what was read; the
players fix anything wrong and confirm it (3v3: RESULT_CONFIRMATIONS players, at
least RESULT_LOSER_CONFIRMATIONS from the losing team; 2v2: the _2V2 settings), then it's logged.
Admins can still log, change or discard results on the website.
"""
from __future__ import annotations

import asyncio
import difflib
import io
import re
import logging
import os
import random
import time
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from brstats.api import ApiError, WebsiteApi
from brstats.state import MODES, TEAM_SIZE, Lobby, Match, Member, Store

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
log = logging.getLogger("brstats-bot")


# ---------------------------------------------------------------- config
def _ids(name: str) -> set:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID") or 0)
API_URL = os.getenv("WEBSITE_API_URL", "http://localhost:9001/api")
SITE_URL = os.getenv("WEBSITE_URL", "http://localhost:9000").rstrip("/")
BOT_KEY = os.getenv("BOT_API_KEY", "")
LOBBY_CHANNELS = _ids("LOBBY_CHANNEL_IDS")          # empty = any channel
MATCH_FEED_CHANNEL = int(os.getenv("MATCH_FEED_CHANNEL_ID") or 0)  # 0 = same channel as lobby
# Where the WEBSITE posts confirmed results (DISCORD_MATCH_LOG_WEBHOOK_URL). Only used to point players there.
MATCH_HISTORY_CHANNEL = int(os.getenv("MATCH_HISTORY_CHANNEL_ID") or 0)
ADMIN_CHANNEL = int(os.getenv("ADMIN_CHANNEL_ID") or 0)            # link approvals
ADMIN_ROLES = _ids("ADMIN_ROLE_IDS")
ADMIN_USERS = _ids("ADMIN_USER_IDS")
DEFAULT_MAPS = [
    "Mount Araz (Day)", "Mount Araz (Night)", "Blackstone Arena (Day)", "Blackstone Arena (Night)",
    "Dragon Garden (Day)", "Dragon Garden (Night)", "Daharin Battlegrounds (Day)",
    "Daharin Battlegrounds (Night)", "The Great Market (Night)", "Meriko Summit (Night)",
    "Orman Temple (Night)",
]
MAPS = [m.strip() for m in os.getenv("MAP_POOL", "").split("|") if m.strip()] or DEFAULT_MAPS

CYAN = discord.Colour.from_str("#22D3EE")
RED = discord.Colour.from_str("#FF2E55")
GREY = discord.Colour.from_str("#475569")
GREEN = discord.Colour.from_str("#22C55E")
# /join reuses the settings of the channel's last match for this long, then goes back to defaults
STICKY_SETTINGS_MINUTES = float(os.getenv("STICKY_SETTINGS_MINUTES") or 30)
# Close a lobby nobody has joined or left for this long (0 = never)
LOBBY_TIMEOUT_MINUTES = float(os.getenv("LOBBY_TIMEOUT_MINUTES") or 30)
# Clear a match's posts from the matchmaking chat once it's submitted or cancelled
CLEAN_MATCH_POST = (os.getenv("CLEAN_MATCH_POST", "true").strip().lower() not in ("0", "false", "no", "off"))
SUBMIT_NOTICE_SECONDS = 60  # how long short notices (submitted, cancelled, logged) stay
LOBBY_BUMP_DELAY = 2  # seconds to wait after chat before moving the lobby back to the bottom
# While results are being confirmed, check this often whether an admin settled one on the website
REVIEW_POLL_SECONDS = int(os.getenv("REVIEW_POLL_SECONDS") or 300)
# A submitted result is logged once this many players confirm it, this many of them from the losing team
RESULT_CONFIRMATIONS = int(os.getenv("RESULT_CONFIRMATIONS") or 4)                    # 3v3
RESULT_LOSER_CONFIRMATIONS = int(os.getenv("RESULT_LOSER_CONFIRMATIONS") or 2)
RESULT_CONFIRMATIONS_2V2 = int(os.getenv("RESULT_CONFIRMATIONS_2V2") or 3)            # 2v2
RESULT_LOSER_CONFIRMATIONS_2V2 = int(os.getenv("RESULT_LOSER_CONFIRMATIONS_2V2") or 1)
# Role given to every member linked to a website player (taken back off when they're unlinked). 0 = off
LINKED_ROLE_ID = int(os.getenv("LINKED_ROLE_ID") or 0)
# Once this many screenshots in a row were logged with no corrections (by players or admins),
# results are shown as read with just a Confirm button. Any correction starts the count again. 0 = always allow fixes
OCR_TRUST_STREAK = int(os.getenv("OCR_TRUST_STREAK") if os.getenv("OCR_TRUST_STREAK") is not None else 5)


# ---------------------------------------------------------------- client
class BRBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.api = WebsiteApi(API_URL, BOT_KEY)
        self.store = Store(os.path.join(HERE, "state.json"))
        self.lock = asyncio.Lock()

    async def setup_hook(self):
        await self.api.start()
        ensure_review_watcher()   # only runs while submitted results are waiting for confirmation
        for lobby_id in list(self.store.lobbies):   # lobbies saved before a restart keep their timeout
            schedule_lobby_timeout(lobby_id)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        await self.api.close()
        await super().close()


bot = BRBot()


# ---------------------------------------------------------------- helpers
def is_admin(user: discord.abc.User) -> bool:
    if user.id in ADMIN_USERS:
        return True
    if isinstance(user, discord.Member):
        if any(r.id in ADMIN_ROLES for r in user.roles):
            return True
        return user.guild_permissions.manage_guild
    return False


async def reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True, **kw):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral, **kw)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral, **kw)


async def linked_player(interaction: discord.Interaction, user: Optional[discord.abc.User] = None) -> Optional[dict]:
    user = user or interaction.user
    player = await bot.api.player_by_discord(user.id)
    if not player and user.id == interaction.user.id:
        await reply(interaction, "You're not linked to a website player yet. Use `/link name:<your name on the website>` first.")
    return player


def lobby_title(lobby: Lobby) -> str:
    return f"{lobby.format} · {MODES[lobby.mode]} · {'Ranked' if lobby.ranked else 'Unranked'}"


def lobby_embed(lobby: Lobby, note: str = "") -> discord.Embed:
    e = discord.Embed(title=f"Lobby — {lobby_title(lobby)}", colour=CYAN if lobby.ranked else GREY)
    names = "\n".join(f"`{i + 1}.` {m.name} (<@{m.discord_id}>)" for i, m in enumerate(lobby.members)) or "Nobody yet"
    e.add_field(name=f"Players ({len(lobby.members)}/{lobby.size})", value=names, inline=False)
    e.set_footer(text=note or "Use /join or the button. Teams are balanced by the BR Stats website.")
    return e


def lobby_view(lobby: Lobby) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Join", style=discord.ButtonStyle.success, custom_id=f"brs:lobby:join:{lobby.id}"))
    v.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary, custom_id=f"brs:lobby:leave:{lobby.id}"))
    return v


def _team_lines(team: list) -> str:
    return "\n".join(f"**{p['name']}** · {round(p['mmr'])}" for p in team)


def match_embed(m: Match) -> discord.Embed:
    colour = {"live": CYAN, "review": discord.Colour.gold(), "logged": GREEN,
              "rejected": GREY, "cancelled": GREY}.get(m.status, CYAN)
    e = discord.Embed(
        title=f"Match #{m.id} — {m.format} {'Ranked' if m.ranked else 'Unranked'}",
        description=f"**Map:** {m.map}\n**Balance:** {MODES.get(m.mode, m.mode)}",
        colour=colour,
    )
    wa = m.win_probability_a
    e.add_field(name=f"Team A · avg {round(m.team_a_mmr)} · {wa:.0f}% win", value=_team_lines(m.team_a), inline=True)
    e.add_field(name=f"Team B · avg {round(m.team_b_mmr)} · {100 - wa:.0f}% win", value=_team_lines(m.team_b), inline=True)
    if m.status == "live":
        status = ("When the game is over, one player uses **/submit** with a screenshot of the final "
                  "scoreboard (**Average Total** tab, round orbs visible at the top).")
    elif m.status == "review":
        r = m.report or {}
        status = f"⏳ **Players are confirming the result** (submitted by {r.get('by_name', '?')})."
    elif m.status == "logged":
        status = "✅ Logged" + (f" - see the result in <#{MATCH_HISTORY_CHANNEL}>." if MATCH_HISTORY_CHANNEL else ".")
    elif m.status == "rejected":
        status = "❌ Not logged - an admin rejected this result."
    else:
        status = f"Cancelled by {m.report.get('cancelled_by') if m.report else 'someone'}."
    e.add_field(name="Status", value=status, inline=False)
    e.set_footer(text="MMR from the current ranked season on BR Stats")
    return e


def match_view(m: Match) -> Optional[discord.ui.View]:
    if m.status != "live":
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Cancel match", style=discord.ButtonStyle.danger, custom_id=f"brs:match:cancel:{m.id}"))
    return v


async def get_channel(channel_id: int) -> Optional[discord.abc.Messageable]:
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return ch


async def update_lobby_board(lobby: Lobby, note: str = "", final: bool = False):
    """Show or refresh a lobby's post. A new lobby is posted at the bottom of its channel; an
    existing post is edited where it is (chat in the channel moves the lobbies back down, see
    schedule_lobby_bump). A closed/filled lobby (final) is edited to say so, without buttons."""
    ch = await get_channel(lobby.channel_id)
    if ch is None:
        return
    embed = lobby_embed(lobby, note)
    view = None if final else lobby_view(lobby)
    if lobby.message_id:
        try:
            msg = await ch.fetch_message(lobby.message_id)
            await msg.edit(embed=embed, view=view)
            return
        except discord.HTTPException:
            if final:
                return
    if not final:
        await _repost_lobby(ch, lobby, embed, view)


async def close_lobby_posts(lobby: Lobby, note: str):
    """A lobby closed without a match: clear its posts (CLEAN_MATCH_POST), or mark the post closed."""
    task = _timeout_tasks.pop(lobby.id, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    if CLEAN_MATCH_POST:
        await _delete_message(lobby.channel_id, lobby.message_id)
        await _delete_message(lobby.channel_id, lobby.opened_message_id)
    else:
        await update_lobby_board(lobby, note=note, final=True)


async def _repost_lobby(ch, lobby: Lobby, embed=None, view=None):
    old = lobby.message_id
    msg = await ch.send(embed=embed or lobby_embed(lobby), view=view or lobby_view(lobby))
    lobby.message_id = msg.id
    bot.store.save()
    if old:
        try:
            await (await ch.fetch_message(old)).delete()
        except discord.HTTPException:
            pass  # already gone


_bump_tasks: dict = {}


def schedule_lobby_bump(channel_id: int):
    """Someone posted in a lobby channel: move its lobbies back to the bottom (batched)."""
    t = _bump_tasks.get(channel_id)
    if t and not t.done():
        return
    _bump_tasks[channel_id] = asyncio.get_running_loop().create_task(_bump(channel_id))


async def _bump(channel_id: int):
    await asyncio.sleep(LOBBY_BUMP_DELAY)
    async with bot.lock:
        await bump_lobbies(channel_id)


async def bump_lobbies(channel_id: int, force: bool = False):
    """Repost a channel's lobbies at the bottom, oldest first. Called with bot.lock held."""
    lobbies = bot.store.lobbies_in(channel_id)
    ch = await get_channel(channel_id)
    if not lobbies or ch is None:
        return
    if not force and getattr(ch, "last_message_id", None) == lobbies[-1].message_id:
        return    # already at the bottom
    try:
        for lobby in lobbies:
            await _repost_lobby(ch, lobby)
    except discord.HTTPException as e:
        log.warning("Couldn't move lobbies to the bottom: %s", e)


def _is_lobby_post(message: discord.Message) -> bool:
    return (bot.user is not None and message.author.id == bot.user.id and bool(message.embeds)
            and (message.embeds[0].title or "").startswith("Lobby —"))


async def update_match_message(m: Match):
    ch = await get_channel(m.channel_id)
    if ch is None or not m.message_id:
        return
    try:
        msg = await ch.fetch_message(m.message_id)
        await msg.edit(embed=match_embed(m), view=match_view(m))
    except discord.HTTPException as e:
        log.warning("Couldn't edit match message %s: %s", m.id, e)


def channel_ok(channel_id: int) -> bool:
    return not LOBBY_CHANNELS or channel_id in LOBBY_CHANNELS


# ---------------------------------------------------------------- lobby timeout
_timeout_tasks: dict = {}   # lobby id -> its sleeping timeout task


def touch_lobby(lobby: Lobby):
    """Someone opened, joined or left: restart the lobby's idle countdown."""
    lobby.last_activity = time.time()
    schedule_lobby_timeout(lobby.id)


def schedule_lobby_timeout(lobby_id: str):
    """One sleeping task per open lobby - nothing runs on a timer while lobbies are active."""
    if LOBBY_TIMEOUT_MINUTES <= 0:
        return
    old = _timeout_tasks.pop(lobby_id, None)
    if old and old is not asyncio.current_task():
        old.cancel()
    _timeout_tasks[lobby_id] = asyncio.get_event_loop().create_task(_lobby_timeout(lobby_id))


async def _lobby_timeout(lobby_id: str):
    await bot.wait_until_ready()
    lobby = bot.store.lobbies.get(lobby_id)
    if not lobby:
        return
    await asyncio.sleep(max(0.0, lobby.last_activity + LOBBY_TIMEOUT_MINUTES * 60 - time.time()))
    async with bot.lock:
        if bot.store.lobbies.get(lobby_id) is not lobby:
            return                      # it filled or closed meanwhile
        if time.time() - lobby.last_activity < LOBBY_TIMEOUT_MINUTES * 60 - 1:
            return schedule_lobby_timeout(lobby_id)   # activity since we started waiting
        del bot.store.lobbies[lobby_id]
        bot.store.save()
    _timeout_tasks.pop(lobby_id, None)
    minutes = f"{LOBBY_TIMEOUT_MINUTES:g}"
    await close_lobby_posts(lobby, f"Closed - nobody joined or left for {minutes} minutes.")
    ch = await get_channel(lobby.channel_id)
    if ch and lobby.members:
        try:
            who = " ".join(f"<@{m.discord_id}>" for m in lobby.members)
            await ch.send(f"The **{lobby_title(lobby)}** lobby timed out after {minutes} minutes with no joins - "
                          f"{who}, use /join to queue again.", delete_after=SUBMIT_NOTICE_SECONDS)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- lobby flow
def lobby_list(lobbies: list) -> str:
    return "\n".join(f"• **{lobby_title(l)}** ({len(l.members)}/{l.size})" for l in lobbies)


async def join_lobby(interaction: discord.Interaction, channel_id: int, lobby_id: Optional[str] = None):
    """Join a lobby: the one whose button was pressed, or (/join) the channel's only lobby -
    opening a default one if there's none."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not channel_ok(channel_id):
        return await reply(interaction, "Lobbies aren't allowed in this channel.")
    player = await linked_player(interaction)
    if not player:
        return
    me = interaction.user.id
    created = recent = None
    async with bot.lock:
        busy = bot.store.busy_reason(me)
        if busy:
            return await reply(interaction, f"Can't join: {busy}.")
        if lobby_id:
            lobby = bot.store.lobbies.get(lobby_id)
            if not lobby:
                return await reply(interaction, "That lobby has closed or filled.")
        else:
            here = bot.store.lobbies_in(channel_id)
            open_here = [l for l in here if not l.has(me)]
            if len(here) > 1:
                if not open_here:
                    return await reply(interaction, "You're already in every lobby here.")
                if len(open_here) > 1:
                    return await reply(interaction, "There are several lobbies here - press **Join** on the one you want:\n"
                                       + lobby_list(open_here))
            if here:
                lobby = open_here[0] if open_here else here[0]
            else:
                recent = bot.store.recent_settings(channel_id, STICKY_SETTINGS_MINUTES)
                lobby = Lobby(channel_id=channel_id, opener_id=me)
                if recent:
                    lobby.format, lobby.mode, lobby.ranked = recent["format"], recent["mode"], recent["ranked"]
                bot.store.lobbies[lobby.id] = lobby
                created = True
        if lobby.has(me):
            return await reply(interaction, f"You're already in the **{lobby_title(lobby)}** lobby.")
        lobby.members.append(Member(me, player["id"], player["name"]))
        touch_lobby(lobby)
        bot.store.save()
        if len(lobby.members) >= lobby.size:
            await launch_match(lobby)
            return await reply(interaction, "Lobby full — teams are up!")
        await update_lobby_board(lobby)
    if created and recent:
        extra = f" (opened a new **{lobby_title(lobby)}** lobby - same as the last match)"
    elif created:
        extra = f" (opened a new default **{lobby_title(lobby)}** lobby)"
    else:
        extra = f" the **{lobby_title(lobby)}** lobby"
    others = len(bot.store.lobbies_of(me)) - 1
    also = f" You're also in {others} other lobb{'y' if others == 1 else 'ies'}." if others else ""
    await reply(interaction, f"You're in{extra}: {len(lobby.members)}/{lobby.size}.{also}")


async def remove_from_lobbies(discord_id: int, lobbies: list, note_closed: str):
    """Take a player out of these lobbies; a lobby left empty closes. Called with bot.lock held."""
    for lobby in lobbies:
        lobby.members = [m for m in lobby.members if m.discord_id != discord_id]
        if not lobby.members:
            bot.store.lobbies.pop(lobby.id, None)
            bot.store.save()
            await close_lobby_posts(lobby, note_closed)
        else:
            touch_lobby(lobby)
            bot.store.save()
            await update_lobby_board(lobby)


async def leave_lobby(interaction: discord.Interaction, lobby_id: Optional[str] = None):
    """Leave one lobby (its Leave button) or, with /leave, every lobby you're in."""
    me = interaction.user.id
    async with bot.lock:
        if lobby_id:
            lobby = bot.store.lobbies.get(lobby_id)
            mine = [lobby] if lobby and lobby.has(me) else []
        else:
            mine = bot.store.lobbies_of(me)
        if not mine:
            return await reply(interaction, "You're not in that lobby." if lobby_id else "You're not in a lobby.")
        await remove_from_lobbies(me, mine, "Lobby closed (everyone left).")
    if len(mine) == 1:
        await reply(interaction, f"You left the **{lobby_title(mine[0])}** lobby.")
    else:
        await reply(interaction, f"You left {len(mine)} lobbies.")


async def launch_match(lobby: Lobby):
    """Called with bot.lock held, when a lobby is full."""
    del bot.store.lobbies[lobby.id]
    task = _timeout_tasks.pop(lobby.id, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    bot.store.remember_settings(lobby.channel_id, lobby.format, lobby.mode, lobby.ranked)
    bot.store.save()
    by_pid = {m.player_id: m for m in lobby.members}
    try:
        # unranked: the website avoids the teams these players had in their recent games
        res = await bot.api.balance(list(by_pid), lobby.format, lobby.mode, avoid_repeats=not lobby.ranked)
    except ApiError as e:
        bot.store.lobbies[lobby.id] = lobby  # put it back so nobody loses their spot
        bot.store.save()
        await update_lobby_board(lobby, note=f"Couldn't balance teams: {e.detail}. An admin can /admin clear-lobby.")
        return

    def side(players):
        return [{"discord_id": by_pid[p["id"]].discord_id, "player_id": p["id"],
                 "name": p["name"], "mmr": p.get("mmr", 0)} for p in players]

    try:   # S2-3v3-a1f9: season, format, unique id (cancelled matches never reuse theirs)
        code = await bot.api.reserve_match_code(lobby.format, lobby.ranked)
    except (ApiError, KeyError, TypeError):
        code = bot.store.new_match_id()   # website unreachable: it adds the season/format part when logged
    if code in bot.store.matches:
        code = bot.store.new_match_id()
    m = Match(
        id=code, channel_id=MATCH_FEED_CHANNEL or lobby.channel_id,
        format=lobby.format, mode=lobby.mode, ranked=lobby.ranked, map=random.choice(MAPS),
        team_a=side(res["team_a"]), team_b=side(res["team_b"]),
        team_a_mmr=res.get("team_a_mmr", 0), team_b_mmr=res.get("team_b_mmr", 0),
        win_probability_a=res.get("win_probability_a", 50),
        lobby_channel_id=lobby.channel_id,
        # the filled lobby post goes now (the Match found card replaces it); the rest at /submit
        cleanup_message_ids=[i for i in (lobby.opened_message_id,) if i]
                            + ([] if CLEAN_MATCH_POST else [i for i in (lobby.message_id,) if i]),
    )
    ch = await get_channel(m.channel_id)
    if ch is None:  # match feed channel missing - fall back to the lobby channel
        m.channel_id = lobby.channel_id
        ch = await get_channel(m.channel_id)
    mentions = " ".join(f"<@{i}>" for i in m.all_ids())
    msg = await ch.send(content=f"Match found! {mentions}", embed=match_embed(m), view=match_view(m))
    m.message_id = msg.id
    try:   # the website counts these teams as played straight away (ranked games too)
        await bot.api.record_lineup(m.id, m.format, [p["player_id"] for p in m.team_a], [p["player_id"] for p in m.team_b])
    except ApiError as e:
        log.warning("Couldn't record the line-up of match %s: %s", m.id, e.detail)
    bot.store.matches[m.id] = m      # its players can't join lobbies until it's submitted or cancelled
    bot.store.save()
    if CLEAN_MATCH_POST:
        await _delete_message(lobby.channel_id, lobby.message_id)
    else:
        await update_lobby_board(lobby, note=f"Lobby filled — see match #{m.id}.", final=True)
    # everyone in this match leaves the other lobbies they were waiting in
    for member in lobby.members:
        others = bot.store.lobbies_of(member.discord_id)
        if others:
            await remove_from_lobbies(member.discord_id, others, "Lobby closed (everyone left for a match).")


# ---------------------------------------------------------------- match flow
async def forget_lineup(m: Match):
    """A cancelled match's teams don't count as played for repeat avoidance."""
    try:
        await bot.api.forget_lineup(m.id)
    except ApiError as e:
        log.warning("Couldn't forget the line-up of match %s: %s", m.id, e.detail)


async def handle_match_button(interaction: discord.Interaction, action: str, match_id: str):
    m = bot.store.matches.get(match_id)
    if not m:
        return await reply(interaction, "This match is no longer active.")
    if not m.team_of(interaction.user.id) and not is_admin(interaction.user):
        return await reply(interaction, "Only players in this match (or admins) can do that.")
    if action == "cancel":
        async with bot.lock:
            m.status = "cancelled"
            m.report = {"cancelled_by": interaction.user.display_name}
            bot.store.matches.pop(m.id, None)
            bot.store.save()
        asyncio.get_running_loop().create_task(forget_lineup(m))
        if CLEAN_MATCH_POST:
            await interaction.response.defer()
            await tidy_up_match(m)
            note = await interaction.followup.send(
                f"Match #{m.id} was cancelled by {interaction.user.display_name}. Use /join to queue again.", wait=True)
            try:
                await note.delete(delay=SUBMIT_NOTICE_SECONDS)
            except (discord.HTTPException, AttributeError):
                pass
            return
        await update_match_message(m)
        return await reply(interaction, f"Match #{m.id} cancelled. Use /join to queue again.")


_names_checked: dict = {}   # discord id -> username already confirmed on the website


async def refresh_discord_name(user: discord.abc.User):
    """When someone uses the bot, make sure their linked player shows their current
    Discord username (fills it in after an admin links by user ID, follows renames),
    and that they have the linked-player role. Each is checked once, then remembered."""
    need_name = bool(user.name) and _names_checked.get(user.id) != user.name
    need_role = bool(LINKED_ROLE_ID) and user.id not in _role_checked
    if not need_name and not need_role:
        return
    try:
        p = await bot.api.player_by_discord(user.id)
        if need_name and p and p.get("discord_name") != user.name:
            await bot.api.set_discord(p["id"], user.id, user.name)
            log.info("Updated %s's Discord username to %s", p["name"], user.name)
    except ApiError:
        return
    if p:
        _names_checked[user.id] = user.name
    if need_role:
        await latch_linked_role(user.id, bool(p))


_review_task: Optional[asyncio.Task] = None


def ensure_review_watcher():
    """Start the review checker while results are waiting for confirmation (an admin may log
    or discard one on the website meanwhile). Stops by itself when none are left."""
    global _review_task
    if bot.store.reviews and (_review_task is None or _review_task.done()):
        _review_task = asyncio.get_event_loop().create_task(review_watcher())


async def review_watcher():
    """While results are being confirmed, check every REVIEW_POLL_SECONDS whether an admin has
    logged or discarded one on the website, then update its posts. Stops when none are left."""
    await bot.wait_until_ready()
    while not bot.is_closed() and bot.store.reviews:
        await asyncio.sleep(REVIEW_POLL_SECONDS)
        try:
            await check_reviews()
        except Exception:
            log.exception("Review check failed")


async def _delete_message(channel_id: Optional[int], message_id: Optional[int]):
    if not channel_id or not message_id:
        return
    ch = await get_channel(channel_id)
    if ch is None:
        return
    try:
        await (await ch.fetch_message(message_id)).delete()
    except discord.HTTPException:
        pass  # already gone


async def tidy_up_match(m: Match):
    """Clear the matchmaking chat of this match's posts: the 'Opened a lobby' message and the
    'Match found' post (the filled lobby post was already deleted when the match started)."""
    if not CLEAN_MATCH_POST:
        return
    for mid in m.cleanup_message_ids:
        await _delete_message(m.lobby_channel_id or m.channel_id, mid)
    m.cleanup_message_ids = []
    await _delete_message(m.channel_id, m.message_id)
    m.message_id = None
    bot.store.save()


async def check_reviews() -> bool:
    """Post results for matches an admin has logged or rejected since the last check."""
    any_logged = False
    for pid, m in list(bot.store.reviews.items()):
        if m.draft is not None:
            any_logged = (not await refresh_from_website(m) and m.status == "logged") or any_logged
            continue
        try:
            pm = await bot.api.pending_match(pid)
        except ApiError as e:
            if e.status == 404:
                pm = {"status": "discarded"}
            else:
                continue  # website down - try again next time
        if pm.get("status") == "pending":
            continue
        logged = pm.get("match") if pm.get("status") == "completed" else None
        m.status = "logged" if logged else "rejected"
        any_logged = any_logged or bool(logged)
        # The website posts the result itself (match log webhook). Here we only update the
        # match post, if it's still around, and remove review cards older bot versions left.
        if m.history_message_id and MATCH_HISTORY_CHANNEL:
            await _delete_message(MATCH_HISTORY_CHANNEL, m.history_message_id)
        await update_match_message(m)
        async with bot.lock:
            bot.store.reviews.pop(pid, None)
            bot.store.save()
    return any_logged


# ---------------------------------------------------------------- result check (players confirm)
STAT_KEYS = [("score", "Score"), ("damage", "Damage"), ("protection", "Protection"), ("disables", "Disables")]
STAT_SHORT = {"score": "Score", "damage": "Dmg", "protection": "Prot", "disables": "Dis"}
_champions: list = []


async def champion_names() -> list:
    global _champions
    if not _champions:
        try:
            _champions = [c["name"] for c in await bot.api.champions()]
        except ApiError:
            return []
    return _champions


def match_champion(text: str, names: list) -> Optional[str]:
    t = text.strip().lower()
    if not t:
        return None
    for n in names:
        if n.lower() == t:
            return n
    starts = [n for n in names if n.lower().startswith(t)]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(t, [n.lower() for n in names], n=1, cutoff=0.75)
    return next((n for n in names if close and n.lower() == close[0]), None)


def confirms_needed(m: Match) -> tuple:
    """(confirmations needed, of which from the losing team)"""
    total = len(m.team_a) + len(m.team_b)
    if m.format == "2v2":
        need, losers = RESULT_CONFIRMATIONS_2V2, RESULT_LOSER_CONFIRMATIONS_2V2
    else:
        need, losers = RESULT_CONFIRMATIONS, RESULT_LOSER_CONFIRMATIONS
    return min(need, total), min(losers, total // 2)


def new_draft(m: Match, pending: dict) -> dict:
    pre = pending.get("prefill") or {}
    pre_players, pre_teams = pre.get("players") or {}, pre.get("teams") or {}
    players = {}
    for team, side in (("A", m.team_a), ("B", m.team_b)):
        for p in side:
            got = pre_players.get(p["player_id"]) or {}
            stats = {k: (got.get("stats") or {}).get(k) for k, _ in STAT_KEYS}
            players[p["player_id"]] = {
                "name": p["name"], "discord_id": p["discord_id"], "team": pre_teams.get(p["player_id"]) or team,
                "champion": got.get("champion"), "stats": stats,
            }
    return {"a_rounds": pending.get("team_a_rounds"), "b_rounds": pending.get("team_b_rounds"),
            "players": players, "warnings": list(pre.get("warnings") or []), "edits": []}


def losing_team(d: dict) -> Optional[str]:
    a, b = d.get("a_rounds"), d.get("b_rounds")
    if a is None or b is None or a == b:
        return None
    return "A" if a < b else "B"


def draft_problems(d: dict) -> list:
    out = []
    if losing_team(d) is None:
        out.append("the round score" + (" can't be tied" if d.get("a_rounds") is not None and d.get("a_rounds") == d.get("b_rounds") else ""))
    for info in d["players"].values():
        if not info.get("champion"):
            out.append(f"{info['name']}'s champion")
    teams = [info["team"] for info in d["players"].values()]
    if teams.count("A") != teams.count("B"):
        out.append("both teams need the same number of players (an admin can fix teams on the website)")
    for t in ("A", "B"):
        champs = [i["champion"] for i in d["players"].values() if i["team"] == t and i.get("champion")]
        if len(set(champs)) != len(champs):
            out.append(f"Team {t} has the same champion twice")
    return out


def confirm_counts(m: Match) -> tuple:
    d = m.draft
    loser = losing_team(d)
    team_by_discord = {i["discord_id"]: i["team"] for i in d["players"].values()}
    return len(m.confirms), sum(team_by_discord.get(x) == loser for x in m.confirms)


def _stat_line(stats: dict) -> str:
    return " · ".join(f"{STAT_SHORT[k]} {stats.get(k) if stats.get(k) is not None else '?'}" for k, _ in STAT_KEYS)


def result_embed(m: Match, final: str = "") -> discord.Embed:
    d = m.draft
    need, need_losers = confirms_needed(m)
    a, b = d.get("a_rounds"), d.get("b_rounds")
    score = f"Team A **{a if a is not None else '?'} – {b if b is not None else '?'}** Team B"
    e = discord.Embed(title=f"Match #{m.id} — check the result", colour=GREEN if final.startswith("✅") else
                      GREY if final else discord.Colour.gold())
    rule = f"It's logged once **{need}** players confirm, at least **{need_losers}** from the losing team."
    if final:
        e.description = f"{score} · {m.map}\n\n{final}"
    elif locked(m):
        e.description = (f"{score} · {m.map}\n\nCheck the result against the screenshot. If the wrong screenshot "
                         f"was uploaded or something's wrong, don't confirm - message an admin.\n{rule}")
    elif not m.has_screenshot:
        e.description = (f"{score} · {m.map}\n\nTyped in by {(m.report or {}).get('by_name', '?')} (no screenshot). Anything "
                         f"wrong? Any player in the match can fix it with the ✏️ buttons (a fix clears the confirmations). "
                         f"Got the screenshot after all? `/submit` it to replace this.\n{rule}")
    else:
        e.description = (f"{score} · {m.map}\n\nCheck the result against the screenshot. Anything wrong? Any player in "
                         f"the match can fix it with the ✏️ buttons (a fix clears the confirmations). If the wrong "
                         f"screenshot was uploaded, message an admin.\n{rule}")
    loser = losing_team(d)
    for t in ("A", "B"):
        rows = [i for i in d["players"].values() if i["team"] == t]
        tag = "" if loser is None else (" · lost" if t == loser else " · won")
        lines = "\n".join(f"{'✅ ' if i['discord_id'] in m.confirms else ''}**{i['name']}** · {i.get('champion') or '❓ champion'}"
                          + (f"\n{_stat_line(i['stats'])}" if m.has_screenshot else "") for i in rows)
        e.add_field(name=f"Team {t}{tag}", value=lines or "-", inline=True)
    if not final:
        problems = draft_problems(d)
        if problems:
            e.add_field(name="Needs fixing before anyone can confirm", value="\n".join(f"• {p}" for p in problems)[:1024], inline=False)
        else:
            got, got_losers = confirm_counts(m)
            e.add_field(name="Confirmations",
                        value=f"**{got}/{need}** confirmed · losing team **{min(got_losers, need_losers)}/{need_losers}**",
                        inline=False)
        if d.get("warnings"):
            e.add_field(name="Screenshot notes", value="\n".join(f"• {w}" for w in d["warnings"])[:1024], inline=False)
    if d.get("edits"):
        e.add_field(name="Changes", value="\n".join(d["edits"][-4:])[:1024], inline=False)
    if m.has_screenshot:
        e.set_image(url=f"attachment://{d.get('shot_name') or 'scoreboard.png'}")
    e.set_footer(text="Admins can still change or discard results on the website")
    return e


def locked(m: Match) -> bool:
    """Trusted screenshot read with nothing missing: players only confirm, no fixes."""
    return bool(m.draft.get("trusted")) and not draft_problems(m.draft)


def result_view(m: Match) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success,
                                 custom_id=f"brs:res:ok:{m.id}", disabled=bool(draft_problems(m.draft)), row=0))
    if locked(m):
        return v
    v.add_item(discord.ui.Button(label="Round score", emoji="✏️", style=discord.ButtonStyle.secondary,
                                 custom_id=f"brs:res:score:{m.id}", row=0))
    for n, (pid, info) in enumerate(sorted(m.draft["players"].items(), key=lambda x: x[1]["team"])):
        v.add_item(discord.ui.Button(label=info["name"][:70], emoji="✏️", style=discord.ButtonStyle.secondary,
                                     custom_id=f"brs:res:p:{m.id}:{pid}", row=1 + n // 4))
    return v


async def post_result_card(m: Match, screenshot: Optional[bytes]):
    ch = await get_channel(m.channel_id)
    if ch is None:
        return
    kw = {"file": discord.File(io.BytesIO(screenshot), filename=m.draft.get("shot_name") or "scoreboard.png")} if screenshot else {}
    try:
        msg = await ch.send(embed=result_embed(m), view=result_view(m), **kw)
        m.result_message_id = msg.id
        bot.store.save()
    except discord.HTTPException as e:
        log.warning("Couldn't post the result check for match %s: %s", m.id, e)


async def close_result_card(m: Match, text: str):
    """The result is settled: clear the card (CLEAN_MATCH_POST) or leave it showing the outcome."""
    if not m.result_message_id:
        return
    if CLEAN_MATCH_POST:
        await _delete_message(m.channel_id, m.result_message_id)
        ch = await get_channel(m.channel_id)
        if ch is not None:
            try:
                await ch.send(f"Match #{m.id}: {text}", delete_after=SUBMIT_NOTICE_SECONDS)
            except discord.HTTPException:
                pass
    else:
        ch = await get_channel(m.channel_id)
        try:
            msg = await ch.fetch_message(m.result_message_id)
            await msg.edit(embed=result_embed(m, final=text), view=None)
        except (discord.HTTPException, AttributeError):
            pass
    m.result_message_id = None


async def settle_review(m: Match, logged: bool, text: str):
    m.status = "logged" if logged else "rejected"
    async with bot.lock:
        bot.store.reviews.pop(m.pending_id, None)
        bot.store.save()
    await close_result_card(m, text)
    await update_match_message(m)


async def refresh_from_website(m: Match) -> bool:
    """True if the website still has it pending (an admin may have logged or discarded it)."""
    try:
        pm = await bot.api.pending_match(m.pending_id)
    except ApiError as e:
        if e.status != 404:
            return True   # website down - assume nothing changed
        pm = {"status": "discarded"}
    if pm.get("status") == "pending":
        return True
    if pm.get("status") == "completed":
        await settle_review(m, True, "✅ Logged by an admin" + (f" - see <#{MATCH_HISTORY_CHANNEL}>." if MATCH_HISTORY_CHANNEL else "."))
    else:
        await settle_review(m, False, "❌ Discarded by an admin.")
    return False


async def handle_result_button(interaction: discord.Interaction, action: str, match_id: str, pid: str = ""):
    m = bot.store.review_by_match(match_id)
    if not m or not m.draft:
        return await reply(interaction, "This result is already settled.")
    me = interaction.user.id
    if me not in m.all_ids():
        return await reply(interaction, "Only players in this match can confirm or fix it. "
                                        "(Admins: change it on the website's Pending page.)")
    if action in ("score", "p") and locked(m):
        return await reply(interaction, "This result can't be changed here. If the wrong screenshot was uploaded or "
                                        "something's wrong, don't confirm - message an admin.")
    if action == "score":
        return await interaction.response.send_modal(ScoreModal(m))
    if action == "p":
        if pid not in m.draft["players"]:
            return await reply(interaction, "That player isn't in this match.")
        return await interaction.response.send_modal(PlayerModal(m, pid))
    # confirm
    problems = draft_problems(m.draft)
    if problems:
        return await reply(interaction, "Fix these first: " + "; ".join(problems))
    if me in m.confirms:
        return await reply(interaction, "You've already confirmed this. It's logged once enough players do.")
    await interaction.response.defer()
    if not await refresh_from_website(m):
        return
    async with bot.lock:
        if me not in m.confirms:
            m.confirms.append(me)
        bot.store.save()
    need, need_losers = confirms_needed(m)
    got, got_losers = confirm_counts(m)
    if got >= need and got_losers >= need_losers:
        return await log_confirmed(interaction, m)
    await interaction.edit_original_response(embed=result_embed(m), view=result_view(m))


async def log_confirmed(interaction: discord.Interaction, m: Match):
    d = m.draft
    names = {i["discord_id"]: i["name"] for i in d["players"].values()}
    players = []
    for pid, i in d["players"].items():
        stats = {k: v for k, v in i["stats"].items() if v is not None}
        players.append({"player_id": pid, "champion": i["champion"], "team": i["team"], "stats": stats or None})
    try:
        await bot.api.complete_pending(m.pending_id, {
            "players": players, "team_a_rounds": d["a_rounds"], "team_b_rounds": d["b_rounds"],
            "confirmed_by": ", ".join(names.get(x, "?") for x in m.confirms),
        })
    except ApiError as e:
        if e.status in (404, 409):
            await refresh_from_website(m)
            return
        await interaction.edit_original_response(embed=result_embed(m), view=result_view(m))
        return await interaction.followup.send(f"Confirmed, but the website didn't take it: {e.detail}. "
                                               "Try confirming again later, or ask an admin.", ephemeral=True)
    where = f" - see the result in <#{MATCH_HISTORY_CHANNEL}>." if MATCH_HISTORY_CHANNEL else "."
    await settle_review(m, True, f"✅ Confirmed by the players and logged{where}")


async def apply_edit(interaction: discord.Interaction, m: Match, changes: list, update):
    """Apply a player's fix: it clears the confirmations (the editor's counts for the new version)."""
    if not changes:
        return await reply(interaction, "Nothing changed.")
    if not bot.store.review_by_match(m.id):
        return await reply(interaction, "This result was settled in the meantime.")
    async with bot.lock:
        update()
        who = interaction.user.display_name
        m.draft["edits"] = (m.draft.get("edits") or [])[-9:] + [f"✏️ {who}: " + "; ".join(changes)]
        m.confirms = [] if draft_problems(m.draft) else [interaction.user.id]
        bot.store.save()
    try:
        await interaction.response.edit_message(embed=result_embed(m), view=result_view(m))
    except discord.HTTPException:
        await reply(interaction, "Saved.")


def _num(label: str, text: str) -> Optional[int]:
    text = text.strip().replace(",", "")
    if not text or text == "?":
        return None
    if not text.isdigit():
        raise ValueError(f"{label} must be a whole number")
    return int(text)


class ScoreModal(discord.ui.Modal):
    def __init__(self, m: Match):
        super().__init__(title=f"Match #{m.id} - round score")
        self.m = m
        d = m.draft
        self.a = discord.ui.TextInput(label="Team A rounds won", max_length=2, default=str(d.get("a_rounds") if d.get("a_rounds") is not None else ""))
        self.b = discord.ui.TextInput(label="Team B rounds won", max_length=2, default=str(d.get("b_rounds") if d.get("b_rounds") is not None else ""))
        self.add_item(self.a)
        self.add_item(self.b)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            a, b = _num("Team A rounds", self.a.value), _num("Team B rounds", self.b.value)
        except ValueError as e:
            return await reply(interaction, str(e))
        if a is None or b is None or a == b:
            return await reply(interaction, "Enter both teams' rounds - they can't be tied.")
        d = self.m.draft
        changes = [] if (a, b) == (d.get("a_rounds"), d.get("b_rounds")) else \
            [f"score {d.get('a_rounds', '?')}-{d.get('b_rounds', '?')} → {a}-{b}"]
        await apply_edit(interaction, self.m, changes, lambda: d.update(a_rounds=a, b_rounds=b))


class PlayerModal(discord.ui.Modal):
    def __init__(self, m: Match, pid: str):
        info = m.draft["players"][pid]
        super().__init__(title=f"{info['name']} - match #{m.id}"[:45])
        self.m, self.pid = m, pid
        self.champ = discord.ui.TextInput(label="Champion", max_length=30, default=info.get("champion") or "", required=False)
        self.add_item(self.champ)
        self.stats = {}
        for k, label in (STAT_KEYS if m.has_screenshot else []):   # no screenshot: no stats
            v = info["stats"].get(k)
            ti = discord.ui.TextInput(label=f"{label} (Average Total tab)", max_length=7, required=False,
                                      default="" if v is None else str(v))
            self.stats[k] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        info = self.m.draft["players"][self.pid]
        champ = info.get("champion")
        if self.champ.value.strip():
            champ = match_champion(self.champ.value, await champion_names())
            if not champ:
                return await reply(interaction, f"'{self.champ.value}' isn't a champion I know - check the spelling.")
        try:
            stats = {**info["stats"], **{k: _num(dict(STAT_KEYS)[k], ti.value) for k, ti in self.stats.items()}}
        except ValueError as e:
            return await reply(interaction, str(e))
        changes = []
        if champ != info.get("champion"):
            changes.append(f"{info['name']}'s champion {info.get('champion') or '?'} → {champ}")
        for k, v in stats.items():
            if v != info["stats"].get(k):
                old = info["stats"].get(k)
                changes.append(f"{info['name']}'s {dict(STAT_KEYS)[k].lower()} {'?' if old is None else old} → {'?' if v is None else v}")
        await apply_edit(interaction, self.m, changes, lambda: info.update(champion=champ, stats=stats))


# ---------------------------------------------------------------- linked-player role
# Checked like the username: the first time someone uses the bot (then remembered), and right
# away when they're linked or unlinked through the bot. Nothing runs on a timer.
_role_checked: dict = {}   # discord id -> whether they were linked when last checked


def _linked_role() -> Optional[discord.Role]:
    for g in bot.guilds:
        r = g.get_role(LINKED_ROLE_ID)
        if r:
            return r
    return None


async def latch_linked_role(user_id: int, linked: bool):
    """Give the role to a linked member; take it back off if they're no longer linked (only
    when the bot gave it - members given it by hand keep it)."""
    if not LINKED_ROLE_ID:
        return
    _role_checked[user_id] = linked
    role = _linked_role()
    if role is None:
        log.warning("LINKED_ROLE_ID %s isn't a role in any server the bot is in", LINKED_ROLE_ID)
        return
    try:
        member = role.guild.get_member(user_id) or await role.guild.fetch_member(user_id)
    except discord.HTTPException:
        return   # not in the server
    try:
        if linked and role not in member.roles:
            await member.add_roles(role, reason="Linked to a BR Stats player")
        elif not linked and user_id in bot.store.roled and role in member.roles:
            await member.remove_roles(role, reason="No longer linked to a BR Stats player")
    except discord.Forbidden:
        log.warning("Can't change the %s role - the bot needs Manage Roles, with its own role "
                    "above it in Server Settings > Roles", role.name)
        return
    except discord.HTTPException:
        return
    if linked and user_id not in bot.store.roled:
        bot.store.roled.append(user_id)
        bot.store.save()
    elif not linked and user_id in bot.store.roled:
        bot.store.roled.remove(user_id)
        bot.store.save()


# ---------------------------------------------------------------- link approval flow
async def handle_link_button(interaction: discord.Interaction, decision: str, request_id: str, discord_id: str):
    if not is_admin(interaction.user):
        return await reply(interaction, "Only admins can approve links.")
    approve = decision == "approve"
    try:
        req = await bot.api.decide_link(request_id, approve, interaction.user.display_name)
    except ApiError as e:
        return await reply(interaction, f"Couldn't update: {e.detail}")
    verdict = "Approved" if approve else "Denied"
    embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed()
    embed.colour = GREEN if approve else GREY
    embed.set_footer(text=f"{verdict} by {interaction.user.display_name}")
    await interaction.response.edit_message(embed=embed, view=None)
    if approve:
        await latch_linked_role(int(discord_id), True)
    try:
        user = await bot.fetch_user(int(discord_id))
        new = " as a new player" if req.get("new_player") and approve else ""
        await user.send(f"Your BR Stats link to **{req['player_name']}**{new} was {verdict.lower()}.")
    except discord.HTTPException:
        pass  # DMs closed - fine


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.user and not interaction.user.bot:
        asyncio.get_running_loop().create_task(refresh_discord_name(interaction.user))
    if interaction.type != discord.InteractionType.component:
        return
    cid = (interaction.data or {}).get("custom_id", "")
    if not cid.startswith("brs:"):
        return
    parts = cid.split(":")
    try:
        if parts[1] == "lobby":
            lid = parts[3]
            if lid.isdigit() and len(lid) >= 15:   # a button from before lobbies had ids (it held the channel id)
                lid = next((l.id for l in bot.store.lobbies_in(int(lid))), "gone")
            if parts[2] == "join":
                await join_lobby(interaction, interaction.channel_id, lid)
            else:
                await leave_lobby(interaction, lid)
        elif parts[1] == "match":
            await handle_match_button(interaction, parts[2], parts[3])
        elif parts[1] == "res":
            await handle_result_button(interaction, parts[2], parts[3], parts[4] if len(parts) > 4 else "")
        elif parts[1] == "link":
            await handle_link_button(interaction, parts[2], parts[3], parts[4])
    except Exception:
        log.exception("Button %s failed", cid)
        try:
            await reply(interaction, "Something went wrong — check the bot window for details.")
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- slash commands
MODE_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in MODES.items()]
FORMAT_CHOICES = [app_commands.Choice(name=f, value=f) for f in TEAM_SIZE]


@bot.tree.command(description="Join the lobby in this channel (opens one if there isn't one; with several, use the Join buttons)")
async def join(interaction: discord.Interaction):
    await join_lobby(interaction, interaction.channel_id)


@bot.tree.command(description="Leave every lobby you're in (or use a lobby's Leave button)")
async def leave(interaction: discord.Interaction):
    await leave_lobby(interaction)


lobby_group = app_commands.Group(name="lobby", description="Open, view or close a lobby")


@lobby_group.command(name="open", description="Open a lobby with your own settings")
@app_commands.describe(format="Team size", mode="How the website balances teams (default: Hybrid, or MMR Balance when unranked)",
                       ranked="Ranked games change MMR")
@app_commands.choices(format=FORMAT_CHOICES, mode=MODE_CHOICES)
async def lobby_open(interaction: discord.Interaction, format: Optional[app_commands.Choice[str]] = None,
                     mode: Optional[app_commands.Choice[str]] = None, ranked: bool = True):
    ch_id = interaction.channel_id
    if not channel_ok(ch_id):
        return await reply(interaction, "Lobbies aren't allowed in this channel.")
    # unranked lobbies balance by MMR alone unless told otherwise
    fmt, md = format.value if format else "3v3", mode.value if mode else ("hybrid" if ranked else "mmr")
    busy = bot.store.busy_reason(interaction.user.id)
    if busy:
        return await reply(interaction, f"Can't open a lobby: {busy}.")
    async with bot.lock:
        same = next((l for l in bot.store.lobbies_in(ch_id) if l.same_settings(fmt, md, ranked)), None)
        if same:
            return await reply(interaction, f"There's already a **{lobby_title(same)}** lobby here "
                                            f"({len(same.members)}/{same.size}) - press its **Join** button.")
        lobby = Lobby(channel_id=ch_id, opener_id=interaction.user.id, format=fmt, mode=md, ranked=ranked)
        bot.store.lobbies[lobby.id] = lobby
        touch_lobby(lobby)
        bot.store.save()
    await reply(interaction, f"Opened a **{lobby_title(lobby)}** lobby.", ephemeral=False)
    try:
        lobby.opened_message_id = (await interaction.original_response()).id
        bot.store.save()
    except discord.HTTPException:
        pass
    await join_lobby_after_open(interaction, lobby)


async def join_lobby_after_open(interaction: discord.Interaction, lobby: Lobby):
    player = await bot.api.player_by_discord(interaction.user.id)
    async with bot.lock:
        if player and not bot.store.busy_reason(interaction.user.id) and lobby.id in bot.store.lobbies:
            lobby.members.append(Member(interaction.user.id, player["id"], player["name"]))
            bot.store.save()
        await update_lobby_board(lobby)
    if not player:
        await reply(interaction, "You're not linked yet, so you weren't added. Use `/link` first, then /join.")


@lobby_group.command(name="show", description="Move this channel's lobbies to the bottom")
async def lobby_show(interaction: discord.Interaction):
    here = bot.store.lobbies_in(interaction.channel_id)
    if not here:
        return await reply(interaction, "No lobby here. /join opens a default one.")
    await reply(interaction, "Moved the lobb" + ("y" if len(here) == 1 else "ies") + " to the bottom.")
    async with bot.lock:
        await bump_lobbies(interaction.channel_id, force=True)


async def lobby_choices(interaction: discord.Interaction, current: str):
    here = bot.store.lobbies_in(interaction.channel_id)
    return [app_commands.Choice(name=f"{lobby_title(l)} ({len(l.members)}/{l.size})"[:100], value=l.id)
            for l in here if current.lower() in lobby_title(l).lower()][:25]


@lobby_group.command(name="close", description="Close a lobby in this channel (the player who opened it, or an admin)")
@app_commands.describe(lobby="Which lobby (only needed when there are several)")
@app_commands.autocomplete(lobby=lobby_choices)
async def lobby_close(interaction: discord.Interaction, lobby: Optional[str] = None):
    me, admin = interaction.user.id, is_admin(interaction.user)
    async with bot.lock:
        here = bot.store.lobbies_in(interaction.channel_id)
        if lobby:
            target = next((l for l in here if l.id == lobby), None)
            if not target:   # typed instead of picked from the list: match it against the lobby names
                named = [l for l in here if lobby.strip().lower() in lobby_title(l).lower()]
                target = named[0] if len(named) == 1 else None
            if not target:
                return await reply(interaction, "That lobby isn't open here - pick it from the list:\n" + lobby_list(here)
                                   if here else "No lobby here.")
        else:
            mine = [l for l in here if l.opener_id == me] or (here if admin else [])
            if not mine:
                return await reply(interaction, "No lobby here that you opened." if here else "No lobby here.")
            if len(mine) > 1:
                return await reply(interaction, "There are several - pick one with the `lobby` option:\n" + lobby_list(mine))
            target = mine[0]
        if me != target.opener_id and not admin:
            return await reply(interaction, "Only the player who opened it, or an admin, can close it.")
        bot.store.lobbies.pop(target.id, None)
        bot.store.save()
        await close_lobby_posts(target, f"Closed by {interaction.user.display_name}.")
    await reply(interaction, f"Closed the **{lobby_title(target)}** lobby.")


bot.tree.add_command(lobby_group)


@bot.tree.command(description="Submit your match result with the final scoreboard screenshot")
@app_commands.describe(screenshot="Final scoreboard (Average Total tab). Skipped the end screen? Leave empty and type in the result",
                       match_id="Only needed by admins submitting for someone else")
async def submit(interaction: discord.Interaction, screenshot: Optional[discord.Attachment] = None,
                 match_id: Optional[str] = None):
    if screenshot:
        if not (screenshot.content_type or "").startswith("image/"):
            return await reply(interaction, "That isn't an image - attach a PNG or JPG screenshot.")
        if screenshot.size > 15 * 1024 * 1024:
            return await reply(interaction, "That screenshot is over 15 MB.")
    me = interaction.user.id
    if match_id:
        mid = bot.store.norm_match_id(match_id)
        m = bot.store.matches.get(mid) or bot.store.review_by_match(mid)
        if m and not m.team_of(me) and not is_admin(interaction.user):
            return await reply(interaction, "You're not in that match.")
    else:
        m = next((x for x in bot.store.matches.values() if x.team_of(me)), None) or \
            next((x for x in bot.store.reviews.values() if x.team_of(me) and x.draft and not x.has_screenshot), None)
    if not m:
        return await reply(interaction, "You don't have a match to submit. (Admins: add `match_id`.)")
    if m.status == "review":   # submitted without a screenshot - a screenshot replaces the typed result
        if not screenshot:
            return await reply(interaction, f"Match #{m.id} was already submitted without a screenshot. Confirm it on its "
                                            "card, or `/submit` again with the screenshot to replace it.")
        if m.has_screenshot or not m.draft:
            return await reply(interaction, f"Match #{m.id} already has a screenshot.")
        return await add_screenshot(interaction, m, screenshot)
    if m.status != "live":
        return await reply(interaction, "This match was already submitted or cancelled.")
    if not screenshot:
        return await interaction.response.send_modal(NoShotModal(m))
    await interaction.response.defer(thinking=True)  # public: everyone sees the result
    await finish_submit(interaction, m, data=await screenshot.read(), filename=screenshot.filename)


def _shot_name(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return "scoreboard" + (ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".png")


def _trusted(m: Match, pending: dict) -> bool:
    """The reader has been right often enough in a row: show what it read, no corrections."""
    return bool(OCR_TRUST_STREAK and m.has_screenshot and (pending.get("ocr_streak") or 0) >= OCR_TRUST_STREAK
                and not m.draft.get("warnings") and not draft_problems(m.draft))


async def finish_submit(interaction: discord.Interaction, m: Match, data: Optional[bytes] = None,
                        filename: str = "", rounds: Optional[tuple] = None, champs: Optional[dict] = None):
    async with bot.lock:
        if m.status != "live" or m.id not in bot.store.matches:
            return await reply(interaction, "This match was already submitted or cancelled.")
        m.status = "submitting"
    try:
        # link to the match post on the website - unless that post is about to be tidied away
        jump = None if CLEAN_MATCH_POST else \
            f"https://discord.com/channels/{interaction.guild_id}/{m.channel_id}/{m.message_id}"
        pending = await bot.api.create_pending_match({
            "ranked": m.ranked, "format": m.format,
            "team_a": [p["player_id"] for p in m.team_a], "team_b": [p["player_id"] for p in m.team_b],
            "team_a_rounds": rounds[0] if rounds else None, "team_b_rounds": rounds[1] if rounds else None,
            "balance_mode": m.mode, "map": m.map, "reported_by": interaction.user.display_name,
            "source": "discord", "source_ref": jump, "discord_match_id": m.id,
            "notes": "" if data else "No screenshot",   # the match code is shown on the website already
        })
    except ApiError as e:
        m.status = "live"
        return await reply(interaction, f"The website rejected it: {e.detail}. Nothing was saved - try again.", ephemeral=False)
    upload_note = None
    if data:
        try:
            pending = await bot.api.upload_screenshot(pending["id"], data, filename, interaction.user.display_name)
        except ApiError as e:
            data = None
            upload_note = (f"The screenshot wasn't used ({e.detail}). Fill in the result with the ✏️ buttons, "
                           "or `/submit` the right screenshot.")
    async with bot.lock:
        m.status = "review"
        m.pending_id = pending["id"]
        m.report = {"by_name": interaction.user.display_name, "manual": not data}
        m.draft = new_draft(m, pending)
        for pid, champ in (champs or {}).items():
            m.draft["players"][pid]["champion"] = champ
        m.has_screenshot = bool(data)
        if data:
            m.draft["shot_name"] = _shot_name(filename)
        m.draft["trusted"] = _trusted(m, pending)
        m.confirms = []
        bot.store.matches.pop(m.id, None)      # players are free to queue again
        bot.store.reviews[m.pending_id] = m
        bot.store.save()
    if CLEAN_MATCH_POST:
        await tidy_up_match(m)
    else:
        await update_match_message(m)
    await post_result_card(m, data)
    ensure_review_watcher()
    note = f"\n{upload_note}" if upload_note else ""
    need, need_losers = confirms_needed(m)
    await reply(interaction, f"Match #{m.id} submitted - **check the result below** and confirm it. "
                             f"It's logged once {need} players confirm, at least {need_losers} from the losing team.{note}",
                ephemeral=False)
    await _expire_reply(interaction)


async def _expire_reply(interaction: discord.Interaction):
    if CLEAN_MATCH_POST:
        try:  # the notice tidies itself away too; the website's match log is the record
            await (await interaction.original_response()).delete(delay=SUBMIT_NOTICE_SECONDS)
        except (discord.HTTPException, AttributeError):
            pass


async def add_screenshot(interaction: discord.Interaction, m: Match, screenshot: discord.Attachment):
    """A match submitted without a screenshot gets one: the screenshot replaces the typed result."""
    await interaction.response.defer(thinking=True)
    data = await screenshot.read()
    try:
        pending = await bot.api.upload_screenshot(m.pending_id, data, screenshot.filename, interaction.user.display_name)
    except ApiError as e:
        if e.status in (404, 409) and not await refresh_from_website(m):
            return await reply(interaction, f"Match #{m.id} was already settled on the website.")
        return await reply(interaction, f"Couldn't use that screenshot: {e.detail}")
    async with bot.lock:
        old = m.draft
        new = new_draft(m, pending)
        pre = pending.get("prefill") or {}
        a, b = pre.get("team_a_rounds"), pre.get("team_b_rounds")
        new["a_rounds"], new["b_rounds"] = (a, b) if a is not None and b is not None and a != b else \
            (old.get("a_rounds"), old.get("b_rounds"))
        for pid, info in new["players"].items():
            if not info.get("champion"):
                info["champion"] = old["players"][pid].get("champion")
        new["edits"] = (old.get("edits") or [])[-9:] + [f"📷 {interaction.user.display_name} added the screenshot"]
        new["shot_name"] = _shot_name(screenshot.filename)
        m.draft, m.has_screenshot, m.confirms = new, True, []
        m.draft["trusted"] = _trusted(m, pending)
        old_card, m.result_message_id = m.result_message_id, None
        bot.store.save()
    await _delete_message(m.channel_id, old_card)
    await post_result_card(m, data)
    await reply(interaction, f"Screenshot added to match #{m.id} - the confirmations started again. "
                             "Check the result below and confirm it.", ephemeral=False)
    await _expire_reply(interaction)


def _parse_champs(text: str, team: list, names: list) -> tuple:
    """'Jade, Pearl, Lucie' -> ({player_id: champion}, error)"""
    parts = [x.strip() for x in re.split(r"[,/;\n]+", text) if x.strip()]
    if len(parts) != len(team):
        return {}, f"give {len(team)} champions, in the order shown"
    out, bad = {}, []
    for p, txt in zip(team, parts):
        champ = match_champion(txt, names)
        if not champ:
            bad.append(f"'{txt}'")
        out[p["player_id"]] = champ
    if bad:
        return {}, f"{', '.join(bad)} isn't a champion I know"
    if len(set(out.values())) != len(out):
        return {}, "the same champion is in there twice"
    return out, None


class NoShotModal(discord.ui.Modal):
    """/submit without a screenshot: the round score and every champion, typed in."""
    def __init__(self, m: Match):
        super().__init__(title=f"Match #{m.id} - result (no screenshot)")
        self.m = m
        self.a = discord.ui.TextInput(label="Team A rounds won", max_length=2)
        self.b = discord.ui.TextInput(label="Team B rounds won", max_length=2)
        self.champs = {}
        for t, team in (("A", m.team_a), ("B", m.team_b)):
            who = ", ".join(p["name"] for p in team)
            ti = discord.ui.TextInput(label=f"Team {t} champions: {who}"[:45], max_length=100,
                                      placeholder=f"Champions of {who}, in that order"[:100])
            self.champs[t] = ti
        for item in (self.a, self.b, self.champs["A"], self.champs["B"]):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        m = self.m
        try:
            a, b = _num("Team A rounds", self.a.value), _num("Team B rounds", self.b.value)
        except ValueError as e:
            return await reply(interaction, f"{e} - nothing was submitted.")
        if a is None or b is None or a == b:
            return await reply(interaction, "Enter both teams' rounds - they can't be tied. Nothing was submitted.")
        names = await champion_names()
        champs, errors = {}, []
        for t, team in (("A", m.team_a), ("B", m.team_b)):
            got, err = _parse_champs(self.champs[t].value, team, names)
            if err:
                errors.append(f"Team {t}: {err}")
            champs.update(got)
        if errors:
            return await reply(interaction, "; ".join(errors) + ". Nothing was submitted - try `/submit` again.")
        await interaction.response.defer(thinking=True)
        await finish_submit(interaction, m, rounds=(a, b), champs=champs)


@bot.tree.command(description="Set your in-game Battlerite name (helps match you on screenshots)")
@app_commands.describe(name="Your name exactly as it shows in Battlerite")
async def ign(interaction: discord.Interaction, name: str):
    p = await linked_player(interaction)
    if not p:
        return
    await bot.api.set_ign(p["id"], name)
    await reply(interaction, f"In-game name set to **{name}**.")


@bot.tree.command(description="Link your Discord account to your BR Stats player (or ask for a new one)")
@app_commands.describe(name="Your player name on the website - if you're not on it yet, the name you want")
async def link(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        req = await bot.api.request_link(interaction.user.id, interaction.user.name, name)
    except ApiError as e:
        return await reply(interaction, f"Couldn't request that link: {e.detail}")
    admin_ch = await get_channel(ADMIN_CHANNEL) if ADMIN_CHANNEL else None
    if admin_ch:
        if req.get("new_player"):
            desc = (f"{interaction.user.mention} (`{interaction.user.name}`) isn't on the website yet and wants a "
                    f"**new player** called **{req['player_name']}**.\nApproving creates the player and links them.")
        else:
            desc = f"{interaction.user.mention} (`{interaction.user.name}`) wants to be **{req['player_name']}**."
        e = discord.Embed(title="New player request" if req.get("new_player") else "Link request",
                          colour=discord.Colour.gold() if req.get("new_player") else CYAN, description=desc)
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success,
                                     custom_id=f"brs:link:approve:{req['id']}:{interaction.user.id}"))
        v.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger,
                                     custom_id=f"brs:link:deny:{req['id']}:{interaction.user.id}"))
        await admin_ch.send(embed=e, view=v)
    if req.get("new_player"):
        await reply(interaction, f"There's no **{req['player_name']}** on the website yet, so I've asked the admins to "
                                 f"create you as a new player. You'll get a DM when it's approved.\n"
                                 f"Typo? Just run `/link` again with the right name - it replaces this request.")
    else:
        await reply(interaction, f"Request sent to link you to **{req['player_name']}**. An admin will approve it "
                                 f"(here in Discord or on the website's Pending page).")


@bot.tree.command(description="Show which website player you're linked to")
async def whoami(interaction: discord.Interaction):
    p = await bot.api.player_by_discord(interaction.user.id)
    if not p:
        return await reply(interaction, "You're not linked. Use `/link name:<your website name>`.")
    await reply(interaction, f"You're linked to **{p['name']}** — {SITE_URL}/players/{p['id']}")


@bot.tree.command(description="Show a player's current-season rank from the website")
@app_commands.describe(member="Leave empty for yourself")
async def rank(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    p = await bot.api.player_by_discord(target.id)
    if not p:
        return await reply(interaction, f"{target.display_name} isn't linked to a website player.")
    lb = await bot.api.leaderboard()
    played = [r for r in lb if r["matches"]]
    row = next((r for r in played if r["player_id"] == p["id"]), None)
    if not row or not row["matches"]:
        return await reply(interaction, f"**{p['name']}** hasn't played a ranked match this season.", ephemeral=False)
    streak = row.get("streak", 0)
    streak_txt = (f"{streak} wins" if streak > 1 else "1 win" if streak == 1
                  else "1 loss" if streak == -1 else f"{-streak} losses" if streak < -1 else "none")
    e = discord.Embed(title=f"#{played.index(row) + 1} {p['name']}", url=f"{SITE_URL}/players/{p['id']}", colour=CYAN)
    e.add_field(name="MMR", value=f"{round(row['mmr'])} (peak {round(row['peak_mmr'])})")
    e.add_field(name="Record", value=f"{row['wins']}W – {row['losses']}L · {row['winrate']}%")
    e.add_field(name="Streak", value=streak_txt)
    await interaction.response.send_message(embed=e)


@bot.tree.command(description="Top 10 of the current ranked season")
async def leaderboard(interaction: discord.Interaction):
    lb = [r for r in await bot.api.leaderboard() if r["matches"]][:10]
    if not lb:
        return await reply(interaction, "No ranked matches this season yet.", ephemeral=False)
    lines = [f"`#{i:>2}` **{r['name']}** · {round(r['mmr'])} · {r['wins']}-{r['losses']}" for i, r in enumerate(lb, 1)]
    e = discord.Embed(title="BR Stats leaderboard", url=f"{SITE_URL}/leaderboard", description="\n".join(lines), colour=CYAN)
    await interaction.response.send_message(embed=e)


admin_group = app_commands.Group(name="admin", description="Admin tools",
                                 default_permissions=discord.Permissions(manage_guild=True))


async def _admin_guard(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction.user):
        await reply(interaction, "Admins only.")
        return False
    return True


@admin_group.command(name="link", description="Link a member to a website player now (creates the player if needed)")
async def admin_link(interaction: discord.Interaction, member: discord.Member, name: str):
    if not await _admin_guard(interaction):
        return
    try:
        req = await bot.api.request_link(member.id, member.name, name)
        await bot.api.decide_link(req["id"], True, interaction.user.display_name)
    except ApiError as e:
        return await reply(interaction, f"Couldn't link: {e.detail}")
    created = " (new player created)" if req.get("new_player") else ""
    await latch_linked_role(member.id, True)
    await reply(interaction, f"Linked {member.mention} to **{req['player_name']}**{created}.")


@admin_group.command(name="unlink", description="Remove a Discord member's link")
async def admin_unlink(interaction: discord.Interaction, member: discord.Member):
    if not await _admin_guard(interaction):
        return
    p = await bot.api.player_by_discord(member.id)
    if not p:
        return await reply(interaction, f"{member.display_name} isn't linked.")
    await bot.api.set_discord(p["id"], None)
    _names_checked.pop(member.id, None)
    await latch_linked_role(member.id, False)
    await reply(interaction, f"Unlinked {member.mention} from **{p['name']}**.")


@admin_group.command(name="cancel-match", description="Cancel a live match by its #id")
async def admin_cancel(interaction: discord.Interaction, match_id: str):
    if not await _admin_guard(interaction):
        return
    async with bot.lock:
        m = bot.store.matches.pop(bot.store.norm_match_id(match_id), None)
        bot.store.save()
    if not m:
        return await reply(interaction, "No live match with that id.")
    m.status, m.report = "cancelled", {"cancelled_by": interaction.user.display_name}
    await forget_lineup(m)
    await update_match_message(m)
    await tidy_up_match(m)
    await reply(interaction, f"Cancelled match #{m.id}.")


@admin_group.command(name="clear-lobby", description="Remove every lobby in this channel")
async def admin_clear(interaction: discord.Interaction):
    if not await _admin_guard(interaction):
        return
    async with bot.lock:
        gone = bot.store.lobbies_in(interaction.channel_id)
        for lobby in gone:
            bot.store.lobbies.pop(lobby.id, None)
        bot.store.save()
    for lobby in gone:
        await close_lobby_posts(lobby, "Cleared by an admin.")
    await reply(interaction, f"Cleared {len(gone)} lobb{'y' if len(gone) == 1 else 'ies'}." if gone else "No lobby here.")


bot.tree.add_command(admin_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, ApiError):
        msg = f"Website error: {original.detail}"
    else:
        log.exception("Command failed", exc_info=original)
        msg = "Something went wrong — check the bot window for details."
    try:
        await reply(interaction, msg)
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or _is_lobby_post(message):
        return
    if bot.store.lobbies_in(message.channel.id):
        schedule_lobby_bump(message.channel.id)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s). Website API: %s", bot.user, bot.user.id, API_URL)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [n for n, v in [("DISCORD_TOKEN", TOKEN), ("BOT_API_KEY", BOT_KEY)] if not v]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in .env - see .env.example")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
