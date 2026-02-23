"""
handlers/email_handlers.py
Email approval workflow: commands that operate on pending emails,
email-related callbacks, and the reply-revision free-text flow.
"""

import html
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from gmail_client import send_email, mark_as_read
from gemini_client import refine_reply_draft
from classifier import extract_email_address
from handlers.common import send_reply_draft, _build_api_usage_text, MAX_MESSAGE_LEN

logger = logging.getLogger(__name__)


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_search_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/search <keyword> command: search emails in DB by keyword and show results."""
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
        logger.error(f"/search error: {e}")
        await update.message.reply_text("⚠️ 検索中にエラーが発生しました。")
        return

    if not results:
        await update.message.reply_text(
            f"🔍 「{html.escape(keyword)}」に一致するメールが見つかりませんでした"
        )
        return

    # Status display labels
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
        sender       = html.escape(row.get("sender", "（不明）"))
        subject      = html.escape(row.get("subject", "（件名なし）"))
        status_label = status_labels.get(row.get("status", ""), row.get("status", ""))
        lines.append(f"{i}. {date_str} {sender} - {subject} [{status_label}]")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/schedule [tomorrow] command: show today's or tomorrow's events and free slots."""
    bot_data        = context.bot_data
    calendar_client = bot_data.get("calendar_client")

    if calendar_client is None:
        await update.message.reply_text("📅 カレンダーが設定されていません")
        return

    # Determine target day from arguments
    args         = context.args or []
    show_tomorrow = bool(args) and args[0].lower() == "tomorrow"

    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    JST           = ZoneInfo("Asia/Tokyo")
    now_jst       = datetime.now(JST)

    try:
        if show_tomorrow:
            target_date = (now_jst + timedelta(days=1)).date()
            events      = calendar_client.get_tomorrow_events()
        else:
            target_date = now_jst.date()
            events      = calendar_client.get_today_events()

        slots = calendar_client.get_free_slots(target_date)

        date_display = target_date.strftime("%Y/%m/%d")
        weekday      = weekday_names[target_date.weekday()]
        lines        = [f"📅 {date_display}（{weekday}）の予定", "─────────────"]

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
                title           = html.escape(event["title"])
                attendees_count = len(event["attendees"])
                attendee_str    = f"（{attendees_count}名）" if attendees_count > 1 else ""
                lines.append(f"{time_str} {title}{attendee_str}")

        lines.append("─────────────")

        # Free slots (omit section if empty)
        if slots:
            slot_strs = ", ".join(
                f"{s['start'].strftime('%H:%M')}-{s['end'].strftime('%H:%M')}"
                for s in slots
            )
            lines.append(f"空き時間：{slot_strs}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"/schedule error: {e}")
        await update.message.reply_text("⚠️ カレンダーの取得に失敗しました")


async def handle_stats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/stats [weekly] command: show today's statistics or a 7-day weekly summary."""
    bot_data = context.bot_data
    db       = bot_data.get("db")

    if db is None:
        await update.message.reply_text("⚠️ データベースが利用できません")
        return

    args        = context.args or []
    show_weekly = bool(args) and args[0].lower() == "weekly"

    try:
        if show_weekly:
            # Weekly statistics
            week = await db.get_weekly_stats()

            start_date  = week[0]["date"]
            end_date    = week[-1]["date"]
            start_disp  = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d")
            end_disp    = datetime.strptime(end_date,   "%Y-%m-%d").strftime("%m/%d")
            weekday_names = ["月", "火", "水", "木", "金", "土", "日"]

            lines = [f"📊 週間統計（{start_disp}〜{end_disp}）", "─────────────"]
            total_received_sum = 0
            total_approved_sum = 0

            for entry in week:
                d           = datetime.strptime(entry["date"], "%Y-%m-%d")
                day_disp    = d.strftime("%m/%d")
                weekday     = weekday_names[d.weekday()]
                received    = entry.get("total_received", 0)
                approved    = entry.get("approved", 0)
                total_received_sum += received
                total_approved_sum += approved
                lines.append(f"{day_disp}（{weekday}）：{received}件受信 / 返信{approved}件")

            lines.extend([
                "─────────────",
                f"週合計：{total_received_sum}件受信 / 返信{total_approved_sum}件",
            ])

        else:
            # Today's statistics
            stats = await db.get_daily_stats()
            today = datetime.now().strftime("%Y/%m/%d")

            urgent                = stats.get("urgent", 0)
            normal                = stats.get("normal", 0)
            read_only             = stats.get("read_only", 0)
            ignored               = stats.get("ignored", 0)
            total_received        = stats.get("total_received", 0)
            approved              = stats.get("approved", 0)
            pending               = stats.get("pending", 0)
            gemini_calls          = stats.get("gemini_calls", 0)
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
        logger.error(f"/stats error: {e}")
        await update.message.reply_text("⚠️ 統計の取得中にエラーが発生しました。")


async def handle_pending_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/pending command: show pending emails with Approve / Reject inline buttons."""
    pending = context.bot_data.get("pending_approvals", {})
    if not pending:
        await update.message.reply_text("✅ 承認待ちはありません")
        return

    for email_id, info in list(pending.items()):
        email        = info.get("email", {})
        subject      = html.escape(email.get("subject", "（件名なし）"))
        sender_addr  = extract_email_address(email.get("sender", ""))
        category     = info.get("category", "")

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
    """/check command: trigger an immediate email check and report new mail count."""
    bot_data   = context.bot_data
    recheck_fn = bot_data.get("_recheck_fn")
    if not recheck_fn:
        await update.message.reply_text("⚠️ 再チェック機能が初期化されていません。")
        return

    await update.message.reply_text("🔄 チェック中...")

    gmail_service   = bot_data.get("gmail_service")
    gemini_client   = bot_data.get("gemini_client")
    config          = bot_data.get("config", {})
    calendar_client = bot_data.get("calendar_client")
    db              = bot_data.get("db")

    # Capture stats before check to compute diff
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
        logger.error(f"/check execution error: {e}")
        await update.message.reply_text(
            f"⚠️ チェック中にエラーが発生しました：{html.escape(str(e))}",
            parse_mode="HTML",
        )
        return

    # Calculate new mail count from stats diff
    new_count = 0
    if db:
        try:
            stats_after = await db.get_daily_stats()
            new_count   = (
                stats_after.get("total_processed", 0)
                - stats_before.get("total_processed", 0)
            )
        except Exception:
            pass

    await update.message.reply_text(f"✅ チェック完了：新着{new_count}件")


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_email_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle email-related callback queries.
    query.answer() has already been called by the main dispatcher.
    Handles: show_drafts, later, approve:, revise:, viewonly:, reject:,
             recheck_now, detailed_status, show_calendar.
    """
    query    = update.callback_query
    data     = query.data
    bot_data = context.bot_data
    pending  = bot_data.setdefault("pending_approvals", {})
    chat_id  = bot_data.get("chat_id", "")

    # --- Show draft list ---
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

    # --- Acknowledge / dismiss ---
    elif data == "later":
        await query.edit_message_text("了解しました。後でご確認ください。")

    # --- Approve and send reply via Gmail ---
    elif data.startswith("approve:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        info             = pending[email_id]
        draft            = info["draft"]
        email            = info["email"]
        original_subject = email.get("subject", "")

        # Normalize reply subject
        if original_subject.lower().startswith("re:"):
            reply_subject = original_subject
        else:
            reply_subject = f"Re: {original_subject}"

        # Reply address is the sender of the original email
        to_addr       = extract_email_address(email.get("sender", ""))
        gmail_service = bot_data.get("gmail_service")
        success       = send_email(gmail_service, to=to_addr, subject=reply_subject, body=draft)

        if success:
            # Mark original as read after approval
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

    # --- Request revision ---
    elif data.startswith("revise:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        # Set awaiting-revision state; next text message will contain the instruction
        bot_data["awaiting_revision"] = email_id
        await query.edit_message_text(
            "✏️ 修正指示を入力してください。\n"
            "（例：「もっと簡潔に」「敬語を柔らかく」「締め切りを強調して」）"
        )

    # --- View only (no reply, mark as read) ---
    elif data.startswith("viewonly:"):
        email_id = data.split(":", 1)[1]

        if email_id not in pending:
            await query.edit_message_text("⚠️ この返信案は既に処理済みです。")
            return

        info          = pending[email_id]
        email         = info["email"]
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

    # --- Reject draft ---
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

    # --- Re-check emails now ---
    elif data == "recheck_now":
        await query.edit_message_text("🔄 メールをチェック中...")
        recheck_fn = bot_data.get("_recheck_fn")
        if recheck_fn:
            gmail_service = bot_data.get("gmail_service")
            gemini_client = bot_data.get("gemini_client")
            config        = bot_data.get("config", {})
            try:
                await recheck_fn(gmail_service, gemini_client, context.application, config)
            except Exception as e:
                logger.error(f"Re-check error: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ 再チェック中にエラーが発生しました：{html.escape(str(e))}",
                    parse_mode="HTML",
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ 再チェック機能が初期化されていません。"
            )

    # --- Detailed status (equivalent to /status) ---
    elif data == "detailed_status":
        count        = len(pending)
        awaiting     = bot_data.get("awaiting_revision")
        status_text  = f"📊 <b>MY-SECRETARY ステータス</b>\n\n承認待ち返信案: {count} 件"
        if awaiting:
            status_text += f"\n修正指示待ち: {awaiting}"
        if pending:
            status_text += "\n\n<b>承認待ちリスト:</b>"
            for eid, info in list(pending.items()):
                subject      = html.escape(info["email"].get("subject", "（件名なし）"))
                cat          = info.get("category", "")
                status_text += f"\n・{subject}（{cat}）"
        status_text += _build_api_usage_text(bot_data)
        await query.edit_message_text(status_text, parse_mode="HTML")

    # --- Show today's calendar ---
    elif data == "show_calendar":
        calendar_client = bot_data.get("calendar_client")
        if calendar_client is None:
            await query.edit_message_text("📅 カレンダーが設定されていません。")
            return
        try:
            summary = calendar_client.format_today_summary()
            await query.edit_message_text(summary)
        except Exception as e:
            logger.error(f"Calendar re-display error: {e}")
            await query.edit_message_text("⚠️ カレンダーの取得に失敗しました。")


# ── Free-text handler (awaiting_revision state) ───────────────────────────────

async def handle_email_revision_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Process a free-text revision instruction for a pending email draft.
    Called when bot_data['awaiting_revision'] is set.
    Asks Gemini to refine the draft, then re-sends it.
    """
    bot_data = context.bot_data
    awaiting = bot_data.get("awaiting_revision")
    pending  = bot_data.get("pending_approvals", {})
    chat_id  = bot_data.get("chat_id", "")

    if awaiting not in pending:
        bot_data["awaiting_revision"] = None
        await update.message.reply_text("⚠️ 修正対象の返信案が見つかりません。")
        return

    user_instruction = update.message.text
    info             = pending[awaiting]

    await update.message.reply_text("返信案を修正中...")

    try:
        gemini_client = bot_data.get("gemini_client")
        revised_draft = refine_reply_draft(gemini_client, info["draft"], user_instruction)

        pending[awaiting]["draft"]   = revised_draft
        bot_data["awaiting_revision"] = None

        # Re-send the revised draft
        await send_reply_draft(
            bot=context.bot,
            chat_id=chat_id,
            email_id=awaiting,
            draft=revised_draft,
            subject=info["email"].get("subject", ""),
            sender=info["email"].get("sender", ""),
        )

    except Exception as e:
        logger.error(f"Reply draft revision error: {e}")
        bot_data["awaiting_revision"] = None
        await update.message.reply_text(
            f"⚠️ 修正中にエラーが発生しました：{html.escape(str(e))}",
            parse_mode="HTML",
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _log_classification_correction(email: dict, memory_path: str) -> None:
    """
    Append a classification correction (reply→view-only) to the
    '## 分類修正ログ' section of MEMORY.md. Creates the section if absent.
    """
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = email.get("subject", "（件名なし）")
    sender  = email.get("sender",  "（送信者不明）")
    entry   = f"- {now} | 件名: {subject} | 送信者: {sender} | 修正: 要返信→閲覧のみ\n"

    try:
        if os.path.exists(memory_path):
            with open(memory_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""

        section_header = "## 分類修正ログ\n"
        if section_header in content:
            content = content.replace(section_header, section_header + entry, 1)
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n{section_header}{entry}"

        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Classification correction logged: {subject}")
    except Exception as e:
        logger.error(f"MEMORY.md classification correction write error: {e}")
