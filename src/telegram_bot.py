"""
telegram_bot.py
Telegram Bot API を操作するモジュール。
ユーザーへの通知送信と、ボタン・テキスト返答の受け取りを担当する。
python-telegram-bot v20+ の非同期 API を使用する。
"""

import html
import logging
import os
from datetime import datetime
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

logger = logging.getLogger(__name__)

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
    app.add_handler(CommandHandler("status",  handle_status_command))
    app.add_handler(CommandHandler("help",    handle_help_command))
    app.add_handler(CommandHandler("pending", handle_pending_command))
    app.add_handler(CommandHandler("check",   handle_check_command))

    # インラインキーボードのコールバックハンドラー
    app.add_handler(CallbackQueryHandler(handle_callback))

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
        "/help — このヘルプを表示\n\n"
        "<i>未実装（予定）:</i>\n"
        "/search — メール検索\n"
        "/schedule — 今日の予定\n"
        "/stats — 統計レポート\n"
        "/contacts — 重要連絡先\n"
        "/quiet — 通知一時停止\n"
        "/resume — 通知再開"
    )
    await update.message.reply_text(text, parse_mode="HTML")


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
