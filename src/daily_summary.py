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

import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# JST タイムゾーン（固定オフセット +9 時間）
JST = timezone(timedelta(hours=9))
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def get_today_events(calendar_service) -> list[dict] | None:
    """
    Google Calendar API から今日（JST）のイベントを取得して返す。
    戻り値: [{ "start": "HH:MM" | "終日", "title": str, "location": str }, ...]
    エラー時は None を返す（"予定なし" と "取得失敗" を区別するため）。
    """
    if calendar_service is None:
        return None

    try:
        now = datetime.now(JST)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        events_result = calendar_service.events().list(
            calendarId="primary",
            timeMin=today_start.isoformat(),
            timeMax=today_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        items = events_result.get("items", [])
        events = []
        for item in items:
            start = item.get("start", {})
            start_str = start.get("dateTime") or start.get("date", "")

            if "T" in start_str:
                # 時刻付きイベント: JST に変換して HH:MM 表示
                dt = datetime.fromisoformat(start_str).astimezone(JST)
                time_display = dt.strftime("%H:%M")
            else:
                time_display = "終日"

            events.append({
                "start": time_display,
                "title": item.get("summary", "（タイトルなし）"),
                "location": item.get("location", ""),
            })

        return events

    except Exception as e:
        logger.error(f"Google Calendar 取得エラー: {e}")
        return None


def _get_unread_summary(gmail_service, contacts: dict) -> dict:
    """
    昨日以降の未読メールを集計する内部ヘルパー。
    戻り値: { "total": int, "important_senders": list[str] }
    """
    from gmail_client import _fetch_message_headers, _extract_name_and_email

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
        from gemini_client import _call_model

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
    lines.append("📅 <b>今日の予定</b>")
    events = get_today_events(calendar_service)
    if events is None:
        # Calendar API 未設定 or 取得エラー
        lines.append("・取得できませんでした（Calendar API の設定を確認してください）")
    elif events:
        for ev in events:
            loc = f"（{ev['location']}）" if ev["location"] else ""
            lines.append(f"・{ev['start']} {ev['title']}{loc}")
    else:
        lines.append("・予定なし")

    lines.append("")

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
