"""
daily_summary.py
毎朝のブリーフィング（日次サマリー）を生成して Telegram に送信するモジュール。

【Google Calendar API 有効化の手順】
1. Google Cloud Console でプロジェクトを開く
2. 「APIとサービス」→「ライブラリ」から「Google Calendar API」を検索して有効化
3. src/gmail_client.py の SCOPES に calendar.readonly が追加済みなので、
   token.json を削除して python src/main.py を再実行すると再認証が走る
   （ブラウザが開いて Google アカウントへのアクセスを許可する）
4. 再認証後は Calendar の予定が日次ブリーフィングに表示されるようになる
"""

import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from classifier import extract_email_address
from gmail_client import _fetch_message_headers, _extract_name_and_email
from gemini_client import _call_model

logger = logging.getLogger(__name__)

# JST タイムゾーン（固定オフセット +9 時間）
JST = timezone(timedelta(hours=9))
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _format_attendees(attendees: list[str], contacts: dict, max_display: int = 3) -> str:
    """
    参加者メールアドレスリストを表示用テキストにフォーマットする内部ヘルパー。
    contacts に登録済みなら名前を、未登録なら「外部：email」形式で表示する。
    例: （田中、外部：user@example.com）
    """
    if not attendees:
        return ""

    parts = []
    for email in attendees[:max_display]:
        if email in contacts:
            name = contacts[email].get("name") or email.split("@")[0]
            parts.append(html.escape(name))
        else:
            parts.append(f"外部：{html.escape(email)}")

    remaining = len(attendees) - max_display
    if remaining > 0:
        parts.append(f"他{remaining}名")

    return "（" + "、".join(parts) + "）"


def _format_calendar_section(
    events: list[dict] | None,
    contacts: dict,
) -> list[str]:
    """
    CalendarClient.get_today_events() の戻り値をブリーフィング行リストに変換する。
    events=None（取得失敗）→ 「取得できませんでした」
    events=[]（予定なし） → 「予定なし」
    それ以外 → セパレーター + 「HH:MM-HH:MM タイトル（参加者）」行を返す
    """
    if events is None:
        return ["・取得できませんでした"]
    if not events:
        return ["・予定なし"]

    lines = ["─────────────"]
    for ev in events:
        if ev["is_all_day"]:
            time_str = "終日"
        elif ev["start"] and ev["end"]:
            time_str = (
                f"{ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')}"
            )
        else:
            time_str = "時刻不明"

        attendees_str = _format_attendees(ev["attendees"], contacts)
        lines.append(f"{time_str} {html.escape(ev['title'])}{attendees_str}")

    return lines


def _format_related_emails_section(
    pending_approvals: dict,
    today_events: list[dict],
    contacts: dict,
) -> list[str]:
    """
    今日の会議参加者からの未返信メールを検出してセクション行リストを返す。
    - pending_approvals の送信者メールと today_events の attendees を照合する
    - 一致がなければ空リストを返す（セクション自体を省略）
    """
    # 非終日イベントから attendee_email → 開始時刻 のマッピングを構築
    attendee_to_time: dict[str, str] = {}
    for ev in today_events:
        if ev["is_all_day"]:
            continue
        time_str = ev["start"].strftime("%H:%M") if ev["start"] else "?"
        for email in ev["attendees"]:
            if email not in attendee_to_time:
                attendee_to_time[email] = time_str

    if not attendee_to_time:
        return []

    # pending_approvals の送信者が参加者と一致するか照合
    matched: dict[str, dict] = {}  # email -> {"time": str, "count": int}
    for info in pending_approvals.values():
        sender_addr = extract_email_address(info["email"].get("sender", ""))
        if sender_addr in attendee_to_time:
            if sender_addr not in matched:
                matched[sender_addr] = {
                    "time": attendee_to_time[sender_addr],
                    "count": 0,
                }
            matched[sender_addr]["count"] += 1

    if not matched:
        return []

    lines = ["", "⚡ <b>予定に関連するメール</b>", "─────────────"]
    for email, data in matched.items():
        lines.append(
            f"・{data['time']}の会議参加者 {html.escape(email)} から"
            f"未返信メール{data['count']}件"
        )
    return lines


