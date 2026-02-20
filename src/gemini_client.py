"""
gemini_client.py
Google Gemini API を操作するクライアントモジュール。
メール分類・返信案生成などのLLMタスクを担当する。
google-genai ライブラリ（新しい Gen AI SDK）を使用する。
"""

import json
import re
import time
import logging
from datetime import datetime
from google import genai

try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:
    # google-api-core が無い環境向けフォールバック
    class ResourceExhausted(Exception):  # type: ignore
        pass

logger = logging.getLogger(__name__)

# Gemini 無料枠の上限
_DAILY_LIMIT = 1500
_MINUTE_LIMIT = 15

# 日程調整メールの判定キーワード
_SCHEDULING_KEYWORDS = [
    "日程", "打ち合わせ", "ミーティング", "スケジュール", "都合", "候補", "調整",
    "お時間", "ご都合", "schedule", "meeting", "available", "availability",
]


def is_scheduling_email(subject: str, body: str) -> bool:
    """件名・本文に日程調整キーワードが含まれるか判定する。"""
    text = (subject + " " + body).lower()
    return any(kw.lower() in text for kw in _SCHEDULING_KEYWORDS)


def init_client(api_key: str, model_name: str = "gemini-2.5-flash") -> dict:
    """
    Gemini クライアントを初期化して返す。
    api_key: config.yaml から渡される API キー
    model_name: 使用するモデル名（デフォルト: gemini-2.5-flash）
    戻り値: {"client": Client, "model": str, "daily_count": int, ...} の辞書
    """
    client = genai.Client(api_key=api_key)
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Gemini クライアント初期化完了: {model_name}")
    return {
        "client": client,
        "model": model_name,
        "daily_count": 0,
        "daily_date": today,
        "minute_calls": [],  # タイムスタンプのリスト（直近60秒分）
        "api_call_log": [],  # {"endpoint": str, "ts": str} のリスト（main.py でフラッシュ）
    }


def _do_api_call(client_data: dict, prompt: str) -> str:
    """Gemini API を実際に呼び出す内部ヘルパー（リトライなし）。"""
    client = client_data["client"]
    model = client_data["model"]
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


def _increment_api_counter(client_data: dict) -> None:
    """API 呼び出し成功後にカウンターを更新する内部ヘルパー。"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 日付が変わっていれば daily_count をリセット
    if client_data.get("daily_date") != today_str:
        client_data["daily_count"] = 0
        client_data["daily_date"] = today_str

    client_data["daily_count"] = client_data.get("daily_count", 0) + 1

    # 直近60秒のタイムスタンプを管理
    now_ts = now.timestamp()
    minute_calls = client_data.setdefault("minute_calls", [])
    minute_calls.append(now_ts)
    client_data["minute_calls"] = [ts for ts in minute_calls if now_ts - ts <= 60]

    logger.debug(
        f"API使用カウンター: 本日{client_data['daily_count']}回, "
        f"直近1分{len(client_data['minute_calls'])}回"
    )


def _call_model(client_data: dict, prompt: str) -> str:
    """
    Gemini モデルにプロンプトを送信してテキスト応答を取得する内部ヘルパー。
    ResourceExhausted (429) 発生時は 30 秒待機して1回リトライする。
    リトライ後も失敗した場合は例外をそのまま伝播させる。
    """
    # 1回目の試行
    try:
        result = _do_api_call(client_data, prompt)
        _increment_api_counter(client_data)
        return result
    except ResourceExhausted:
        logger.warning("429エラー: 30秒待機後にリトライします")
        time.sleep(30)
    except Exception:
        raise

    # 2回目（リトライ）
    result = _do_api_call(client_data, prompt)
    _increment_api_counter(client_data)
    return result


def get_api_usage(client_data: dict) -> dict:
    """
    現在の API 使用状況を返す。
    戻り値: {
        "daily_count": int,     # 本日の合計呼び出し回数
        "daily_remaining": int, # 残り推定回数（上限1500回/日）
        "minute_count": int,    # 直近1分の呼び出し回数
        "minute_remaining": int # 残り推定回数（上限15回/分）
    }
    """
    daily_count = client_data.get("daily_count", 0)
    now_ts = datetime.now().timestamp()
    minute_count = sum(
        1 for ts in client_data.get("minute_calls", []) if now_ts - ts <= 60
    )
    return {
        "daily_count": daily_count,
        "daily_remaining": max(0, _DAILY_LIMIT - daily_count),
        "minute_count": minute_count,
        "minute_remaining": max(0, _MINUTE_LIMIT - minute_count),
    }


def _parse_json_response(text: str) -> dict:
    """
    LLM の応答からJSON部分を抽出してパースする内部ヘルパー。
    マークダウンのコードブロック（```json ... ```）にも対応する。
    """
    # コードブロックのフェンスを除去
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()

    # JSONオブジェクトを正規表現で探して抽出
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())

    raise ValueError(f"JSON が見つかりませんでした: {text[:200]}")


def classify_email(
    client_data: dict,
    subject: str,
    sender: str,
    snippet: str,
    memory_context: str = "",
    calendar_note: str = "",
) -> dict:
    """
    メールの件名・送信者・本文冒頭をもとに Gemini で分類を行う。
    memory_context: MEMORY.md の内容（ユーザーの傾向・過去の修正ログ）を渡すと精度が向上する。
    calendar_note: カレンダー情報（会議参加者かどうか等）を渡すと精度が向上する。
    戻り値: { "category": str, "reason": str, "confidence": float }
    カテゴリ候補: "要返信（重要）" / "要返信（通常）" / "閲覧のみ" / "無視"
    レート制限時は category="__RETRY__" を返す（main.py で retry_queue に追加される）。
    """
    context_section = ""
    if memory_context:
        excerpt = memory_context[:500]
        context_section = f"\n【ユーザー傾向メモ（参考）】\n{excerpt}\n"

    calendar_section = ""
    if calendar_note:
        calendar_section = f"\n【カレンダー情報】\n{calendar_note}\n"

    prompt = f"""以下のメールを分類してください。{context_section}{calendar_section}
