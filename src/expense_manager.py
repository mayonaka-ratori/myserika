"""
expense_manager.py
MoneyForward ME CSV インポート・経費照合モジュール。
ExpenseManager クラスで CSV の取り込みと expenses テーブルとの照合を担当する。
"""

import csv
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# MF CSV のカラム名マッピング（日本語ヘッダー → 内部キー）
_MF_COLUMN_MAP = {
    "計算対象":     "is_calculated",
    "日付":         "date",
    "内容":         "content",
    "金額（円）":   "amount",
    "保有金融機関": "institution",
    "大項目":       "category_large",
    "中項目":       "category_medium",
    "メモ":         "memo",
    "振替":         "is_transfer",
    "ID":           "mf_id",
}

# CSV エンコード候補（BOM 付き UTF-8 を最初に試す）
_ENCODINGS = ["utf-8-sig", "shift-jis", "cp932"]


def _parse_amount(s: str) -> int:
    """"1,234" / "-1,234" → int に変換する。
    / Convert MF amount string to integer."""
    s = s.strip().replace(",", "")
    if not s:
        return 0
    return int(s)


def _parse_flag(s: str) -> int:
    """"1" / "0" / "○" / "" → int（0 or 1）に変換する。
    / Convert flag string to int."""
    s = s.strip()
    if s in ("1", "○", "true", "True", "TRUE"):
        return 1
    return 0


def _partial_match_score(a: str, b: str) -> bool:
    """片方がもう片方の部分文字列かを判定（空白除去・小文字化後）。
    / Check if one string is a substring of the other after normalization."""
    a_norm = a.strip().lower().replace(" ", "").replace("　", "")
    b_norm = b.strip().lower().replace(" ", "").replace("　", "")
    if not a_norm or not b_norm:
        return False
    return a_norm in b_norm or b_norm in a_norm


