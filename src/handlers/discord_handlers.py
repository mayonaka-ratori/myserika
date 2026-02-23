"""
handlers/discord_handlers.py
Discord reply and approval callbacks, plus the Discord free-text reply flows.
"""

import asyncio
import html
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from gemini_client import generate_discord_reply

logger = logging.getLogger(__name__)


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _discord_send_and_record(
    discord_client,
    bot_data: dict,
    msg_key: str,
    msg_info: dict,
    content: str,
) -> bool:
    """
    Send content to Discord (threaded reply or DM) and update DB + pending state.
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


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_discord_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle Discord-related callback queries.
    query.answer() has already been called by the main dispatcher.
    Handles: discord_reply:, discord_dismiss:, discord_draft_send:,
             discord_draft_edit:, discord_unreplied_generate:, discord_mark_read:.
    """
    query    = update.callback_query
    data     = query.data
    bot_data = context.bot_data
    chat_id  = bot_data.get("chat_id", "")

    # --- Initiate Discord reply (free-text flow) ---
    if data.startswith("discord_reply:"):
        msg_key        = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return
        bot_data["awaiting_discord_reply"] = msg_key
        await query.edit_message_text("💬 返信内容を入力してください。")

    # --- Dismiss Discord message (mark as read, no reply) ---
    elif data.startswith("discord_dismiss:"):
        msg_key        = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if discord_client and msg_key in discord_client.pending_discord_messages:
            del discord_client.pending_discord_messages[msg_key]
        await query.edit_message_text("👀 既読にしました。")

    # --- Send Discord draft as-is ---
    elif data.startswith("discord_draft_send:"):
        msg_key        = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return

        msg_info = discord_client.pending_discord_messages[msg_key]
        draft    = msg_info.get("draft", "")
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
            location     = f"#{html.escape(channel_name)}" if channel_name else "DM"
            await query.edit_message_text(
                f"✅ Replied on Discord ({location} → {sender})",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ Discord への返信に失敗しました（{html.escape(sender)}）",
                parse_mode="HTML",
            )

    # --- Edit Discord draft before sending ---
    elif data.startswith("discord_draft_edit:"):
        msg_key        = data.split(":", 1)[1]
        discord_client = bot_data.get("discord_client")
        if not discord_client or msg_key not in discord_client.pending_discord_messages:
            await query.edit_message_text("⚠️ このメッセージは既に処理済みです。")
            return

        bot_data["awaiting_discord_draft_edit"] = msg_key
        await query.edit_message_text(
            "📝 送信する内容を入力してください。\n"
            "Enter the text you want to send on Discord:"
        )

    # --- Generate reply for an unreplied Discord message ---
    elif data.startswith("discord_unreplied_generate:"):
        db_id_str      = data.split(":", 1)[1]
        db             = bot_data.get("db")
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

        # Fetch message details from DB
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
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ メッセージが見つかりません。"
            )
            return

        row         = dict(row)
        sender_name = row.get("sender_name", "Unknown")
        content     = row.get("content", "")
        is_dm       = bool(row.get("is_dm", 0))
        channel_id  = row.get("channel_id", "")
        sender_id   = row.get("sender_id", "")

        # Generate reply draft via Gemini
        try:
            discord_style = discord_client._read_discord_style_from_memory()
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                generate_discord_reply,
                discord_client.gemini_client,
                sender_name,
                content,
                "DM" if is_dm else "#channel",
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

        # Store in pending so the standard approval flow works
        msg_key = f"unreplied_{db_id}"
        discord_client.pending_discord_messages[msg_key] = {
            "type":         "dm" if is_dm else "mention",
            "message_id":   int(row.get("message_id", 0)),
            "channel_id":   int(channel_id) if channel_id else 0,
            "user_id":      int(sender_id)   if sender_id   else 0,
            "sender_name":  sender_name,
            "content":      content,
            "server_name":  None,
            "channel_name": None,
            "draft":        draft_text,
            "confidence":   confidence,
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

    # --- Mark unreplied Discord message as read without replying ---
    elif data.startswith("discord_mark_read:"):
        db_id_str = data.split(":", 1)[1]
        db        = bot_data.get("db")
        if not db:
            await query.edit_message_text("⚠️ DB が利用できません。")
            return
        try:
            db_id = int(db_id_str)
            await db.mark_as_replied(db_id, "")
            await query.edit_message_text("👀 既読にしました（返信なし）。")
        except Exception as e:
            logger.error(f"discord_mark_read error: {e}")
            await query.edit_message_text(
                f"⚠️ エラー：{html.escape(str(e))}", parse_mode="HTML"
            )


# ── Free-text handlers ────────────────────────────────────────────────────────

async def handle_discord_reply_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Send a free-text message as a Discord reply.
    Called when bot_data['awaiting_discord_reply'] is set.
    """
    bot_data        = context.bot_data
    awaiting_key    = bot_data.get("awaiting_discord_reply")
    discord_client  = bot_data.get("discord_client")
    msg_info        = (
        discord_client.pending_discord_messages.get(awaiting_key, {})
        if discord_client else {}
    )
    success = False
    if msg_info.get("type") == "dm":
        success = await discord_client.send_dm(msg_info["user_id"], update.message.text)
    elif msg_info:
        success = await discord_client.send_to_channel(
            msg_info["channel_id"], update.message.text
        )
    bot_data["awaiting_discord_reply"] = None
    if success:
        if discord_client and awaiting_key in discord_client.pending_discord_messages:
            del discord_client.pending_discord_messages[awaiting_key]
        await update.message.reply_text("✅ Discord に返信しました。")
    else:
        await update.message.reply_text("❌ Discord への返信に失敗しました。")


async def handle_discord_draft_edit_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Send user-edited text as a Discord reply.
    Called when bot_data['awaiting_discord_draft_edit'] is set.
    """
    bot_data        = context.bot_data
    awaiting_key    = bot_data.get("awaiting_discord_draft_edit")
    bot_data["awaiting_discord_draft_edit"] = None

    discord_client  = bot_data.get("discord_client")
    msg_info        = (
        discord_client.pending_discord_messages.get(awaiting_key, {})
        if discord_client else {}
    )
    if not msg_info:
        await update.message.reply_text(
            "⚠️ 対象メッセージが見つかりません（既に処理済みの可能性あり）。"
        )
        return

    edited_content = update.message.text.strip()
    if not edited_content:
        await update.message.reply_text("⚠️ 空のテキストのためキャンセルしました。")
        return

    success = await _discord_send_and_record(
        discord_client=discord_client,
        bot_data=bot_data,
        msg_key=awaiting_key,
        msg_info=msg_info,
        content=edited_content,
    )
    sender = html.escape(msg_info.get("sender_name", ""))
    if success:
        channel_name = msg_info.get("channel_name")
        location     = f"#{html.escape(channel_name)}" if channel_name else "DM"
        await update.message.reply_text(
            f"✅ Replied on Discord ({location} → {sender})",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"❌ Discord への返信に失敗しました（{html.escape(sender)}）",
            parse_mode="HTML",
        )