【送信者】{sender}
【件名】{subject}
【本文冒頭】{snippet}

分類カテゴリ（一つだけ選択）:
- 要返信（重要）: 期限・依頼・承認など即時対応が必要なもの
- 要返信（通常）: 返信すべきだが急がないもの
- 閲覧のみ: 情報共有・CC・ニュースレターなど
- 無視: 広告・スパム・自動通知など

必ずJSON形式のみで回答してください（説明文不要）:
{{"category": "カテゴリ名", "reason": "理由（日本語50字以内）", "confidence": 0.0から1.0の数値}}"""

    try:
        text = _call_model(client_data, prompt)
        result = _parse_json_response(text)

        # カテゴリが有効な値かチェックし、無効なら安全なデフォルトに戻す
        valid_categories = {"要返信（重要）", "要返信（通常）", "閲覧のみ", "無視"}
        if result.get("category") not in valid_categories:
            logger.warning(f"不明なカテゴリ '{result.get('category')}' → 閲覧のみ に変換")
            result["category"] = "閲覧のみ"
            result["confidence"] = 0.5

        # confidence を float に正規化
        result["confidence"] = float(result.get("confidence", 0.5))

        client_data.setdefault("api_call_log", []).append(
            {"endpoint": "classify_email", "ts": datetime.now().isoformat()}
        )
        return result

    except ResourceExhausted:
        logger.warning("Gemini API レート制限: メールを retry_queue に追加します")
        return {
            "category": "__RETRY__",
            "reason": "API レート制限のため後で再試行",
            "confidence": 0.0,
        }
    except Exception as e:
        logger.error(f"メール分類エラー: {e}")
        return {
            "category": "閲覧のみ",
            "reason": "分類に失敗したためデフォルト値を使用",
            "confidence": 0.0,
        }


def _detect_language(text: str) -> str:
    """
    テキストに日本語文字（ひらがな・カタカナ・漢字）が含まれるか判定する。
    含まれていれば "ja"、それ以外は "en" を返す。
    """
    if re.search(r"[\u3040-\u30ff\u3400-\u4fff\u4e00-\u9fff]", text):
        return "ja"
    return "en"


def generate_reply_draft(
    client_data: dict,
    original_email: dict,
    user_style: str,
    sender_info: str,
    calendar_context: dict | None = None,
) -> str:
    """
    元メールの内容・ユーザーの返信スタイル・送信者情報をもとに返信案を生成する。
    メールの言語（日本語／英語）を自動判定し、同じ言語で返信案を生成する。
    日本語: 署名なし、英語: "Best regards, [Your Name]" 形式の署名あり。
    calendar_context: カレンダー情報を渡すと返信案にカレンダー対応指示が追加される。
      構造: {
        "client": CalendarClient,      # get_free_slots() / get_today_events() 用
        "is_busy": bool,               # 現在会議中かどうか
        "current_meeting": dict|None,  # 現在の会議情報
        "participants": set[str],      # 本日の会議参加者メールアドレス
        "is_participant": bool,        # 送信者が参加者かどうか
        "sender_email": str,           # 送信者メールアドレス
      }
    レート制限時は "__RETRY__" を返す（main.py で retry_queue に追加される）。
    戻り値: 返信本文テキスト（件名を含む）
    """
    style_instruction = user_style if user_style else "丁寧で簡潔なビジネス文体"
    original_subject = original_email.get("subject", "")
    original_body = original_email.get("body", original_email.get("snippet", ""))

    lang = _detect_language(original_subject + original_body)
    if lang == "ja":
        lang_instruction = "日本語で返信し、署名は一切つけないでください。"
    else:
        lang_instruction = "英語で返信し、末尾に「Best regards, [Your Name]」形式の署名をつけてください。"

    # カレンダー対応指示セクションを構築
    calendar_section = ""
    if calendar_context is not None:
        cal_client = calendar_context.get("client")
        is_busy = calendar_context.get("is_busy", False)
        current_meeting = calendar_context.get("current_meeting")
        is_participant = calendar_context.get("is_participant", False)
        sender_email = calendar_context.get("sender_email", "")

        cal_lines = []

        # (a) 現在会議中: 返信遅延の旨を冒頭に含める
        if is_busy and current_meeting:
            meeting_title = current_meeting.get("title", "会議")
            end_dt = current_meeting.get("end")
            end_str = end_dt.strftime("%H:%M") if end_dt else "終了時刻不明"
            cal_lines.append(
                f"現在「{meeting_title}」（〜{end_str}）に参加中です。"
                f"返信文の冒頭に「現在会議中のため、返信が遅くなりました」等の一言を入れてください。"
            )

        # (b) 日程調整メール: 今日・明日の空き時間を提案
        if cal_client is not None and is_scheduling_email(original_subject, original_body):
            try:
                from datetime import timedelta
                today = datetime.now().date()
                tomorrow = today + timedelta(days=1)
                today_slots = cal_client.format_free_slots_text(target_date=today)
                tomorrow_slots = cal_client.format_free_slots_text(target_date=tomorrow)
                cal_lines.append(
                    f"日程調整メールです。以下の空き時間を返信文に含めてください:\n"
                    f"{today_slots}\n{tomorrow_slots}"
                )
            except Exception as e:
                logger.warning(f"空き時間取得失敗（スキップ）: {e}")

        # (c) 会議参加者: 共通会議の文脈情報を含める
        if is_participant and cal_client is not None and sender_email:
            try:
                today_events = cal_client.get_today_events()
                related = [
                    e for e in today_events
                    if sender_email in e.get("attendees", [])
                ]
                if related:
                    event_names = "、".join(e["title"] for e in related[:3])
                    cal_lines.append(
                        f"この送信者（{sender_email}）は本日の会議「{event_names}」の参加者です。"
                        f"この文脈を踏まえて返信してください。"
                    )
            except Exception as e:
                logger.warning(f"会議参加者文脈取得失敗（スキップ）: {e}")

        if cal_lines:
            calendar_section = "\n【カレンダー対応指示】\n" + "\n".join(cal_lines) + "\n"

    prompt = f"""以下のメールに対する返信案を生成してください。
{calendar_section}
【スタイル指示】{style_instruction}
【言語・署名指示】{lang_instruction}
【送信者情報】{sender_info if sender_info else "不明"}
【元メール件名】{original_subject}
【元メール送信者】{original_email.get("sender", "")}
【元メール本文】
{original_body}

