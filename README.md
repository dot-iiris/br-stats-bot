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

**Teams.** When a lobby is full, the website balances the teams using MMR from
the current ranked season, and the bot posts them together with a random map.
Unranked lobbies default to MMR Balance and never give the same players the
same teams as their previous game; the next-fairest split is used instead. The
players are removed from any other lobbies they were waiting in and cannot join
another lobby until the match has been submitted or cancelled.

**Submitting the result.** After the game, one player runs `/submit` and
attaches a screenshot of the final scoreboard, taken on the Average Total tab
with the round orbs visible at the top. If the end screen was skipped, `/submit`
without an attachment opens a form for the round score and each player's
champion, without scoreboard statistics. Such a result needs the same
confirmations, and anyone in the match can later `/submit` the screenshot to
replace it, in which case the confirmations start again.

`/submit` also tidies the matchmaking channel: the "Opened a lobby" message, the
filled lobby post and the "Match found" post are deleted, and the submission
notice disappears after a minute. Cancelling a match clears them in the same
way. This can be turned off with `CLEAN_MATCH_POST=false`.

**Checking the result.** The website reads the screenshot: the round score from
the orbs, and each player's name, champion, score, damage, protection and
disables. The bot posts what was read, with the screenshot, as a card for the
players to check. The players can queue again straight away.

Any player in the match can correct the round score, or any player's champion
and statistics, with the edit buttons on the card. A correction clears the
existing confirmations, and the player who made it counts as the first new one.
The result is logged once `RESULT_CONFIRMATIONS` players (4 by default) have
pressed Confirm, at least `RESULT_LOSER_CONFIRMATIONS` (2 by default) of them
from the losing team. 2v2 matches use `RESULT_CONFIRMATIONS_2V2` (3 by default)
and `RESULT_LOSER_CONFIRMATIONS_2V2` (1 by default) instead.

Once `OCR_TRUST_STREAK` screenshots in a row (5 by default) have been logged
without corrections, results are shown exactly as read, with only a Confirm
button. If something is wrong, the players do not confirm and ask an
administrator instead. Any correction, whether a player's edit or an
administrator changing a screenshot result on the website before or after it is
logged, starts the count again. Cards with a missing champion or score still
show the edit buttons.

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
Anthropic, used as a coding agent. The agent was used in the retrieval of
relevant functions within the project's codebase, and code is implemented under
the supervision of the maintainer.
