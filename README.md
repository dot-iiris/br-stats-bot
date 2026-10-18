# BR Stats Discord bot

The BR Stats Discord bot runs pick-up lobbies for a small Battlerite OCE group.
Players gather in lobbies in Discord, and once a lobby is full the bot posts
balanced teams and collects the result of the game. Everything else, namely
the players, MMR, team balancing and match records, is handled by the BR Stats
website, which the bot talks to through its API.

The website is maintained in a separate repository:
https://github.com/dot-iiris/Battlerite-Stat-Tracker

## How a game is played

**Lobbies.** A channel can have several lobbies open at once, one for each
combination of settings, and a player can wait in more than one of them.
`/lobby open` opens a new lobby with a chosen format (2v2 or 3v3), balance mode
(Hybrid Balance, Role Synergy or MMR Balance) and ranked setting. `/join` joins
the channel's lobby when there is only one. When there is none, it opens one
with the same settings as the channel's last match, provided that match started
within the last `STICKY_SETTINGS_MINUTES` (30 by default), and otherwise with
the default of 3v3, Hybrid Balance, ranked. When there are several, players use
the Join button on the lobby they want.

`/leave` leaves every lobby a player is in, while a lobby's Leave button leaves
only that one. `/lobby close` closes a lobby (its opener can close it, and
administrators can close any), `/lobby show` moves the channel's lobbies to the
bottom, and `/admin clear-lobby` removes them all. Lobbies always stay at the
bottom of their channel: when anyone posts there, the bot reposts them
underneath, oldest first. A lobby that nobody joins or leaves for
`LOBBY_TIMEOUT_MINUTES` (30 by default) closes itself, and its players receive a
short notice that it timed out.

**Pinging players.** When `LOBBY_PING_ROLE_ID` is set, the bot mentions that role
to gather players. A new lobby gets a ping when no lobby of its format (2v2 or
3v3) has filled in the last `LOBBY_PING_QUIET_MINUTES` (30 by default), with at
most one such ping every `LOBBY_PING_COOLDOWN_MINUTES` (5 by default), so
opening lobbies of several formats at once pings only once. Any lobby that is
one player short (5/6 or 3/4) also gets a ping, at most once per lobby per
cooldown. A lobby's pings are deleted when it fills or closes (with
`CLEAN_MATCH_POST`). The role must allow the bot to mention it: turn on "Allow
anyone to @mention this role" in its settings, or give the bot the Mention
@everyone, @here and All Roles permission.

**Teams.** When a lobby is full, the website balances the teams using MMR from
the current ranked season, and the bot posts them together with a random map.
Unranked lobbies default to MMR Balance and avoid repeating the teams the
players had in their recent games (the last 2 games in 3v3, the last game in
2v2); the next-fairest split is used instead. The website keeps this history,
including matches that have started but not yet been confirmed. The
players are removed from any other lobbies they were waiting in and cannot join
another lobby until the match has been submitted or cancelled.

**Submitting the result.** After the game, one player runs `/submit` and
attaches a screenshot of the final scoreboard, taken on the Average Total tab
with the round orbs visible at the top. If the end screen was skipped, `/submit`
without an attachment opens a form for the round score and each player's
champion, without scoreboard statistics. Such a result needs the same
confirmations, and anyone in the match can later `/submit` the screenshot to
replace it, in which case the confirmations start again.

When a lobby fills, its post is deleted, since the "Match found" card replaces
it. `/submit` then tidies the rest of the matchmaking channel: the "Opened a
lobby" message and the "Match found" post are deleted, and the submission notice
disappears after a minute. Cancelling a match clears them in the same
way. This can be turned off with `CLEAN_MATCH_POST=false`.

**Checking the result.** The players are free to queue again as soon as the
result is submitted. With a screenshot, the website first reads only the round
score from the orbs, which takes well under a second, and the bot posts it with
the screenshot as a card. The orbs show the score from the point of view of the
team that took the screenshot, which is taken to be the submitter's team. The
players only confirm the round score; any player in the match can correct it
with the Round score button, which clears the existing confirmations (the
player who made the correction counts as the first new one).

Meanwhile the website reads the rest of the scoreboard: each player's name,
champion, score, damage, protection and disables. These are not confirmed by
the players. If the names show that the screenshot came from the other team,
the score is corrected and confirmation starts again. The result is logged once
`RESULT_CONFIRMATIONS` players (4 by default) have confirmed the score, at
least `RESULT_LOSER_CONFIRMATIONS` (2 by default) of them from the losing team,
and the scoreboard has been read. 2v2 matches use `RESULT_CONFIRMATIONS_2V2`
(3 by default) and `RESULT_LOSER_CONFIRMATIONS_2V2` (1 by default) instead.

Anything the reader was unsure of is logged with the match and listed for
administrators under "Logged matches to check" on the website's Pending page.
If a champion could not be read at all, the match is not logged automatically:
it stays on the Pending page with the confirmed score for an administrator to
finish. A result entered without a screenshot is checked in full by the
players, as before, with edit buttons for each player's champion.