class ExpenseManager:
    """
    MoneyForward ME CSV のインポートと経費照合を担当するマネージャークラス。
    Manager class for importing MoneyForward ME CSV and matching expenses.
    """

    def __init__(self, db, gemini_client: dict):
        """
        db: Database インスタンス / Database instance
        gemini_client: gemini_client.init_client() の戻り値辞書
        / dict returned by gemini_client.init_client()
        """
        self._db = db
        self._gemini = gemini_client

    async def import_moneyforward_csv(self, file_path: str) -> int:
        """MF CSV を読み込んで DB に保存し、インポート件数を返す。
        / Parse MoneyForward CSV and persist to DB; returns count of new rows."""
        # エンコード順試行
        content: str | None = None
        for enc in _ENCODINGS:
            try:
                with open(file_path, encoding=enc, newline="") as f:
                    content = f.read()
                logger.info(f"CSV エンコード検出: {enc}")
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            raise ValueError("CSV ファイルのエンコードを判定できませんでした。/ Could not detect CSV encoding.")

        reader = csv.DictReader(content.splitlines())
        if reader.fieldnames is None:
            raise ValueError("CSV ヘッダーが見つかりません。/ CSV header not found.")

        new_count = 0
        skipped = 0

        for row in reader:
            # ID が空の行はスキップ
            raw_id = row.get("ID", "").strip()
            if not raw_id:
                skipped += 1
                continue

            # カラムマッピング
            mf_id = raw_id
            is_calculated = _parse_flag(row.get("計算対象", "1"))
            date_str = row.get("日付", "").strip()
            content_str = row.get("内容", "").strip()
            amount_str = row.get("金額（円）", "0").strip()
            institution = row.get("保有金融機関", "").strip()
            category_large = row.get("大項目", "").strip()
            category_medium = row.get("中項目", "").strip()
            memo = row.get("メモ", "").strip()
            is_transfer = _parse_flag(row.get("振替", "0"))

            # 日付を YYYY-MM-DD に正規化
            date_normalized = _normalize_date(date_str)
            if not date_normalized:
                logger.warning(f"日付パース失敗でスキップ: {date_str!r} (ID={mf_id})")
                skipped += 1
                continue

            try:
                amount = _parse_amount(amount_str)
            except ValueError:
                logger.warning(f"金額パース失敗でスキップ: {amount_str!r} (ID={mf_id})")
                skipped += 1
                continue

            inserted = await self._db.save_mf_transaction(
                mf_id=mf_id,
                is_calculated=is_calculated,
                date=date_normalized,
                content=content_str,
                amount=amount,
                institution=institution,
                category_large=category_large,
                category_medium=category_medium,
                memo=memo,
                is_transfer=is_transfer,
            )
            if inserted:
                new_count += 1

        logger.info(
            f"MF CSV インポート完了: 新規 {new_count} 件, スキップ {skipped} 件"
            f" / MF CSV import done: {new_count} new, {skipped} skipped"
        )
        return new_count

    async def match_with_moneyforward(self) -> list[dict]:
        """
        expenses テーブルの未照合レコードに対して MF 候補を探す。
        / Find MoneyForward candidates for unmatched expense records.

        戻り値 / Returns:
            [
                {
                    "expense": {...},
                    "candidates": [
                        {"mf": {...}, "confidence": "確実"|"可能性高"|"不明"}
                    ]
                },
                ...
            ]
        """
        # 未照合経費を取得（matched_mf_id IS NULL）
        try:
            import aiosqlite
            async with aiosqlite.connect(self._db._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT * FROM expenses WHERE matched_mf_id IS NULL ORDER BY date DESC LIMIT 50"
                )
                rows = await cursor.fetchall()
                unmatched_expenses = [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"未照合経費取得エラー / Error fetching unmatched expenses: {e}")
            return []

        results = []

        for expense in unmatched_expenses:
            expense_date = expense.get("date", "")
            expense_amount = expense.get("amount", 0)
            expense_desc = expense.get("description", "")

            # ±2日の日付範囲で候補を絞り込む
            candidates_raw = await self._get_mf_candidates_in_range(
                expense_date, expense_amount, days=2
            )

            candidates = []
            auto_matched = False

            for mf in candidates_raw:
                mf_content = mf.get("content", "")

                # 部分一致チェック
                if _partial_match_score(expense_desc, mf_content):
                    confidence = "確実"
                    # 確実な照合は自動的に matched に更新
                    if not auto_matched:
                        await self._db.update_mf_match_status(
                            mf["mf_id"], "matched", expense["id"]
                        )
                        await self._update_expense_matched_mf(expense["id"], mf["mf_id"])
                        auto_matched = True
                else:
                    # Gemini で類似判定
                    try:
                        is_similar = await self._gemini_similarity_check(
                            expense_desc, mf_content
                        )
                    except Exception as e:
                        logger.warning(f"Gemini 類似判定エラー (スキップ): {e}")
                        is_similar = False
                    confidence = "可能性高" if is_similar else "不明"

                candidates.append({"mf": mf, "confidence": confidence})

            if candidates or not auto_matched:
                results.append({"expense": expense, "candidates": candidates})

        return results

    async def _get_mf_candidates_in_range(
        self, date_str: str, amount: int, days: int = 2
    ) -> list[dict]:
        """指定日付の ±days 日・同額（絶対値）の MF 取引を返す。
        / Return MF transactions within ±days of date with matching absolute amount."""
        try:
            base_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return []

        date_from = (base_date - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
        abs_amount = abs(amount)

        try:
            import aiosqlite
            async with aiosqlite.connect(self._db._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT * FROM moneyforward_transactions
                    WHERE date BETWEEN ? AND ?
                      AND ABS(amount) = ?
                      AND match_status = 'pending'
                    ORDER BY date DESC
                    """,
                    (date_from, date_to, abs_amount),
                )
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"MF 候補取得エラー: {e}")
            return []

    async def _update_expense_matched_mf(self, expense_id: int, mf_id: str) -> None:
        """expenses テーブルの matched_mf_id を更新する。
        / Update matched_mf_id in expenses table."""
        try:
            import aiosqlite
            now = datetime.now().isoformat()
            async with aiosqlite.connect(self._db._db_path) as db:
                await db.execute(
                    "UPDATE expenses SET matched_mf_id=?, updated_at=? WHERE id=?",
                    (mf_id, now, expense_id),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"expense 照合更新エラー: {e}")

    async def _gemini_similarity_check(self, content_a: str, content_b: str) -> bool:
        """Gemini で2つの店名・内容が同一取引か判定する。
        / Use Gemini to check if two descriptions refer to the same transaction."""
        if not content_a or not content_b:
            return False

        prompt = (
            "以下の2つの店名・内容が同一取引を指しているか yes/no のみで答えてください。\n\n"
            f"A: {content_a}\n"
            f"B: {content_b}"
        )

        try:
            # gemini_client は dict。_call_model 相当の処理を直接実行する
            client = self._gemini.get("client")
            model = self._gemini.get("model", "gemini-2.5-flash")
            if client is None:
                return False
            response = client.models.generate_content(model=model, contents=prompt)
            answer = response.text.strip().lower()
            return "yes" in answer
        except Exception as e:
            logger.warning(f"Gemini 類似判定失敗: {e}")
            return False


def _normalize_date(date_str: str) -> str:
    """各種日付フォーマットを YYYY-MM-DD に正規化する。
    / Normalize various date formats to YYYY-MM-DD."""
    date_str = date_str.strip()
    if not date_str:
        return ""

    # YYYY/MM/DD
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""
