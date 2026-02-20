"""
gmail_client.py
Gmail API を操作するクライアントモジュール。
OAuth2認証、メール取得、メール送信などの機能を提供する。
"""

import json
import os
import re
import base64
import pickle
import logging
import time
from collections import Counter
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Google から返されるスコープが要求と異なる場合でも認証を続行させる
# （GCP の OAuth 同意画面にスコープを追加するまでの暫定対処）
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

logger = logging.getLogger(__name__)

# Gmail 読み取り・送信・変更 + Google Calendar 読み取りスコープ
# 【重要】スコープを追加した場合は token.json を削除して再認証が必要
# 手順: 1) token.json を削除  2) python src/main.py を実行  3) ブラウザで認証
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
]

# contacts.md / MEMORY.md の自動学習セクションマーカー
_AUTO_CONTACTS_MARKER = "## 自動学習済み連絡先"
_AUTO_STYLE_MARKER = "## 自動学習: 返信スタイル分析"
_AUTO_LEARNING_FLAG_MARKER = "## 自動学習フラグ"


def _load_credentials(credentials_path: str, token_path: str):
    """
    OAuth2認証情報を読み込む内部ヘルパー。
    保存済みトークンがあれば再利用し、期限切れなら自動更新する。
    初回実行時はブラウザでOAuth2フローを実行してトークンを保存する。
    """
    creds = None

    if os.path.exists(token_path):
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return creds


def authenticate(credentials_path: str, token_path: str):
    """
    OAuth2認証を行い、Gmail APIサービスオブジェクトを返す。
    トークンが存在する場合は再利用し、期限切れなら自動更新する。
    初回実行時はブラウザが起動してGoogleアカウントへのアクセスを許可する。
    """
    creds = _load_credentials(credentials_path, token_path)
    service = build("gmail", "v1", credentials=creds)
    logger.info("Gmail 認証完了")
    return service


def build_calendar_service(credentials_path: str, token_path: str):
    """
    Google Calendar APIサービスオブジェクトを返す。
    Gmail と同じ OAuth2 認証情報（token.json）を使用する。
    token.json に calendar.readonly スコープが含まれていない場合は
    token.json を削除して再認証（python src/main.py の再実行）が必要。
    """
    creds = _load_credentials(credentials_path, token_path)
    service = build("calendar", "v3", credentials=creds)
    logger.info("Google Calendar サービス初期化完了")
    return service