返信案を以下の形式で出力してください（件名行から始める）:
件名: Re: {original_subject}

（本文をここに記述）"""

    try:
        draft = _call_model(client_data, prompt)
        client_data.setdefault("api_call_log", []).append(
            {"endpoint": "generate_reply_draft", "ts": datetime.now().isoformat()}
        )
        return draft.strip()
    except ResourceExhausted:
        logger.warning("Gemini API レート制限: 返信案生成を retry_queue に追加します")
        return "__RETRY__"
    except Exception as e:
        logger.error(f"返信案生成エラー: {e}")
        return (
            f"件名: Re: {original_subject}\n\n"
            "返信案の生成に失敗しました。手動で作成してください。"
        )


def refine_reply_draft(client_data: dict, previous_draft: str, user_instruction: str) -> str:
    """
    ユーザーの修正指示をもとに、既存の返信案を再生成する。
    修正指示は日本語の自由文を想定（例:「もっと短く」「敬語を柔らかく」）。
    戻り値: 修正後の返信本文テキスト
    """
    prompt = f"""以下の返信案を、修正指示に従って書き直してください。

【現在の返信案】
{previous_draft}

【修正指示】
{user_instruction}

修正後の返信案のみ出力してください（説明文・前置き不要）:"""

    try:
        revised = _call_model(client_data, prompt)
        client_data.setdefault("api_call_log", []).append(
            {"endpoint": "refine_reply_draft", "ts": datetime.now().isoformat()}
        )
        return revised.strip()
    except Exception as e:
        logger.error(f"返信案修正エラー: {e}")
        # エラー時は元の案を保持して返す
        return previous_draft
