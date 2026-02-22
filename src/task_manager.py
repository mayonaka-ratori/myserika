"""
task_manager.py
メール・Discord メッセージからタスクを自動抽出し、
SQLite に永続化・優先度判定・リマインダー通知を管理するモジュール。

Auto-extract tasks from emails/Discord, persist to SQLite,
determine priority, and send Telegram reminder notifications.
"""

import json
import logging
import re
from datetime import datetime, timedelta

from gemini_client import _call_model

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, db, gemini_client, calendar_client=None):
        self._db = db
        self._gemini = gemini_client
        self._calendar = calendar_client
        # リマインダー送信済みキー: {(task_id, hours_before), ...}
        self._reminded: set = set()

    # ── 公開 API / Public API ───────────────────────────────────────────────

    async def extract_tasks_from_email(
        self, sender: str, subject: str, body: str, category: str
    ) -> list[dict]:
        """
        メール本文からアクションアイテムを抽出し DB に保存する。
        Extract action items from email body and persist to DB.
        戻り値: 保存済みタスク辞書のリスト（タスクなし or エラー時は []）
        """
        prompt = self._build_email_task_prompt(sender, subject, body, category)
        try:
            raw = _call_model(self._gemini, prompt)
            tasks = self._parse_task_list(raw)
        except Exception as e:
            logger.warning(f"タスク抽出エラー（スキップ）/ Task extraction error (skip): {e}")
            return []

        saved = []
        for t in tasks:
            if not t.get("title"):
                continue
            priority = self.auto_prioritize(t)
            try:
                task_id = await self._db.save_task(
                    title=t["title"],
                    description=t.get("description", ""),
                    source="email",
                    source_id=subject,
                    priority=priority,
                    due_date=t.get("due_date", ""),
                )
                t["id"] = task_id
                saved.append(t)
            except Exception as e:
                logger.warning(f"タスク DB 保存エラー: {e}")

        return saved

    async def extract_tasks_from_discord(
        self, sender: str, content: str
    ) -> list[dict]:
        """
        Discord メッセージからアクションアイテムを抽出し DB に保存する。
        Extract action items from Discord message and persist to DB.
        """
        prompt = self._build_discord_task_prompt(sender, content)
        try:
            raw = _call_model(self._gemini, prompt)
            tasks = self._parse_task_list(raw)
        except Exception as e:
            logger.warning(f"Discord タスク抽出エラー（スキップ）: {e}")
            return []

        saved = []
        for t in tasks:
            if not t.get("title"):
                continue
            priority = self.auto_prioritize(t)
            try:
                task_id = await self._db.save_task(
                    title=t["title"],
                    description=t.get("description", ""),
                    source="discord",
                    source_id=sender,
                    priority=priority,
                    due_date=t.get("due_date", ""),
                )
                t["id"] = task_id
                saved.append(t)
            except Exception as e:
                logger.warning(f"Discord タスク DB 保存エラー: {e}")

        return saved

    def auto_prioritize(self, task_dict: dict, calendar_context=None) -> str:
        """
        タスク辞書から優先度を自動判定して返す（同期関数）。
        Determine priority from task dict synchronously.
        Gemini が返した priority が有効値なら尊重し、無効なら title/description のキーワードで判定する。
        """
        priority = task_dict.get("priority", "medium")
        valid = {"urgent", "high", "medium", "low"}
        if priority in valid:
            return priority

        # フォールバック: タイトル・説明のキーワードで判定
        title = (task_dict.get("title", "") + " " + task_dict.get("description", "")).lower()
        urgent_kws = ["緊急", "urgent", "asap", "今すぐ", "至急", "deadline today"]
        high_kws = ["重要", "important", "今日中", "本日", "締切"]
        if any(kw in title for kw in urgent_kws):
            return "urgent"
        if any(kw in title for kw in high_kws):
            return "high"
        return "medium"

    async def get_top_tasks(self, n: int = 3) -> list[dict]:
        """
        優先度・期日順に上位 n 件のアクティブタスクを返す。
        Return top n active tasks ordered by priority and due date.
        """
        tasks = await self._db.get_tasks(limit=n * 3)
        active = [t for t in tasks if t.get("status") not in ("done", "cancelled")]
        return active[:n]

    async def check_reminders(self, bot, chat_id: str, config: dict) -> None:
        """
        締切前リマインダーをチェックして Telegram に通知する。
        Check task reminders and send Telegram notifications.
        reminder_hours_before リストに記載された N 時間前に通知する。
        同一タスクで複数の hours_before に該当する場合は最初の 1 件のみ送信する。
        """
        reminder_hours = config.get("task", {}).get("reminder_hours_before", [24, 1])
        now = datetime.now()

        try:
            tasks = await self._db.get_tasks(limit=100)
        except Exception as e:
            logger.warning(f"リマインダーチェック中にタスク取得失敗: {e}")
            return

        for task in tasks:
            if task.get("status") in ("done", "cancelled"):
                continue
            due_str = task.get("due_date", "")
            if not due_str:
                continue

            try:
                due = datetime.fromisoformat(due_str)
            except ValueError:
                # YYYY-MM-DD 形式は当日 23:59 として扱う
                try:
                    due = datetime.strptime(due_str[:10], "%Y-%m-%d").replace(
                        hour=23, minute=59, second=0
                    )
                except ValueError:
                    continue

            for hours in reminder_hours:
                remind_at = due - timedelta(hours=hours)
                diff_min = abs((now - remind_at).total_seconds() / 60)
                if diff_min > 5:
                    continue

                key = (task["id"], hours)
                if key in self._reminded:
                    break  # 既通知 → 後続の hours_before もスキップ

                await self._send_reminder(bot, chat_id, task, hours)
                self._reminded.add(key)
                break  # 同一タスクで複数ヒットしても最初の 1 件のみ

    # ── 内部ヘルパー / Internal Helpers ────────────────────────────────────

    def _build_email_task_prompt(
        self, sender: str, subject: str, body: str, category: str
    ) -> str:
        body_excerpt = body[:1500] if body else ""
        return (
            "以下のメールからアクションアイテム（タスク）を抽出してください。\n"
            "Extract action items (tasks) from the following email.\n\n"
            f"【送信者】{sender}\n"
            f"【件名】{subject}\n"
            f"【カテゴリ】{category}\n"
            f"【本文 先頭1500字】\n{body_excerpt}\n\n"
            "タスクがあれば JSON 配列で返し、なければ [] を返してください。\n"
            "Return JSON array of tasks, or [] if none.\n"
            '[{"title": "タスクタイトル", "description": "詳細説明", '
            '"priority": "urgent|high|medium|low", "due_date": "YYYY-MM-DD or empty string"}]\n'
            "JSON のみ出力。説明文は不要。Output JSON only, no explanations."
        )

    def _build_discord_task_prompt(self, sender: str, content: str) -> str:
        content_excerpt = content[:1000] if content else ""
        return (
            "以下の Discord メッセージからアクションアイテム（タスク）を抽出してください。\n"
            "Extract action items (tasks) from the following Discord message.\n\n"
            f"【送信者】{sender}\n"
            f"【メッセージ】\n{content_excerpt}\n\n"
            "タスクがあれば JSON 配列で返し、なければ [] を返してください。\n"
            "Return JSON array of tasks, or [] if none.\n"
            '[{"title": "タスクタイトル", "description": "詳細説明", '
            '"priority": "urgent|high|medium|low", "due_date": "YYYY-MM-DD or empty string"}]\n'
            "JSON のみ出力。説明文は不要。Output JSON only, no explanations."
        )

    def _parse_task_list(self, raw: str) -> list[dict]:
        """Gemini の応答から JSON 配列を抽出してパースする。"""
        text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError as e:
            logger.warning(f"タスクリスト JSON パースエラー: {e}")
        return []

    async def _send_reminder(
        self, bot, chat_id: str, task: dict, hours_before: int
    ) -> None:
        """リマインダーメッセージを Telegram に送信する。"""
        priority_icon = {
            "urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"
        }.get(task.get("priority", "medium"), "🟡")

        due_str = task.get("due_date", "")
        due_display = due_str[:10] if due_str else "期日不明"
        hours_text = f"{hours_before}時間前"

        import html as _html
        text = (
            f"⏰ <b>タスクリマインダー（期日 {hours_text}）</b>\n"
            f"{priority_icon} {_html.escape(task['title'])}\n"
            f"期日: {due_display}"
        )
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"リマインダー送信エラー: {e}")
