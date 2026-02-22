"""
telegram_bot.py
Telegram Bot API を操作するモジュール。
ユーザーへの通知送信と、ボタン・テキスト返答の受け取りを担当する。
python-telegram-bot v20+ の非同期 API を使用する。
"""

import html
import logging
import os
import re
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from gmail_client import send_email, mark_as_read
from gemini_client import get_api_usage, refine_reply_draft
from classifier import extract_email_address
from expense_manager import CATEGORY_KEYWORDS

logger = logging.getLogger(__name__)

# ── 日付パース / Date Parsing Helpers ─────────────────────────────────────

_DATE_SPLIT_RE = re.compile(
    r'\s+('
    r'\d{4}-\d{2}-\d{2}'           # 2026-03-15
    r'|\d{1,2}/\d{1,2}'            # 3/15
    r'|\d{1,2}月\d{1,2}日'          # 3月15日
    r'|明日|今日|明後日'
    r'|来週[月火水木金土日]曜日?'
    r'|来週'
    r')$'
)

_WEEKDAY_MAP = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}


def _split_title_and_date(text: str) -> tuple[str, str]:
    """
    末尾の日付表現を分離してタイトルと日付文字列を返す。
    Split trailing date expression from title text.
    例: "書類準備 3/15" → ("書類準備", "3/15")
    """
    m = _DATE_SPLIT_RE.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1)
    return text.strip(), ""


def _parse_due_date(text: str) -> str:
    """
    日本語・英語の日付表現を YYYY-MM-DD 文字列に変換する。
    Parse Japanese/English date expression to YYYY-MM-DD string.
    変換できない場合は空文字列を返す。/ Returns "" if unparseable.
    """
    text = text.strip()
    today = date.today()

    # YYYY-MM-DD / ISO format
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text

    # M/D format → same or next year
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})', text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return d.isoformat()
        except ValueError:
            return ""

    # M月D日 / Japanese format
    m = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return d.isoformat()
        except ValueError:
            return ""

    # 相対表現 / Relative expressions
    if text == "今日":
        return today.isoformat()
    if text == "明日":
        return (today + timedelta(days=1)).isoformat()
    if text == "明後日":
        return (today + timedelta(days=2)).isoformat()

    # 来週[曜日] / Next [weekday]
    m = re.fullmatch(r'来週([月火水木金土日])曜日?', text)
    if m:
        target = _WEEKDAY_MAP[m.group(1)]
        days = (target - today.weekday()) % 7 or 7
        days += 7  # "来週" = next week
        return (today + timedelta(days=days)).isoformat()

    if text == "来週":
        days = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days)).isoformat()

    return ""


def _format_due_display(due_date: str) -> str:
    """
    DB の due_date 文字列を表示用テキストに変換する（残り日数付き）。
    Convert DB due_date string to display text with days remaining.
    """
    if not due_date:
        return "（期限なし）"
    try:
        today = date.today()
        due = date.fromisoformat(due_date[:10])
        delta = (due - today).days
        label = f"{due.month}/{due.day}"
        if delta < 0:
            return f"（期限：{label} ⚠️期限切れ）"
        if delta == 0:
            return "（期限：今日）"
        if delta == 1:
            return "（期限：明日）"
        return f"（期限：{label} 残り{delta}日）"
    except (ValueError, TypeError):
        return f"（期限：{due_date[:10]}）"


# Telegram メッセージの最大文字数（余裕を持って設定）
MAX_MESSAGE_LEN = 3800


def build_application(bot_token: str) -> Application:
    """
    Telegram Bot アプリケーションを初期化してハンドラーを登録して返す。
    bot_token: config.yaml から渡される Bot トークン
    登録ハンドラー:
      - /status コマンド: 現在の承認待ち件数を表示
      - インラインボタン: 承認・修正・却下などのコールバック処理
      - テキストメッセージ: 修正指示などの自由入力を処理
    """
    app = Application.builder().token(bot_token).build()

    # コマンドハンドラー / command handlers
    app.add_handler(CommandHandler("status",   handle_status_command))
    app.add_handler(CommandHandler("help",     handle_help_command))
    app.add_handler(CommandHandler("pending",  handle_pending_command))
    app.add_handler(CommandHandler("check",    handle_check_command))
    app.add_handler(CommandHandler("search",   handle_search_command))
    app.add_handler(CommandHandler("schedule", handle_schedule_command))
    app.add_handler(CommandHandler("stats",    handle_stats_command))
    app.add_handler(CommandHandler("quiet",    handle_quiet_command))
    app.add_handler(CommandHandler("resume",   handle_resume_command))
    app.add_handler(CommandHandler("contacts", handle_contacts_command))
    app.add_handler(CommandHandler("todo",    handle_todo_command))
    app.add_handler(CommandHandler("tasks",   handle_tasks_command))
    app.add_handler(CommandHandler("done",    handle_done_command))
    app.add_handler(CommandHandler("expense", handle_expense_command))

    # インラインキーボードのコールバックハンドラー
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Photo handler — receipt OCR flow
    app.add_handler(MessageHandler(filters.PHOTO, handle_receipt_photo))

    # Document ハンドラー（テキストハンドラーより前に登録）
    # Register before text handler to ensure proper priority
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # テキストメッセージハンドラー（コマンド以外）
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    return app


def _build_api_usage_text(bot_data: dict) -> str:
    """API 使用量テキストを生成する内部ヘルパー。"""
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


