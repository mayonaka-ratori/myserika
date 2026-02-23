"""
handlers/task_handlers.py
/todo, /tasks, /done commands; task callbacks; task-edit free-text flow.
Also contains date-parsing helpers used exclusively by this module.
"""

import html
import logging
import re
from datetime import date, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from handlers.common import _format_due_display

logger = logging.getLogger(__name__)


# ── Date parsing helpers ──────────────────────────────────────────────────────

_DATE_SPLIT_RE = re.compile(
    r'\s+('
    r'\d{4}-\d{2}-\d{2}'       # 2026-03-15
    r'|\d{1,2}/\d{1,2}'        # 3/15
    r'|\d{1,2}月\d{1,2}日'     # 3月15日
    r'|明日|今日|明後日'
    r'|来週[月火水木金土日]曜日?'
    r'|来週'
    r')$'
)

_WEEKDAY_MAP = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}


def _split_title_and_date(text: str) -> tuple[str, str]:
    """
    Split a trailing date expression from title text.
    Example: "書類準備 3/15" → ("書類準備", "3/15")
    Returns (title, date_token); date_token is "" if no date found.
    """
    m = _DATE_SPLIT_RE.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1)
    return text.strip(), ""


def _parse_due_date(text: str) -> str:
    """
    Parse a Japanese/English date expression to a YYYY-MM-DD string.
    Returns "" if the text cannot be parsed.
    """
    text  = text.strip()
    today = date.today()

    # ISO format: YYYY-MM-DD
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text

    # M/D format → this year or next year if already past
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})', text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return d.isoformat()
        except ValueError:
            return ""

    # M月D日 (Japanese)
    m = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return d.isoformat()
        except ValueError:
            return ""

    # Relative expressions
    if text == "今日":
        return today.isoformat()
    if text == "明日":
        return (today + timedelta(days=1)).isoformat()
    if text == "明後日":
        return (today + timedelta(days=2)).isoformat()

    # Next [weekday]: 来週[曜日]
    m = re.fullmatch(r'来週([月火水木金土日])曜日?', text)
    if m:
        target = _WEEKDAY_MAP[m.group(1)]
        days   = (target - today.weekday()) % 7 or 7
        days  += 7  # "来週" means next week
        return (today + timedelta(days=days)).isoformat()

    if text == "来週":
        days = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days)).isoformat()

    return ""


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_todo_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /todo <title> [due_date] command: manually add a task.
    Examples:
      /todo 確定申告の書類準備 3/15
      /todo デザイン案を送る 明日
      /todo 請求書のテンプレート作成
    """
    args_text = " ".join(context.args) if context.args else ""
    if not args_text:
        await update.message.reply_text(
            "使用方法 / Usage: /todo &lt;内容&gt; [期限]\n"
            "例 / Example: /todo 確定申告の書類準備 3/15",
            parse_mode="HTML",
        )
        return

    db           = context.bot_data.get("db")
    task_manager = context.bot_data.get("task_manager")
    chat_id      = context.bot_data.get("chat_id", "")

    if not db:
        await update.message.reply_text("⚠️ DB が初期化されていません。")
        return

    # Split trailing date expression from title
    title, date_token = _split_title_and_date(args_text)
    due_date          = _parse_due_date(date_token) if date_token else ""

    # Auto-determine priority from keywords
    task_dict = {"title": title, "description": "", "due_date": due_date}
    priority  = task_manager.auto_prioritize(task_dict) if task_manager else "medium"

    # Save to DB
    try:
        task_id = await db.save_task(
            title=title,
            description="",
            source="manual",
            source_id="telegram",
            priority=priority,
            due_date=due_date,
        )
    except Exception as e:
        logger.error(f"Task save error: {e}")
        await update.message.reply_text(f"⚠️ タスクの保存に失敗しました：{e}")
        return

    priority_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(priority, "🟡")
    priority_ja   = {"urgent": "緊急", "high": "高",   "medium": "中",   "low": "低"  }.get(priority, "中")
    due_part      = f" / 期限：{due_date[:10]}" if due_date else ""

    await update.message.reply_text(
        f"✅ タスク追加：{html.escape(title)}\n"
        f"（{priority_icon} 優先度：{priority_ja}{due_part}）",
        parse_mode="HTML",
    )
    logger.info(f"Manual task added: id={task_id} title={title!r}")


async def handle_tasks_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /tasks [filter] command: show active task list.
    Optional filters: urgent / today / overdue.
    """
    db = context.bot_data.get("db")
    if not db:
        await update.message.reply_text("⚠️ DB が初期化されていません。")
        return

    filter_arg = (context.args[0].lower() if context.args else "").strip()

    try:
        if filter_arg == "urgent":
            tasks = await db.get_tasks(priority="urgent", limit=20)
            tasks = [t for t in tasks if t.get("status") not in ("done", "cancelled")]
        elif filter_arg == "today":
            tasks = await db.get_today_tasks()
        elif filter_arg == "overdue":
            tasks = await db.get_overdue_tasks()
        else:
            raw   = await db.get_tasks(limit=30)
            tasks = [t for t in raw if t.get("status") not in ("done", "cancelled")]
    except Exception as e:
        await update.message.reply_text(f"⚠️ タスク取得エラー：{e}")
        return

    if not tasks:
        label = {"urgent": "緊急", "today": "今日", "overdue": "期限切れ"}.get(filter_arg, "未完了")
        await update.message.reply_text(f"📋 {label}タスクはありません。")
        return

    # Save last displayed list to bot_data for /done <number> reference
    context.bot_data["last_task_list"] = tasks

    PRIORITY_ICON = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    lines = [f"📋 <b>タスク一覧（{len(tasks)}件）</b>", "─────────────"]
    for i, t in enumerate(tasks, 1):
        icon = (
            "🔵" if t.get("status") == "in_progress"
            else PRIORITY_ICON.get(t.get("priority", "medium"), "🟡")
        )
        due   = _format_due_display(t.get("due_date", ""))
        lines.append(f"{icon} {i}. {html.escape(t['title'])}{due}")

    # Inline buttons for up to 10 tasks (3 buttons × 1 row per task)
    buttons = []
    for i, t in enumerate(tasks[:10], 1):
        tid = t["id"]
        buttons.append([
            InlineKeyboardButton(f"✅ {i}完了", callback_data=f"task_done:{tid}"),
            InlineKeyboardButton(f"📝 {i}編集", callback_data=f"task_edit:{tid}"),
            InlineKeyboardButton(f"🗑 {i}削除", callback_data=f"task_del:{tid}"),
        ])

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=keyboard
    )


