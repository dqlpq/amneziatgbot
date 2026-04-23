import aiosqlite
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

MAX_PROFILES_PER_USER = 3


class Database:
    def __init__(self, db_path: str, encryption_key: str):
        self.db_path = db_path
        self.fernet = Fernet(encryption_key.encode("utf-8"))
        self._conn: Optional[aiosqlite.Connection] = None

    # ─────────────────── Шифрование ───────────────────────────────

    def _encrypt(self, data: str | None) -> str | None:
        if not data:
            return data
        return self.fernet.encrypt(data.encode("utf-8")).decode("utf-8")

    def _decrypt(self, data: str | None) -> str | None:
        if not data:
            return data
        try:
            return self.fernet.decrypt(data.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return data  # старые незашифрованные данные

    # ─────────────────── Инициализация и миграции ─────────────────

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        # ── Новые таблицы ───────────────────────────────────────
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                banned      INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS vpn_profiles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                vpn_name     TEXT    NOT NULL UNIQUE,
                peer_id      TEXT,
                raw_response TEXT,
                last_ip      TEXT,
                disabled     INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_tgid ON vpn_profiles(telegram_id)"
        )

        await self._conn.commit()

        # ── Миграция с прежней схемы ────────────────────────────
        await self._migrate_from_old_schema()

        # ── Миграция: дошифровать старые данные в vpn_profiles ──
        await self._encrypt_plain_data()

        logger.info("Database initialized: %s", self.db_path)

    async def _migrate_from_old_schema(self):
        """Перенос данных из vpn_users → users + vpn_profiles."""
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vpn_users'"
        ) as cur:
            if not await cur.fetchone():
                return  # старой таблицы нет — миграция не нужна

        logger.info("Обнаружена старая таблица vpn_users, выполняю миграцию…")

        async with self._conn.execute("SELECT * FROM vpn_users") as cur:
            rows = await cur.fetchall()

        migrated = 0
        for row in rows:
            tg_id = row["telegram_id"]
            vpn_name = row["vpn_name"]
            peer_id = row["peer_id"]
            raw_resp = row["raw_response"]
            banned = row["banned"] if "banned" in row.keys() else 0
            last_ip_val = row["last_ip"] if "last_ip" in row.keys() else None
            created_at = row["created_at"] if "created_at" in row.keys() else None

            try:
                # Вставляем в users
                await self._conn.execute(
                    "INSERT OR IGNORE INTO users (telegram_id, banned, created_at) VALUES (?, ?, ?)",
                    (tg_id, banned, created_at),
                )
                # Шифруем поля если нужно
                enc_peer = peer_id if (peer_id and peer_id.startswith("gAAAAA")) else self._encrypt(peer_id)
                enc_raw = raw_resp if (raw_resp and raw_resp.startswith("gAAAAA")) else self._encrypt(raw_resp)
                enc_ip = last_ip_val if (last_ip_val and last_ip_val.startswith("gAAAAA")) else self._encrypt(last_ip_val)

                # Вставляем профиль
                await self._conn.execute(
                    """INSERT OR IGNORE INTO vpn_profiles
                       (telegram_id, vpn_name, peer_id, raw_response, last_ip, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tg_id, vpn_name, enc_peer, enc_raw, enc_ip, created_at),
                )
                migrated += 1
            except Exception as e:
                logger.warning("Миграция строки tg=%d: %s", tg_id, e)

        await self._conn.commit()

        # Переименовываем старую таблицу чтобы не мигрировать повторно
        await self._conn.execute("ALTER TABLE vpn_users RENAME TO vpn_users_migrated")
        await self._conn.commit()
        logger.info("Миграция завершена: перенесено %d записей.", migrated)

    async def _encrypt_plain_data(self):
        """Дошифровывает незашифрованные поля в vpn_profiles."""
        async with self._conn.execute(
            "SELECT id, peer_id, raw_response, last_ip FROM vpn_profiles"
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            pid, raw, lip = row["peer_id"], row["raw_response"], row["last_ip"]
            needs_update = False
            if pid and not pid.startswith("gAAAAA"):
                pid = self._encrypt(pid)
                needs_update = True
            if raw and not raw.startswith("gAAAAA"):
                raw = self._encrypt(raw)
                needs_update = True
            if lip and not lip.startswith("gAAAAA"):
                lip = self._encrypt(lip)
                needs_update = True
            if needs_update:
                await self._conn.execute(
                    "UPDATE vpn_profiles SET peer_id=?, raw_response=?, last_ip=? WHERE id=?",
                    (pid, raw, lip, row["id"]),
                )
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            logger.info("Database connection closed.")

    # ─────────────────── Вспомогательное ──────────────────────────

    def _profile_row_to_dict(self, row: aiosqlite.Row) -> dict:
        d = dict(row)
        d["peer_id"] = self._decrypt(d.get("peer_id"))
        d["raw_response"] = self._decrypt(d.get("raw_response"))
        d["last_ip"] = self._decrypt(d.get("last_ip"))
        d["disabled"] = bool(d.get("disabled", 0))
        return d

    # ─────────────────── Пользователи ─────────────────────────────

    async def ensure_user(self, telegram_id: int) -> None:
        """Создаёт запись в users если не существует."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,)
        )
        await self._conn.commit()

    async def get_user_banned(self, telegram_id: int) -> bool:
        async with self._conn.execute(
            "SELECT banned FROM users WHERE telegram_id=?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row["banned"]) if row else False

    async def set_user_banned(self, telegram_id: int, banned: bool) -> None:
        await self._conn.execute(
            "UPDATE users SET banned=? WHERE telegram_id=?",
            (1 if banned else 0, telegram_id),
        )
        await self._conn.commit()

    async def get_all_telegram_ids(self) -> list[int]:
        async with self._conn.execute("SELECT telegram_id FROM users") as cur:
            return [r[0] for r in await cur.fetchall()]

    # ─────────────────── Профили ──────────────────────────────────

    async def get_profiles(self, telegram_id: int) -> list[dict]:
        """Все профили пользователя."""
        async with self._conn.execute(
            "SELECT * FROM vpn_profiles WHERE telegram_id=? ORDER BY created_at",
            (telegram_id,),
        ) as cur:
            return [self._profile_row_to_dict(r) for r in await cur.fetchall()]

    async def get_profile_by_id(self, profile_id: int) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM vpn_profiles WHERE id=?", (profile_id,)
        ) as cur:
            row = await cur.fetchone()
            return self._profile_row_to_dict(row) if row else None

    async def get_profile_by_name(self, vpn_name: str) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM vpn_profiles WHERE vpn_name=?", (vpn_name,)
        ) as cur:
            row = await cur.fetchone()
            return self._profile_row_to_dict(row) if row else None

    async def count_profiles(self, telegram_id: int) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM vpn_profiles WHERE telegram_id=?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def can_create_profile(self, telegram_id: int) -> bool:
        return await self.count_profiles(telegram_id) < MAX_PROFILES_PER_USER

    async def is_vpn_name_taken(self, vpn_name: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM vpn_profiles WHERE vpn_name=?", (vpn_name,)
        ) as cur:
            return await cur.fetchone() is not None

    async def add_profile(self, telegram_id: int, vpn_name: str,
                          peer_id: Optional[str], raw_response: str) -> int:
        """Добавляет профиль, возвращает его id. Создаёт users-запись если нет."""
        await self.ensure_user(telegram_id)
        cur = await self._conn.execute(
            """INSERT INTO vpn_profiles (telegram_id, vpn_name, peer_id, raw_response)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, vpn_name, self._encrypt(peer_id), self._encrypt(raw_response)),
        )
        await self._conn.commit()
        logger.info("Added profile: tg=%d name=%s id=%d", telegram_id, vpn_name, cur.lastrowid)
        return cur.lastrowid

    async def delete_profile(self, profile_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM vpn_profiles WHERE id=?", (profile_id,)
        )
        await self._conn.commit()
        # Удаляем запись users если профилей не осталось
        async with self._conn.execute(
            """SELECT telegram_id FROM vpn_profiles
               WHERE telegram_id=(SELECT telegram_id FROM vpn_profiles WHERE id=?)
               LIMIT 1""",
            (profile_id,)
        ) as check:
            pass  # profile уже удалён, не найдём
        return cur.rowcount > 0

    async def delete_profile_by_name(self, vpn_name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM vpn_profiles WHERE vpn_name=?", (vpn_name,)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_all_profiles(self, telegram_id: int) -> int:
        cur = await self._conn.execute(
            "DELETE FROM vpn_profiles WHERE telegram_id=?", (telegram_id,)
        )
        await self._conn.commit()
        return cur.rowcount

    async def set_profile_disabled(self, profile_id: int, disabled: bool) -> None:
        await self._conn.execute(
            "UPDATE vpn_profiles SET disabled=? WHERE id=?",
            (1 if disabled else 0, profile_id),
        )
        await self._conn.commit()

    async def set_last_ip(self, profile_id: int, ip: str) -> None:
        await self._conn.execute(
            "UPDATE vpn_profiles SET last_ip=? WHERE id=?",
            (self._encrypt(ip), profile_id),
        )
        await self._conn.commit()

    # ─────────────────── Сводные запросы ──────────────────────────

    async def get_all_users_with_profiles(self) -> list[dict]:
        """
        Возвращает список пользователей с вложенным списком профилей.
        Формат: [{telegram_id, banned, created_at, profiles: [...]}, ...]
        """
        async with self._conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ) as cur:
            user_rows = await cur.fetchall()

        result = []
        for u in user_rows:
            profiles = await self.get_profiles(u["telegram_id"])
            result.append({
                "telegram_id": u["telegram_id"],
                "banned": bool(u["banned"]),
                "created_at": u["created_at"],
                "profiles": profiles,
            })
        return result

    async def get_all_profiles(self) -> list[dict]:
        """Все профили всех пользователей (для CSV-экспорта и т.п.)."""
        async with self._conn.execute(
            "SELECT * FROM vpn_profiles ORDER BY created_at DESC"
        ) as cur:
            return [self._profile_row_to_dict(r) for r in await cur.fetchall()]

    async def search_users(self, query: str) -> list[dict]:
        """
        Поиск по имени профиля (подстрока) или telegram_id.
        Возвращает список пользователей (с профилями).
        """
        q = f"%{query.lower()}%"
        async with self._conn.execute(
            """SELECT DISTINCT u.telegram_id, u.banned, u.created_at
               FROM users u
               LEFT JOIN vpn_profiles p ON p.telegram_id = u.telegram_id
               WHERE LOWER(p.vpn_name) LIKE ?
                  OR CAST(u.telegram_id AS TEXT) = ?
               ORDER BY u.created_at DESC""",
            (q, query),
        ) as cur:
            user_rows = await cur.fetchall()

        result = []
        for u in user_rows:
            profiles = await self.get_profiles(u["telegram_id"])
            result.append({
                "telegram_id": u["telegram_id"],
                "banned": bool(u["banned"]),
                "created_at": u["created_at"],
                "profiles": profiles,
            })
        return result

    # ─────────────────── Legacy-совместимость ─────────────────────
    # (для BannedUserMiddleware и мест, где ожидается единственный профиль)

    async def get_user(self, telegram_id: int) -> Optional[dict]:
        """
        Возвращает dict с полями: telegram_id, banned, profiles.
        Если у пользователя нет профилей — возвращает None.
        """
        async with self._conn.execute(
            "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        profiles = await self.get_profiles(telegram_id)
        return {
            "telegram_id": telegram_id,
            "banned": bool(row["banned"]),
            "created_at": row["created_at"],
            "profiles": profiles,
        }