async def handle_help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /help コマンドで利用可能なコマンド一覧を表示する。
    /help command: show the list of available commands
    """
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


async def handle_search_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /search <keyword> コマンドで DB 内のメールをキーワード検索して結果を表示する。
    /search command: search emails in DB by keyword and show results
    """
    bot_data = context.bot_data
    db = bot_data.get("db")

    if db is None:
        await update.message.reply_text("⚠️ データベースが利用できません")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "使い方：<code>/search キーワード</code>", parse_mode="HTML"
        )
        return

    keyword = " ".join(args)

    try:
        results = await db.search_emails(keyword)
    except Exception as e:
        logger.error(f"/search エラー: {e}")
        await update.message.reply_text("⚠️ 検索中にエラーが発生しました。")
        return

    if not results:
        await update.message.reply_text(
            f"🔍 「{html.escape(keyword)}」に一致するメールが見つかりませんでした"
        )
        return

    # ステータス表示ラベル / status display labels
    status_labels = {
        "pending":   "承認待ち",
        "approved":  "返信済み",
        "rejected":  "却下",
        "read_only": "閲覧のみ",
    }

    lines = [
        f"🔍 「{html.escape(keyword)}」の検索結果（{len(results)}件）",
        "─────────────",
    ]
    for i, row in enumerate(results, 1):
        try:
            dt = datetime.fromisoformat(row["created_at"])
            date_str = dt.strftime("%m/%d")
        except Exception:
            date_str = "??"
        sender = html.escape(row.get("sender", "（不明）"))
        subject = html.escape(row.get("subject", "（件名なし）"))
        status_label = status_labels.get(row.get("status", ""), row.get("status", ""))
        lines.append(f"{i}. {date_str} {sender} - {subject} [{status_label}]")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /schedule [tomorrow] コマンドで今日または明日の予定と空き時間を表示する。
    /schedule command: show today's (or tomorrow's) events and free time slots
    """
    bot_data = context.bot_data
    calendar_client = bot_data.get("calendar_client")

    if calendar_client is None:
        await update.message.reply_text("📅 カレンダーが設定されていません")
        return

    # 引数判定 / determine target day from arguments
    args = context.args or []
    show_tomorrow = bool(args) and args[0].lower() == "tomorrow"

    # 曜日名 / weekday names in Japanese
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    JST = ZoneInfo("Asia/Tokyo")
    now_jst = datetime.now(JST)

    try:
        if show_tomorrow:
            target_date = (now_jst + timedelta(days=1)).date()
            events = calendar_client.get_tomorrow_events()
        else:
            target_date = now_jst.date()
            events = calendar_client.get_today_events()

        slots = calendar_client.get_free_slots(target_date)

        # ヘッダー / header
        date_display = target_date.strftime("%Y/%m/%d")
        weekday = weekday_names[target_date.weekday()]
        lines = [f"📅 {date_display}（{weekday}）の予定", "─────────────"]

        if not events:
            lines.append("予定はありません")
        else:
            for event in events:
                if event["is_all_day"]:
                    time_str = "終日"
                elif event["start"] and event["end"]:
                    time_str = (
                        f"{event['start'].strftime('%H:%M')}-"
                        f"{event['end'].strftime('%H:%M')}"
                    )
                else:
                    time_str = "時刻不明"
                title = html.escape(event["title"])
                attendees_count = len(event["attendees"])
                attendee_str = f"（{attendees_count}名）" if attendees_count > 1 else ""
                lines.append(f"{time_str} {title}{attendee_str}")

        lines.append("─────────────")

        # 空き時間（0件の場合は行ごと省略）/ free slots (omit if empty)
        if slots:
            slot_strs = ", ".join(
                f"{s['start'].strftime('%H:%M')}-{s['end'].strftime('%H:%M')}"
                for s in slots
            )
            lines.append(f"空き時間：{slot_strs}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"/schedule エラー: {e}")
        await update.message.reply_text("⚠️ カレンダーの取得に失敗しました")


async def handle_stats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /stats [weekly] コマンドで本日または週間の統計を表示する。
    /stats command: show today's statistics or a 7-day weekly summary
    """
    bot_data = context.bot_data
    db = bot_data.get("db")

    if db is None:
        await update.message.reply_text("⚠️ データベースが利用できません")
        return

    args = context.args or []
    show_weekly = bool(args) and args[0].lower() == "weekly"

    try:
        if show_weekly:
            # 週間統計 / weekly statistics
            week = await db.get_weekly_stats()

            start_date = week[0]["date"]
            end_date = week[-1]["date"]
            start_disp = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d")
            end_disp = datetime.strptime(end_date, "%Y-%m-%d").strftime("%m/%d")
            weekday_names = ["月", "火", "水", "木", "金", "土", "日"]

            lines = [f"📊 週間統計（{start_disp}〜{end_disp}）", "─────────────"]
            total_received_sum = 0
            total_approved_sum = 0

            for entry in week:
                d = datetime.strptime(entry["date"], "%Y-%m-%d")
                day_disp = d.strftime("%m/%d")
                weekday = weekday_names[d.weekday()]
                received = entry.get("total_received", 0)
                approved = entry.get("approved", 0)
                total_received_sum += received
                total_approved_sum += approved
                lines.append(f"{day_disp}（{weekday}）：{received}件受信 / 返信{approved}件")

            lines.extend([
                "─────────────",
                f"週合計：{total_received_sum}件受信 / 返信{total_approved_sum}件",
            ])

        else:
            # 本日統計 / today's statistics
            stats = await db.get_daily_stats()
            today = datetime.now().strftime("%Y/%m/%d")

            urgent               = stats.get("urgent", 0)
            normal               = stats.get("normal", 0)
            read_only            = stats.get("read_only", 0)
            ignored              = stats.get("ignored", 0)
            total_received       = stats.get("total_received", 0)
            approved             = stats.get("approved", 0)
            pending              = stats.get("pending", 0)
            gemini_calls         = stats.get("gemini_calls", 0)
            discord_notifications = stats.get("discord_notifications", 0)

            lines = [
                f"📊 本日の統計（{today}）",
                "─────────────",
                f"📧 受信メール：{total_received}件",
                f"  ├ 要返信（重要）：{urgent}件",
                f"  ├ 要返信（通常）：{normal}件",
                f"  ├ 閲覧のみ：{read_only}件",
                f"  └ 無視：{ignored}件",
                f"✅ 返信済み：{approved}件",
                f"⏳ 承認待ち：{pending}件",
                f"🧠 Gemini API：{gemini_calls}回使用",
                f"💬 Discord通知：{discord_notifications}件",
            ]

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"/stats エラー: {e}")
        await update.message.reply_text("⚠️ 統計の取得中にエラーが発生しました。")


