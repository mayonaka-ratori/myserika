"""
telegram_bot.py
Slim entry point for the Telegram Bot.
Builds the Application, registers all handlers, and provides thin
callback/text dispatchers that route to domain-specific handler modules.

All business logic lives in handlers/:
  handlers/common.py          shared notification senders + cross-domain commands
  handlers/email_handlers.py  email approval workflow
  handlers/discord_handlers.py Discord reply/approval flow
  handlers/task_handlers.py   /todo /tasks /done and task callbacks
  handlers/expense_handlers.py /expense, receipt OCR, CSV import
"""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import handlers.common          as common
import handlers.email_handlers  as email_handlers
import handlers.discord_handlers as discord_handlers
import handlers.task_handlers   as task_handlers
import handlers.expense_handlers as expense_handlers

# Re-export public notification functions so callers that imported them from
# telegram_bot directly (e.g. main.py) continue to work without changes.
from handlers.common import (  # noqa: F401
    send_notification,
    send_email_summary,
    send_task_detection_notification,
)

logger = logging.getLogger(__name__)

# ── Callback routing constants ────────────────────────────────────────────────

_EMAIL_EXACT = frozenset({
    "show_drafts", "later", "recheck_now", "detailed_status", "show_calendar",
})
_EMAIL_PREFIXES = ("approve:", "revise:", "viewonly:", "reject:")
_DISCORD_PREFIXES = (
    "discord_reply:", "discord_dismiss:", "discord_draft_send:",
    "discord_draft_edit:", "discord_unreplied_generate:", "discord_mark_read:",
)
_TASK_PREFIXES = (
    "task_done:", "task_del:", "task_edit:", "task_confirm:", "task_ignore:",
)
_EXPENSE_STARTS = ("expense_", "ematch_", "rcpt_")


# ── Application factory ───────────────────────────────────────────────────────

def build_application(bot_token: str) -> Application:
    """
    Build and return the Telegram Application with all handlers registered.
    bot_token: Bot token from config.yaml.
    """
    app = Application.builder().token(bot_token).build()

    # Command handlers
    app.add_handler(CommandHandler("status",   common.handle_status_command))
    app.add_handler(CommandHandler("help",     common.handle_help_command))
    app.add_handler(CommandHandler("quiet",    common.handle_quiet_command))
    app.add_handler(CommandHandler("resume",   common.handle_resume_command))
    app.add_handler(CommandHandler("contacts", common.handle_contacts_command))

    app.add_handler(CommandHandler("pending",  email_handlers.handle_pending_command))
    app.add_handler(CommandHandler("check",    email_handlers.handle_check_command))
    app.add_handler(CommandHandler("search",   email_handlers.handle_search_command))
    app.add_handler(CommandHandler("schedule", email_handlers.handle_schedule_command))
    app.add_handler(CommandHandler("stats",    email_handlers.handle_stats_command))

    app.add_handler(CommandHandler("todo",     task_handlers.handle_todo_command))
    app.add_handler(CommandHandler("tasks",    task_handlers.handle_tasks_command))
    app.add_handler(CommandHandler("done",     task_handlers.handle_done_command))

    app.add_handler(CommandHandler("expense",  expense_handlers.handle_expense_command))

    # Inline-keyboard callback dispatcher
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Photo handler — receipt OCR flow
    app.add_handler(MessageHandler(filters.PHOTO, expense_handlers.handle_receipt_photo))

    # Document handler — CSV upload (registered before text handler for correct priority)
    app.add_handler(MessageHandler(filters.Document.ALL, expense_handlers.handle_document))

    # Free-text message dispatcher (non-command text only)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    return app


# ── Thin dispatchers ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route an inline-keyboard callback to the appropriate domain handler.
    Calls query.answer() once here before forwarding.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in _EMAIL_EXACT or data.startswith(_EMAIL_PREFIXES):
        await email_handlers.handle_email_callback(update, context)
    elif data.startswith(_DISCORD_PREFIXES):
        await discord_handlers.handle_discord_callback(update, context)
    elif data.startswith(_TASK_PREFIXES):
        await task_handlers.handle_task_callback(update, context)
    elif data.startswith(_EXPENSE_STARTS):
        await expense_handlers.handle_expense_callback(update, context)
    else:
        logger.warning(f"Unknown callback data: {data}")


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Route a free-text message to the appropriate domain handler based on the
    active awaiting_* state in bot_data. Falls back to a usage hint.
    """
    bot_data = context.bot_data

    # Priority order matches the original handle_text_message checks
    if bot_data.get("awaiting_task_edit"):
        await task_handlers.handle_task_edit_text(update, context)
        return

    if bot_data.get("awaiting_csv_upload"):
        await expense_handlers.handle_csv_upload_text(update, context)
        return

    if bot_data.get("awaiting_discord_draft_edit"):
        await discord_handlers.handle_discord_draft_edit_text(update, context)
        return

    if bot_data.get("awaiting_discord_reply"):
        await discord_handlers.handle_discord_reply_text(update, context)
        return

    if bot_data.get("awaiting_revision"):
        await email_handlers.handle_email_revision_text(update, context)
        return

    # No active awaiting state — guide the user to use buttons
    await update.message.reply_text(
        "コマンドを受け付けていません。\n"
        "返信案のボタン（✅ 承認 / ✏️ 修正 / ❌ 却下）を使用してください。"
    )