def _get_unread_summary(gmail_service, contacts: dict) -> dict:
    """
    昨日以降の未読メールを集計する内部ヘルパー。
    戻り値: { "total": int, "important_senders": list[str] }
    """
    try:
        result = gmail_service.users().messages().list(
            userId="me",
            q="is:unread in:inbox newer_than:1d",
            maxResults=50,
        ).execute()
        messages = result.get("messages", [])
        total = len(messages)

        # 重要な送信者（contacts.md で優先度「高」）からの未読を特定
        important_senders = []
        for msg in messages[:20]:
            headers = _fetch_message_headers(gmail_service, msg["id"], ["From"])
            _, addr = _extract_name_and_email(headers.get("From", ""))
            if addr in contacts and contacts[addr].get("priority") == "高":
                name = contacts[addr].get("name") or addr.split("@")[0]
                if name not in important_senders:
                    important_senders.append(name)

        return {"total": total, "important_senders": important_senders}

    except Exception as e:
        logger.error(f"未読メール集計エラー: {e}")
        return {"total": 0, "important_senders": []}


def _get_todo_suggestions(gemini_client, pending_approvals: dict) -> list[str]:
    """
    承認待ち返信案から Gemini が「今日中に返信すべきもの」を優先度順にリスト化する。
    戻り値: 優先度順の文字列リスト（例: "田中様の見積もり依頼に返信（重要）"）
    エラー時・Gemini 制限時は空リストを返す。
    """
    if not pending_approvals:
        return []

    try:
        items_text = "\n".join(
            f"- 件名: {info['email'].get('subject', '')} "
            f"| 送信者: {info['email'].get('sender', '')} "
            f"| カテゴリ: {info.get('category', '')}"
            for info in pending_approvals.values()
        )

        prompt = (
            "以下の未処理メールを「今日中に返信すべき順」に並べて、"
            "各項目を「[送信者名]の[用件]に返信（[重要度]）」という形式で5件以内にまとめてください。"
            "日本語で回答し、箇条書き記号や番号は不要で、各行1項目のみ出力してください。\n\n"
            f"【未処理メール】\n{items_text}"
        )

        text = _call_model(gemini_client, prompt)
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        return lines[:5]

    except Exception as e:
        logger.error(f"TODO提案生成エラー: {e}")
        return []


def _get_discord_summary(discord_client) -> dict | None:
    """Discord の未読カウントを取得する（日次サマリー用）。"""
    from discord_client import get_discord_stats
    return get_discord_stats(discord_client)


