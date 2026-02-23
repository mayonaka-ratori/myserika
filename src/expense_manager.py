"""
expense_manager.py
MoneyForward ME CSV インポート・経費照合モジュール。
ExpenseManager クラスで CSV の取り込みと expenses テーブルとの照合を担当する。
"""

import asyncio
import csv
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# MF CSV column name mapping (Japanese header → internal key)
_MF_COLUMN_MAP = {
    "計算対象":     "is_calculation_target",
    "日付":         "date",
    "内容":         "content",
    "金額（円）":   "amount",
    "保有金融機関": "source_account",
    "大項目":       "large_category",
    "中項目":       "medium_category",
    "メモ":         "memo",
    "振替":         "is_transfer",
    "ID":           "mf_id",
}

# Keyword → tax category mapping for Japanese freelancer accounts
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "通信費":     ["携帯", "Wi-Fi", "プロバイダ", "サーバー", "ドメイン", "SIM"],
    "旅費交通費": ["電車", "バス", "タクシー", "新幹線", "飛行機", "ETC", "Suica", "PASMO"],
    "消耗品費":   ["文房具", "インク", "USB", "ケーブル", "マウス", "キーボード"],
    "接待交際費": ["会食", "お中元", "お歳暮", "慶弔", "贈答"],
    "会議費":     ["カフェ", "スタバ", "ドトール", "打ち合わせ"],
    "地代家賃":   ["事務所", "コワーキング", "レンタルオフィス"],
    "水道光熱費": ["電気", "ガス", "水道", "東京電力", "東京ガス"],
    "広告宣伝費": ["Google広告", "SNS広告", "名刺", "チラシ"],
    "外注費":     ["デザイン依頼", "開発依頼", "翻訳", "Fiverr", "Lancers"],
    "新聞図書費": ["書籍", "Kindle", "技術書", "サブスク"],
    "研修費":     ["セミナー", "勉強会", "Udemy", "オンライン講座"],
    "雑費":       [],
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

    async def import_moneyforward_csv(self, file_path: str) -> dict:
        """Parse MoneyForward ME CSV and persist to DB.
        Returns {"imported": int, "skipped": int, "errors": list[str]}."""
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
        errors: list[str] = []

        for row in reader:
            # ID が空の行はスキップ
            raw_id = row.get("ID", "").strip()
            if not raw_id:
                skipped += 1
                continue

            # カラムマッピング
            mf_id = raw_id
            is_calculation_target = _parse_flag(row.get("計算対象", "1"))
            date_str = row.get("日付", "").strip()
            content_str = row.get("内容", "").strip()
            amount_str = row.get("金額（円）", "0").strip()
            source_account = row.get("保有金融機関", "").strip()
            large_category = row.get("大項目", "").strip()
            medium_category = row.get("中項目", "").strip()
            memo = row.get("メモ", "").strip()
            is_transfer = _parse_flag(row.get("振替", "0"))

            # 日付を YYYY-MM-DD に正規化
            date_normalized = _normalize_date(date_str)
            if not date_normalized:
                msg = f"Date parse failed for ID={mf_id}: {date_str!r}"
                logger.warning(msg)
                errors.append(msg)
                skipped += 1
                continue

            try:
                amount = _parse_amount(amount_str)
            except ValueError:
                msg = f"Amount parse failed for ID={mf_id}: {amount_str!r}"
                logger.warning(msg)
                errors.append(msg)
                skipped += 1
                continue

            inserted = await self._db.save_mf_transaction(
                mf_id=mf_id,
                is_calculation_target=is_calculation_target,
                date=date_normalized,
                content=content_str,
                amount=amount,
                source_account=source_account,
                large_category=large_category,
                medium_category=medium_category,
                memo=memo,
                is_transfer=is_transfer,
            )
            if inserted:
                new_count += 1
            else:
                skipped += 1  # duplicate mf_id already in DB

        logger.info(
            f"MF CSV import done: {new_count} new, {skipped} skipped, {len(errors)} errors"
        )
        return {"imported": new_count, "skipped": skipped, "errors": errors}

    async def match_with_moneyforward(self) -> list[dict]:
        """Find MoneyForward candidates for unmatched expense records.

        Confidence levels:
          - "certain":   ±1-day, exact amount, name similarity → auto-matched silently.
          - "likely":    ±2-day, exact amount, not "certain"   → returned for user review.
          - "uncertain": amount-only match, outside ±2-day     → returned for manual review.

        Returns only "likely" and "uncertain" items; "certain" are resolved in DB automatically.
        """
        try:
            unmatched_expenses = await self._db.get_unmatched_expenses()
        except Exception as e:
            logger.error(f"Error fetching unmatched expenses: {e}")
            return []

        results = []

        for expense in unmatched_expenses:
            expense_date = expense.get("date", "")
            expense_amount = expense.get("amount", 0)
            expense_desc = expense.get("store_name", "")
            abs_amount = abs(expense_amount)

            # Step 1: ±2-day, exact-amount, spending-only MF candidates
            candidates_2day = await self._get_mf_candidates_in_range(
                expense_date, abs_amount, days=2
            )

            found_mf_ids: set[str] = set()
            certain_matched = False
            likely_candidates: list[dict] = []

            for mf in candidates_2day:
                mf_id = mf.get("mf_id", "")
                found_mf_ids.add(mf_id)
                mf_content = mf.get("content", "")

                # Calendar-day delta to distinguish ±1-day ("certain") from ±2-day ("likely")
                try:
                    mf_dt = datetime.strptime(mf.get("date", "")[:10], "%Y-%m-%d")
                    exp_dt = datetime.strptime(expense_date[:10], "%Y-%m-%d")
                    day_delta = abs((mf_dt - exp_dt).days)
                except (ValueError, TypeError):
                    day_delta = 99

                # Name similarity: substring first, Gemini as fallback
                name_match = _partial_match_score(expense_desc, mf_content)
                if not name_match:
                    try:
                        name_match = await self._gemini_similarity_check(
                            expense_desc, mf_content
                        )
                    except Exception as e:
                        logger.warning(f"Gemini similarity check skipped: {e}")

                if day_delta <= 1 and name_match and not certain_matched:
                    # "certain" → auto-match silently, do not add to results
                    try:
                        await self._db.match_expense_to_mf(expense["id"], mf_id)
                        certain_matched = True
                        logger.info(
                            f"Auto-matched expense {expense['id']} to MF {mf_id} (certain)"
                        )
                    except Exception as e:
                        logger.error(f"Auto-match DB write error: {e}")
                else:
                    likely_candidates.append({"mf": mf, "confidence": "likely"})

            if certain_matched:
                continue  # expense fully resolved — move to next

            # Step 2: Amount-only matches outside the ±2-day window ("uncertain")
            uncertain_candidates: list[dict] = []
            for mf in await self._get_mf_candidates_amount_only(abs_amount, found_mf_ids):
                uncertain_candidates.append({"mf": mf, "confidence": "uncertain"})

            results.append({
                "expense": expense,
                "candidates": likely_candidates + uncertain_candidates,
            })

        return results

    async def _get_mf_candidates_in_range(
        self, date_str: str, amount: int, days: int = 2
    ) -> list[dict]:
        """Return MF spending transactions within ±days of date with matching absolute amount.
        Spending rows have amount < 0 in MoneyForward; income rows (amount > 0) are excluded."""
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
                      AND amount < 0
                      AND matched_expense_id IS NULL
                      AND is_transfer = 0
                      AND is_calculation_target = 1
                    ORDER BY date DESC
                    """,
                    (date_from, date_to, abs_amount),
                )
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"MF candidate fetch error: {e}")
            return []

    async def _get_mf_candidates_amount_only(
        self, abs_amount: int, exclude_mf_ids: set[str] | None = None
    ) -> list[dict]:
        """Return MF spending transactions matching the absolute amount with no date constraint.
        Used for "uncertain" confidence matches where only the amount aligns.
        Excludes mf_ids already found by _get_mf_candidates_in_range to avoid duplicates."""
        if exclude_mf_ids is None:
            exclude_mf_ids = set()

        try:
            import aiosqlite
            async with aiosqlite.connect(self._db._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT * FROM moneyforward_transactions
                    WHERE ABS(amount) = ?
                      AND amount < 0
                      AND matched_expense_id IS NULL
                      AND is_transfer = 0
                      AND is_calculation_target = 1
                    ORDER BY date DESC
                    LIMIT 10
                    """,
                    (abs_amount,),
                )
                rows = await cursor.fetchall()
                return [
                    dict(row) for row in rows
                    if row["mf_id"] not in exclude_mf_ids
                ]
        except Exception as e:
            logger.error(f"MF amount-only candidate fetch error: {e}")
            return []

    def rule_based_categorize(
        self, store_name: str, items_text: str = ""
    ) -> tuple[str, None] | None:
        """Match store name and items text against CATEGORY_KEYWORDS dictionary.
        Returns (category, None) on first keyword match, or None if no match found.
        subcategory is always None for rule-based matching."""
        haystack = f"{store_name} {items_text}".lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in haystack:
                    return (category, None)
        return None

    async def analyze_receipt_image(self, image_path: str) -> dict:
        """OCR a receipt image using Gemini vision and return structured data.

        Resizes images to max 1024px on the longest side before sending to Gemini
        to reduce token usage. Never raises — returns a partial/empty dict on failure.

        Returns:
            {
                "date": str | None,      # YYYY-MM-DD
                "store_name": str,       # fallback "不明"
                "items": list[dict],     # [{"name": str, "price": int, "quantity": int}]
                "subtotal": int | None,
                "tax": int | None,
                "total": int,            # fallback 0
                "payment_method": str,   # "cash" / "credit_card" / "electronic"
            }
        """
        _FALLBACK: dict = {
            "date": None,
            "store_name": "不明",
            "items": [],
            "subtotal": None,
            "tax": None,
            "total": 0,
            "payment_method": "cash",
        }

        try:
            from PIL import Image  # deferred — bot still starts if Pillow missing
        except ImportError:
            logger.error("Pillow is not installed; cannot OCR receipt images")
            return _FALLBACK

        client = self._gemini.get("client")
        model = self._gemini.get("model", "gemini-2.5-flash")
        if client is None:
            logger.error("Gemini client not initialized; cannot OCR receipt image")
            return _FALLBACK

        try:
            with Image.open(image_path) as _raw:
                # Resize so the longest side is at most 1024 px
                if max(_raw.width, _raw.height) > 1024:
                    _raw.thumbnail((1024, 1024), Image.LANCZOS)
                # Convert to RGB (handles RGBA PNGs, palette images, etc.)
                img = _raw.convert("RGB") if _raw.mode != "RGB" else _raw.copy()
        except Exception as e:
            logger.error(f"Failed to open/resize receipt image {image_path}: {e}")
            return _FALLBACK

        ocr_prompt = (
            "このレシート画像から以下の情報をJSON形式で抽出してください：\n"
            "{\n"
            '  "date": "YYYY-MM-DD（購入日）",\n'
            '  "store_name": "店名",\n'
            '  "items": [{"name": "品名", "price": 単価, "quantity": 数量}],\n'
            '  "subtotal": 小計,\n'
            '  "tax": 消費税額,\n'
            '  "total": 合計金額,\n'
            '  "payment_method": "支払方法（記載があれば cash/credit_card/electronic、なければnull）"\n'
            "}\n"
            "読み取れない項目はnullにしてください。"
        )

        try:
            response = await asyncio.to_thread(
                client.models.generate_content, model=model, contents=[img, ocr_prompt]
            )
            raw_text = response.text or ""
        except Exception as e:
            logger.error(f"Gemini vision API error for {image_path}: {e}")
            return _FALLBACK

        # Parse JSON — strip markdown fences then regex-extract the object
        try:
            cleaned = re.sub(r"```(?:json)?\s*", "", raw_text).strip().rstrip("`").strip()
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not m:
                raise ValueError("No JSON object found in Gemini response")
            parsed = json.loads(m.group())
        except Exception as e:
            logger.warning(f"Receipt OCR JSON parse failed ({e}); returning partial data")
            parsed = {}

        # Normalize and coerce each field
        valid_payment = {"cash", "credit_card", "electronic"}
        pm_raw = parsed.get("payment_method") or "cash"
        payment_method = pm_raw if pm_raw in valid_payment else "cash"

        items_raw = parsed.get("items")
        items: list[dict] = []
        if isinstance(items_raw, list):
            for it in items_raw:
                if isinstance(it, dict):
                    items.append({
                        "name":     str(it.get("name") or ""),
                        "price":    int(it["price"]) if it.get("price") is not None else 0,
                        "quantity": int(it["quantity"]) if it.get("quantity") is not None else 1,
                    })

        def _to_int_or_none(v: object) -> int | None:
            try:
                return int(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        date_raw = parsed.get("date")
        date_ok = (
            date_raw if isinstance(date_raw, str) and re.match(r"\d{4}-\d{2}-\d{2}$", date_raw)
            else None
        )

        return {
            "date":           date_ok,
            "store_name":     str(parsed.get("store_name") or "不明"),
            "items":          items,
            "subtotal":       _to_int_or_none(parsed.get("subtotal")),
            "tax":            _to_int_or_none(parsed.get("tax")),
            "total":          int(parsed.get("total") or 0),
            "payment_method": payment_method,
        }

    async def auto_categorize(
        self, store_name: str, items: list[dict]
    ) -> tuple[str, str | None]:
        """Determine the tax category for an expense using a three-stage pipeline:
        1. Rule-based keyword match (instant, no API call)
        2. DB history: reuse category if same store_name was seen before
        3. Gemini LLM fallback (Japanese prompt for accuracy)
        Returns (category, subcategory) — always succeeds, falls back to ("雑費", None).
        """
        items_text = " ".join(item.get("name", "") for item in items if item.get("name"))

        # Stage 1: rule-based keyword match
        rule_result = self.rule_based_categorize(store_name, items_text)
        if rule_result is not None:
            return rule_result

        # Stage 2: DB history — same store_name used before
        try:
            past = await self._db.get_expenses(store_name=store_name.strip(), limit=1)
            if past:
                cat = past[0].get("category") or ""
                if cat:
                    sub = past[0].get("subcategory") or None
                    logger.debug(
                        f"auto_categorize: DB history match for '{store_name}' → {cat}"
                    )
                    return (cat, sub)
        except Exception as e:
            logger.warning(f"auto_categorize: DB history lookup failed: {e}")

        # Stage 3: Gemini fallback
        client = self._gemini.get("client")
        model = self._gemini.get("model", "gemini-2.5-flash")
        if client is None:
            return ("雑費", None)

        category_list = "、".join(CATEGORY_KEYWORDS.keys())
        items_summary = items_text[:200] if items_text else "（品目不明）"
        prompt = (
            f"フリーランスの青色申告において、"
            f"店名「{store_name}」での購入品「{items_summary}」は"
            f"どの勘定科目に分類すべきですか？\n"
            f"選択肢: {category_list}\n"
            f'JSON形式のみで回答してください: {{"category": "...", "subcategory": "...またはnull"}}'
        )

        try:
            response = await asyncio.to_thread(
                client.models.generate_content, model=model, contents=prompt
            )
            raw = response.text or ""
            cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not m:
                raise ValueError("No JSON in Gemini categorize response")
            parsed = json.loads(m.group())
            cat = str(parsed.get("category") or "").strip()
            sub_raw = parsed.get("subcategory")
            sub = str(sub_raw).strip() if sub_raw and str(sub_raw).lower() != "null" else None
            if cat in CATEGORY_KEYWORDS:
                logger.debug(
                    f"auto_categorize: Gemini result for '{store_name}' → {cat}"
                )
                return (cat, sub)
        except Exception as e:
            logger.warning(f"auto_categorize: Gemini fallback failed: {e}")

        return ("雑費", None)

    async def _gemini_similarity_check(self, content_a: str, content_b: str) -> bool:
        """Use Gemini to check if two store names / descriptions refer to the same transaction.
        Returns True if Gemini answers "yes", False otherwise or on any error."""
        if not content_a or not content_b:
            return False

        prompt = (
            "以下の2つの店名・内容が同一取引を指しているか yes/no のみで答えてください。\n\n"
            f"A: {content_a}\n"
            f"B: {content_b}"
        )

        try:
            client = self._gemini.get("client")
            model = self._gemini.get("model", "gemini-2.5-flash")
            if client is None:
                return False
            response = await asyncio.to_thread(
                client.models.generate_content, model=model, contents=prompt
            )
            answer = response.text.strip().lower()
            return "yes" in answer
        except Exception as e:
            logger.warning(f"Gemini similarity check failed: {e}")
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