async def handle_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /status コマンドでシステム状態・稼働時間・本日統計・API 使用量を表示する。
    /status command: show system status, uptime, daily stats, and API usage
    """
    bot_data = context.bot_data
    pending = bot_data.get("pending_approvals", {})
    awaiting = bot_data.get("awaiting_revision")
    count = len(pending)

    lines = ["📊 <b>MY-SECRETARY ステータス</b>\n"]

    # 稼働時間 / uptime
    start_time = bot_data.get("start_time")
    if start_time:
        try:
            delta = datetime.now() - start_time
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            lines.append(f"⏱ 稼働時間: {hours}時間{minutes}分")
        except Exception:
            pass

    # 最終チェック時刻 / last check time
    last_check = bot_data.get("last_check_time")
    if last_check:
        try:
            lines.append(f"🕐 最終チェック: {last_check.strftime('%H:%M')}")
        except Exception:
            pass

    lines.append(f"📬 承認待ち: {count} 件")
    if awaiting:
        lines.append(f"✏️ 修正指示待ち: {awaiting}")

    # 本日統計 / today's stats
    db = bot_data.get("db")
    if db:
        try:
            stats = await db.get_daily_stats()
            total = stats.get("total_processed", 0)
            approved = stats.get("approved", 0)
            lines.append(f"📈 本日: {total}件処理 / {approved}件送信済み")
        except Exception:
            pass

    # Gemini API 使用量 / Gemini API usage
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

    # Discord 接続状態 / Discord connection status
    discord_client = bot_data.get("discord_client")
    if discord_client is not None:
        lines.append("💬 Discord: 接続中")
    else:
        lines.append("💬 Discord: 未接続")

    # 次の予定（12時間以内）/ next calendar event within 12 hours
    calendar_client = bot_data.get("calendar_client")
    if calendar_client is not None:
        try:
            events = calendar_client.get_upcoming_events(hours=12)
            if events:
                ev = events[0]
                ev_time = ev["start"].strftime("%H:%M")
                ev_title = html.escape(ev["title"])
                lines.append(f"📅 次の予定: {ev_time} {ev_title}")
        except Exception:
            pass

    # Web UI URL
    config = bot_data.get("config", {})
    web_port = config.get("web", {}).get("port", 8080)
    lines.append(f"🌐 Web UI: http://localhost:{web_port}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_pending_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /pending コマンドで承認待ちメール一覧をインラインボタン付きで表示する。
    /pending command: show pending emails with approve/reject inline buttons
    """
    pending = context.bot_data.get("pending_approvals", {})
    if not pending:
        await update.message.reply_text("✅ 承認待ちはありません")
        return

    for email_id, info in list(pending.items()):
        email = info.get("email", {})
        subject = html.escape(email.get("subject", "（件名なし）"))
        sender_addr = extract_email_address(email.get("sender", ""))
        category = info.get("category", "")

        text = (
            f"✉️ <b>{subject}</b>\n"
            f"差出人: {html.escape(sender_addr)}\n"
            f"分類: {category}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 承認", callback_data=f"approve:{email_id}"),
                InlineKeyboardButton("❌ 却下", callback_data=f"reject:{email_id}"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_check_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /check コマンドでメールを即時チェックし、新着件数を報告する。
    /check command: trigger immediate email check and report new mail count
    """
    bot_data = context.bot_data
    recheck_fn = bot_data.get("_recheck_fn")
    if not recheck_fn:
        await update.message.reply_text("⚠️ 再チェック機能が初期化されていません。")
        return

    await update.message.reply_text("🔄 チェック中...")

    gmail_service = bot_data.get("gmail_service")
    gemini_client = bot_data.get("gemini_client")
    config = bot_data.get("config", {})
    calendar_client = bot_data.get("calendar_client")
    db = bot_data.get("db")

    # 実行前の統計を取得 / get stats before check to calculate diff
    stats_before = {}
    if db:
        try:
            stats_before = await db.get_daily_stats()
        except Exception:
            pass

    try:
        await recheck_fn(
            gmail_service, gemini_client, context.application, config,
            calendar_client=calendar_client,
        )
    except Exception as e:
        logger.error(f"/check 実行エラー: {e}")
        await update.message.reply_text(
            f"⚠️ チェック中にエラーが発生しました：{html.escape(str(e))}",
            parse_mode="HTML",
        )
        return

    # 実行後の統計差分から新着件数を算出 / calculate new mail count from stats diff
    new_count = 0
    if db:
        try:
            stats_after = await db.get_daily_stats()
            new_count = (
                stats_after.get("total_processed", 0)
                - stats_before.get("total_processed", 0)
            )
        except Exception:
            pass

    await update.message.reply_text(f"✅ チェック完了：新着{new_count}件")


async def send_notification(bot: Bot, chat_id: str, text: str) -> None:
    """
    シンプルなテキストメッセージを Telegram に送信する。
    主にエラー通知・ステータス報告・システムメッセージに使用する。
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Telegram 通知送信エラー: {e}")


async def send_task_detection_notification(
    bot: Bot,
    chat_id: str,
    task: dict,
    source_label: str = "",
) -> None:
    """
    自動抽出タスクの確認通知を Telegram に送信する。
    Send task detection confirmation notification to Telegram.
    task は DB 保存済み（id あり）。ユーザーが「❌ 無視」を押せば DB から削除する。
    The task is already saved to DB; clicking "❌ 無視する" will delete it.
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
        logger.error(f"タスク検出通知送信エラー / Task detection notification error: {e}")


async def send_email_summary(
    bot: Bot, chat_id: str, classified_emails: list[dict]
) -> None:
    """
    分類済みメールのサマリーを Telegram に送信する。
    要返信メールがある場合は「返信案を確認」「後で」ボタンを添付する。
    """
    # カテゴリ別に件数を集計
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

    # サマリーテキストの組み立て
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

    # 返信が必要なメールがある場合はボタンを表示
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
        logger.error(f"メールサマリー送信エラー: {e}")


async def send_reply_draft(
    bot: Bot,
    chat_id: str,
    email_id: str,
    draft: str,
    subject: str,
    sender: str,
) -> None:
    """
    返信案を Telegram に送信する。
    「承認して送信」「修正指示」「却下」のインラインボタンを添付する。
    HTML 特殊文字をエスケープしてメッセージの崩れを防ぐ。
    """
    # Telegram の文字数制限に合わせて切り詰め
    draft_display = draft[:MAX_MESSAGE_LEN]
    if len(draft) > MAX_MESSAGE_LEN:
        draft_display += "\n...（以下省略）"

    # HTML 特殊文字をエスケープ
    subject_esc = html.escape(subject)
    sender_esc = html.escape(sender)
    draft_esc = html.escape(draft_display)

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
        logger.error(f"返信案送信エラー: {e}")


async def _discord_send_and_record(
    discord_client,
    bot_data: dict,
    msg_key: str,
    msg_info: dict,
    content: str,
) -> bool:
    """Send content to Discord (reply or DM) and update DB + pending state.
    Returns True on success, False on failure.
    Used by both discord_draft_send and awaiting_discord_draft_edit flows.
    """
    msg_type   = msg_info.get("type", "mention")
    channel_id = msg_info.get("channel_id", 0)
    user_id    = msg_info.get("user_id", 0)
    message_id = msg_info.get("message_id", 0)
    db_id      = msg_info.get("discord_db_id")

    if msg_type == "dm":
        success = await discord_client.send_dm(user_id, content)
    else:
        # Use send_reply() to post as a threaded reply when message_id is known
        if message_id:
            success = await discord_client.send_reply(channel_id, message_id, content)
        else:
            success = await discord_client.send_to_channel(channel_id, content)

    if success:
        # Remove from in-memory pending
        discord_client.pending_discord_messages.pop(msg_key, None)
        # Mark as replied in DB if we have a db_id
        db = bot_data.get("db")
        if db and db_id:
            try:
                await db.mark_as_replied(db_id, content)
            except Exception as e:
                logger.warning(f"mark_as_replied DB update error: {e}")

    return success


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    インラインキーボードのコールバック（ボタン押下）を処理する。
    - show_drafts: 全承認待ち返信案を順次送信
    - later: サマリーを閉じる
    - approve:{id}: 承認して Gmail 経由で送信、既読処理
    - revise:{id}: 修正指示待ち状態に設定
    - reject:{id}: 返信案を却下して pending から削除
    """
    query = update.callback_query
    await query.answer()  # Telegram のローディングスピナーを解除

    data = query.data
    bot_data = context.bot_data
    pending = bot_data.setdefault("pending_approvals", {})
    chat_id = bot_data.get("chat_id", "")

    # --- 返信案一覧を表示 ---
    if data == "show_drafts":
        if not pending:
            await query.edit_message_text("現在、承認待ちの返信案はありません。")
            return

        await query.edit_message_text(f"返信案 {len(pending)} 件を送信します...")
        for email_id, info in list(pending.items()):
            await send_reply_draft(
                bot=context.bot,
                chat_id=chat_id,
                email_id=email_id,
                draft=info["draft"],
                subject=info["email"].get("subject", ""),
                sender=info["email"].get("sender", ""),
            )

    # --- 後で確認 ---
    elif data == "later":
        await query.edit_message_text("了解しました。後でご確認ください。")

    # --- 返信案を承認して送信 ---
    elif data.startswith("approve:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        info = pending[email_id]
        draft = info["draft"]
        email = info["email"]
        original_subject = email.get("subject", "")

        # 件名を "Re: ..." 形式に整える
        if original_subject.lower().startswith("re:"):
            reply_subject = original_subject
        else:
            reply_subject = f"Re: {original_subject}"

        # 宛先は元メールの送信者（From アドレス）
        to_addr = extract_email_address(email.get("sender", ""))

        # Gmail 経由で送信
        gmail_service = bot_data.get("gmail_service")
        success = send_email(gmail_service, to=to_addr, subject=reply_subject, body=draft)

        if success:
            # 承認後に元メールを既読にする
            mark_as_read(gmail_service, email_id)
            del pending[email_id]
            db = bot_data.get("db")
            if db:
                await db.update_email_status(email_id, "approved")
            await query.edit_message_text(
                f"✅ 返信を送信しました。\n宛先：{html.escape(to_addr)}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                "❌ 送信に失敗しました。後で再試行してください。"
            )

    # --- 修正指示を求める ---
    elif data.startswith("revise:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        # 修正指示待ち状態に設定（次のテキストメッセージで処理）
        bot_data["awaiting_revision"] = email_id
        await query.edit_message_text(
            "✏️ 修正指示を入力してください。\n"
            "（例：「もっと簡潔に」「敬語を柔らかく」「締め切りを強調して」）"
        )

    # --- 閲覧のみ（返信不要・既読処理） ---
    elif data.startswith("viewonly:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        info = pending[email_id]
        email = info["email"]
        gmail_service = bot_data.get("gmail_service")
        mark_as_read(gmail_service, email_id)
        del pending[email_id]
        db = bot_data.get("db")
        if db:
            await db.update_email_status(email_id, "read_only")

        memory_path = bot_data.get(
            "memory_path",
            r"C:\Users\hosom\.claude\projects\C--Users-hosom-my-secretary\memory\MEMORY.md",
        )
        _log_classification_correction(email, memory_path)

        await query.edit_message_text(
            f"📖 閲覧のみに変更しました。\n件名：{html.escape(email.get('subject', ''))}",
            parse_mode="HTML",
        )

    # --- 返信案を却下 ---
    elif data.startswith("reject:"):
        email_id = data.split(":", 1)[1]

        if email_id in pending:
            subject = pending[email_id]["email"].get("subject", "")
            del pending[email_id]
            db = bot_data.get("db")
            if db:
                await db.update_email_status(email_id, "rejected")
            await query.edit_message_text(
                f"❌ 返信案を却下しました。\n件名：{html.escape(subject)}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")

    # --- メール再チェック ---
    elif data == "recheck_now":
        await query.edit_message_text("🔄 メールをチェック中...")
        recheck_fn = bot_data.get("_recheck_fn")
        if recheck_fn:
            gmail_service = bot_data.get("gmail_service")
            gemini_client = bot_data.get("gemini_client")
            config = bot_data.get("config", {})
            try:
                await recheck_fn(gmail_service, gemini_client, context.application, config)
            except Exception as e:
                logger.error(f"再チェックエラー: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ 再チェック中にエラーが発生しました：{html.escape(str(e))}",
                    parse_mode="HTML",
                )
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 再チェック機能が初期化されていません。")

    # --- 詳細ステータス（/status と同等） ---
    elif data == "detailed_status":
        count = len(pending)
        awaiting = bot_data.get("awaiting_revision")
        status_text = f"📊 <b>MY-SECRETARY ステータス</b>\n\n承認待ち返信案: {count} 件"
        if awaiting:
            status_text += f"\n修正指示待ち: {awaiting}"
        if pending:
            status_text += "\n\n<b>承認待ちリスト:</b>"
            for eid, info in list(pending.items()):
                subject = html.escape(info["email"].get("subject", "（件名なし）"))
                cat = info.get("category", "")
                status_text += f"\n・{subject}（{cat}）"
        status_text += _build_api_usage_text(bot_data)
        await query.edit_message_text(status_text, parse_mode="HTML")

    # --- 今日の予定を再表示 ---
    elif data == "show_calendar":
        calendar_client = bot_data.get("calendar_client")
        if calendar_client is None:
            await query.edit_message_text("📅 カレンダーが設定されていません。")
            return
        try:
            summary = calendar_client.format_today_summary()
            await query.edit_message_text(summary)
        except Exception as e:
            logger.error(f"カレンダー再表示エラー: {e}")
            await query.edit_message_text("⚠️ カレンダーの取得に失敗しました。")

    # --- Discord 返信 ---
    elif data.startswith("discord_reply:"):
        msg_key = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return
        bot_data["awaiting_discord_reply"] = msg_key
        await query.edit_message_text("💬 返信内容を入力してください。")

    # --- Discord 既読のみ ---
    elif data.startswith("discord_dismiss:"):
        msg_key = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if discord_client and msg_key in discord_client.pending_discord_messages:
            del discord_client.pending_discord_messages[msg_key]
        await query.edit_message_text("👀 既読にしました。")

    # --- Discord draft: send as-is ---
    elif data.startswith("discord_draft_send:"):
        msg_key = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return

        msg_info = discord_client.pending_discord_messages[msg_key]
        draft = msg_info.get("draft", "")
        if not draft:
            await query.edit_message_text("⚠️ 返信案が見つかりません。")
            return

        success = await _discord_send_and_record(
            discord_client=discord_client,
            bot_data=bot_data,
            msg_key=msg_key,
            msg_info=msg_info,
            content=draft,
        )
        sender = html.escape(msg_info.get("sender_name", ""))
        if success:
            channel_name = msg_info.get("channel_name")
            location = f"#{html.escape(channel_name)}" if channel_name else "DM"
            await query.edit_message_text(
                f"✅ Replied on Discord ({location} → {sender})",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ Discord への返信に失敗しました（{html.escape(sender)}）",
                parse_mode="HTML",
            )

    # --- Discord draft: edit before sending ---
    elif data.startswith("discord_draft_edit:"):
        msg_key = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return

        bot_data["awaiting_discord_draft_edit"] = msg_key
        await query.edit_message_text(
            "📝 送信する内容を入力してください。\n"
            "Enter the text you want to send on Discord:"
        )

    # --- Discord unreplied: generate reply now ---
    elif data.startswith("discord_unreplied_generate:"):
        db_id_str = data.split(":", 1)[1]
        db = bot_data.get("db")
        discord_client = bot_data.get("discord_client")

        if not db or not discord_client:
            await query.edit_message_text("⚠️ Discord クライアントまたは DB が利用できません。")
            return

        try:
            db_id = int(db_id_str)
        except ValueError:
            await query.edit_message_text("⚠️ データ形式エラー。")
            return

        await query.edit_message_text("💬 返信案を生成中...")

        # Fetch the DB row to get message details
        try:
            row = await db.get_discord_message_by_id(db_id)
        except Exception as e:
            logger.error(f"discord_unreplied_generate DB fetch error: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ DB 取得エラー：{html.escape(str(e))}",
                parse_mode="HTML",
            )
            return

        if row is None:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ メッセージが見つかりません。")
            return

        row = dict(row)
        sender_name = row.get("sender_name", "Unknown")
        content     = row.get("content", "")
        is_dm       = bool(row.get("is_dm", 0))
        channel_id  = row.get("channel_id", "")
        sender_id   = row.get("sender_id", "")

        # Generate reply draft via Gemini
        try:
            from gemini_client import generate_discord_reply
            discord_style = discord_client._read_discord_style_from_memory()
            import asyncio as _asyncio
            result = await _asyncio.get_event_loop().run_in_executor(
                None,
                generate_discord_reply,
                discord_client.gemini_client,
                sender_name,
                content,
                "DM" if is_dm else f"#channel",
                [],
                discord_style,
            )
            draft_text = result.get("reply_text", "")
            confidence = result.get("confidence", 0.0)
        except Exception as e:
            logger.error(f"discord_unreplied_generate Gemini error: {e}")
            draft_text = ""
            confidence = 0.0

        if not draft_text or draft_text == "__RETRY__":
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ 返信案の生成に失敗しました。直接返信内容を入力するか再試行してください。",
            )
            return

        # Build a new msg_key and store in pending so the approval flow works
        msg_key = f"unreplied_{db_id}"
        discord_client.pending_discord_messages[msg_key] = {
            "type": "dm" if is_dm else "mention",
            "message_id": int(row.get("message_id", 0)),
            "channel_id": int(channel_id) if channel_id else 0,
            "user_id":    int(sender_id)   if sender_id   else 0,
            "sender_name": sender_name,
            "content":    content,
            "server_name": None,
            "channel_name": None,
            "draft":      draft_text,
            "confidence": confidence,
            "discord_db_id": db_id,
        }

        confidence_pct = int(confidence * 100)
        reply_text = (
            f"💬 <b>Discord 返信案（リマインダーより）</b>\n\n"
            f"送信者: {html.escape(sender_name)}\n"
            f"──────────────────\n"
            f"{html.escape(content)}\n"
            f"──────────────────\n"
            f"返信案（信頼度: {confidence_pct}%）:\n"
            f"{html.escape(draft_text)}\n"
            f"──────────────────"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 送信", callback_data=f"discord_draft_send:{msg_key}"),
            InlineKeyboardButton("📝 編集", callback_data=f"discord_draft_edit:{msg_key}"),
            InlineKeyboardButton("❌ 無視", callback_data=f"discord_dismiss:{msg_key}"),
        ]])
        await context.bot.send_message(
            chat_id=chat_id, text=reply_text, parse_mode="HTML", reply_markup=keyboard,
        )

    # --- Discord unreplied: mark as read without replying ---
    elif data.startswith("discord_mark_read:"):
        db_id_str = data.split(":", 1)[1]
        db = bot_data.get("db")
        if not db:
            await query.edit_message_text("⚠️ DB が利用できません。")
            return
        try:
            db_id = int(db_id_str)
            await db.mark_as_replied(db_id, "")
            await query.edit_message_text("👀 既読にしました（返信なし）。")
        except Exception as e:
            logger.error(f"discord_mark_read error: {e}")
            await query.edit_message_text(f"⚠️ エラー：{html.escape(str(e))}", parse_mode="HTML")

    # ── タスクコールバック / Task Callbacks ────────────────────────────
    elif data.startswith("task_done:"):
        task_id = int(data.split(":", 1)[1])
        db = context.bot_data.get("db")
        if db:
            task_list = context.bot_data.get("last_task_list", [])
            task = next((t for t in task_list if t["id"] == task_id), None)
            try:
                await db.update_task_status(task_id, "done")
                title = task["title"] if task else f"タスク#{task_id}"
                await query.edit_message_text(f"✅ 完了：{html.escape(title)}", parse_mode="HTML")
                if task:
                    context.bot_data["last_task_list"] = [
                        t for t in task_list if t["id"] != task_id
                    ]
            except Exception as e:
                await query.answer(f"エラー: {e}")

    elif data.startswith("task_del:"):
        task_id = int(data.split(":", 1)[1])
        db = context.bot_data.get("db")
        if db:
            task_list = context.bot_data.get("last_task_list", [])
            task = next((t for t in task_list if t["id"] == task_id), None)
            try:
                await db.delete_task(task_id)
                title = task["title"] if task else f"タスク#{task_id}"
                await query.edit_message_text(f"🗑 削除：{html.escape(title)}", parse_mode="HTML")
                if task:
                    context.bot_data["last_task_list"] = [
                        t for t in task_list if t["id"] != task_id
                    ]
            except Exception as e:
                await query.answer(f"エラー: {e}")

    elif data.startswith("task_edit:"):
        # 編集モードに入る：次のテキストメッセージで新タイトルを受け取る
        # Enter edit mode: the next text message will be the new title
        task_id = int(data.split(":", 1)[1])
        context.bot_data["awaiting_task_edit"] = task_id
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ 新しいタイトルを入力してください。\nEnter the new task title:",
        )

    elif data.startswith("task_confirm:"):
        # 既に DB 保存済み → 承認のみ（何もしない）
        # Task already saved to DB; just acknowledge
        await query.edit_message_text(
            query.message.text + "\n\n✅ タスクとして追加しました。",
            parse_mode="HTML",
        )

    elif data.startswith("task_ignore:"):
        task_id = int(data.split(":", 1)[1])
        db = context.bot_data.get("db")
        if db:
            try:
                await db.delete_task(task_id)
                await query.edit_message_text(
                    query.message.text + "\n\n❌ 無視しました。",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.answer(f"エラー: {e}")

    # ── 経費管理コールバック / Expense Callbacks ───────────────────────────

    elif data == "expense_receipt":
        await query.edit_message_text(
            "📸 レシートの写真を送信してください。\n（近日実装予定 / Coming soon）"
        )

    elif data == "expense_summary":
        db = context.bot_data.get("db")
        if not db:
            await query.edit_message_text("⚠️ DB が利用できません。")
            return
        month = datetime.now().strftime("%Y-%m")
        try:
            summary = await db.get_monthly_expense_summary(month)
        except Exception as e:
            await query.edit_message_text(f"⚠️ サマリー取得エラー：{html.escape(str(e))}", parse_mode="HTML")
            return

        if not summary:
            await query.edit_message_text(f"📊 {month} のデータがありません。")
            return

        lines = [f"📊 <b>{month} 支出サマリー</b>", "─────────────"]
        total = 0
        for cat, vals in sorted(summary.items(), key=lambda x: x[1]["total"], reverse=True):
            cat_esc = html.escape(cat)
            amount = vals["total"]
            cnt = vals["count"]
            total += amount
            lines.append(f"{cat_esc}：¥{amount:,}（{cnt}件）")
        lines.append("─────────────")
        lines.append(f"合計：¥{total:,}")
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")

    elif data == "expense_csv_start":
        context.bot_data["awaiting_csv_upload"] = True
        await query.edit_message_text(
            "📥 MoneyForward ME の CSV ファイルを送信してください。\n"
            "/ Please send your MoneyForward ME CSV file."
        )

    elif data == "expense_match_run":
        expense_manager = context.bot_data.get("expense_manager")
        db = context.bot_data.get("db")
        if not expense_manager or not db:
            await query.edit_message_text("⚠️ 経費マネージャーが初期化されていません。")
            return

        await query.edit_message_text("🔍 照合を実行中...")

        try:
            results = await expense_manager.match_with_moneyforward()
        except Exception as e:
            logger.error(f"照合エラー / Matching error: {e}")
            await query.edit_message_text(f"⚠️ 照合エラー：{html.escape(str(e))}", parse_mode="HTML")
            return

        if not results:
            # 経費テーブルが空 → 未確認の MF 取引を表示
            pending_mf = await db.get_mf_transactions(unmatched_only=True, limit=5)
            if not pending_mf:
                await query.edit_message_text("✅ 未照合の取引はありません。")
                return

            chat_id = context.bot_data.get("chat_id", "")
            await query.edit_message_text(
                f"📋 未確認の取引が {len(pending_mf)} 件あります。確認してください。"
            )
            for mf in pending_mf:
                mf_id = mf["mf_id"]
                date_disp = mf.get("date", "")[:10]
                content_disp = html.escape(mf.get("content", "（内容不明）"))
                amount = mf.get("amount", 0)
                cat = html.escape(mf.get("large_category", "未分類"))
                text = (
                    f"📝 <b>{date_disp}</b> {content_disp}\n"
                    f"金額：¥{abs(amount):,} / カテゴリ：{cat}"
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 確定", callback_data=f"ematch_y:0:{mf_id}"),
                    InlineKeyboardButton("❌ 無視", callback_data=f"ematch_no:{mf_id}"),
                ]])
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb
                    )
                except Exception as e:
                    logger.warning(f"MF 取引通知エラー: {e}")
        else:
            chat_id = context.bot_data.get("chat_id", "")
            await query.edit_message_text(f"🔍 照合候補が {len(results)} 件見つかりました。")
            for item in results[:5]:
                expense = item["expense"]
                candidates = item["candidates"]
                exp_desc = html.escape(expense.get("store_name", ""))
                exp_date = expense.get("date", "")[:10]
                exp_amount = expense.get("amount", 0)
                lines = [
                    f"💰 経費：<b>{exp_desc}</b>（{exp_date} / ¥{abs(exp_amount):,}）",
                ]
                for cand in candidates[:3]:
                    mf = cand["mf"]
                    conf = cand["confidence"]
                    mf_id = mf["mf_id"]
                    mf_content = html.escape(mf.get("content", ""))
                    mf_date = mf.get("date", "")[:10]
                    lines.append(f"  [{conf}] {mf_date} {mf_content}")
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✅ 照合確定",
                            callback_data=f"ematch_y:{expense['id']}:{mf_id}"
                        ),
                        InlineKeyboardButton(
                            "❌ 無視",
                            callback_data=f"ematch_no:{mf_id}"
                        ),
                    ]])
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="\n".join(lines),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception as e:
                    logger.warning(f"照合候補通知エラー: {e}")

    elif data.startswith("ematch_y:"):
        # "ematch_y:{expense_id}:{mft_id}"
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.answer("データ形式エラー")
            return
        exp_id_str, mft_id = parts[1], parts[2]
        db = context.bot_data.get("db")
        if db:
            exp_id = int(exp_id_str) if exp_id_str.isdigit() else 0
            if exp_id:
                await db.match_expense_to_mf(exp_id, mft_id)
        await query.edit_message_text("✅ 照合を確定しました。/ Match confirmed.")

    elif data.startswith("ematch_no:"):
        # "ematch_no:{mft_id}"
        mft_id = data.split(":", 1)[1]
        await query.edit_message_text("❌ 現金払い（照合なし）として登録しました。/ Marked as cash (no match).")

    elif data == "expense_annual":
        db = context.bot_data.get("db")
        if not db:
            await query.edit_message_text("⚠️ DB が利用できません。")
            return
        year = datetime.now().strftime("%Y")
        lines = [f"📋 <b>{year}年 月別支出レポート</b>", "─────────────"]
        year_total = 0
        for m in range(1, 13):
            month_str = f"{year}-{m:02d}"
            try:
                summary = await db.get_monthly_expense_summary(month_str)
                month_total = sum(v["total"] for v in summary.values())
            except Exception:
                month_total = 0
            if month_total > 0:
                lines.append(f"{m}月：¥{month_total:,}")
                year_total += month_total
        if year_total == 0:
            lines.append("データがありません。")
        else:
            lines.append("─────────────")
            lines.append(f"年間合計：¥{year_total:,}")
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")

    elif data == "expense_later":
        await query.edit_message_text("了解です。/expense でいつでも確認できます。")

    # ── Receipt photo approval flow ──────────────────────────────────────────

    elif data == "rcpt_save":
        chat_id = str(update.effective_chat.id)
        db = context.bot_data.get("db")
        pending = context.bot_data.get("pending_receipts", {}).pop(chat_id, None)
        if not pending or not db:
            await query.edit_message_text("⚠️ 保存するレシートが見つかりません。")
            return
        ocr = pending["ocr"]
        try:
            await db.save_expense(
                date=ocr.get("date") or datetime.now().strftime("%Y-%m-%d"),
                store_name=ocr.get("store_name") or "不明",
                amount=ocr.get("total") or 0,
                category=pending["category"],
                tax_amount=ocr.get("tax"),
                subcategory=pending.get("subcategory"),
                payment_method=ocr.get("payment_method") or "cash",
                receipt_image_path=pending["image_path"],
                source="receipt_photo",
            )
            await query.edit_message_text(
                f"✅ <b>保存しました</b>\n"
                f"店名: {html.escape(ocr.get('store_name','不明'))} / "
                f"¥{(ocr.get('total') or 0):,} / {html.escape(pending['category'])}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Receipt save error: {e}")
            await query.edit_message_text(f"⚠️ 保存エラー：{html.escape(str(e))}", parse_mode="HTML")

    elif data == "rcpt_discard":
        chat_id = str(update.effective_chat.id)
        pending = context.bot_data.get("pending_receipts", {}).pop(chat_id, None)
        if pending:
            try:
                Path(pending["image_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        await query.edit_message_text("❌ 破棄しました。/ Receipt discarded.")

    elif data == "rcpt_edit":
        chat_id = str(update.effective_chat.id)
        pending = context.bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        cats = list(CATEGORY_KEYWORDS.keys())
        # Build 2-per-row keyboard
        rows = []
        for i in range(0, len(cats), 2):
            row = [InlineKeyboardButton(cats[i], callback_data=f"rcpt_cat:{cats[i]}")]
            if i + 1 < len(cats):
                row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"rcpt_cat:{cats[i + 1]}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅️ 戻る", callback_data="rcpt_back")])
        await query.edit_message_text(
            "📂 勘定科目を選択してください：",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("rcpt_cat:"):
        chat_id = str(update.effective_chat.id)
        new_category = data.split(":", 1)[1]
        pending = context.bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        pending["category"] = new_category
        pending["subcategory"] = None
        await query.edit_message_text(
            _format_receipt_summary(pending["ocr"], new_category),
            parse_mode="HTML",
            reply_markup=_receipt_approval_keyboard(),
        )

    elif data == "rcpt_back":
        chat_id = str(update.effective_chat.id)
        pending = context.bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        await query.edit_message_text(
            _format_receipt_summary(pending["ocr"], pending["category"]),
            parse_mode="HTML",
            reply_markup=_receipt_approval_keyboard(),
        )

    else:
        logger.warning(f"未知のコールバックデータ: {data}")


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    ユーザーからの自由テキスト（修正指示など）を受け取って処理する。
    awaiting_revision が設定されている場合、Gemini に再生成を依頼して
    修正済み返信案を再送する。
    """
    bot_data = context.bot_data

    # タスク編集待ち状態の確認 / Check task edit mode
    # awaiting_discord_reply・awaiting_revision より先にチェック
    awaiting_task_edit = bot_data.get("awaiting_task_edit")
    if awaiting_task_edit:
        new_title = update.message.text.strip()
        db = bot_data.get("db")
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
        return

    # CSV アップロード待ち中のテキスト案内 / Guide text while waiting for CSV upload
    if bot_data.get("awaiting_csv_upload"):
        await update.message.reply_text(
            "📎 テキストではなく CSV ファイルを添付してください。/ Please attach a CSV file, not text."
        )
        return

    # Discord draft edit mode: user typed the edited reply text
    awaiting_draft_edit = bot_data.get("awaiting_discord_draft_edit")
    if awaiting_draft_edit:
        bot_data["awaiting_discord_draft_edit"] = None
        discord_client = bot_data.get("discord_client")
        msg_info = (
            discord_client.pending_discord_messages.get(awaiting_draft_edit, {})
            if discord_client else {}
        )
        if not msg_info:
            await update.message.reply_text("⚠️ 対象メッセージが見つかりません（既に処理済みの可能性あり）。")
            return

        edited_content = update.message.text.strip()
        if not edited_content:
            await update.message.reply_text("⚠️ 空のテキストのためキャンセルしました。")
            return

        success = await _discord_send_and_record(
            discord_client=discord_client,
            bot_data=bot_data,
            msg_key=awaiting_draft_edit,
            msg_info=msg_info,
            content=edited_content,
        )
        sender = html.escape(msg_info.get("sender_name", ""))
        if success:
            channel_name = msg_info.get("channel_name")
            location = f"#{html.escape(channel_name)}" if channel_name else "DM"
            await update.message.reply_text(
                f"✅ Replied on Discord ({location} → {sender})",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ Discord への返信に失敗しました（{html.escape(sender)}）",
                parse_mode="HTML",
            )
        return

    # Discord 返信待ち状態の確認（awaiting_revision より先にチェック）
    awaiting_discord = bot_data.get("awaiting_discord_reply")
    if awaiting_discord:
        discord_client = bot_data.get("discord_client")
        msg_info = discord_client.pending_discord_messages.get(awaiting_discord, {}) if discord_client else {}
        success = False
        if msg_info.get("type") == "dm":
            success = await discord_client.send_dm(msg_info["user_id"], update.message.text)
        elif msg_info:
            success = await discord_client.send_to_channel(msg_info["channel_id"], update.message.text)
        bot_data["awaiting_discord_reply"] = None
        if success:
            if discord_client and awaiting_discord in discord_client.pending_discord_messages:
                del discord_client.pending_discord_messages[awaiting_discord]
            await update.message.reply_text("✅ Discord に返信しました。")
        else:
            await update.message.reply_text("❌ Discord への返信に失敗しました。")
        return

    awaiting = bot_data.get("awaiting_revision")

    # 修正待ち状態でなければ案内メッセージを返す
    if not awaiting:
        await update.message.reply_text(
            "コマンドを受け付けていません。\n"
            "返信案のボタン（✅ 承認 / ✏️ 修正 / ❌ 却下）を使用してください。"
        )
        return

    pending = bot_data.get("pending_approvals", {})

    # 修正対象の返信案が見つからない場合
    if awaiting not in pending:
        bot_data["awaiting_revision"] = None
        await update.message.reply_text("⚠️ 修正対象の返信案が見つかりません。")
        return

    user_instruction = update.message.text
    info = pending[awaiting]
    chat_id = bot_data.get("chat_id", "")

    await update.message.reply_text("返信案を修正中...")

    try:
        gemini_client = bot_data.get("gemini_client")

        # Gemini に修正を依頼
        revised_draft = refine_reply_draft(
            gemini_client, info["draft"], user_instruction
        )

        # 修正済み返信案を保存して修正待ち状態をリセット
        pending[awaiting]["draft"] = revised_draft
        bot_data["awaiting_revision"] = None

        # 修正済み返信案を再送
        await send_reply_draft(
            bot=context.bot,
            chat_id=chat_id,
            email_id=awaiting,
            draft=revised_draft,
            subject=info["email"].get("subject", ""),
            sender=info["email"].get("sender", ""),
        )

    except Exception as e:
        logger.error(f"返信案修正エラー: {e}")
        bot_data["awaiting_revision"] = None
        await update.message.reply_text(
            f"⚠️ 修正中にエラーが発生しました：{html.escape(str(e))}",
            parse_mode="HTML",
        )


def _parse_important_contacts(content: str) -> list[dict]:
    """
    contacts.md から優先度「高」またはタグ「重要」の連絡先を抽出する。
    Parse contacts with priority '高' or tag '重要' from contacts.md.
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
        # 優先度「高」またはタグ「重要」でフィルタ / filter by priority or tag
        if data.get('priority') == '高' or '重要' in tags:
            contacts.append({
                'name': name,
                'email': data.get('email', ''),
                'frequency': data.get('frequency', ''),
                'last_contact': data.get('last_contact', ''),
            })
    return contacts


async def handle_quiet_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /quiet [N] コマンドで Telegram 通知を N 時間（デフォルト1時間）停止する。
    /quiet command: pause Telegram notifications for N hours (default 1)
    """
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

    now = datetime.now()
    until = now + timedelta(hours=hours)
    bot_data["quiet_until"] = until
    bot_data["quiet_since"] = now
    bot_data["quiet_email_count"] = 0

    resume_str = until.strftime("%H:%M")
    await update.message.reply_text(
        f"🔇 通知を{hours}時間停止しました（{resume_str} に再開）"
    )


async def handle_resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /resume コマンドで停止中の Telegram 通知を再開する。
    /resume command: resume Telegram notifications
    """
    bot_data = context.bot_data
    quiet_until = bot_data.get("quiet_until")

    if not quiet_until or datetime.now() >= quiet_until:
        await update.message.reply_text("🔔 通知は停止中ではありません")
        return

    email_count = bot_data.get("quiet_email_count", 0)
    bot_data["quiet_until"] = None
    bot_data["quiet_since"] = None
    bot_data["quiet_email_count"] = 0

    msg = "🔔 通知を再開しました"
    if email_count > 0:
        msg += f"\n📬 停止中に届いたメール：{email_count}件"
    await update.message.reply_text(msg)


async def handle_contacts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /contacts コマンドで重要連絡先（優先度「高」またはタグ「重要」）一覧を表示する。
    /contacts command: show important contacts list
    """
    bot_data = context.bot_data
    contacts_path = bot_data.get("contacts_path")

    if not contacts_path or not os.path.exists(contacts_path):
        await update.message.reply_text("👥 重要連絡先はまだ登録されていません")
        return

    try:
        with open(contacts_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"/contacts 読み込みエラー: {e}")
        await update.message.reply_text("⚠️ 連絡先ファイルの読み込みに失敗しました")
        return

    contacts = _parse_important_contacts(content)

    if not contacts:
        await update.message.reply_text("👥 重要連絡先はまだ登録されていません")
        return

    lines = [f"👥 重要連絡先（{len(contacts)}名）", "─────────────"]
    for c in contacts:
        name = html.escape(c['name'])
        email = html.escape(c['email'])
        last = c.get('last_contact', '')
        freq = c.get('frequency', '')
        try:
            date_disp = datetime.strptime(last, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            date_disp = last
        lines.append(f"⭐ {name} - {email}")
        lines.append(f"   最終：{date_disp} / 頻度：{freq}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_todo_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /todo <内容> [期限] でタスクを手動追加する。
    Manually add a task: /todo <title> [due_date]

    使用例 / Examples:
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

    db = context.bot_data.get("db")
    task_manager = context.bot_data.get("task_manager")
    chat_id = context.bot_data.get("chat_id", "")

    if not db:
        await update.message.reply_text("⚠️ DB が初期化されていません。")
        return

    # 末尾の日付表現を分離 / Split trailing date expression
    title, date_token = _split_title_and_date(args_text)
    due_date = _parse_due_date(date_token) if date_token else ""

    # 優先度を自動判定 / Auto-determine priority from keywords
    task_dict = {"title": title, "description": "", "due_date": due_date}
    priority = task_manager.auto_prioritize(task_dict) if task_manager else "medium"

    # DB に保存 / Save to DB
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
        logger.error(f"タスク保存エラー / Task save error: {e}")
        await update.message.reply_text(f"⚠️ タスクの保存に失敗しました：{e}")
        return

    priority_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(priority, "🟡")
    priority_ja  = {"urgent": "緊急", "high": "高", "medium": "中", "low": "低"}.get(priority, "中")
    due_part = f" / 期限：{due_date[:10]}" if due_date else ""

    await update.message.reply_text(
        f"✅ タスク追加：{html.escape(title)}\n"
        f"（{priority_icon} 優先度：{priority_ja}{due_part}）",
        parse_mode="HTML",
    )
    logger.info(f"タスク手動追加 / Manual task added: id={task_id} title={title!r}")


async def handle_tasks_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /tasks [filter] で未完了タスク一覧を表示する。
    Show active task list. Optional filters: urgent / today / overdue

    表示例 / Display example:
      📋 タスク一覧（5件）
      🔴 1. 確定申告の書類準備（期限：3/15 残り21日）
      🟠 2. 見積送付（期限：2/25 残り3日）
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
            raw = await db.get_tasks(limit=30)
            tasks = [t for t in raw if t.get("status") not in ("done", "cancelled")]
    except Exception as e:
        await update.message.reply_text(f"⚠️ タスク取得エラー：{e}")
        return

    if not tasks:
        label = {"urgent": "緊急", "today": "今日", "overdue": "期限切れ"}.get(filter_arg, "未完了")
        await update.message.reply_text(f"📋 {label}タスクはありません。")
        return

    # 最後に表示したリストを bot_data に保存（/done <番号> で参照）
    # Save last displayed list to bot_data for /done <number> reference
    context.bot_data["last_task_list"] = tasks

    PRIORITY_ICON = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    lines = [f"📋 <b>タスク一覧（{len(tasks)}件）</b>", "─────────────"]
    for i, t in enumerate(tasks, 1):
        icon = "🔵" if t.get("status") == "in_progress" else PRIORITY_ICON.get(t.get("priority", "medium"), "🟡")
        due  = _format_due_display(t.get("due_date", ""))
        lines.append(f"{icon} {i}. {html.escape(t['title'])}{due}")

    # 最大 10 件分のインラインボタン（3 ボタン × 1 行/タスク）
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
    /done <番号> で /tasks 一覧の番号に対応するタスクを完了にする。
    Mark a task as done by its number from the last /tasks list.
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

    idx = int(context.args[0]) - 1  # 1-indexed → 0-indexed
    task_list: list = context.bot_data.get("last_task_list", [])

    if not task_list:
        await update.message.reply_text("先に /tasks でタスク一覧を表示してください。")
        return
    if idx < 0 or idx >= len(task_list):
        await update.message.reply_text(f"⚠️ 番号 {idx + 1} は範囲外です（1〜{len(task_list)}）。")
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
    # リストから除去して番号ズレを防ぐ / Remove from list to keep numbers consistent
    context.bot_data["last_task_list"] = [t for t in task_list if t["id"] != task["id"]]
    logger.info(f"タスク完了 / Task done: id={task['id']} title={task['title']!r}")


async def handle_expense_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /expense で経費管理メニューを表示する。
    / Show expense management menu.
    """
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 レシート撮影", callback_data="expense_receipt")],
        [InlineKeyboardButton("📊 今月のサマリー", callback_data="expense_summary")],
        [InlineKeyboardButton("📥 MoneyForward CSV 読込", callback_data="expense_csv_start")],
        [InlineKeyboardButton("🔍 未照合の経費を確認", callback_data="expense_match_run")],
        [InlineKeyboardButton("📋 年間レポート", callback_data="expense_annual")],
    ])
    await update.message.reply_text("💰 <b>経費管理</b>", parse_mode="HTML", reply_markup=keyboard)


