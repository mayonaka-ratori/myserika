"""
calendar_client.py
Google Calendar API を操作するクライアントモジュール。
予定取得・空き時間計算・会議参加者抽出などの機能を提供する。
認証情報は gmail_client.py の build_calendar_service() を共有して使用する。
"""

import logging
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 日本標準時タイムゾーン
JST = ZoneInfo("Asia/Tokyo")

# カレンダーAPIで使用する日時フォーマット（RFC 3339）
_RFC3339_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _parse_event_dt(dt_obj: dict) -> datetime | None:
    """
    Calendar API のイベント日時オブジェクト（dateTime または date）を
    JST の datetime に変換する内部ヘルパー。
    終日イベントの場合は当日の 00:00:00 JST を返す。
    """
    if not dt_obj:
        return None

    if "dateTime" in dt_obj:
        # タイムゾーン付き文字列をパース
        dt = datetime.fromisoformat(dt_obj["dateTime"])
        return dt.astimezone(JST)
    elif "date" in dt_obj:
        # 終日イベント: "YYYY-MM-DD" → 当日 00:00 JST
        d = date.fromisoformat(dt_obj["date"])
        return datetime.combine(d, time.min, tzinfo=JST)

    return None


def _extract_attendee_emails(event: dict) -> list[str]:
    """
    イベント辞書から参加者のメールアドレス一覧を抽出する内部ヘルパー。
    organizer（主催者）のメールも含む。重複は除去する。
    """
    emails: set[str] = set()

    # 参加者リストから取得
    for attendee in event.get("attendees", []):
        email = attendee.get("email", "").lower()
        if email:
            emails.add(email)

    # 主催者を追加（attendees に含まれない場合がある）
    organizer_email = event.get("organizer", {}).get("email", "").lower()
    if organizer_email:
        emails.add(organizer_email)

    return sorted(emails)


def _format_event(event: dict) -> dict:
    """
    Calendar API のイベント辞書を統一フォーマットの辞書に変換する内部ヘルパー。
    戻り値: {
        "id": str,
        "title": str,
        "start": datetime (JST),
        "end": datetime (JST),
        "attendees": list[str],
        "is_all_day": bool,
        "location": str,
        "status": str,  # confirmed / tentative / cancelled
    }
    """
    start_dt = _parse_event_dt(event.get("start", {}))
    end_dt = _parse_event_dt(event.get("end", {}))
    is_all_day = "date" in event.get("start", {}) and "dateTime" not in event.get("start", {})

    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "（タイトルなし）"),
        "start": start_dt,
        "end": end_dt,
        "attendees": _extract_attendee_emails(event),
        "is_all_day": is_all_day,
        "location": event.get("location", ""),
        "status": event.get("status", "confirmed"),
    }