Administrators can also log, correct or discard a result on the website's
Pending page while it is being confirmed, and edit or delete it on the Matches
page afterwards. The bot notices such changes within `REVIEW_POLL_SECONDS` (300
by default).

**After the result is logged.** Ranked games are recorded in the active season.
Unranked games are recorded in the permanent "Unranked" season, which never
changes MMR. Two Discord posts are handled by the website rather than the bot:

- The match log. The website posts each confirmed result (score, champions,
  MMR changes and statistics) when `DISCORD_MATCH_LOG_WEBHOOK_URL` is set in its
  `backend/.env`. The webhook is created under the channel's settings, in
  Integrations, then Webhooks. Editing a match on the website edits its post,
  and deleting the match deletes it. Discarded submissions are never posted.
- The live leaderboard. When `DISCORD_LEADERBOARD_WEBHOOK_URL` is set in the
  website's `backend/.env`, the website keeps a single leaderboard post in that
  channel up to date whenever a match is logged, edited or deleted.

Players whose Battlerite name differs from their name on the website should run
`/ign` once, so that their scoreboard rows are matched to them reliably.

## Changing settings

Administrators can change most of the bot's settings without restarting it,
with `/admin settings` and `/admin maps` in Discord or on the website's
Settings page: lobby timeouts and channels, the ping role and its timings,
result confirmations, tidying of match posts, the
linked and admin roles, and separate map pools for 2v2 and 3v3. The website
keeps the changed values and the bot reads them every minute, keeping a copy
in `state.json` for when the website cannot be reached. The values in `.env`
are the defaults that a reset returns to. The bot token, the API key and the
channel IDs used for posting can only be set in `.env`.

## Linking Discord accounts to players

A player runs `/link name:<website name>`. If no player with that name exists
yet, the request asks for a new player instead, and approving it creates the
player on the website and links them. Requests appear in `ADMIN_CHANNEL_ID` with
Approve and Deny buttons, and on the website's Pending page; either can be used.
Administrators can skip the request with `/admin link @member <name>`, which
also creates the player if needed, and remove a link with `/admin unlink
@member`.

When `LINKED_ROLE_ID` is set, linked members are given that role: immediately
when they are linked through the bot, and otherwise the first time they use the
bot. Unlinking with `/admin unlink` removes the role again, although members who
were given it by hand keep it. The bot needs the Manage Roles permission, with
its own role placed above that one.

The website accepts links only from the bot (using the shared `BOT_API_KEY`) or
from a signed-in administrator, so players cannot link themselves from a
browser.

## Commands

| Command | Available to | Purpose |
|---|---|---|
| `/join`, `/leave` | Everyone | Join or leave the lobby in the channel |
| `/lobby open`, `show`, `close` | Everyone; closing by the opener or an administrator | Open a lobby, move lobbies to the bottom, close a lobby |
| `/submit` | Players in the match | Upload the final scoreboard screenshot, or enter the result without one |
| `/link`, `/whoami`, `/ign` | Everyone | Link an account, check the link, set an in-game name |
| `/rank [member]`, `/leaderboard` | Everyone | Current-season statistics from the website |
| `/admin link`, `unlink`, `cancel-match`, `clear-lobby` | Administrators | Corrections and clean-up |
| `/admin settings show`, `set`, `reset` | Administrators | View or change the bot's settings |
| `/admin maps show`, `add`, `remove`, `reset` | Administrators | The 2v2 and 3v3 map pools |

Administrators are members with the Manage Server permission, or those listed
in `ADMIN_ROLE_IDS` or `ADMIN_USER_IDS`.

## Setup

The bot requires the BR Stats website running from the bot-integration branch
of [dot-iiris/Battlerite-Stat-Tracker](https://github.com/dot-iiris/Battlerite-Stat-Tracker/tree/bot-integration).
The main branch contains the website on its own, without the API the bot uses.

1. In the Discord Developer Portal, create an application, open its Bot page
   and reset the token. Invite the bot through OAuth2, URL Generator, with the
   `bot` and `applications.commands` scopes and the Send Messages, Embed Links
   and Read Message History permissions.
2. Run `start-bot.bat`. The first run creates `.env` and opens it. Fill in
   `DISCORD_TOKEN` and `GUILD_ID`; `BOT_API_KEY` must match the value in the
   website's `backend/.env`.
3. Run `start-bot.bat` again. The website must be running while the bot is in
   use; running it locally is described in the website's README.

Screenshot reading is performed by the website using local OCR and does not
require any external accounts. Lobbies and live matches are saved in
`state.json`, so restarting the bot keeps them.

## AI Disclaimer

This bot was written with the assistance of Claude, an AI model made by
Anthropic, used as a coding agent. The logic and calculations behind each
feature are understood and directed by the maintainer; the agent was used
mainly to find the relevant syntax in the languages and frameworks involved,
and the most efficient way to write that logic. All code is implemented under
the supervision of the maintainer.