# ── Receipt photo helpers ────────────────────────────────────────────────────

def _receipt_approval_keyboard() -> InlineKeyboardMarkup:
    """Return the Save / Edit Category / Discard inline keyboard for receipt review."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 保存",      callback_data="rcpt_save"),
        InlineKeyboardButton("📝 科目変更",  callback_data="rcpt_edit"),
        InlineKeyboardButton("❌ 破棄",      callback_data="rcpt_discard"),
    ]])


def _format_receipt_summary(ocr: dict, category: str) -> str:
    """Return the HTML summary string shown after receipt OCR."""
    date_str   = html.escape(ocr.get("date") or "不明")
    store_str  = html.escape(ocr.get("store_name") or "不明")
    total      = ocr.get("total") or 0
    tax        = ocr.get("tax") or 0
    items      = ocr.get("items") or []
    item_names = " / ".join(
        html.escape(it.get("name", "")) for it in items[:5] if it.get("name")
    ) or "（品目なし）"
    cat_str = html.escape(category)
    return (
        "🧾 <b>レシート読み取り結果</b>\n"
        "─────────────\n"
        f"📅 日付: {date_str}\n"
        f"🏪 店名: {store_str}\n"
        f"💰 金額: ¥{total:,}（消費税: ¥{tax:,}）\n"
        f"📦 品目: {item_names}\n"
        f"📂 勘定科目: {cat_str}（自動）\n"
        "─────────────"
    )


async def handle_receipt_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle an incoming photo message as a receipt.
    Downloads, OCRs via Gemini vision, auto-categorizes, then shows a
    Save / Edit / Discard approval flow.
    """
    expense_manager = context.bot_data.get("expense_manager")
    db = context.bot_data.get("db")
    if not expense_manager or not db:
        await update.message.reply_text("⚠️ 経費マネージャーが初期化されていません。")
        return

    # Send placeholder while processing
    placeholder = await update.message.reply_text("⏳ OCR 中... / Scanning receipt...")

    # Save photo to data/receipts/
    save_dir = Path("data/receipts")
    save_dir.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".jpg"
    save_path = save_dir / filename

    try:
        photo = update.message.photo[-1]  # largest available size
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(str(save_path))
    except Exception as e:
        logger.error(f"Receipt photo download error: {e}")
        await placeholder.edit_text(f"⚠️ 画像の取得に失敗しました：{html.escape(str(e))}", parse_mode="HTML")
        return

    # OCR
    try:
        ocr = await expense_manager.analyze_receipt_image(str(save_path))
    except Exception as e:
        logger.error(f"Receipt OCR error: {e}")
        ocr = {"store_name": "不明", "total": 0, "items": [], "tax": 0, "date": None}

    # Auto-categorize
    try:
        category, subcategory = await expense_manager.auto_categorize(
            ocr.get("store_name", "不明"), ocr.get("items", [])
        )
    except Exception as e:
        logger.warning(f"Receipt auto-categorize error: {e}")
        category, subcategory = "雑費", None

    # Store pending state keyed by chat_id
    chat_id = str(update.effective_chat.id)
    context.bot_data.setdefault("pending_receipts", {})[chat_id] = {
        "image_path": str(save_path),
        "ocr": ocr,
        "category": category,
        "subcategory": subcategory,
    }

    await placeholder.edit_text(
        _format_receipt_summary(ocr, category),
        parse_mode="HTML",
        reply_markup=_receipt_approval_keyboard(),
    )


