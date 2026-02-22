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

                CREATE TABLE IF NOT EXISTS moneyforward_transactions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    mf_id           TEXT UNIQUE NOT NULL,
                    is_calculated   INTEGER DEFAULT 1,
                    date            TEXT NOT NULL,
                    content         TEXT,
                    amount          INTEGER NOT NULL,
                    institution     TEXT,
                    category_large  TEXT,
                    category_medium TEXT,
                    memo            TEXT,
                    is_transfer     INTEGER DEFAULT 0,
                    match_status    TEXT DEFAULT 'pending',
                    matched_expense_id INTEGER,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS expenses (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    date                TEXT NOT NULL,
                    description         TEXT NOT NULL,
                    amount              INTEGER NOT NULL,
                    category            TEXT,
                    receipt_image_path  TEXT,
                    matched_mf_id       TEXT,
                    match_confidence    TEXT,
                    source              TEXT DEFAULT 'manual',
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS discord_messages (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id       TEXT UNIQUE NOT NULL,
                    channel_id       TEXT,
                    guild_id         TEXT,
                    sender_id        TEXT,
                    sender_name      TEXT,
                    content          TEXT,
                    is_mention       INTEGER DEFAULT 0,
                    is_dm            INTEGER DEFAULT 0,
                    replied          INTEGER DEFAULT 0,
                    reply_content    TEXT,
                    created_at       TEXT NOT NULL,
                    replied_at       TEXT,
                    reminder_sent_at TEXT
                );
            """)
            await db.commit()

            # reminded_at カラムを追加（既存 DB は ALTER TABLE でマイグレーション）
            # Add reminded_at column (migrates existing DB via ALTER TABLE)
            try:
                await db.execute(
                    "ALTER TABLE tasks ADD COLUMN reminded_at TEXT"
                )
                await db.commit()
            except Exception:
                pass  # カラムが既に存在する場合はスキップ / Skip if column already exists

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

    async def update_task_title(self, task_id: int, title: str) -> None:
        """タスクのタイトルを更新する。/ Update task title."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                (title, now, task_id),
            )
            await db.commit()

    async def update_task_due_date(self, task_id: int, due_date: str) -> None:
        """タスクの期日を更新する。空文字列を渡すと NULL にクリアされる。
        / Update task due date. Passing an empty string clears it to NULL."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET due_date=?, updated_at=? WHERE id=?",
                (due_date if due_date else None, now, task_id),
            )
            await db.commit()

    async def get_upcoming_reminders(self, hours_before: int = 3) -> list[dict]:
        """
        hours_before 時間以内に期日が来るタスクで、まだ reminded_at が NULL のものを返す。
        / Return tasks due within hours_before hours that have not been reminded yet.
        条件 / Conditions:
          - status が done/cancelled でない / status is not done/cancelled
          - due_date が現在〜(現在+hours_before) の範囲内
            / due_date is between now and (now + hours_before)
          - reminded_at IS NULL
        """
        now = datetime.now()
        deadline = (now + timedelta(hours=hours_before)).isoformat()
        now_str = now.isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE status NOT IN ('done', 'cancelled')
                  AND due_date IS NOT NULL
                  AND due_date != ''
                  AND due_date >= ?
                  AND due_date <= ?
                  AND (reminded_at IS NULL)
                ORDER BY due_date ASC
                """,
                (now_str, deadline),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def mark_reminded(self, task_id: int) -> None:
        """
        指定タスクの reminded_at を現在時刻の ISO 文字列に更新する。
        / Set reminded_at to current ISO timestamp for the given task.
        """
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET reminded_at=? WHERE id=?",
                (now, task_id),
            )
            await db.commit()

    # ── MoneyForward 取引 CRUD ──────────────────────────────────────────────

    async def save_mf_transaction(
        self,
        mf_id: str,
        is_calculated: int,
        date: str,
        content: str,
        amount: int,
        institution: str,
        category_large: str,
        category_medium: str,
        memo: str,
        is_transfer: int,
    ) -> bool:
        """INSERT OR IGNORE で重複スキップ。新規挿入なら True を返す。
        / Insert with duplicate check; returns True if newly inserted."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO moneyforward_transactions
                    (mf_id, is_calculated, date, content, amount,
                     institution, category_large, category_medium,
                     memo, is_transfer, match_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (mf_id, is_calculated, date, content, amount,
                 institution, category_large, category_medium,
                 memo, is_transfer, now),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_mf_transactions(
        self,
        month: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """月（YYYY-MM）・照合ステータスで絞り込んで返す。
        / Filter by month and match_status."""
        conditions: list[str] = []
        params: list = []

        if month:
            conditions.append("date LIKE ?")
            params.append(f"{month}%")
        if status:
            conditions.append("match_status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM moneyforward_transactions {where} "
                f"ORDER BY date DESC LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def update_mf_match_status(
        self,
        mf_id: str,
        status: str,
        matched_expense_id: int | None = None,
    ) -> None:
        """照合ステータスと紐付け expense_id を更新する。
        / Update match_status and linked expense id."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE moneyforward_transactions
                SET match_status=?, matched_expense_id=?
                WHERE mf_id=?
                """,
                (status, matched_expense_id, mf_id),
            )
            await db.commit()

    async def get_monthly_expense_summary(self, month: str) -> dict:
        """指定月（YYYY-MM）の category_large 別集計を返す。
        / Return category breakdown for the given month."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT category_large, SUM(amount) AS total, COUNT(*) AS cnt
                FROM moneyforward_transactions
                WHERE date LIKE ? AND is_transfer = 0 AND amount < 0
                GROUP BY category_large
                ORDER BY total ASC
                """,
                (f"{month}%",),
            )
            rows = await cursor.fetchall()
            summary: dict = {}
            for row in rows:
                cat = row["category_large"] or "未分類"
                summary[cat] = {
                    "total": abs(row["total"]),
                    "count": row["cnt"],
                }
            return summary

    # ── Expense CRUD ───────────────────────────────────────────────────────

    # ── Discord message CRUD ────────────────────────────────────────────────

    async def save_discord_message(
        self,
        message_id: str,
        channel_id: str,
        guild_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        is_mention: bool = False,
        is_dm: bool = False,
    ) -> int:
        """Save a Discord message to the discord_messages table.
        Uses INSERT OR IGNORE to skip duplicates.
        Returns the row id (lastrowid), or 0 if already existed."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO discord_messages
                    (message_id, channel_id, guild_id, sender_id, sender_name,
                     content, is_mention, is_dm, replied, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    message_id, channel_id, guild_id, sender_id, sender_name,
                    content, int(is_mention), int(is_dm), now,
                ),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_unreplied_messages(self, older_than_hours: int = 2) -> list[dict]:
        """Return Discord messages that have not been replied to and have no
        pending reminder (reminder_sent_at IS NULL), older than older_than_hours."""
        cutoff = (
            datetime.now() - timedelta(hours=older_than_hours)
        ).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM discord_messages
                WHERE replied = 0
                  AND reminder_sent_at IS NULL
                  AND created_at <= ?
                ORDER BY created_at ASC
                """,
                (cutoff,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def mark_as_replied(self, discord_db_id: int, reply_content: str) -> None:
        """Mark a discord_messages row as replied with the sent content."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE discord_messages
                SET replied=1, reply_content=?, replied_at=?
                WHERE id=?
                """,
                (reply_content, now, discord_db_id),
            )
            await db.commit()

    async def get_discord_message_by_id(self, discord_db_id: int) -> dict | None:
        """Fetch a single discord_messages row by primary key. Returns None if not found."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM discord_messages WHERE id=?", (discord_db_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_discord_reminder_sent(self, discord_db_id: int) -> None:
        """Set reminder_sent_at to now for the given discord_messages row.
        Prevents duplicate reminder notifications for the same message."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE discord_messages SET reminder_sent_at=? WHERE id=?",
                (now, discord_db_id),
            )
            await db.commit()

    # ── Expense CRUD ───────────────────────────────────────────────────────

    async def save_expense(
        self,
        date: str,
        description: str,
        amount: int,
        category: str = "",
        source: str = "manual",
    ) -> int:
        """経費を保存して lastrowid を返す。
        / Save an expense and return its id."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO expenses
                    (date, description, amount, category, source,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date, description, amount, category, source, now, now),
            )
            await db.commit()
            return cursor.lastrowid
