"""
utils.py
Shared utility functions used across multiple modules.
No project-local imports — only stdlib.
"""

import re
from datetime import date, timedelta


# ── Numeric helper ────────────────────────────────────────────────────────────

def safe_int(v: object, default: int = 0) -> int:
    """Convert a value to int, tolerating commas, yen signs, and decimals.
    Returns default on any conversion failure."""
    try:
        return int(str(v).replace(",", "").replace("¥", "").replace("￥", "").split(".")[0])
    except (ValueError, TypeError):
        return default


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


def split_title_and_date(text: str) -> tuple[str, str]:
    """
    Split a trailing date expression from title text.
    Example: "書類準備 3/15" → ("書類準備", "3/15")
    Returns (title, date_token); date_token is "" when no date is found.
    """
    m = _DATE_SPLIT_RE.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1)
    return text.strip(), ""


def parse_due_date(text: str) -> str:
    """
    Parse a Japanese/English date expression to a YYYY-MM-DD string.
    Returns "" if the text cannot be parsed.
    Handles: ISO, M/D, M月D日, 今日/明日/明後日, 来週, 来週[曜日].
    """
    text  = text.strip()
    today = date.today()

    # ISO format: YYYY-MM-DD
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text

    # M/D format → this year, or next year if already past
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


def format_due_display(due_date: str) -> str:
    """Convert a DB due_date string to display text with days-remaining info."""
    if not due_date:
        return "（期限なし）"
    try:
        today = date.today()
        due   = date.fromisoformat(due_date[:10])
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
