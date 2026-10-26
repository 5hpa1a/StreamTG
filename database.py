# database.py
import aiosqlite
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT UNIQUE NOT NULL,
                photo_id TEXT NOT NULL,
                caption TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                active_message_id INTEGER DEFAULT NULL,
                offline_since REAL DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                channel_slug TEXT NOT NULL,
                channel_url TEXT NOT NULL,
                is_live INTEGER DEFAULT 0,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                UNIQUE(player_id, platform)
            )
        """)
        # Таблица учета времени стримов по месяцам (YYYY-MM)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stream_stats (
                stream_id INTEGER NOT NULL,
                month_key TEXT NOT NULL,
                duration_seconds INTEGER DEFAULT 0,
                PRIMARY KEY (stream_id, month_key),
                FOREIGN KEY (stream_id) REFERENCES streams(id) ON DELETE CASCADE
            )
        """)
        # Таблица системных настроек (хранит месяц последнего отправленного отчета)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


async def create_player(nickname: str, photo_id: str, caption: str, entities_json: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO players (nickname, photo_id, caption, entities_json) VALUES (?, ?, ?, ?)",
            (nickname, photo_id, caption, entities_json)
        )
        await db.commit()
        return cursor.lastrowid


async def add_or_update_stream(player_id: int, platform: str, slug: str, url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO streams (player_id, platform, channel_slug, channel_url, is_live)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(player_id, platform) DO UPDATE SET
                channel_slug = excluded.channel_slug,
                channel_url = excluded.channel_url
        """, (player_id, platform, slug, url))
        await db.commit()


async def get_all_players():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM players ORDER BY nickname")
        return await cursor.fetchall()


async def get_player(player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM players WHERE id = ?", (player_id,))
        return await cursor.fetchone()


async def get_player_by_nickname(nickname: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM players WHERE LOWER(nickname) = LOWER(?)", (nickname,))
        return await cursor.fetchone()


async def get_player_streams(player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM streams WHERE player_id = ? ORDER BY platform", (player_id,))
        return await cursor.fetchall()


async def update_stream_status(stream_id: int, is_live: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE streams SET is_live = ? WHERE id = ?", (1 if is_live else 0, stream_id))
        await db.commit()


async def update_player_post_state(player_id: int, message_id: int | None, offline_since: float | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET active_message_id = ?, offline_since = ? WHERE id = ?",
            (message_id, offline_since, player_id)
        )
        await db.commit()


async def update_player_photo(player_id: int, photo_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET photo_id = ? WHERE id = ?", (photo_id, player_id))
        await db.commit()


async def update_player_text(player_id: int, caption: str, entities_json: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET caption = ?, entities_json = ? WHERE id = ?",
            (caption, entities_json, player_id)
        )
        await db.commit()


async def delete_stream(stream_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        await db.commit()


async def delete_player(player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM players WHERE id = ?", (player_id,))
        await db.commit()


# --- Функции для статистики ---
async def add_stream_duration(stream_id: int, month_key: str, seconds: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO stream_stats (stream_id, month_key, duration_seconds)
            VALUES (?, ?, ?)
            ON CONFLICT(stream_id, month_key) DO UPDATE SET
                duration_seconds = duration_seconds + excluded.duration_seconds
        """, (stream_id, month_key, seconds))
        await db.commit()


async def get_monthly_stats(month_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT 
                p.nickname,
                s.platform,
                COALESCE(st.duration_seconds, 0) AS total_seconds
            FROM streams s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN stream_stats st ON st.stream_id = s.id AND st.month_key = ?
            ORDER BY p.nickname ASC, s.platform ASC
        """, (month_key,))
        return await cursor.fetchall()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        await db.commit()