async def send_daily_briefing(
    bot: Bot,
    chat_id: str,
    gmail_service,
    calendar_service,
    gemini_client,
    contacts: dict,
    pending_approvals: dict,
    discord_client=None,
    calendar_client=None,
    config: dict | None = None,
    task_manager=None,
) -> None:
    """
    毎朝のブリーフィングメッセージを Telegram に送信する。
    メッセージ本文 + 操作ボタン（承認待ち確認・再チェック・詳細ステータス）を添付する。
    Google Calendar の取得が失敗してもメール部分のみで送信を継続する。
    """
    now = datetime.now(JST)
    weekday = WEEKDAY_JA[now.weekday()]
    today_str = now.strftime(f"%Y年%m月%d日（{weekday}）")

    lines = [f"☀️ <b>おはようございます！{today_str}のブリーフィングです</b>\n"]

    # ── 📬 メール状況 ──────────────────────────────
    unread = _get_unread_summary(gmail_service, contacts)
    total = unread["total"]
    important_senders = unread["important_senders"]
    pending_count = len(pending_approvals)

    lines.append("📬 <b>メール状況</b>")
    if total > 0:
        lines.append(f"・未読：{total}件（うち重要：{len(important_senders)}件）")
    else:
        lines.append("・未読メールなし")

    if pending_count > 0:
        lines.append(f"・承認待ち：{pending_count}件")

    for name in important_senders[:3]:
        lines.append(f"・重要：{name}からの未読メール")

    lines.append("")

    # ── 💬 Discord 状況 ──────────────────────────────
    discord_stats = _get_discord_summary(discord_client)
    if discord_stats and (discord_stats["mention_count"] > 0 or discord_stats["dm_count"] > 0):
        lines.append("💬 <b>Discord 状況</b>")
        if discord_stats["mention_count"] > 0:
            lines.append(f"・未確認メンション：{discord_stats['mention_count']}件")
        if discord_stats["dm_count"] > 0:
            lines.append(f"・未読DM：{discord_stats['dm_count']}件")
        lines.append("")

    # ── 📅 今日の予定 ──────────────────────────────
    calendar_enabled = True
    if config is not None:
        calendar_enabled = config.get("calendar", {}).get("enabled", True)

    if calendar_enabled:
        today_events = None
        if calendar_client is not None:
            try:
                today_events = calendar_client.get_today_events()
            except Exception as e:
                logger.error(f"カレンダー取得エラー: {e}")

        event_count = len(today_events) if today_events is not None else 0
        lines.append(f"📅 <b>本日の予定（{event_count}件）</b>")
        lines.extend(_format_calendar_section(today_events, contacts))
        lines.append("")

        # 関連メールセクション（今日の参加者からの未返信メールがあれば表示）
        if today_events and pending_approvals:
            related = _format_related_emails_section(
                pending_approvals, today_events, contacts
            )
            if related:
                lines.extend(related)
                lines.append("")

    # ── 📋 Today's Top Tasks ──────────────────────────────────────
    if task_manager is not None and config is not None:
        task_cfg = config.get("task", {})
        if task_cfg.get("enabled", False):
            try:
                top_n = task_cfg.get("daily_top_n", 3)
                # 新メソッド: 期日が近い/優先度が高いタスクを取得
                # New method: get tasks due today or high priority
                top = await task_manager.get_today_top_tasks(n=top_n)
                # 新メソッド: 期限切れタスクを days_overdue 付きで取得
                # New method: get overdue tasks with days_overdue field
                overdue = await task_manager.get_overdue_tasks()
                stats = await task_manager._db.get_task_stats()

                lines.append(
                    f"📋 <b>タスク Top {top_n}"
                    f"（未着手 {stats['todo']}件・進行中 {stats['in_progress']}件）</b>"
                )

                if top:
                    for t in top:
                        icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                            t.get("priority", "medium"), "🟡"
                        )
                        due = f"（{t['due_date'][:10]}まで）" if t.get("due_date") else ""
                        prog = "🔄 " if t.get("status") == "in_progress" else ""
                        lines.append(f"・{icon}{prog}{html.escape(t['title'])}{due}")
                else:
                    lines.append("・本日のタスクなし")

                # ⚠️ 期限切れタスクセクション / Overdue tasks section
                if overdue:
                    lines.append("")
                    lines.append(f"⚠️ <b>期限切れタスク（{len(overdue)}件）</b>")
                    for t in overdue[:5]:
                        days = t.get("days_overdue", 0)
                        overdue_label = f"（{days}日超過）" if days > 0 else ""
                        lines.append(f"・{html.escape(t['title'])}{overdue_label}")

                lines.append("")
            except Exception as e:
                logger.warning(f"タスクセクションエラー（スキップ）/ Task section error: {e}")

    # ── 📝 今日のTODO ──────────────────────────────
    lines.append("📝 <b>今日のTODO</b>")
    todos = _get_todo_suggestions(gemini_client, pending_approvals)
    if todos:
        for i, todo in enumerate(todos, 1):
            lines.append(f"{i}. {todo}")
    else:
        lines.append("・特になし")

    lines.append("")
    lines.append("💡 <b>操作ガイド</b>")
    if pending_count > 0:
        lines.append("・承認待ちがあります → 下のボタンで確認できます")
    lines.append("・新しいメールを今すぐチェック → 🔄 ボタン")
    lines.append("\n良い一日を！")

    text = "\n".join(lines)

    # ── インラインボタンを構築 ──────────────────────
    button_row_1 = []
    if pending_count > 0:
        button_row_1.append(
            InlineKeyboardButton("📋 承認待ちを確認", callback_data="show_drafts")
        )
    button_row_1.append(
        InlineKeyboardButton("🔄 メール再チェック", callback_data="recheck_now")
    )
    keyboard_rows = [button_row_1]
    keyboard_rows.append([
        InlineKeyboardButton("📅 今日の予定を再表示", callback_data="show_calendar"),
        InlineKeyboardButton("📊 詳細ステータス", callback_data="detailed_status"),
    ])
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("日次ブリーフィングを送信しました")
    except Exception as e:
        logger.error(f"日次ブリーフィング送信エラー: {e}")
