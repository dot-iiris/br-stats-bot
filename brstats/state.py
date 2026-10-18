"""Lobbies and live matches, saved to state.json so a restart doesn't lose them."""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

TEAM_SIZE = {"2v2": 2, "3v3": 3}
MODES = {"hybrid": "Hybrid Balance", "synergy": "Role Synergy", "mmr": "MMR Balance"}


@dataclass
class Member:
    discord_id: int
    player_id: str
    name: str


def new_lobby_id() -> str:
    """Random lobby id - never all digits, so it can't be mistaken for an old-style button (a channel id)."""
    while True:
        lid = secrets.token_hex(3)
        if not lid.isdigit():
            return lid


@dataclass
class Lobby:
    channel_id: int
    opener_id: int
    id: str = field(default_factory=new_lobby_id)   # a channel can have several lobbies
    created: float = field(default_factory=time.time)
    format: str = "3v3"
    mode: str = "hybrid"
    ranked: bool = True
    members: List[Member] = field(default_factory=list)
    message_id: Optional[int] = None
    opened_message_id: Optional[int] = None   # the "Opened a ... lobby" announcement
    last_activity: float = field(default_factory=time.time)  # last open/join/leave (for the timeout)
    ping_message_ids: List[int] = field(default_factory=list)  # role pings about this lobby (tidied away with it)
    last_spot_ping: float = 0                                  # when the role was last told it needs one more

    @property
    def size(self) -> int:
        return TEAM_SIZE[self.format] * 2

    def has(self, discord_id: int) -> bool:
        return any(m.discord_id == discord_id for m in self.members)

    def same_settings(self, fmt: str, mode: str, ranked: bool) -> bool:
        return (self.format, self.mode, self.ranked) == (fmt, mode, ranked)


