"""Small async client for the BR Stats website API."""
from __future__ import annotations

from typing import Any, Optional

import aiohttp


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class WebsiteApi:
    def __init__(self, base_url: str, bot_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {"X-Bot-Key": bot_key}
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=20))

    async def close(self):
        if self._session:
            await self._session.close()

    async def _req(self, method: str, path: str, **kw) -> Any:
        assert self._session, "WebsiteApi.start() was not called"
        try:
            async with self._session.request(method, f"{self.base}{path}", **kw) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                if r.status >= 400:
                    detail = (data or {}).get("detail") if isinstance(data, dict) else None
                    raise ApiError(r.status, str(detail or f"Website returned {r.status}"))
                return data
        except aiohttp.ClientError as e:
            raise ApiError(503, f"Can't reach the website ({e.__class__.__name__})") from e

    # --- settings admins can change (the website keeps them) ---
    async def settings_hello(self, defaults: dict, roles: list, channels: list) -> dict:
        return await self._req("POST", "/bot/settings/hello",
                               json={"defaults": defaults, "roles": roles, "channels": channels})

    async def settings(self) -> dict:
        return await self._req("GET", "/bot/settings")

    async def set_setting(self, key: str, value, by: str) -> dict:
        return await self._req("PUT", f"/bot/settings/{key}", json={"value": value, "by": by})

    async def reset_setting(self, key: str) -> dict:
        return await self._req("DELETE", f"/bot/settings/{key}")

    # --- players / linking ---
    async def player_by_discord(self, discord_id: int) -> Optional[dict]:
        try:
            return await self._req("GET", f"/players/by-discord/{discord_id}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    async def linked_discord(self) -> list:
        return await self._req("GET", "/players/linked-discord")

    async def players(self) -> list:
        return await self._req("GET", "/players")

    async def request_link(self, discord_id: int, discord_name: str, player_name: str) -> dict:
        return await self._req("POST", "/link-requests", json={
            "discord_id": str(discord_id), "discord_name": discord_name, "player_name": player_name,
        })

    async def decide_link(self, request_id: str, approve: bool, decided_by: str) -> dict:
        return await self._req("POST", f"/link-requests/{request_id}/decision",
                               json={"approve": approve, "decided_by": decided_by})

    async def set_discord(self, player_id: str, discord_id: Optional[int], discord_name: str = "") -> dict:
        return await self._req("PUT", f"/players/{player_id}/discord", json={
            "discord_id": str(discord_id) if discord_id else None, "discord_name": discord_name,
        })

    # --- matchmaking / matches ---
    async def balance(self, player_ids: list, fmt: str, mode: str, avoid_repeats: bool = False) -> dict:
        # No season_id: the website uses the active ranked season's MMR. avoid_repeats: the website
        # steers away from teams these players had in their recent games (logged or just launched).
        return await self._req("POST", "/matchmaking/balance",
                               json={"player_ids": player_ids, "format": fmt, "mode": mode,
                                     "avoid_repeats": avoid_repeats})

    async def record_lineup(self, code: str, fmt: str, team_a: list, team_b: list) -> None:
        """A match was launched with these teams (counts for repeat avoidance before it's logged)."""
        await self._req("POST", "/matchmaking/lineups",
                        json={"code": code, "format": fmt, "team_a": team_a, "team_b": team_b})

    async def forget_lineup(self, code: str) -> None:
        """A launched match was cancelled."""
        await self._req("DELETE", f"/matchmaking/lineups/{code}")

    async def reserve_match_code(self, fmt: str, ranked: bool) -> str:
        """The match's code from the website, e.g. S2-3v3-a1f9 (U-... for unranked)."""
        return (await self._req("POST", "/matches/reserve-code", json={"format": fmt, "ranked": ranked}))["code"]

    async def create_pending_match(self, payload: dict) -> dict:
        return await self._req("POST", "/pending-matches", json=payload)

    async def upload_screenshot(self, pending_id: str, data: bytes, filename: str, uploaded_by: str) -> dict:
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename)
        # reading the screenshot takes a few seconds on the website
        return await self._req("POST", f"/pending-matches/{pending_id}/screenshot", data=form,
                               params={"uploaded_by": uploaded_by}, timeout=aiohttp.ClientTimeout(total=120))

    async def pending_match(self, pending_id: str) -> dict:
        return await self._req("GET", f"/pending-matches/{pending_id}")

    async def complete_pending(self, pending_id: str, payload: dict) -> dict:
        """Log a reported match (the players confirmed it)."""
        return await self._req("POST", f"/pending-matches/{pending_id}/complete", json=payload)

    async def champions(self) -> list:
        return await self._req("GET", "/champions")

    async def set_ign(self, player_id: str, ign: str) -> dict:
        return await self._req("PUT", f"/players/{player_id}/ign", json={"ign": ign})

    # --- stats ---
    async def leaderboard(self) -> list:
        return await self._req("GET", "/stats/leaderboard")
