"""
handlers/__init__.py
Handler package for the Telegram Bot.
Re-exports public notification functions so external callers (main.py)
can import them from either `handlers.common` or `handlers` directly.
"""

from handlers.common import (
    send_notification,
    send_email_summary,
    send_task_detection_notification,
)

__all__ = [
    "send_notification",
    "send_email_summary",
    "send_task_detection_notification",
]
