# checkers.py
import re
import json
import logging
import aiohttp
from curl_cffi.requests import AsyncSession
from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET


class StreamChecker:
    def __init__(self):
        self.twitch_token: str | None = None

    @staticmethod
    def parse_channel_input(platform: str, raw_input: str) -> tuple[str, str]:
        """Преобразует введенную ссылку или никнейм в (slug, full_url)."""
        raw = raw_input.strip().rstrip("/")

        if platform == "twitch":
            slug = raw.split("/")[-1].lstrip("@").lower()
            return slug, f"https://www.twitch.tv/{slug}"

        elif platform == "kick":
            slug = raw.split("/")[-1].lstrip("@").lower()
            return slug, f"https://kick.com/{slug}"

        elif platform == "youtube":
            # Убираем суффиксы /live, /streams, /videos если скопировали полную ссылку
            raw = re.sub(r"/(live|streams|videos|featured)$", "", raw)
            if "youtube.com/" in raw:
                slug = raw.split("youtube.com/")[-1]
                url = f"https://www.youtube.com/{slug}"
            else:
                slug = raw if raw.startswith("@") or raw.startswith("channel/") else f"@{raw}"
                url = f"https://www.youtube.com/{slug}"
            return slug, url

        raise ValueError(f"Неизвестная платформа: {platform}")

    async def get_twitch_token(self, session: aiohttp.ClientSession) -> str | None:
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        try:
            async with session.post(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.twitch_token = data.get("access_token")
                    return self.twitch_token
                logging.error(f"Ошибка получения токена Twitch: {resp.status}")
        except Exception as e:
            logging.error(f"Исключение при получении токена Twitch: {e}")
        return None

    async def check_twitch(self, slug: str) -> bool:
        if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
            logging.warning("Twitch ключи не заданы в config.py")
            return False

        async with aiohttp.ClientSession() as session:
            if not self.twitch_token:
                await self.get_twitch_token(session)
            if not self.twitch_token:
                return False

            url = f"https://api.twitch.tv/helix/streams?user_login={slug}"
            headers = {
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {self.twitch_token}"
            }
            try:
                async with session.get(url, headers=headers, timeout=10) as resp:
                    if resp.status == 401:
                        # Токен истек, обновляем
                        await self.get_twitch_token(session)
                        headers["Authorization"] = f"Bearer {self.twitch_token}"
                        async with session.get(url, headers=headers, timeout=10) as retry_resp:
                            data = await retry_resp.json()
                            return bool(data.get("data"))
                    elif resp.status == 200:
                        data = await resp.json()
                        streams = data.get("data", [])
                        return len(streams) > 0 and streams[0].get("type") == "live"
            except Exception as e:
                logging.error(f"Ошибка проверки Twitch ({slug}): {e}")
        return False

    async def check_kick(self, slug: str) -> bool:
        url = f"https://kick.com/api/v2/channels/{slug}"
        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    livestream = data.get("livestream")
                    return livestream is not None and livestream.get("is_live", True)
        except Exception as e:
            logging.error(f"Ошибка проверки Kick ({slug}): {e}")
        return False

    async def check_youtube(self, channel_url: str) -> bool:
        live_url = f"{channel_url.rstrip('/')}/live"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9"
        }
        cookies = {"CONSENT": "YES+cb.20230531-07-p0.en+FX+908"}

        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.get(live_url, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        return False
                    html = await resp.text()

                    # Ищем объект ytInitialPlayerResponse на странице /live
                    match = re.search(r"var ytInitialPlayerResponse\s*=\s*(\{.+?\});", html)
                    if not match:
                        return False

                    player_data = json.loads(match.group(1))
                    video_details = player_data.get("videoDetails", {})
                    microformat = (
                        player_data.get("microformat", {})
                        .get("playerMicroformatRenderer", {})
                        .get("liveBroadcastDetails", {})
                    )

                    # Проверяем, что это именно активный эфир, а не запланированный анонс
                    is_live = video_details.get("isLive") is True
                    is_live_now = microformat.get("isLiveNow") is True
                    return is_live and is_live_now
        except Exception as e:
            logging.error(f"Ошибка проверки YouTube ({channel_url}): {e}")
        return False

    async def is_stream_live(self, platform: str, slug: str, url: str) -> bool:
        if platform == "twitch":
            return await self.check_twitch(slug)
        elif platform == "kick":
            return await self.check_kick(slug)
        elif platform == "youtube":
            return await self.check_youtube(url)
        return False