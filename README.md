# BR Stats Discord bot

Runs pick-up lobbies in Discord. The BR Stats website does everything else:
players, MMR, team balancing and match records.

## How a game goes

1. A channel can have several lobbies open at once (one per combination of
   settings), and a player can wait in more than one.
   `/lobby open` opens a new lobby with your choice of format (2v2/3v3), balance
   mode (Hybrid Balance, Role Synergy, MMR Balance) and ranked on/off.
   `/join` joins the channel's lobby when there's only one. With none, it opens one
   with the **same settings as that channel's last match** (if it started in the
   last `STICKY_SETTINGS_MINUTES`, default 30), otherwise the default
   **3v3 · Hybrid Balance · Ranked**. With several, use the lobby's **Join** button.
   `/leave` leaves every lobby you're in; a lobby's **Leave** button leaves just that
   one. `/lobby close` closes one you opened (admins: any), `/lobby show` moves the
   channel's lobbies to the bottom, and `/admin clear-lobby` removes them all.
   Lobbies always stay at the bottom of their channel: when anyone posts there,
   the bot reposts them underneath, oldest first.
   A lobby nobody joins or leaves for `LOBBY_TIMEOUT_MINUTES` (default 30) closes
   itself, and the players in it get a short ping that it timed out.
2. When a lobby is full, the website balances the teams using MMR from the
   current ranked season, and the bot posts them with a random map. Unranked
   lobbies default to MMR Balance, and never give the same players the same
   teams as their previous game (the next-fairest split is used instead). Its players
   are taken out of any other lobbies they were waiting in, and can't join lobbies
   again until the match is submitted (or cancelled).
3. After the game one player runs `/submit` and attaches a screenshot of the
   final scoreboard (the **Average Total** tab, with the round orbs visible at
   the top). Skipped the end screen? `/submit` with no attachment opens a form for
   the round score and every player's champion (no stats). That still needs the
   same confirmations, and anyone in the match can `/submit` the screenshot later
   to replace the typed result (the confirmations start again).
   `/submit` also tidies the matchmaking chat: the "Opened a lobby" message, the
   filled lobby post and the "Match found" post are deleted, and the submit
   confirmation disappears after a minute. Cancelling a match clears them the same
   way. Turn this off with `CLEAN_MATCH_POST=false`.
4. The website reads the screenshot: round score (orbs), each player's name,
   champion, score, damage, protection and disables. The bot posts what it read,
   with the screenshot, as a **check the result** card (players can queue again
   straight away).
5. The players check it. Any player in the match can fix the round score or any
   player's champion and stats with the ✏️ buttons; a fix clears the
   confirmations (the player who made it counts as the first one again). The
   result is logged once `RESULT_CONFIRMATIONS` players (default 4) press
   **Confirm**, at least `RESULT_LOSER_CONFIRMATIONS` (default 2) of them from the
   losing team. 2v2 uses `RESULT_CONFIRMATIONS_2V2` (default 3) and
   `RESULT_LOSER_CONFIRMATIONS_2V2` (default 1).
   Once `OCR_TRUST_STREAK` screenshots in a row (default 5) were logged with no
   corrections, results are shown exactly as read with only a **Confirm**
   button - if something's wrong, players don't confirm and ask an admin. Any
   correction (a player's fix, or an admin changing a screenshot result on the
   website, before or after it's logged) starts the count again. Cards with a
   missing champion or score still get the ✏️ buttons.
   Admins can still log, fix or discard a result on the website's **Pending**
   page while it's being confirmed, and edit or delete it on the Matches page
   afterwards. The bot notices within `REVIEW_POLL_SECONDS` (default 300).
   - The **website** then posts the confirmed result (score, champions, MMR
     changes, stats) to the match log channel. Set `DISCORD_MATCH_LOG_WEBHOOK_URL`
     in the website's `backend/.env` (channel settings → Integrations → Webhooks →
     New Webhook → Copy URL). Editing the match on the website edits the post;
     deleting the match deletes it. Discarded submissions are never posted.
   - The live leaderboard post is handled by the **website**, not the bot: set
     `DISCORD_LEADERBOARD_WEBHOOK_URL` in the website's `backend/.env` and it edits
     one post in that channel the moment a match is logged, edited or deleted.
   Ranked games go into the active season; unranked games go into the permanent
   "Unranked" season, which never changes MMR.

If someone's Battlerite name differs from their website name, they should run
`/ign` once so the screenshot rows match them reliably.

## Linking Discord to website players

- With `LINKED_ROLE_ID` set, linked members get that role: right away when linked through the bot,
  otherwise the first time they use the bot. Unlinking with `/admin unlink` takes it back off
  (members given the role by hand keep it). The bot needs Manage Roles, with its role above that one.

- A player runs `/link name:<their website name>`. If there's no player with that
  name yet, the request asks for a **new player** instead; approving it creates
  the player on the website and links them.
- The request appears in `ADMIN_CHANNEL_ID` with Approve/Deny buttons, and on the
  website's Pending page. Either one works.
- Admins can skip the request with `/admin link @member <name>` (also creates the
  player if needed) and remove one
  with `/admin unlink @member`.

The website only accepts links from the bot (secret `BOT_API_KEY`) or a logged-in
admin, so nobody can link themselves from a browser.

## Commands

| Command | Who | What |
|---|---|---|
| `/join`, `/leave` | everyone | Join/leave the lobby here |
| `/lobby open` / `show` / `close` | everyone / opener or admin | Open / move to bottom / close the lobby |
| `/submit` | players in the match | Upload the final scoreboard screenshot (or submit without one for manual entry) |
| `/link`, `/whoami`, `/ign` | everyone | Link your account / check it / set your in-game name |
| `/rank [member]`, `/leaderboard` | everyone | Current-season stats from the website |
| `/admin link`, `unlink`, `cancel-match`, `clear-lobby` | admins | Fix-ups |

Admins = anyone with Manage Server, or listed in `ADMIN_ROLE_IDS` / `ADMIN_USER_IDS`.

## Setup

The bot needs the BR Stats website running from the **`bot-integration`** branch of
[dot-iiris/Battlerite-Stat-Tracker](https://github.com/dot-iiris/Battlerite-Stat-Tracker/tree/bot-integration)
(`main` is the website on its own, without the bot's API).

1. Discord Developer Portal → New Application → **Bot** → Reset Token.
   Invite it with **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`;
   permissions Send Messages, Embed Links, Read Message History.
2. Double-click `start-bot.bat`. The first run creates `.env` and opens it.
   Fill in `DISCORD_TOKEN` and `GUILD_ID`. `BOT_API_KEY` must match the
   website's `backend/.env`.
3. Run `start-bot.bat` again. Keep the website running (start-local.bat) while testing.

Screenshot reading runs on the website (local OCR - no extra accounts). Lobbies and live matches are saved in `state.json`, so restarting the bot keeps them.

## AI assistance

Much of this bot was written with the help of Claude, an AI model made by
Anthropic, used as a coding agent at the maintainer's direction. Commits are
authored by the maintainer and don't record that assistance individually. As
with any software, it may have bugs and is provided as is.
