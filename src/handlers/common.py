"""
handlers/common.py
Shared notification senders and cross-domain command handlers.
Used by multiple handler modules; must not import from any other handlers/* module.
"""

import html
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from gemini_client import get_api_usage
from classifier import extract_email_address
from utils import format_due_display as _format_due_display

logger = logging.getLogger(__name__)

# Maximum Telegram message length (with safety margin)
MAX_MESSAGE_LEN = 3800


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_api_usage_text(bot_data: dict) -> str:
    """Build API usage summary string for status displays."""
    gemini_client = bot_data.get("gemini_client")
    if not gemini_client:
        return ""
    try:
        usage = get_api_usage(gemini_client)
        return (
            f"\n本日のAPI使用: {usage['daily_count']}回 "
            f"/ 残り推定: {usage['daily_remaining']:,}回（上限1,500回/日）"
            f"\n直近1分の使用: {usage['minute_count']}回 "
            f"/ 残り: {usage['minute_remaining']}回（上限15回/分）"
        )
    except Exception:
        return ""


def _parse_important_contacts(content: str) -> list[dict]:
    """
    Parse contacts with priority '高' or tag '重要' from contacts.md.
    Returns list of dicts with name, email, frequency, last_contact.
    """
    contacts = []
    sections = re.split(r'\n### ', '\n' + content)
    for section in sections[1:]:
        lines = section.strip().split('\n')
        if not lines:
            continue
        name = lines[0].strip()
        data: dict[str, str] = {}
        tags: list[str] = []
        for line in lines[1:]:
            if line.startswith('- メールアドレス：'):
                data['email'] = line[len('- メールアドレス：'):].strip()
            elif line.startswith('- やり取り頻度：'):
                data['frequency'] = line[len('- やり取り頻度：'):].strip()
            elif line.startswith('- 最終連絡日：'):
                data['last_contact'] = line[len('- 最終連絡日：'):].strip()
            elif line.startswith('- 優先度：'):
                data['priority'] = line[len('- 優先度：'):].strip()
            elif line.startswith('- タグ：'):
                tags = [t.strip() for t in line[len('- タグ：'):].split(',')]
        # Filter by priority '高' or tag '重要'
        if data.get('priority') == '高' or '重要' in tags:
            contacts.append({
                'name': name,
                'email': data.get('email', ''),
                'frequency': data.get('frequency', ''),
                'last_contact': data.get('last_contact', ''),
            })
    return contacts


# ── Outbound notification senders ────────────────────────────────────────────

