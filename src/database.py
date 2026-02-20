"""
database.py
SQLite を使ったメール処理履歴・API ログ・通知履歴の永続化モジュール。
aiosqlite を使用して非同期で DB 操作を行う。
"""

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path

    async def init_db(self) -> None:
        """テーブルを作成する。data/ ディレクトリが存在しない場合は自動生成する。"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS emails (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id   TEXT UNIQUE NOT NULL,
                    sender       TEXT,
                    subject      TEXT,
                    body_preview TEXT,
                    category     TEXT,
                    reply_draft  TEXT,
                    status       TEXT DEFAULT 'pending',
                    language     TEXT DEFAULT 'ja',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    service     TEXT NOT NULL,
                    endpoint    TEXT NOT NULL,
                    tokens_used INTEGER DEFAULT 0,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    type       TEXT NOT NULL,
                    content    TEXT,
                    read       INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            await db.commit()

        logger.info(f"DB 初期化完了: {self._db_path}")

    async def save_email(
        self,
        message_id: str,
        sender: str,
        subject: str,
        body_preview: str,
        category: str,
        reply_draft: str = "",
        language: str = "ja",
    ) -> None:
        """メールを保存する。message_id が重複する場合は無視する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO emails
                    (message_id, sender, subject, body_preview, category,
                     reply_draft, status, language, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (message_id, sender, subject, body_preview, category,
                 reply_draft, language, now, now),
            )
            await db.commit()

    async def update_email_status(self, message_id: str, status: str) -> None:
        """emails テーブルの status と updated_at を更新する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE emails SET status=?, updated_at=? WHERE message_id=?",
                (status, now, message_id),
            )
            await db.commit()

    async def update_email_draft(self, message_id: str, draft: str) -> None:
        """emails テーブルの reply_draft と updated_at を更新する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE emails SET reply_draft=?, updated_at=? WHERE message_id=?",
                (draft, now, message_id),
            )
            await db.commit()

    async def get_emails(
        self,
        status: str | None = None,
        date_str: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        メール一覧を返す。
        status: 絞り込むステータス（None なら全件）
        date_str: "YYYY-MM-DD" 形式の日付（None なら全件）
        """
        conditions = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if date_str:
            conditions.append("created_at LIKE ?")
            params.append(f"{date_str}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM emails {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def log_api_call(
        self,
        service: str,
        endpoint: str,
        count: int = 1,
        tokens_used: int = 0,
    ) -> None:
        """API 呼び出し count 件分を api_logs テーブルに INSERT する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO api_logs (service, endpoint, tokens_used, created_at) VALUES (?, ?, ?, ?)",
                [(service, endpoint, tokens_used, now)] * count,
            )
            await db.commit()

    async def get_daily_stats(self, date_str: str | None = None) -> dict:
        """
        本日（または指定日）の統計を返す。
        戻り値:
            approved, rejected, pending, total_processed, gemini_calls, calendar_calls
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # メールステータス別集計
            cursor = await db.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM emails
                WHERE created_at LIKE ?
                GROUP BY status
                """,
                (f"{date_str}%",),
            )
            rows = await cursor.fetchall()
            status_counts: dict[str, int] = {row["status"]: row["cnt"] for row in rows}

            # API ログ集計
            cursor = await db.execute(
                """
                SELECT service, COUNT(*) AS cnt
                FROM api_logs
                WHERE created_at LIKE ?
                GROUP BY service
                """,
                (f"{date_str}%",),
            )
            api_rows = await cursor.fetchall()
            api_counts: dict[str, int] = {row["service"]: row["cnt"] for row in api_rows}

        approved = status_counts.get("approved", 0)
        rejected = status_counts.get("rejected", 0)
        pending = status_counts.get("pending", 0)
        read_only = status_counts.get("read_only", 0)

        return {
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "read_only": read_only,
            "total_processed": approved + rejected + read_only,
            "gemini_calls": api_counts.get("gemini", 0),
            "calendar_calls": api_counts.get("calendar", 0),
        }

    async def save_notification(self, type: str, content: str) -> None:
        """通知を保存する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO notifications (type, content, read, created_at) VALUES (?, ?, 0, ?)",
                (type, content, now),
            )
            await db.commit()

    async def get_unread_notifications(self, limit: int = 50) -> list[dict]:
        """未読通知を返す（新着順）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM notifications WHERE read=0 ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def mark_notification_read(self, notification_id: int) -> None:
        """指定 ID の通知を既読にする。"""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE notifications SET read=1 WHERE id=?",
                (notification_id,),
            )
            await db.commit()
