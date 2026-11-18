"""SQLite WAL storage: users, hashed API keys, daily usage rollups."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  telegram_id INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  chat_model TEXT NOT NULL DEFAULT '',
  daily_requests INTEGER NOT NULL DEFAULT 300,
  daily_usd REAL NOT NULL DEFAULT 2.0,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  created REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  model TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  usd REAL NOT NULL DEFAULT 0,
  UNIQUE(key_id, day, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_key_day ON usage(key_id, day);
"""


def billing_day(timestamp: float | None = None) -> str:
    """The billing day key; the day flips at RESET_HOUR_UTC, not midnight."""
    moment = (timestamp if timestamp is not None else time.time())
    moment -= config.RESET_HOUR_UTC * 3600
    return time.strftime("%Y-%m-%d", time.gmtime(moment))


def hash_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path | None = None, *,
                 daily_requests: int | None = None, daily_usd: float | None = None):
        path = Path(path) if path else config.DATABASE
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self.daily_requests = (config.DEFAULT_DAILY_REQUESTS if daily_requests is None
                               else daily_requests)
        self.daily_usd = config.DEFAULT_DAILY_USD if daily_usd is None else daily_usd

    def close(self) -> None:
        self.db.close()

    # -- users -----------------------------------------------------------
    def user(self, telegram_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()

    def ensure_user(self, telegram_id: int, username: str = "",
                    is_admin: bool | None = None) -> sqlite3.Row:
        existing = self.user(telegram_id)
        if existing:
            if username and username != existing["username"]:
                with self.lock:
                    self.db.execute("UPDATE users SET username=? WHERE telegram_id=?",
                                    (username, telegram_id))
                    self.db.commit()
            if is_admin and not existing["is_admin"]:
                with self.lock:
                    self.db.execute("UPDATE users SET is_admin=1 WHERE telegram_id=?",
                                    (telegram_id,))
                    self.db.commit()
            return self.user(telegram_id)
        with self.lock:
            self.db.execute(
                "INSERT INTO users(telegram_id, username, daily_requests, daily_usd,"
                " is_admin, created) VALUES (?,?,?,?,?,?)",
                (telegram_id, username, self.daily_requests, self.daily_usd,
                 1 if is_admin else 0, time.time()))
            self.db.commit()
        return self.user(telegram_id)

    def set_quota(self, telegram_id: int, *, requests: int | None = None,
                  usd: float | None = None) -> None:
        with self.lock:
            if requests is not None:
                self.db.execute("UPDATE users SET daily_requests=? WHERE telegram_id=?",
                                (max(0, int(requests)), telegram_id))
            if usd is not None:
                self.db.execute("UPDATE users SET daily_usd=? WHERE telegram_id=?",
                                (max(0.0, float(usd)), telegram_id))
            self.db.commit()

    def set_chat_model(self, telegram_id: int, model: str) -> None:
        with self.lock:
            self.db.execute("UPDATE users SET chat_model=? WHERE telegram_id=?",
                            (model, telegram_id))
            self.db.commit()

    # -- keys ------------------------------------------------------------
    def issue_key(self, telegram_id: int, name: str = "") -> tuple[str, str]:
        secret = "sk-" + secrets.token_urlsafe(32)
        prefix = secret[:12]
        with self.lock:
            self.db.execute(
                "INSERT INTO api_keys(telegram_id, name, prefix, key_hash, created)"
                " VALUES (?,?,?,?,?)",
                (telegram_id, name, prefix, hash_key(secret), time.time()))
            self.db.commit()
        return secret, prefix

    def verify_key(self, secret: str) -> sqlite3.Row | None:
        with self.lock:
            row = self.db.execute(
                "SELECT k.*, u.telegram_id AS owner_telegram_id, u.daily_requests,"
                " u.daily_usd, u.is_admin"
                " FROM api_keys k JOIN users u ON u.telegram_id = k.telegram_id"
                " WHERE k.key_hash=? AND k.revoked=0", (hash_key(secret),)).fetchone()
        return row

    def active_key(self, telegram_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM api_keys WHERE telegram_id=? AND revoked=0"
                " ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone()

    def revoke_by_prefix(self, prefix: str) -> int:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE api_keys SET revoked=1 WHERE prefix=? AND revoked=0", (prefix,))
            self.db.commit()
            return cursor.rowcount

    # -- usage -----------------------------------------------------------
    def record_usage(self, key_id: int, model: str, *, prompt_tokens: int,
                     completion_tokens: int, usd: float) -> None:
        day = billing_day()
        with self.lock:
            self.db.execute(
                "INSERT INTO usage(key_id, day, model, requests, prompt_tokens,"
                " completion_tokens, usd) VALUES (?,?,?,1,?,?,?)"
                " ON CONFLICT(key_id, day, model) DO UPDATE SET"
                " requests=requests+1, prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
                " completion_tokens=completion_tokens+excluded.completion_tokens,"
                " usd=usd+excluded.usd",
                (key_id, day, model, int(prompt_tokens), int(completion_tokens),
                 max(0.0, float(usd))))
            self.db.commit()

    def today_totals(self, key_id: int) -> dict[str, float]:
        day = billing_day()
        with self.lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(requests),0) requests,"
                " COALESCE(SUM(usd),0) usd, COALESCE(SUM(prompt_tokens),0) pt,"
                " COALESCE(SUM(completion_tokens),0) ct"
                " FROM usage WHERE key_id=? AND day=?", (key_id, day)).fetchone()
        return dict(row)


    def today_by_model(self, key_id: int, limit: int = 3) -> list[dict]:
        day = billing_day()
        with self.lock:
            rows = self.db.execute(
                "SELECT model, SUM(requests) requests, SUM(usd) usd FROM usage"
                " WHERE key_id=? AND day=? GROUP BY model"
                " ORDER BY usd DESC, requests DESC LIMIT ?",
                (key_id, day, max(1, int(limit)))).fetchall()
        return [dict(row) for row in rows]
    def reset_in_seconds(self, now: float | None = None) -> int:
        """Seconds until the next RESET_HOUR_UTC boundary."""
        import calendar
        now = now if now is not None else time.time()
        shifted = time.gmtime(now - config.RESET_HOUR_UTC * 3600)
        boundary = calendar.timegm(shifted[:3] + (0, 0, 0)) + 86400 + config.RESET_HOUR_UTC * 3600
        return max(0, int(boundary - now))