@dataclass
class Match:
    id: str
    channel_id: int
    format: str
    mode: str
    ranked: bool
    map: str
    team_a: List[dict]          # [{discord_id, player_id, name, mmr}]
    team_b: List[dict]
    team_a_mmr: float = 0
    team_b_mmr: float = 0
    win_probability_a: float = 50
    status: str = "live"        # live -> review (players confirming) -> (logged | rejected), or cancelled
    message_id: Optional[int] = None
    report: Optional[dict] = None  # {by_name, ...}
    pending_id: Optional[str] = None  # website pending-match id while under admin review
    history_message_id: Optional[int] = None  # review/result post in the match history channel
    lobby_channel_id: Optional[int] = None    # where the lobby was (for tidying up)
    cleanup_message_ids: List[int] = field(default_factory=list)  # lobby posts to delete on submit
    # result check after /submit: what the screenshot said (players can fix it) and who confirmed it
    draft: Optional[dict] = None   # {a_rounds, b_rounds, players: {pid: {name, discord_id, champion, team, stats}}, warnings, edits}
    confirms: List[int] = field(default_factory=list)   # discord ids that confirmed the current draft
    result_message_id: Optional[int] = None
    has_screenshot: bool = False

    def team_of(self, discord_id: int) -> Optional[str]:
        if any(p["discord_id"] == discord_id for p in self.team_a):
            return "A"
        if any(p["discord_id"] == discord_id for p in self.team_b):
            return "B"
        return None

    def all_ids(self) -> List[int]:
        return [p["discord_id"] for p in self.team_a + self.team_b]


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lobbies: Dict[str, Lobby] = {}   # lobby id -> open lobby (several per channel allowed)
        self.matches: Dict[str, Match] = {}   # match id -> live match
        self.reviews: Dict[str, Match] = {}   # website pending id -> submitted match the players are confirming
        self.last_settings: Dict[str, dict] = {}  # channel id -> {format, mode, ranked, at}
        self.roled: List[int] = []   # members the bot gave the linked-player role to
        self.recent_teams: List[dict] = []   # last matches' teams: {"teams": [[player ids], [player ids]], "at"}
        self.last_filled: Dict[str, float] = {}   # format -> when a lobby of it last filled (any channel)
        self.last_open_ping: float = 0            # when the role was last pinged about a new lobby
        self.settings: Dict[str, object] = {}     # settings admins changed (from the website), used offline too
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for l in data.get("lobbies", []):
            l["members"] = [Member(**m) for m in l.get("members", [])]
            lobby = Lobby(**l)        # lobbies saved by older versions get an id here
            self.lobbies[lobby.id] = lobby
        for m in data.get("matches", []):
            match = Match(**m)
            self.matches[match.id] = match
        for m in data.get("reviews", []):
            match = Match(**m)
            if match.pending_id:
                self.reviews[match.pending_id] = match
        self.last_settings = data.get("last_settings") or {}
        self.roled = [int(x) for x in data.get("roled") or []]
        self.recent_teams = data.get("recent_teams") or []
        self.last_filled = data.get("last_filled") or {}
        self.last_open_ping = data.get("last_open_ping") or 0
        self.settings = data.get("settings") or {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "lobbies": [asdict(l) for l in self.lobbies.values()],
                "matches": [asdict(m) for m in self.matches.values()],
                "reviews": [asdict(m) for m in self.reviews.values()],
                "last_settings": self.last_settings,
                "roled": self.roled,
                "recent_teams": self.recent_teams,
                "last_filled": self.last_filled,
                "last_open_ping": self.last_open_ping,
                "settings": self.settings,
            }, f, indent=2)
        os.replace(tmp, self.path)

    def busy_reason(self, discord_id: int) -> Optional[str]:
        """Why this player can't join a lobby right now (None if they can). Being in other
        lobbies is fine - a player can queue in several at once."""
        for m in self.matches.values():
            if discord_id in m.all_ids():
                return f"you're still in match #{m.id} - submit or cancel it first"
        return None

    def lobbies_in(self, channel_id: int) -> List[Lobby]:
        """A channel's open lobbies, oldest first (the order they sit at the bottom of the chat)."""
        return sorted((l for l in self.lobbies.values() if l.channel_id == channel_id), key=lambda l: l.created)

    def remember_teams(self, team_a: List[str], team_b: List[str]):
        self.recent_teams = (self.recent_teams + [{"teams": [team_a, team_b], "at": time.time()}])[-50:]

    def forget_teams(self, team_a: List[str], team_b: List[str]):
        """A cancelled match was never played - it doesn't count as anyone's last game."""
        for i in range(len(self.recent_teams) - 1, -1, -1):
            if self.recent_teams[i]["teams"] == [team_a, team_b]:
                del self.recent_teams[i]
                return

    def teams_to_avoid(self, player_ids: List[str]) -> List[List[str]]:
        """Teams from each of these players' previous match that could happen again with this
        line-up - so the same players don't get the same teams twice in a row."""
        ids, avoid = set(player_ids), []
        for pid in ids:
            last = next((r for r in reversed(self.recent_teams) if any(pid in t for t in r["teams"])), None)
            for t in (last or {}).get("teams", []):
                if set(t) <= ids and t not in avoid:
                    avoid.append(t)
        return avoid

    def lobbies_of(self, discord_id: int) -> List[Lobby]:
        return [l for l in self.lobbies.values() if l.has(discord_id)]

    def remember_settings(self, channel_id: int, fmt: str, mode: str, ranked: bool):
        self.last_settings[str(channel_id)] = {"format": fmt, "mode": mode, "ranked": ranked, "at": time.time()}

    def recent_settings(self, channel_id: int, max_age_minutes: float) -> Optional[dict]:
        """Settings of this channel's last match, if one started within max_age_minutes."""
        s = self.last_settings.get(str(channel_id))
        if s and time.time() - s.get("at", 0) <= max_age_minutes * 60:
            return s
        return None

    def review_by_match(self, match_id: str) -> Optional[Match]:
        return next((m for m in self.reviews.values() if m.id == match_id), None)

    @staticmethod
    def new_match_id() -> str:
        return secrets.token_hex(2)   # only used if the website can't hand out a code

    def norm_match_id(self, text: str) -> str:
        """'#s2-3V3-A1F9' -> 'S2-3v3-a1f9'; just 'a1f9' finds the live match ending in it."""
        t = (text or "").strip().lstrip("#")
        parts = t.split("-")
        if len(parts) == 3:
            return f"{parts[0].upper()}-{parts[1].lower()}-{parts[2].lower()}"
        t = t.lower()
        ids = list(self.matches) + [m.id for m in self.reviews.values()]
        return next((i for i in ids if i == t or i.endswith("-" + t)), t)
