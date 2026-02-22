"""
database.py
SQLite を使ったメール処理履歴・API ログ・通知履歴の永続化モジュール。
aiosqlite を使用して非同期で DB 操作を行う。
"""

import logging
from datetime import datetime, timedelta
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

                CREATE TABLE IF NOT EXISTS tasks (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    title        TEXT NOT NULL,
                    description  TEXT,
                    source       TEXT,
                    source_id    TEXT,
                    priority     TEXT DEFAULT 'medium',
                    status       TEXT DEFAULT 'todo',
                    due_date     TEXT,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    completed_at TEXT
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

            # メールステータス別集計 / email status aggregation
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

            # API ログ集計 / API log aggregation
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

            # カテゴリ別集計 / category aggregation
            cursor = await db.execute(
                """
                SELECT category, COUNT(*) AS cnt
                FROM emails
                WHERE created_at LIKE ?
                GROUP BY category
                """,
                (f"{date_str}%",),
            )
            cat_rows = await cursor.fetchall()
            cat_counts: dict[str, int] = {row["category"]: row["cnt"] for row in cat_rows}

            # Discord 通知数 / Discord notification count
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM notifications
                WHERE created_at LIKE ? AND type = 'discord'
                """,
                (f"{date_str}%",),
            )
            discord_row = await cursor.fetchone()
            discord_notifications = discord_row["cnt"] if discord_row else 0

        approved = status_counts.get("approved", 0)
        rejected = status_counts.get("rejected", 0)
        pending = status_counts.get("pending", 0)
        read_only = status_counts.get("read_only", 0)

        return {
            # 既存キー（変更なし）/ existing keys (unchanged)
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "read_only": read_only,
            "total_processed": approved + rejected + read_only,
            "gemini_calls": api_counts.get("gemini", 0),
            "calendar_calls": api_counts.get("calendar", 0),
            # 追加キー / added keys
            "total_received": approved + rejected + pending + read_only,
            "urgent": cat_counts.get("要返信（重要）", 0),
            "normal": cat_counts.get("要返信（通常）", 0),
            "ignored": cat_counts.get("無視", 0),
            "discord_notifications": discord_notifications,
        }

    async def search_emails(
        self, keyword: str, days: int = 30, limit: int = 10
    ) -> list[dict]:
        """
        キーワードでメールを全文検索する。
        sender / subject / body_preview を LIKE 検索する。
        Search emails by keyword across sender, subject, and body_preview.

        引数 / args:
            keyword: 検索キーワード / search keyword
            days:    遡る日数（デフォルト 30 日）/ lookback days (default 30)
            limit:   最大取得件数（デフォルト 10 件）/ max results (default 10)
        戻り値 / returns:
            [{"sender", "subject", "body_preview", "category", "status", "created_at"}, ...]
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        like = f"%{keyword}%"

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT sender, subject, body_preview, category, status, created_at
                FROM emails
                WHERE created_at >= ?
                  AND (sender LIKE ? OR subject LIKE ? OR body_preview LIKE ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cutoff, like, like, like, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_weekly_stats(self) -> list[dict]:
        """
        今日を含む過去7日分の日別統計を古い順で返す。
        Returns daily stats for the past 7 days including today, oldest first.

        戻り値 / returns:
            [{"date": "YYYY-MM-DD", "approved": int, ...}, ...]  # 7 要素
        """
        result = []
        for i in range(6, -1, -1):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            stats = await self.get_daily_stats(date_str)
            stats["date"] = date_str
            result.append(stats)
        return result

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

    # ── タスク管理 CRUD / Task management CRUD ─────────────────────────────

    async def save_task(
        self,
        title: str,
        description: str = "",
        source: str = "manual",
        source_id: str = "",
        priority: str = "medium",
        due_date: str = "",
    ) -> int:
        """タスクを INSERT して lastrowid を返す。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks
                    (title, description, source, source_id, priority,
                     status, due_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?)
                """,
                (title, description, source, source_id, priority,
                 due_date if due_date else None, now, now),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_tasks(
        self,
        status: str | None = None,
        priority: str | None = None,
        due_before: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """AND 条件で絞り込んだタスク一覧を優先度・期日順で返す。"""
        conditions: list[str] = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if due_before:
            conditions.append("due_date <= ?")
            params.append(due_before)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT * FROM tasks {where}
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 1
                        WHEN 'high'   THEN 2
                        WHEN 'medium' THEN 3
                        WHEN 'low'    THEN 4
                        ELSE 5
                    END,
                    CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
                    due_date ASC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_today_tasks(self) -> list[dict]:
        """期日が今日以前 OR 優先度 urgent/high のアクティブタスクを返す。"""
        today = datetime.now().strftime("%Y-%m-%d")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE status NOT IN ('done', 'cancelled')
                  AND (
                      (due_date IS NOT NULL AND due_date != '' AND due_date <= ?)
                      OR priority IN ('urgent', 'high')
                  )
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 1
                        WHEN 'high'   THEN 2
                        WHEN 'medium' THEN 3
                        WHEN 'low'    THEN 4
                        ELSE 5
                    END,
                    CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
                    due_date ASC
                """,
                (today,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def update_task_status(self, task_id: int, status: str) -> None:
        """タスクのステータスを更新する。status='done' のとき completed_at も更新する。"""
        now = datetime.now().isoformat()
        completed_at = now if status == "done" else None
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET status=?, updated_at=?, completed_at=? WHERE id=?",
                (status, now, completed_at, task_id),
            )
            await db.commit()

    async def update_task_priority(self, task_id: int, priority: str) -> None:
        """タスクの優先度を更新する。"""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET priority=?, updated_at=? WHERE id=?",
                (priority, now, task_id),
            )
            await db.commit()

    async def delete_task(self, task_id: int) -> None:
        """タスクを削除する。"""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            await db.commit()

    async def get_overdue_tasks(self) -> list[dict]:
        """期日が現在より過去かつ未完了のタスクを返す。"""
        now_str = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE due_date IS NOT NULL AND due_date != '' AND due_date < ?
                  AND status NOT IN ('done', 'cancelled')
                ORDER BY due_date ASC
                """,
                (now_str,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_task_stats(self) -> dict:
        """タスクの統計情報を返す。{"total", "todo", "in_progress", "done", "cancelled", "overdue"}"""
        now_str = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            )
            rows = await cursor.fetchall()
            counts: dict[str, int] = {row["status"]: row["cnt"] for row in rows}

            cursor = await db.execute(
                """
                SELECT COUNT(*) AS cnt FROM tasks
                WHERE due_date IS NOT NULL AND due_date != '' AND due_date < ?
                  AND status NOT IN ('done', 'cancelled')
                """,
                (now_str,),
            )
            overdue_row = await cursor.fetchone()
            overdue = overdue_row["cnt"] if overdue_row else 0

        return {
            "total": sum(counts.values()),
            "todo": counts.get("todo", 0),
            "in_progress": counts.get("in_progress", 0),
            "done": counts.get("done", 0),
            "cancelled": counts.get("cancelled", 0),
            "overdue": overdue,
        }