async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    CSV ファイル受信ハンドラー。awaiting_csv_upload=True の場合のみ処理する。
    / Handle received documents. Only processes when awaiting_csv_upload is True.
    """
    if not context.bot_data.get("awaiting_csv_upload"):
        return  # CSV 待ち状態でなければ無視 / Ignore if not waiting for CSV

    doc = update.message.document
    if not doc.file_name.lower().endswith(".csv"):
        await update.message.reply_text(
            "⚠️ CSV ファイルを送信してください。/ Please send a CSV file."
        )
        return

    context.bot_data["awaiting_csv_upload"] = False
    await update.message.reply_text("⏳ 読み込み中... / Importing...")

    import tempfile
    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    expense_manager = context.bot_data.get("expense_manager")
    try:
        count = await expense_manager.import_moneyforward_csv(tmp_path)
    except Exception as e:
        logger.error(f"CSV インポートエラー / CSV import error: {e}")
        await update.message.reply_text(
            f"⚠️ インポートに失敗しました：{html.escape(str(e))}", parse_mode="HTML"
        )
        return
    finally:
        import os as _os
        _os.unlink(tmp_path)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 照合を実行", callback_data="expense_match_run"),
        InlineKeyboardButton("後で", callback_data="expense_later"),
    ]])
    await update.message.reply_text(
        f"✅ <b>{count}件インポートしました。</b>\n照合を実行しますか？",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _log_classification_correction(email: dict, memory_path: str) -> None:
    """
    分類修正（要返信→閲覧のみ）をMEMORY.mdの「## 分類修正ログ」セクションに追記する。
    セクションが存在しない場合は末尾に新規作成する。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = email.get("subject", "（件名なし）")
    sender = email.get("sender", "（送信者不明）")
    entry = f"- {now} | 件名: {subject} | 送信者: {sender} | 修正: 要返信→閲覧のみ\n"

    try:
        if os.path.exists(memory_path):
            with open(memory_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""

        section_header = "## 分類修正ログ\n"
        if section_header in content:
            # セクションの末尾に追記
            content = content.replace(
                section_header, section_header + entry, 1
            )
        else:
            # セクション自体を末尾に追加
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n{section_header}{entry}"

        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"分類修正ログを記録しました: {subject}")
    except Exception as e:
        logger.error(f"MEMORY.md への分類修正ログ書き込みエラー: {e}")