async def send_notification(bot: Bot, chat_id: str, text: str) -> None:
    """Send a plain text message to Telegram. Used for errors, status, system messages."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Telegram notification send error: {e}")


async def send_task_detection_notification(
    bot: Bot,
    chat_id: str,
    task: dict,
    source_label: str = "",
) -> None:
    """
    Send a task-detection confirmation notification to Telegram.
    Task must already be saved to DB (has an id). Clicking '❌ 無視する' will delete it.
    """
    priority_icon = {
        "urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"
    }.get(task.get("priority", "medium"), "🟡")
    due_display = _format_due_display(task.get("due_date", ""))
    source_part = f"\n{html.escape(source_label)}" if source_label else ""

    text = (
        f"📌 <b>新しいタスクを検出</b>\n"
        f"{priority_icon} {html.escape(task['title'])}"
        f"{source_part}\n"
        f"{due_display}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 追加する", callback_data=f"task_confirm:{task['id']}"),
        InlineKeyboardButton("❌ 無視する", callback_data=f"task_ignore:{task['id']}"),
    ]])
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Task detection notification send error: {e}")


async def send_email_summary(
    bot: Bot, chat_id: str, classified_emails: list[dict]
) -> None:
    """
    Send a classified-email summary to Telegram.
    Attaches approve/later inline buttons when actionable emails exist.
    """
    counts = {
        "要返信（重要）": 0,
        "要返信（通常）": 0,
        "閲覧のみ": 0,
        "無視": 0,
        "要確認": 0,
    }
    for result in classified_emails:
        cat = result.get("category", "閲覧のみ")
        if cat in counts:
            counts[cat] += 1
        else:
            counts["閲覧のみ"] += 1

    total = len(classified_emails)
    urgent = counts["要返信（重要）"]
    normal = counts["要返信（通常）"]

    text = f"📬 <b>新着メール {total} 件</b>\n\n"
    if urgent:
        text += f"🔴 要返信（重要）：{urgent}件\n"
    if normal:
        text += f"🟡 要返信（通常）：{normal}件\n"
    if counts["閲覧のみ"]:
        text += f"📖 閲覧のみ：{counts['閲覧のみ']}件\n"
    if counts["無視"]:
        text += f"🔕 無視：{counts['無視']}件\n"
    if counts["要確認"]:
        text += f"❓ 要確認（手動判断）：{counts['要確認']}件\n"

    keyboard = None
    if urgent + normal > 0:
        text += "\n返信案を確認しますか？"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 返信案を確認", callback_data="show_drafts"),
                InlineKeyboardButton("⏰ 後で", callback_data="later"),
            ]
        ])

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Email summary send error: {e}")


async def send_reply_draft(
    bot: Bot,
    chat_id: str,
    email_id: str,
    draft: str,
    subject: str,
    sender: str,
) -> None:
    """
    Send a reply draft to Telegram with Approve / Revise / Reject / View-only buttons.
    Truncates draft text to MAX_MESSAGE_LEN and escapes HTML special characters.
    """
    draft_display = draft[:MAX_MESSAGE_LEN]
    if len(draft) > MAX_MESSAGE_LEN:
        draft_display += "\n...（以下省略）"

    subject_esc = html.escape(subject)
    sender_esc  = html.escape(sender)
    draft_esc   = html.escape(draft_display)

    text = (
        f"✉️ <b>返信案【{subject_esc}】</b>\n"
        f"宛先：{sender_esc}\n\n"
        f"<pre>{draft_esc}</pre>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 承認して送信", callback_data=f"approve:{email_id}"),
            InlineKeyboardButton("✏️ 修正指示", callback_data=f"revise:{email_id}"),
            InlineKeyboardButton("❌ 却下", callback_data=f"reject:{email_id}"),
        ],
        [
            InlineKeyboardButton("📖 閲覧のみ", callback_data=f"viewonly:{email_id}"),
        ],
    ])

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Reply draft send error: {e}")


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/help command: show the list of available commands."""
    text = (
        "🤖 <b>MY-SECRETARY コマンド一覧</b>\n\n"
        "/status — システム状態・稼働時間・統計\n"
        "/pending — 承認待ちメール一覧\n"
        "/check — メールを今すぐチェック\n"
        "/search — メール検索（例: /search 田中）\n"
        "/schedule — 今日の予定（/schedule tomorrow で明日）\n"
        "/stats — 統計レポート（/stats weekly で週間）\n"
        "/contacts — 重要連絡先一覧\n"
        "/quiet — 通知一時停止（例: /quiet 2 で2時間）\n"
        "/resume — 通知再開\n"
        "/help — このヘルプを表示\n"
        "/todo — タスク追加（例: /todo 確定申告 3/15）\n"
        "/tasks — タスク一覧（/tasks urgent / today / overdue）\n"
        "/done — タスク完了（例: /done 1）\n"
        "/expense — 経費管理メニュー / Expense management"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/status command: show system status, uptime, daily stats, and API usage."""
    bot_data = context.bot_data
    pending = bot_data.get("pending_approvals", {})
    awaiting = bot_data.get("awaiting_revision")
    count = len(pending)

    lines = ["📊 <b>MY-SECRETARY ステータス</b>\n"]

    # Uptime
    start_time = bot_data.get("start_time")
    if start_time:
        try:
            delta = datetime.now() - start_time
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            lines.append(f"⏱ 稼働時間: {hours}時間{minutes}分")
        except Exception:
            pass

    # Last check time
    last_check = bot_data.get("last_check_time")
    if last_check:
        try:
            lines.append(f"🕐 最終チェック: {last_check.strftime('%H:%M')}")
        except Exception:
            pass

    lines.append(f"📬 承認待ち: {count} 件")
    if awaiting:
        lines.append(f"✏️ 修正指示待ち: {awaiting}")

    # Today's statistics
    db = bot_data.get("db")
    if db:
        try:
            stats = await db.get_daily_stats()
            total    = stats.get("total_processed", 0)
            approved = stats.get("approved", 0)
            lines.append(f"📈 本日: {total}件処理 / {approved}件送信済み")
        except Exception:
            pass

    # Gemini API usage
    gemini_client = bot_data.get("gemini_client")
    if gemini_client:
        try:
            usage = get_api_usage(gemini_client)
            lines.append(
                f"🤖 Gemini: {usage['daily_count']}回/日 "
                f"（残り{usage['daily_remaining']:,}回）"
            )
        except Exception:
            pass

    # Discord connection
    discord_client = bot_data.get("discord_client")
    if discord_client is not None:
        lines.append("💬 Discord: 接続中")
    else:
        lines.append("💬 Discord: 未接続")

    # Next calendar event within 12 hours
    calendar_client = bot_data.get("calendar_client")
    if calendar_client is not None:
        try:
            events = calendar_client.get_upcoming_events(hours=12)
            if events:
                ev = events[0]
                ev_time  = ev["start"].strftime("%H:%M")
                ev_title = html.escape(ev["title"])
                lines.append(f"📅 次の予定: {ev_time} {ev_title}")
        except Exception:
            pass

    # Web UI URL
    config   = bot_data.get("config", {})
    web_port = config.get("web", {}).get("port", 8080)
    lines.append(f"🌐 Web UI: http://localhost:{web_port}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_quiet_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/quiet [N] command: pause Telegram notifications for N hours (default 1)."""
    bot_data = context.bot_data
    quiet_until = bot_data.get("quiet_until")

    if quiet_until and datetime.now() < quiet_until:
        resume_str = quiet_until.strftime("%H:%M")
        await update.message.reply_text(
            f"🔇 既に停止中です（{resume_str} に再開）"
        )
        return

    args = context.args or []
    try:
        hours = int(args[0]) if args else 1
        if hours <= 0:
            hours = 1
    except (ValueError, IndexError):
        hours = 1

    now   = datetime.now()
    until = now + timedelta(hours=hours)
    bot_data["quiet_until"]     = until
    bot_data["quiet_since"]     = now
    bot_data["quiet_email_count"] = 0

    resume_str = until.strftime("%H:%M")
    await update.message.reply_text(
        f"🔇 通知を{hours}時間停止しました（{resume_str} に再開）"
    )


async def handle_resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/resume command: resume Telegram notifications."""
    bot_data    = context.bot_data
    quiet_until = bot_data.get("quiet_until")

    if not quiet_until or datetime.now() >= quiet_until:
        await update.message.reply_text("🔔 通知は停止中ではありません")
        return

    email_count = bot_data.get("quiet_email_count", 0)
    bot_data["quiet_until"]     = None
    bot_data["quiet_since"]     = None
    bot_data["quiet_email_count"] = 0

    msg = "🔔 通知を再開しました"
    if email_count > 0:
        msg += f"\n📬 停止中に届いたメール：{email_count}件"
    await update.message.reply_text(msg)


async def handle_contacts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/contacts command: show important contacts (priority '高' or tag '重要')."""
    bot_data      = context.bot_data
    contacts_path = bot_data.get("contacts_path")

    if not contacts_path or not os.path.exists(contacts_path):
        await update.message.reply_text("👥 重要連絡先はまだ登録されていません")
        return

    try:
        with open(contacts_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"/contacts read error: {e}")
        await update.message.reply_text("⚠️ 連絡先ファイルの読み込みに失敗しました")
        return

    contacts = _parse_important_contacts(content)

    if not contacts:
        await update.message.reply_text("👥 重要連絡先はまだ登録されていません")
        return

    lines = [f"👥 重要連絡先（{len(contacts)}名）", "─────────────"]
    for c in contacts:
        name  = html.escape(c['name'])
        email = html.escape(c['email'])
        last  = c.get('last_contact', '')
        freq  = c.get('frequency', '')
        try:
            date_disp = datetime.strptime(last, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            date_disp = last
        lines.append(f"⭐ {name} - {email}")
        lines.append(f"   最終：{date_disp} / 頻度：{freq}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
