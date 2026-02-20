"""
classifier.py
メール分類ロジックのオーケストレーターモジュール。
ルールベース判定と Gemini 判定を組み合わせて最終分類を決定する。
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 分類カテゴリの定数
CATEGORY_URGENT = "要返信（重要）"
CATEGORY_NORMAL = "要返信（通常）"
CATEGORY_READ   = "閲覧のみ"
CATEGORY_IGNORE = "無視"

# スパム・広告の典型的なキーワード（件名・送信者アドレスで判定）
SPAM_KEYWORDS = [
    "unsubscribe", "配信停止", "メルマガ", "newsletter",
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "notification@", "alert@", "info@", "news@", "promo@",
    "広告", "特価", "セール", "キャンペーン", "会員登録",
]

# 緊急・重要度が高い件名キーワード
URGENT_KEYWORDS = [
    "至急", "緊急", "重要", "期限", "締め切り", "締切",
    "deadline", "urgent", "important", "asap",
    "要返信", "ご回答", "ご確認ください", "お願い",
]

# カテゴリ優先度順（低→高）
_PRIORITY_ORDER = [CATEGORY_IGNORE, CATEGORY_READ, CATEGORY_NORMAL, CATEGORY_URGENT]


def _upgrade_category(category: str) -> str:
    """カテゴリを優先度リストで1段階アップする。最高位はそのまま返す。"""
    try:
        idx = _PRIORITY_ORDER.index(category)
        return _PRIORITY_ORDER[min(idx + 1, len(_PRIORITY_ORDER) - 1)]
    except ValueError:
        return category


def extract_email_address(sender: str) -> str:
    """
    "表示名 <email@example.com>" 形式からメールアドレスを抽出する。
    アングルブラケットがない場合はそのまま小文字で返す。
    """
    m = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", sender)
    return m.group().lower() if m else sender.strip().lower()


def load_contacts(contacts_path: str = "../contacts.md") -> dict:
    """
    contacts.md を読み込み、メールアドレス→情報のマッピングを返す。
    戻り値: { "example@example.com": { "name": str, "priority": str, "relationship": str } }
    """
    contacts = {}
    path = Path(contacts_path)

    if not path.exists():
        logger.debug(f"contacts.md が見つかりません: {contacts_path}")
        return contacts

    try:
        content = path.read_text(encoding="utf-8")

        # "### 表示名" で区切られたブロックをパース
        blocks = re.split(r"^###\s+", content, flags=re.MULTILINE)

        for block in blocks[1:]:  # 最初の空ブロックをスキップ
            lines = block.strip().split("\n")
            name = lines[0].strip()
            email_addr = None
            priority = "中"
            relationship = ""

            for line in lines[1:]:
                line = line.strip()
                # メールアドレス行を探す
                if "メールアドレス" in line or "email" in line.lower():
                    m = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", line)
                    if m:
                        email_addr = m.group().lower()
                # 優先度行を探す
                elif "優先度" in line:
                    if "高" in line:
                        priority = "高"
                    elif "低" in line:
                        priority = "低"
                    else:
                        priority = "中"
                # 関係行を探す
                elif "関係" in line and "：" in line:
                    relationship = line.split("：", 1)[-1].strip()

            if email_addr:
                contacts[email_addr] = {
                    "name": name,
                    "priority": priority,
                    "relationship": relationship,
                }

        logger.debug(f"連絡先 {len(contacts)} 件を読み込み")

    except Exception as e:
        logger.error(f"contacts.md 読み込みエラー: {e}")

    return contacts


def rule_based_classify(email: dict, contacts: dict) -> str | None:
    """
    ルールベースで分類を試みる。判定できなかった場合は None を返す。
    ルール（優先順）:
      1. contacts.md の優先度「高」→ 要返信（重要）
      2. contacts.md の優先度「低」→ 閲覧のみ
      3. 件名・送信者にスパムキーワード → 無視
      4. 件名に緊急キーワード → 要返信（重要）
    """
    sender_str = email.get("sender", "").lower()
    sender_addr = extract_email_address(sender_str)
    subject = email.get("subject", "").lower()

    # ルール1・2: contacts.md の登録優先度で判定
    if sender_addr in contacts:
        priority = contacts[sender_addr]["priority"]
        if priority == "高":
            return CATEGORY_URGENT
        elif priority == "低":
            return CATEGORY_READ

    # ルール3: スパム・広告キーワードで無視判定
    for kw in SPAM_KEYWORDS:
        if kw.lower() in sender_str or kw.lower() in subject:
            return CATEGORY_IGNORE

    # ルール4: 件名の緊急キーワードで重要判定
    for kw in URGENT_KEYWORDS:
        if kw.lower() in subject:
            return CATEGORY_URGENT

    # 判定不能 → Gemini に委譲
    return None


def classify(
    email: dict,
    gemini_client,
    contacts: dict,
    memory_context: str = "",
    calendar_client=None,
    meeting_participants=frozenset(),
) -> dict:
    """
    メール1件を分類して結果を返すメイン関数。
    1. まずルールベース判定を試みる
    2. 判定できなければ Gemini に問い合わせる（memory_context を参考情報として渡す）
    3. 信頼度が低ければ "要確認" に変更してユーザーに判断を促す
    4. 送信者が会議参加者であればカテゴリを1段階アップする
    calendar_client: 直接呼び出し時のみ渡す（classify_batch 経由では None を渡す）
    meeting_participants: classify_batch で事前取得済みの参加者 set
    戻り値: { "email_id": str, "category": str, "reason": str, "email": dict,
              "is_meeting_participant": bool }
    """
    from gemini_client import classify_email as gemini_classify

    email_id = email.get("id", "")
    sender_addr = extract_email_address(email.get("sender", ""))

    # 会議参加者セットの決定
    # classify_batch 経由では meeting_participants に事前取得済みの set が渡される
    # 直接呼び出しで calendar_client が渡された場合のみ API を呼ぶ
    participants: set[str] = set(meeting_participants)
    if calendar_client is not None and not participants:
        try:
            fetched = calendar_client.get_meeting_participants(hours=24)
            participants = set(fetched)
        except Exception as e:
            logger.warning(f"会議参加者取得失敗: {e}")

    is_meeting_participant = sender_addr in participants
    if is_meeting_participant:
        logger.info(f"会議参加者からのメール検出: {sender_addr}")

    # ステップ1: ルールベース判定
    rule_result = rule_based_classify(email, contacts)
    if rule_result:
        category = rule_result
        if is_meeting_participant:
            category = _upgrade_category(category)
        logger.debug(f"ルールベース判定: [{category}] {email.get('subject', '')}")
        return {
            "email_id": email_id,
            "category": category,
            "reason": "ルールベース判定",
            "email": email,
            "is_meeting_participant": is_meeting_participant,
        }

    # ステップ2: Gemini による判定（MEMORY.md の内容・カレンダー情報を参考として渡す）
    calendar_note = ""
    if is_meeting_participant:
        calendar_note = f"この送信者（{sender_addr}）は直近24時間以内の会議参加者です。"

    gemini_result = gemini_classify(
        gemini_client,
        subject=email.get("subject", ""),
        sender=email.get("sender", ""),
        snippet=email.get("snippet", ""),
        memory_context=memory_context,
        calendar_note=calendar_note,
    )

    category = gemini_result.get("category", CATEGORY_READ)
    reason = gemini_result.get("reason", "")
    confidence = gemini_result.get("confidence", 1.0)

    # ステップ3: 信頼度が低い場合はユーザー確認を求める
    if confidence < 0.5:
        logger.info(
            f"低信頼度 (confidence={confidence:.2f}): {email.get('subject', '')} → 要確認"
        )
        category = "要確認"
    elif is_meeting_participant:
        category = _upgrade_category(category)

    logger.debug(f"Gemini 判定: [{category}] {email.get('subject', '')}")
    return {
        "email_id": email_id,
        "category": category,
        "reason": reason,
        "email": email,
        "is_meeting_participant": is_meeting_participant,
    }


def classify_batch(
    emails: list[dict],
    gemini_client,
    contacts: dict,
    memory_context: str = "",
    calendar_client=None,
) -> list[dict]:
    """
    複数メールをまとめて分類して結果リストを返す。
    memory_context: MEMORY.md の内容（Gemini 分類の参考情報として各メールに渡す）
    calendar_client: 渡された場合、冒頭で一度だけ get_meeting_participants() を呼んでキャッシュする
    内部で classify() を繰り返し呼び出し、エラーが発生しても処理を継続する。
    """
    # calendar_client があれば N 通 × API 呼び出しを防ぐため冒頭で一括取得
    meeting_participants: set[str] = set()
    if calendar_client is not None:
        try:
            fetched = calendar_client.get_meeting_participants(hours=24)
            meeting_participants = set(fetched)
            logger.info(f"会議参加者 {len(meeting_participants)} 名を一括取得しました")
        except Exception as e:
            logger.warning(f"会議参加者一括取得失敗（カレンダーなしで続行）: {e}")

    results = []
    for email in emails:
        try:
            result = classify(
                email,
                gemini_client,
                contacts,
                memory_context=memory_context,
                calendar_client=None,  # 個別 API 呼び出しを防ぐため None を渡す
                meeting_participants=meeting_participants,
            )
            results.append(result)
            logger.debug(f"分類完了: [{result['category']}] {email.get('subject', '')}")
        except Exception as e:
            logger.error(f"分類エラー (subject={email.get('subject', '')}): {e}")
            # エラーが起きたメールはデフォルト分類にして処理継続
            results.append({
                "email_id": email.get("id", ""),
                "category": CATEGORY_READ,
                "reason": f"分類エラーのためデフォルト: {e}",
                "email": email,
                "is_meeting_participant": False,
            })
    return results


def update_memory(result: dict, memory_path: str = "../MEMORY.md") -> None:
    """
    分類結果をもとに MEMORY.md の学習データを更新する。
    Phase 1 では送信者ごとの分類傾向をログに記録するのみ。
    将来フェーズで本格的な学習データ蓄積を実装予定。
    """
    try:
        sender = result.get("email", {}).get("sender", "")
        category = result.get("category", "")
        logger.debug(f"メモリ更新対象: {sender} → {category}")
        # Phase 1: 実際のファイル更新は行わずログのみ記録
    except Exception as e:
        logger.error(f"MEMORY.md 更新エラー: {e}")