class CalendarClient:
    """
    Google Calendar API クライアントクラス。
    gmail_client.build_calendar_service() で生成した service を受け取り、
    予定の取得・分析を行う。
    """

    def __init__(self, service):
        """
        初期化。
        service: gmail_client.build_calendar_service() の戻り値
                 （googleapiclient.discovery.Resource）
        """
        self._service = service
        logger.info("CalendarClient 初期化完了")

    def _list_events(self, time_min: datetime, time_max: datetime) -> list[dict]:
        """
        指定期間のプライマリカレンダーイベントを取得する内部ヘルパー。
        キャンセル済みイベントは除外する。
        戻り値: フォーマット済みイベント辞書のリスト（開始時刻昇順）
        """
        try:
            result = self._service.events().list(
                calendarId="primary",
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,   # 繰り返しイベントを個別展開
                orderBy="startTime",
                maxResults=100,
            ).execute()

            events = []
            for item in result.get("items", []):
                # キャンセル済みは除外
                if item.get("status") == "cancelled":
                    continue
                events.append(_format_event(item))

            return events

        except Exception as e:
            logger.error(f"カレンダーイベント取得エラー: {e}")
            return []

    def get_today_events(self) -> list[dict]:
        """
        今日の予定一覧を取得して返す。

        戻り値: [
            {
                "id": str,
                "title": str,           # 予定タイトル
                "start": datetime,      # 開始時刻（JST）
                "end": datetime,        # 終了時刻（JST）
                "attendees": list[str], # 参加者メールアドレス
                "is_all_day": bool,     # 終日イベントかどうか
                "location": str,        # 場所
                "status": str,          # confirmed / tentative
            },
            ...
        ]
        """
        # 今日の 00:00:00 JST から 23:59:59 JST までを対象とする
        now_jst = datetime.now(JST)
        day_start = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now_jst.replace(hour=23, minute=59, second=59, microsecond=0)

        events = self._list_events(day_start, day_end)
        logger.info(f"今日の予定 {len(events)} 件を取得")
        return events

    def get_upcoming_events(self, hours: int = 3) -> list[dict]:
        """
        現在時刻から指定した時間（hours）以内に開始または進行中の予定を返す。

        引数:
            hours: 対象とする時間幅（デフォルト: 3時間）

        戻り値: get_today_events() と同形式のリスト
        """
        now_jst = datetime.now(JST)
        end_jst = now_jst + timedelta(hours=hours)

        events = self._list_events(now_jst, end_jst)
        logger.info(f"今後 {hours}h の予定 {len(events)} 件を取得")
        return events

    def is_busy_now(self) -> bool:
        """
        現在時刻に進行中の予定（会議）があるかどうかを返す。

        戻り値:
            True  - 現在進行中の予定が1件以上ある
            False - 予定なし（空き時間）
        """
        now_jst = datetime.now(JST)
        # 現在時刻を含む1分幅でクエリ（終了時刻が今から1秒後の予定を含む）
        events = self._list_events(
            now_jst - timedelta(hours=8),  # 最大8時間前に開始した予定まで含む
            now_jst + timedelta(minutes=1),
        )

        for event in events:
            start = event["start"]
            end = event["end"]
            # 終日イベントは会議中とみなさない
            if event["is_all_day"]:
                continue
            if start is None or end is None:
                continue
            # 開始済み かつ 終了していない予定 = 現在会議中
            if start <= now_jst < end:
                logger.info(f"現在会議中: {event['title']} ({start.strftime('%H:%M')}〜{end.strftime('%H:%M')})")
                return True

        logger.debug("現在は会議中ではありません")
        return False

    def get_current_meeting(self) -> dict | None:
        """
        現在進行中の会議情報を返す。
        会議中でなければ None を返す。

        戻り値: フォーマット済みイベント辞書、または None
        """
        now_jst = datetime.now(JST)
        events = self._list_events(
            now_jst - timedelta(hours=8),
            now_jst + timedelta(minutes=1),
        )

        for event in events:
            if event["is_all_day"]:
                continue
            start = event["start"]
            end = event["end"]
            if start is None or end is None:
                continue
            if start <= now_jst < end:
                return event

        return None

    def get_free_slots(self, target_date: date | None = None, duration_minutes: int = 30) -> list[dict]:
        """
        指定日の空き時間スロット一覧を返す。
        営業時間（09:00〜18:00）内の予定の隙間を空き時間として返す。

        引数:
            target_date:      対象日（date オブジェクト、省略時は今日）
            duration_minutes: スロットの最小時間（分）、デフォルト 30 分

        戻り値: [
            {
                "start": datetime,  # 空き時間の開始（JST）
                "end":   datetime,  # 空き時間の終了（JST）
                "duration_minutes": int,  # 空き時間の長さ（分）
            },
            ...
        ]
        """
        if target_date is None:
            target_date = datetime.now(JST).date()

        # 営業時間: 09:00〜18:00 JST
        work_start = datetime.combine(target_date, time(9, 0), tzinfo=JST)
        work_end = datetime.combine(target_date, time(18, 0), tzinfo=JST)

        events = self._list_events(work_start, work_end)

        # 予定の時間帯を収集（終日イベントを除く）
        busy_periods: list[tuple[datetime, datetime]] = []
        for event in events:
            if event["is_all_day"]:
                continue
            start = event["start"]
            end = event["end"]
            if start is None or end is None:
                continue
            # 営業時間内にクリップ
            clipped_start = max(start, work_start)
            clipped_end = min(end, work_end)
            if clipped_start < clipped_end:
                busy_periods.append((clipped_start, clipped_end))

        # 時刻順にソートしてマージ（重複する予定をまとめる）
        busy_periods.sort(key=lambda x: x[0])
        merged: list[tuple[datetime, datetime]] = []
        for s, e in busy_periods:
            if merged and s <= merged[-1][1]:
                # 直前の予定と重複 → 終了時刻を伸ばす
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        # 空き時間 = 予定の隙間
        free_slots: list[dict] = []
        current = work_start
        for busy_start, busy_end in merged:
            if current < busy_start:
                gap_minutes = int((busy_start - current).total_seconds() // 60)
                if gap_minutes >= duration_minutes:
                    free_slots.append({
                        "start": current,
                        "end": busy_start,
                        "duration_minutes": gap_minutes,
                    })
            current = max(current, busy_end)

        # 最後の予定〜営業終了の隙間
        if current < work_end:
            gap_minutes = int((work_end - current).total_seconds() // 60)
            if gap_minutes >= duration_minutes:
                free_slots.append({
                    "start": current,
                    "end": work_end,
                    "duration_minutes": gap_minutes,
                })

        logger.info(f"{target_date} の空き時間: {len(free_slots)} スロット")
        return free_slots

    def get_meeting_participants(self, hours: int = 24) -> list[str]:
        """
        直近 hours 時間以内に開始された会議の参加者メールアドレス一覧を返す。
        重複は除去し、アルファベット順でソートして返す。
        スケジュール調整の返信先候補として活用できる。

        引数:
            hours: さかのぼる時間数（デフォルト: 24 時間）

        戻り値: メールアドレスのリスト（重複なし・アルファベット順）
        """
        now_jst = datetime.now(JST)
        since = now_jst - timedelta(hours=hours)

        events = self._list_events(since, now_jst + timedelta(hours=hours))

        # 終日イベントを除外して参加者を収集
        all_emails: set[str] = set()
        for event in events:
            if event["is_all_day"]:
                continue
            for email in event["attendees"]:
                if email:
                    all_emails.add(email.lower())

        participants = sorted(all_emails)
        logger.info(f"直近 {hours}h の会議参加者: {len(participants)} 名")
        return participants

    def format_today_summary(self) -> str:
        """
        今日の予定を Telegram 通知向けのテキストにフォーマットして返す。
        予定が0件の場合は「予定なし」メッセージを返す。
        """
        events = self.get_today_events()

        if not events:
            return "📅 今日の予定はありません。"

        lines = [f"📅 今日の予定（{len(events)}件）\n"]
        for event in events:
            if event["is_all_day"]:
                time_str = "終日"
            elif event["start"] and event["end"]:
                time_str = (
                    f"{event['start'].strftime('%H:%M')}〜"
                    f"{event['end'].strftime('%H:%M')}"
                )
            else:
                time_str = "時刻不明"

            title = event["title"]
            attendees_count = len(event["attendees"])
            attendee_str = f"（参加者 {attendees_count}名）" if attendees_count > 1 else ""

            lines.append(f"• {time_str} {title}{attendee_str}")

        return "\n".join(lines)

    def format_free_slots_text(self, target_date: date | None = None, duration_minutes: int = 30) -> str:
        """
        空き時間スロットを返信メール向けのテキストにフォーマットして返す。
        スロットが0件の場合は「空き時間なし」メッセージを返す。

        引数:
            target_date:      対象日（省略時は今日）
            duration_minutes: 最小スロット時間（分）
        """
        if target_date is None:
            target_date = datetime.now(JST).date()

        slots = self.get_free_slots(target_date, duration_minutes)
        date_str = target_date.strftime("%m月%d日")

        if not slots:
            return f"{date_str}は空き時間がございません。"

        lines = [f"{date_str}の空き時間候補："]
        for slot in slots:
            start_str = slot["start"].strftime("%H:%M")
            end_str = slot["end"].strftime("%H:%M")
            lines.append(f"  • {start_str}〜{end_str}")

        return "\n".join(lines)