async def handle_done_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /done <number> command: mark the task at <number> in the last /tasks list as done.
    Show /tasks first, then use /done <number>.
    """
    db = context.bot_data.get("db")
    if not db:
        await update.message.reply_text("⚠️ DB が初期化されていません。")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "使用方法 / Usage: /done &lt;番号&gt;\n"
            "まず /tasks でタスク一覧を表示してください。\n"
            "Show /tasks list first, then use /done &lt;number&gt;.",
            parse_mode="HTML",
        )
        return

    idx        = int(context.args[0]) - 1  # 1-indexed → 0-indexed
    task_list: list = context.bot_data.get("last_task_list", [])

    if not task_list:
        await update.message.reply_text("先に /tasks でタスク一覧を表示してください。")
        return
    if idx < 0 or idx >= len(task_list):
        await update.message.reply_text(
            f"⚠️ 番号 {idx + 1} は範囲外です（1〜{len(task_list)}）。"
        )
        return

    task = task_list[idx]
    try:
        await db.update_task_status(task["id"], "done")
    except Exception as e:
        await update.message.reply_text(f"⚠️ 更新エラー：{e}")
        return

    await update.message.reply_text(
        f"✅ 完了：{html.escape(task['title'])}", parse_mode="HTML"
    )
    # Remove from list to keep numbering consistent for subsequent /done calls
    context.bot_data["last_task_list"] = [
        t for t in task_list if t["id"] != task["id"]
    ]
    logger.info(f"Task done: id={task['id']} title={task['title']!r}")


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_task_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle task-related callback queries.
    query.answer() has already been called by the main dispatcher.
    Handles: task_done:, task_del:, task_edit:, task_confirm:, task_ignore:.
    """
    query    = update.callback_query
    data     = query.data

    # --- Mark task done ---
    if data.startswith("task_done:"):
        task_id   = int(data.split(":", 1)[1])
        db        = context.bot_data.get("db")
        if db:
            task_list = context.bot_data.get("last_task_list", [])
            task      = next((t for t in task_list if t["id"] == task_id), None)
            try:
                await db.update_task_status(task_id, "done")
                title = task["title"] if task else f"タスク#{task_id}"
                await query.edit_message_text(
                    f"✅ 完了：{html.escape(title)}", parse_mode="HTML"
                )
                if task:
                    context.bot_data["last_task_list"] = [
                        t for t in task_list if t["id"] != task_id
                    ]
            except Exception as e:
                await query.answer(f"エラー: {e}")

    # --- Delete task ---
    elif data.startswith("task_del:"):
        task_id   = int(data.split(":", 1)[1])
        db        = context.bot_data.get("db")
        if db:
            task_list = context.bot_data.get("last_task_list", [])
            task      = next((t for t in task_list if t["id"] == task_id), None)
            try:
                await db.delete_task(task_id)
                title = task["title"] if task else f"タスク#{task_id}"
                await query.edit_message_text(
                    f"🗑 削除：{html.escape(title)}", parse_mode="HTML"
                )
                if task:
                    context.bot_data["last_task_list"] = [
                        t for t in task_list if t["id"] != task_id
                    ]
            except Exception as e:
                await query.answer(f"エラー: {e}")

    # --- Enter task-title edit mode ---
    elif data.startswith("task_edit:"):
        task_id = int(data.split(":", 1)[1])
        context.bot_data["awaiting_task_edit"] = task_id
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ 新しいタイトルを入力してください。\nEnter the new task title:",
        )

    # --- Confirm auto-extracted task (already saved to DB, just acknowledge) ---
    elif data.startswith("task_confirm:"):
        await query.edit_message_text(
            query.message.text + "\n\n✅ タスクとして追加しました。",
            parse_mode="HTML",
        )

    # --- Ignore auto-extracted task (delete from DB) ---
    elif data.startswith("task_ignore:"):
        task_id = int(data.split(":", 1)[1])
        db      = context.bot_data.get("db")
        if db:
            try:
                await db.delete_task(task_id)
                await query.edit_message_text(
                    query.message.text + "\n\n❌ 無視しました。",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.answer(f"エラー: {e}")


# ── Free-text handler (awaiting_task_edit state) ──────────────────────────────

async def handle_task_edit_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Apply a new title to the task being edited.
    Called when bot_data['awaiting_task_edit'] is set.
    """
    bot_data            = context.bot_data
    awaiting_task_edit  = bot_data.get("awaiting_task_edit")
    new_title           = update.message.text.strip()
    db                  = bot_data.get("db")
    bot_data["awaiting_task_edit"] = None

    if db and new_title:
        try:
            await db.update_task_title(awaiting_task_edit, new_title)
            await update.message.reply_text(
                f"✅ タスクを更新しました：{html.escape(new_title)}", parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ 更新エラー：{e}")
    else:
        await update.message.reply_text("⚠️ タイトルが空のためキャンセルしました。")