def _decode_base64(data: str) -> str:
    """
    base64url エンコードされたデータをデコードして UTF-8 文字列に変換する内部ヘルパー。
    パディングを自動補完して安全にデコードする。
    """
    # パディング文字を補完
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _extract_body_text(payload: dict) -> str:
    """
    MIMEパートを再帰的に解析して最初のテキスト本文を抽出する内部ヘルパー。
    text/plain を優先し、なければ text/html を簡易テキスト化して返す。
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    # text/plain パートが見つかればそのままデコード
    if mime_type == "text/plain" and body_data:
        return _decode_base64(body_data)

    # マルチパートの場合は再帰的にパートを探索
    parts = payload.get("parts", [])
    for part in parts:
        text = _extract_body_text(part)
        if text:
            return text

    # フォールバック: text/html から簡易的にタグを除去
    if mime_type == "text/html" and body_data:
        html_text = _decode_base64(body_data)
        return re.sub(r"<[^>]+>", "", html_text)

    return ""


def get_email_body(service, message_id: str) -> str:
    """
    指定したメールIDのフルボディテキストを取得して返す。
    MIMEマルチパートにも対応する。
    """
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = msg.get("payload", {})
        return _extract_body_text(payload)
    except Exception as e:
        logger.error(f"メール本文取得エラー (id={message_id}): {e}")
        return ""


def get_unread_emails(service, max_results: int = 20) -> list[dict]:
    """
    受信トレイの未読メールを取得して返す。
    戻り値: [{ "id": str, "subject": str, "sender": str, "snippet": str, "body": str }, ...]
    """
    try:
        # 受信トレイの未読メールID一覧を取得
        result = service.users().messages().list(
            userId="me",
            q="is:unread in:inbox",
            maxResults=max_results,
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            logger.info("未読メールなし")
            return []

        emails = []
        for msg_ref in messages:
            try:
                msg_id = msg_ref["id"]

                # メタデータ（ヘッダー）を取得
                meta = service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date"],
                ).execute()

                headers = {
                    h["name"]: h["value"]
                    for h in meta.get("payload", {}).get("headers", [])
                }

                # 本文を取得（分類・返信案生成に使用）
                body = get_email_body(service, msg_id)

                emails.append({
                    "id": msg_id,
                    "subject": headers.get("Subject", "（件名なし）"),
                    "sender": headers.get("From", "（送信者不明）"),
                    "to": headers.get("To", ""),
                    "date": headers.get("Date", ""),
                    "snippet": body[:200] if body else meta.get("snippet", ""),
                    "body": body,
                })

            except Exception as e:
                logger.error(f"メール取得エラー (id={msg_ref['id']}): {e}")
                continue

        logger.info(f"未読メール {len(emails)} 件を取得")
        return emails

    except Exception as e:
        logger.error(f"未読メール一覧取得エラー: {e}")
        return []


def send_email(service, to: str, subject: str, body: str) -> bool:
    """
    メールを送信する。
    MIMEText メッセージを base64url エンコードして Gmail API 経由で送信する。
    成功したら True、失敗したら False を返す。
    """
    try:
        # MIMEメッセージを組み立て
        message = MIMEText(body, "plain", "utf-8")
        message["to"] = to
        message["subject"] = subject

        # base64url エンコードして送信
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

        logger.info(f"メール送信完了: {to}")
        return True

    except Exception as e:
        logger.error(f"メール送信エラー: {e}")
        return False


def mark_as_read(service, message_id: str) -> None:
    """
    指定したメールIDを既読にする。
    UNREAD ラベルを削除することで既読状態にする。
    """
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        logger.debug(f"既読処理完了: {message_id}")
    except Exception as e:
        logger.error(f"既読処理エラー (id={message_id}): {e}")


def _extract_name_and_email(header_value: str) -> tuple[str, str]:
    """
    "表示名 <email@example.com>" 形式から名前とメールアドレスを抽出する内部ヘルパー。
    アングルブラケットがない場合はアドレスのみを返す。
    """
    m = re.search(r"^(.*?)\s*<([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})>", header_value.strip())
    if m:
        name = m.group(1).strip().strip('"')
        addr = m.group(2).lower()
        return name, addr
    m2 = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", header_value)
    if m2:
        return "", m2.group().lower()
    return "", header_value.strip().lower()


def _parse_date_header(date_str: str) -> str:
    """
    RFC 2822 形式の日付文字列を YYYY-MM-DD 形式に変換する内部ヘルパー。
    解析失敗時は空文字列を返す。
    """
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _is_japanese(text: str) -> bool:
    """テキストに日本語文字（ひらがな・カタカナ・漢字）が含まれるか判定する。"""
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u4fff\u4e00-\u9fff]", text))


def _fetch_message_headers(service, msg_id: str, header_names: list[str]) -> dict:
    """
    指定したメッセージIDのメタデータヘッダーを取得して辞書で返す内部ヘルパー。
    失敗した場合は空辞書を返す。
    """
    try:
        meta = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=header_names,
        ).execute()
        return {
            h["name"]: h["value"]
            for h in meta.get("payload", {}).get("headers", [])
        }
    except Exception as e:
        logger.debug(f"ヘッダー取得エラー (id={msg_id}): {e}")
        return {}


def _read_learning_flags(memory_path: str) -> dict:
    """
    MEMORY.md の「## 自動学習フラグ」セクションを読み込んで辞書で返す。
    キー: style_date, contacts_date, analyzed_ids
    """
    path = Path(memory_path)
    if not path.exists():
        return {}

    content = path.read_text(encoding="utf-8")
    if _AUTO_LEARNING_FLAG_MARKER not in content:
        return {}

    start = content.index(_AUTO_LEARNING_FLAG_MARKER) + len(_AUTO_LEARNING_FLAG_MARKER)
    rest = content[start:]
    next_section = re.search(r"\n## ", rest)
    section_text = rest[:next_section.start()] if next_section else rest

    flags: dict = {}

    m = re.search(r"最終スタイル学習日:\s*(\d{4}-\d{2}-\d{2})", section_text)
    if m:
        flags["style_date"] = m.group(1)

    m = re.search(r"最終連絡先学習日:\s*(\d{4}-\d{2}-\d{2})", section_text)
    if m:
        flags["contacts_date"] = m.group(1)

    m = re.search(r"スタイル分析済みID:\s*(\[.*?\])", section_text, re.DOTALL)
    if m:
        try:
            flags["analyzed_ids"] = json.loads(m.group(1))
        except Exception:
            flags["analyzed_ids"] = []

    return flags


def _update_learning_flags(memory_path: str, updates: dict) -> None:
    """
    MEMORY.md の「## 自動学習フラグ」セクションを updates の内容で更新する。
    既存のフラグ値はマージされ、updates で上書きされる。
    """
    try:
        flags = _read_learning_flags(memory_path)
        flags.update(updates)

        # フラグセクションの内容を再構築
        section_lines = ["\n\n"]
        if flags.get("style_date"):
            section_lines.append(f"最終スタイル学習日: {flags['style_date']}\n")
        if flags.get("analyzed_ids") is not None:
            ids_json = json.dumps(flags["analyzed_ids"], ensure_ascii=False)
            section_lines.append(f"スタイル分析済みID: {ids_json}\n")
        if flags.get("contacts_date"):
            section_lines.append(f"最終連絡先学習日: {flags['contacts_date']}\n")

        new_section = _AUTO_LEARNING_FLAG_MARKER + "".join(section_lines)

        path = Path(memory_path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        if _AUTO_LEARNING_FLAG_MARKER in existing:
            start = existing.index(_AUTO_LEARNING_FLAG_MARKER)
            rest = existing[start + len(_AUTO_LEARNING_FLAG_MARKER):]
            next_m = re.search(r"\n## ", rest)
            after = rest[next_m.start():] if next_m else ""
            new_content = existing[:start] + new_section + after
        else:
            # _AUTO_STYLE_MARKER の前に挿入するか、なければ末尾に追加
            if _AUTO_STYLE_MARKER in existing:
                pos = existing.index(_AUTO_STYLE_MARKER)
                new_content = existing[:pos] + new_section + "\n\n" + existing[pos:]
            else:
                new_content = existing.rstrip() + "\n\n" + new_section

        path.write_text(new_content, encoding="utf-8")
        logger.debug(f"自動学習フラグを更新しました: {flags}")

    except Exception as e:
        logger.error(f"自動学習フラグ更新エラー: {e}")


def learn_contacts(service, contacts_path: str, memory_path: str = "", days: int = 30) -> None:
    """
    過去 days 日分の送受信メールから連絡先を抽出し、contacts.md を更新する。
    フォーマット: 名前・メールアドレス・やり取り頻度・最終連絡日・優先度
    頻度 ≥5 → 高（重要タグ付き）、2〜4 → 中、1 → 低
    「## 自動学習済み連絡先」マーカー以前の手動エントリは保持される。
    memory_path が指定された場合は完了後に学習フラグを更新する。
    """
    try:
        # 自分のメールアドレスを取得（除外用）
        profile = service.users().getProfile(userId="me").execute()
        my_email = profile.get("emailAddress", "").lower()

        query = f"newer_than:{days}d"
        inbox_msgs = service.users().messages().list(
            userId="me", q=f"in:inbox {query}", maxResults=100
        ).execute().get("messages", [])
        sent_msgs = service.users().messages().list(
            userId="me", q=f"in:sent {query}", maxResults=100
        ).execute().get("messages", [])

        all_msg_ids = [m["id"] for m in inbox_msgs + sent_msgs]

        # アドレス → (名前, 最終連絡日) の収集
        addr_info: dict[str, tuple[str, str]] = {}
        addr_counter: Counter = Counter()

        for msg_id in all_msg_ids:
            headers = _fetch_message_headers(service, msg_id, ["From", "To", "Cc", "Date"])
            date_str = _parse_date_header(headers.get("Date", ""))

            for header_key in ("From", "To", "Cc"):
                raw = headers.get(header_key, "")
                if not raw:
                    continue
                for part in raw.split(","):
                    name, addr = _extract_name_and_email(part.strip())
                    if not addr or addr == my_email:
                        continue
                    addr_counter[addr] += 1
                    existing_name, existing_date = addr_info.get(addr, ("", ""))
                    new_date = date_str if date_str > existing_date else existing_date
                    new_name = name if name else existing_name
                    addr_info[addr] = (new_name, new_date)

        if not addr_counter:
            logger.info("自動学習: 連絡先データが見つかりませんでした")
            return

        # 頻度順で contacts.md セクションを構築
        lines = [f"{_AUTO_CONTACTS_MARKER}\n\n"]
        lines.append(f"<!-- 自動生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n")

        for addr, count in addr_counter.most_common():
            name, last_date = addr_info.get(addr, ("", ""))
            if count >= 5:
                priority = "高"
                tag = "重要, 自動学習"
            elif count >= 2:
                priority = "中"
                tag = "自動学習"
            else:
                priority = "低"
                tag = "自動学習"

            display_name = name if name else addr.split("@")[0]
            lines.append(f"### {display_name}\n")
            lines.append(f"- メールアドレス：{addr}\n")
            lines.append(f"- やり取り頻度：{count}回（過去{days}日）\n")
            lines.append(f"- 最終連絡日：{last_date}\n")
            lines.append(f"- 優先度：{priority}\n")
            lines.append(f"- タグ：{tag}\n")
            lines.append("\n")

        # マーカー以前の手動エントリを保持してファイルを更新
        path = Path(contacts_path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        if _AUTO_CONTACTS_MARKER in existing:
            before = existing[:existing.index(_AUTO_CONTACTS_MARKER)]
        else:
            before = existing.rstrip() + "\n\n" if existing.strip() else ""

        path.write_text(before + "".join(lines), encoding="utf-8")
        logger.info(f"連絡先自動学習完了: {len(addr_counter)} 件")

        # 学習済みフラグを更新（memory_path が指定された場合）
        if memory_path:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _update_learning_flags(memory_path, {"contacts_date": today_str})

    except Exception as e:
        logger.error(f"learn_contacts エラー: {e}")


def learn_writing_style(
    service, memory_path: str, gemini_client=None, days: int = 30
) -> None:
    """
    過去 days 日分の送信済みメールから返信スタイルを分析し、MEMORY.md を更新する。
    分析内容: 日本語/英語別の件数・平均文字数・文体・典型的な書き出しと締め
    gemini_client が指定された場合、未分析メールを最大5件 Gemini で深く分析する（2秒間隔）。
    「## 自動学習フラグ」に7日以内の学習日が記録されている場合はスキップする。
    「## 自動学習: 返信スタイル分析」マーカー以前の内容は保持される。
    """
    try:
        # 学習済みフラグチェック（7日以内ならスキップ）
        flags = _read_learning_flags(memory_path)
        style_date = flags.get("style_date", "")
        if style_date:
            try:
                days_since = (
                    datetime.now().date()
                    - datetime.strptime(style_date, "%Y-%m-%d").date()
                ).days
                if days_since < 7:
                    logger.info(f"返信スタイル学習済み（{days_since}日前）、スキップします")
                    return
            except Exception:
                pass  # 日付パース失敗時はスキップせず続行

        result = service.users().messages().list(
            userId="me", q=f"in:sent newer_than:{days}d", maxResults=30
        ).execute()
        msg_ids = [m["id"] for m in result.get("messages", [])]

        if not msg_ids:
            logger.info("自動学習: 送信済みメールが見つかりませんでした")
            return

        ja_openings: Counter = Counter()
        ja_closings: Counter = Counter()
        en_openings: Counter = Counter()
        en_closings: Counter = Counter()
        ja_char_counts: list[int] = []
        en_char_counts: list[int] = []
        keigo_count = 0
        ja_count = 0
        en_count = 0

        keigo_markers = ["でございます", "いただき", "させていただ", "ご確認", "ご連絡", "よろしくお願い"]

        for msg_id in msg_ids:
            body = get_email_body(service, msg_id)
            if not body or len(body.strip()) < 10:
                continue

            body = body.strip()
            is_ja = _is_japanese(body)
            non_empty_lines = [l.strip() for l in body.split("\n") if l.strip()]

            if is_ja:
                ja_count += 1
                ja_char_counts.append(len(body))
                if non_empty_lines:
                    ja_openings[non_empty_lines[0][:20]] += 1
                if len(non_empty_lines) > 1:
                    ja_closings[non_empty_lines[-1][:20]] += 1
                if any(marker in body for marker in keigo_markers):
                    keigo_count += 1
            else:
                en_count += 1
                en_char_counts.append(len(body))
                if non_empty_lines:
                    en_openings[non_empty_lines[0][:30]] += 1
                if len(non_empty_lines) > 1:
                    en_closings[non_empty_lines[-1][:30]] += 1

        # Gemini による深い分析（gemini_client が指定された場合）
        gemini_summary_lines = []
        newly_analyzed_ids = []
        analyzed_ids = flags.get("analyzed_ids", [])

        if gemini_client:
            from gemini_client import (
                _call_model as _gm_call,
                _parse_json_response as _gm_parse,
            )

            unanalyzed = [mid for mid in msg_ids if mid not in analyzed_ids][:5]
            all_characteristics: list[str] = []
            formality_votes: list[str] = []

            for msg_id in unanalyzed:
                body = get_email_body(service, msg_id)
                if not body or len(body.strip()) < 10:
                    continue

                prompt = (
                    "以下の送信済みメール1件の文体を分析し特徴を3点抽出してください。\n"
                    f"【本文（500字以内）】{body[:500]}\n"
                    'JSON: {"characteristics": ["特徴1","特徴2","特徴3"], "formality": "高/中/低"}'
                )

                try:
                    text = _gm_call(gemini_client, prompt)
                    data = _gm_parse(text)
                    chars = data.get("characteristics", [])
                    all_characteristics.extend(chars)
                    formality = data.get("formality", "")
                    if formality:
                        formality_votes.append(formality)
                    newly_analyzed_ids.append(msg_id)
                    logger.debug(f"Gemini スタイル分析完了 (id={msg_id})")
                except Exception as e:
                    logger.warning(f"Gemini スタイル分析エラー (id={msg_id}): {e}")

                time.sleep(2)  # レート制限対策

            if newly_analyzed_ids:
                dominant_formality = (
                    max(set(formality_votes), key=formality_votes.count)
                    if formality_votes
                    else ""
                )
                gemini_summary_lines.append(f"- 分析件数: {len(newly_analyzed_ids)}件\n")
                if all_characteristics:
                    unique_chars = list(dict.fromkeys(all_characteristics))  # 順序保持で重複除去
                    gemini_summary_lines.append(f"- 共通の特徴: {', '.join(unique_chars[:6])}\n")
                if dominant_formality:
                    gemini_summary_lines.append(f"- 敬語レベル: {dominant_formality}\n")

        # 分析結果を MEMORY.md セクションに書き込む
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"{_AUTO_STYLE_MARKER}\n\n"]
        lines.append(f"<!-- 自動生成: {now} -->\n\n")
        lines.append(f"分析対象: 過去{days}日間の送信済みメール {len(msg_ids)} 件\n\n")

        if ja_count > 0:
            avg_ja = sum(ja_char_counts) // len(ja_char_counts) if ja_char_counts else 0
            formality = "丁寧語（敬語使用率高め）" if keigo_count / ja_count >= 0.5 else "標準語"
            lines.append("### 日本語メールの傾向\n")
            lines.append(f"- 件数: {ja_count}件\n")
            lines.append(f"- 平均文字数: 約{avg_ja}字\n")
            lines.append(f"- 文体: {formality}\n")
            if ja_openings:
                lines.append(f"- 典型的な書き出し: 「{ja_openings.most_common(1)[0][0]}」\n")
            if ja_closings:
                lines.append(f"- 典型的な締め: 「{ja_closings.most_common(1)[0][0]}」\n")
            lines.append("\n")

        if en_count > 0:
            avg_en = sum(en_char_counts) // len(en_char_counts) if en_char_counts else 0
            lines.append("### 英語メールの傾向\n")
            lines.append(f"- 件数: {en_count}件\n")
            lines.append(f"- 平均文字数: 約{avg_en}字\n")
            if en_openings:
                lines.append(f"- 典型的な書き出し: \"{en_openings.most_common(1)[0][0]}\"\n")
            if en_closings:
                lines.append(f"- 典型的な締め: \"{en_closings.most_common(1)[0][0]}\"\n")
            lines.append("\n")

        if gemini_summary_lines:
            lines.append("### Gemini 強化分析\n")
            lines.extend(gemini_summary_lines)
            lines.append("\n")

        # マーカー以前のコンテンツを保持してファイルを更新
        path = Path(memory_path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        if _AUTO_STYLE_MARKER in existing:
            before = existing[:existing.index(_AUTO_STYLE_MARKER)]
        else:
            before = existing.rstrip() + "\n\n" if existing.strip() else ""

        path.write_text(before + "".join(lines), encoding="utf-8")
        logger.info(f"返信スタイル自動学習完了: 日本語{ja_count}件, 英語{en_count}件")

        # 学習済みフラグを更新
        all_analyzed = list(set(analyzed_ids + newly_analyzed_ids))
        today_str = datetime.now().strftime("%Y-%m-%d")
        _update_learning_flags(memory_path, {
            "style_date": today_str,
            "analyzed_ids": all_analyzed,
        })

    except Exception as e:
        logger.error(f"learn_writing_style エラー: {e}")
