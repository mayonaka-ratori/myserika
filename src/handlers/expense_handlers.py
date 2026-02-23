"""
handlers/expense_handlers.py
/expense command, receipt photo OCR, CSV document upload,
and all expense/receipt/match callback queries.
"""

import html
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from expense_manager import CATEGORY_KEYWORDS

logger = logging.getLogger(__name__)


# ── Receipt helpers ───────────────────────────────────────────────────────────

def _receipt_approval_keyboard() -> InlineKeyboardMarkup:
    """Return the Save / Edit Category / Discard inline keyboard for receipt review."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 保存",     callback_data="rcpt_save"),
        InlineKeyboardButton("📝 科目変更", callback_data="rcpt_edit"),
        InlineKeyboardButton("❌ 破棄",     callback_data="rcpt_discard"),
    ]])


def _format_receipt_summary(ocr: dict, category: str) -> str:
    """Return the HTML summary string shown after receipt OCR."""
    date_str   = html.escape(ocr.get("date")       or "不明")
    store_str  = html.escape(ocr.get("store_name") or "不明")
    total      = ocr.get("total") or 0
    tax        = ocr.get("tax")   or 0
    items      = ocr.get("items") or []
    item_names = " / ".join(
        html.escape(it.get("name", "")) for it in items[:5] if it.get("name")
    ) or "（品目なし）"
    cat_str = html.escape(category)
    return (
        "🧾 <b>レシート読み取り結果</b>\n"
        "─────────────\n"
        f"📅 日付: {date_str}\n"
        f"🏪 店名: {store_str}\n"
        f"💰 金額: ¥{total:,}（消費税: ¥{tax:,}）\n"
        f"📦 品目: {item_names}\n"
        f"📂 勘定科目: {cat_str}（自動）\n"
        "─────────────"
    )


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_expense_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/expense command: show the expense management menu."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 レシート撮影",           callback_data="expense_receipt")],
        [InlineKeyboardButton("📊 今月のサマリー",         callback_data="expense_summary")],
        [InlineKeyboardButton("📥 MoneyForward CSV 読込", callback_data="expense_csv_start")],
        [InlineKeyboardButton("🔍 未照合の経費を確認",     callback_data="expense_match_run")],
        [InlineKeyboardButton("📋 年間レポート",           callback_data="expense_annual")],
    ])
    await update.message.reply_text(
        "💰 <b>経費管理</b>", parse_mode="HTML", reply_markup=keyboard
    )


async def handle_receipt_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle an incoming photo message as a receipt.
    Downloads the image, runs OCR via Gemini vision, auto-categorizes,
    then shows a Save / Edit / Discard approval flow.
    """
    expense_manager = context.bot_data.get("expense_manager")
    db              = context.bot_data.get("db")
    if not expense_manager or not db:
        await update.message.reply_text("⚠️ 経費マネージャーが初期化されていません。")
        return

    # Reject if a previous receipt is still pending
    chat_id  = str(update.effective_chat.id)
    existing = context.bot_data.get("pending_receipts", {}).get(chat_id)
    if existing:
        await update.message.reply_text(
            "⚠️ 前のレシートがまだ保留中です。先にそちらを保存または破棄してください。"
        )
        return

    placeholder = await update.message.reply_text("⏳ OCR 中... / Scanning receipt...")

    # Save photo to data/receipts/ — microsecond suffix prevents collisions
    save_dir = Path(__file__).parent.parent.parent / "data" / "receipts"
    save_dir.mkdir(parents=True, exist_ok=True)
    now      = datetime.now()
    filename = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond:06d}.jpg"
    save_path = save_dir / filename

    try:
        photo   = update.message.photo[-1]  # largest available size
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(str(save_path))
    except Exception as e:
        logger.error(f"Receipt photo download error: {e}")
        await placeholder.edit_text(
            f"⚠️ 画像の取得に失敗しました：{html.escape(str(e))}", parse_mode="HTML"
        )
        return

    # OCR via Gemini vision
    try:
        ocr = await expense_manager.analyze_receipt_image(str(save_path))
    except Exception as e:
        logger.error(f"Receipt OCR error: {e}")
        ocr = {"store_name": "不明", "total": 0, "items": [], "tax": 0, "date": None}

    # Auto-categorize
    try:
        category, subcategory = await expense_manager.auto_categorize(
            ocr.get("store_name", "不明"), ocr.get("items", [])
        )
    except Exception as e:
        logger.warning(f"Receipt auto-categorize error: {e}")
        category, subcategory = "雑費", None

    # Store pending state keyed by chat_id
    context.bot_data.setdefault("pending_receipts", {})[chat_id] = {
        "image_path": str(save_path),
        "ocr":        ocr,
        "category":   category,
        "subcategory": subcategory,
    }

    await placeholder.edit_text(
        _format_receipt_summary(ocr, category),
        parse_mode="HTML",
        reply_markup=_receipt_approval_keyboard(),
    )


async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    CSV file receive handler.
    Only processes the file when awaiting_csv_upload is True; silently ignores otherwise.
    """
    if not context.bot_data.get("awaiting_csv_upload"):
        return  # Ignore if not waiting for a CSV

    doc = update.message.document
    if not doc.file_name.lower().endswith(".csv"):
        await update.message.reply_text(
            "⚠️ CSV ファイルを送信してください。/ Please send a CSV file."
        )
        return

    context.bot_data["awaiting_csv_upload"] = False
    await update.message.reply_text("⏳ 読み込み中... / Importing...")

    tg_file        = await context.bot.get_file(doc.file_id)
    expense_manager = context.bot_data.get("expense_manager")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        result = await expense_manager.import_moneyforward_csv(tmp_path)
    except Exception as e:
        logger.error(f"CSV import error: {e}")
        await update.message.reply_text(
            f"⚠️ インポートに失敗しました：{html.escape(str(e))}", parse_mode="HTML"
        )
        return
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    n_imported = result["imported"]
    n_skipped  = result["skipped"]
    errors     = result.get("errors", [])
    summary    = f"✅ <b>{n_imported}件インポートしました</b>（{n_skipped}件は重複スキップ）"
    if errors:
        summary += f"\n⚠️ パースエラー {len(errors)}件（例：{html.escape(errors[0])}）"
    summary += "\n照合を実行しますか？"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 照合を実行", callback_data="expense_match_run"),
        InlineKeyboardButton("後で",          callback_data="expense_later"),
    ]])
    await update.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_expense_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle expense/receipt/CSV match callback queries.
    query.answer() has already been called by the main dispatcher.
    Handles all expense_*, ematch_*, and rcpt_* callback data patterns.
    """
    query    = update.callback_query
    data     = query.data
    bot_data = context.bot_data

    # --- Show receipt photo prompt ---
    if data == "expense_receipt":
        await query.edit_message_text(
            "📸 レシートの写真を送信してください。/ Please send a photo of your receipt."
        )

    # --- Monthly expense summary ---
    elif data == "expense_summary":
        expense_manager = bot_data.get("expense_manager")
        if not expense_manager:
            await query.edit_message_text("⚠️ 経費マネージャーが初期化されていません。")
            return
        now = datetime.now()
        try:
            report_text = await expense_manager.generate_monthly_report(now.year, now.month)
        except Exception as e:
            await query.edit_message_text(
                f"⚠️ レポート生成エラー：{html.escape(str(e))}", parse_mode="HTML"
            )
            return
        await query.edit_message_text(
            f"<pre>{html.escape(report_text)}</pre>", parse_mode="HTML"
        )

    # --- Start CSV upload flow ---
    elif data == "expense_csv_start":
        bot_data["awaiting_csv_upload"] = True
        await query.edit_message_text(
            "📥 MoneyForward ME の CSV ファイルを送信してください。\n"
            "/ Please send your MoneyForward ME CSV file."
        )

    # --- Run expense-to-MF matching ---
    elif data == "expense_match_run":
        expense_manager = bot_data.get("expense_manager")
        db              = bot_data.get("db")
        if not expense_manager or not db:
            await query.edit_message_text("⚠️ 経費マネージャーが初期化されていません。")
            return

        await query.edit_message_text("🔍 照合を実行中...")

        try:
            results = await expense_manager.match_with_moneyforward()
        except Exception as e:
            logger.error(f"Matching error: {e}")
            await query.edit_message_text(
                f"⚠️ 照合エラー：{html.escape(str(e))}", parse_mode="HTML"
            )
            return

        chat_id = bot_data.get("chat_id", "")

        if not results:
            # Expense table is empty → show unmatched MF transactions instead
            pending_mf = await db.get_mf_transactions(unmatched_only=True, limit=5)
            if not pending_mf:
                await query.edit_message_text("✅ 未照合の取引はありません。")
                return

            await query.edit_message_text(
                f"📋 未確認の取引が {len(pending_mf)} 件あります。確認してください。"
            )
            for mf in pending_mf:
                mf_id        = mf["mf_id"]
                date_disp    = mf.get("date", "")[:10]
                content_disp = html.escape(mf.get("content", "（内容不明）"))
                amount       = mf.get("amount", 0)
                cat          = html.escape(mf.get("large_category", "未分類"))
                text = (
                    f"📝 <b>{date_disp}</b> {content_disp}\n"
                    f"金額：¥{abs(amount):,} / カテゴリ：{cat}"
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ 確定", callback_data=f"ematch_y:0:{mf_id}"),
                    InlineKeyboardButton("❌ 無視", callback_data=f"ematch_no:{mf_id}"),
                ]])
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb
                    )
                except Exception as e:
                    logger.warning(f"MF transaction notification error: {e}")
        else:
            await query.edit_message_text(
                f"🔍 照合候補が {len(results)} 件見つかりました。"
            )
            for item in results[:5]:
                expense    = item["expense"]
                candidates = item["candidates"]
                exp_id     = expense["id"]
                exp_desc   = html.escape(expense.get("store_name", ""))
                exp_date   = expense.get("date", "")[:10]
                exp_amount = expense.get("amount", 0)
                lines = [
                    f"💰 経費：<b>{exp_desc}</b>（{exp_date} / ¥{abs(exp_amount):,}）",
                ]
                for cand in candidates[:3]:
                    mf         = cand["mf"]
                    conf       = cand["confidence"]
                    mf_content = html.escape(mf.get("content", ""))
                    mf_date    = mf.get("date", "")[:10]
                    lines.append(f"  [{conf}] {mf_date} {mf_content}")

                # Build keyboard: best candidate for Match button; always offer cash/skip
                if candidates:
                    best_mf_id = candidates[0]["mf"]["mf_id"]
                    kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "✅ 照合確定",
                                callback_data=f"ematch_y:{exp_id}:{best_mf_id}",
                            ),
                            InlineKeyboardButton(
                                "❌ 現金払い",
                                callback_data=f"ematch_cash:{exp_id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "⏭ スキップ",
                                callback_data=f"ematch_skip:{exp_id}",
                            ),
                        ],
                    ])
                else:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "❌ 現金払い", callback_data=f"ematch_cash:{exp_id}",
                        ),
                        InlineKeyboardButton(
                            "⏭ スキップ",  callback_data=f"ematch_skip:{exp_id}",
                        ),
                    ]])

                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="\n".join(lines),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception as e:
                    logger.warning(f"Match candidate send error: {e}")

    # --- Confirm match between an expense and an MF transaction ---
    elif data.startswith("ematch_y:"):
        # Format: "ematch_y:{expense_id}:{mft_id}"
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.answer("データ形式エラー")
            return
        exp_id_str, mft_id = parts[1], parts[2]
        db = bot_data.get("db")
        if db:
            exp_id = int(exp_id_str) if exp_id_str.isdigit() else 0
            if exp_id:
                await db.match_expense_to_mf(exp_id, mft_id)
        await query.edit_message_text("✅ 照合を確定しました。/ Match confirmed.")

    # --- Ignore unmatched MF transaction (legacy no-expense path) ---
    elif data.startswith("ematch_no:"):
        await query.edit_message_text("❌ 無視しました。/ Ignored.")

    # --- Record expense as cash payment (no MF transaction expected) ---
    elif data.startswith("ematch_cash:"):
        exp_id_str = data.split(":", 1)[1]
        db         = bot_data.get("db")
        if db and exp_id_str.isdigit():
            try:
                await db.update_expense(int(exp_id_str), moneyforward_matched=1)
            except Exception as e:
                logger.warning(f"ematch_cash DB update error: {e}")
        await query.edit_message_text(
            "❌ 現金払いとして記録しました。/ Recorded as cash payment (no MF match)."
        )

    # --- Skip this expense for now (defer to a later session) ---
    elif data.startswith("ematch_skip:"):
        await query.edit_message_text(
            "⏭ スキップしました。/expense から再度確認できます。/ Skipped for now."
        )

    # --- Annual expense report ---
    elif data == "expense_annual":
        expense_manager = bot_data.get("expense_manager")
        if not expense_manager:
            await query.edit_message_text("⚠️ 経費マネージャーが初期化されていません。")
            return
        year = datetime.now().year
        try:
            report = await expense_manager.generate_annual_report(year)
        except Exception as e:
            await query.edit_message_text(
                f"⚠️ レポート生成エラー：{html.escape(str(e))}", parse_mode="HTML"
            )
            return
        # Offer a CSV download button below the text report
        csv_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📥 CSV をダウンロード",
                callback_data=f"expense_csv_download:{year}",
            )
        ]])
        await query.edit_message_text(
            f"<pre>{html.escape(report['text'])}</pre>",
            parse_mode="HTML",
            reply_markup=csv_keyboard,
        )

    # --- Download annual expense CSV ---
    elif data.startswith("expense_csv_download:"):
        year_str = data.split(":", 1)[1]
        if not year_str.isdigit():
            await query.answer("年の形式が無効です。")
            return
        year            = int(year_str)
        expense_manager = bot_data.get("expense_manager")
        if not expense_manager:
            await query.answer("経費マネージャーが初期化されていません。")
            return

        output_path = str(
            Path(__file__).parent.parent.parent / "data" / "reports"
            / f"{year}_annual_expense.csv"
        )
        try:
            await expense_manager.export_annual_csv(year, output_path)
        except Exception as e:
            logger.error(f"Annual CSV export error: {e}")
            await query.answer(f"CSV 生成エラー: {e}")
            return

        chat_id = bot_data.get("chat_id", "")
        try:
            with open(output_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=f"{year}_annual_expense.csv",
                    caption=f"📥 {year}年 年間経費 CSV",
                )
            await query.answer("CSV を送信しました。")
        except Exception as e:
            logger.error(f"CSV send error: {e}")
            await query.answer(f"送信エラー: {e}")

    # --- Dismiss expense menu ---
    elif data == "expense_later":
        await query.edit_message_text("了解です。/expense でいつでも確認できます。")

    # ── Receipt approval sub-flow ─────────────────────────────────────────────

    # --- Save receipt to DB ---
    elif data == "rcpt_save":
        chat_id = str(update.effective_chat.id)
        db      = bot_data.get("db")
        pending = bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending or not db:
            await query.edit_message_text("⚠️ 保存するレシートが見つかりません。")
            return
        ocr = pending["ocr"]
        try:
            await db.save_expense(
                date=ocr.get("date") or datetime.now().strftime("%Y-%m-%d"),
                store_name=ocr.get("store_name") or "不明",
                amount=ocr.get("total") or 0,
                category=pending["category"],
                tax_amount=ocr.get("tax"),
                subcategory=pending.get("subcategory"),
                payment_method=ocr.get("payment_method") or "cash",
                receipt_image_path=pending["image_path"],
                source="receipt_photo",
            )
            bot_data.get("pending_receipts", {}).pop(chat_id, None)
            await query.edit_message_text(
                f"✅ <b>保存しました</b>\n"
                f"店名: {html.escape(ocr.get('store_name','不明'))} / "
                f"¥{(ocr.get('total') or 0):,} / {html.escape(pending['category'])}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Receipt save error: {e}")
            await query.edit_message_text(
                f"⚠️ 保存エラー：{html.escape(str(e))}", parse_mode="HTML"
            )

    # --- Discard receipt ---
    elif data == "rcpt_discard":
        chat_id = str(update.effective_chat.id)
        pending = bot_data.get("pending_receipts", {}).pop(chat_id, None)
        if pending:
            try:
                Path(pending["image_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        await query.edit_message_text("❌ 破棄しました。/ Receipt discarded.")

    # --- Show category-selection keyboard for receipt ---
    elif data == "rcpt_edit":
        chat_id = str(update.effective_chat.id)
        pending = bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        cats = list(CATEGORY_KEYWORDS.keys())
        # Build 2-per-row keyboard
        rows = []
        for i in range(0, len(cats), 2):
            row = [InlineKeyboardButton(cats[i], callback_data=f"rcpt_cat:{cats[i]}")]
            if i + 1 < len(cats):
                row.append(
                    InlineKeyboardButton(cats[i + 1], callback_data=f"rcpt_cat:{cats[i + 1]}")
                )
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅️ 戻る", callback_data="rcpt_back")])
        await query.edit_message_text(
            "📂 勘定科目を選択してください：",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # --- Apply selected category to pending receipt ---
    elif data.startswith("rcpt_cat:"):
        chat_id      = str(update.effective_chat.id)
        new_category = data.split(":", 1)[1]
        if new_category not in CATEGORY_KEYWORDS:
            await query.edit_message_text("⚠️ 無効な勘定科目です。")
            return
        pending = bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        pending["category"]    = new_category
        pending["subcategory"] = None
        await query.edit_message_text(
            _format_receipt_summary(pending["ocr"], new_category),
            parse_mode="HTML",
            reply_markup=_receipt_approval_keyboard(),
        )

    # --- Go back to receipt approval view ---
    elif data == "rcpt_back":
        chat_id = str(update.effective_chat.id)
        pending = bot_data.get("pending_receipts", {}).get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ 対象のレシートが見つかりません。")
            return
        await query.edit_message_text(
            _format_receipt_summary(pending["ocr"], pending["category"]),
            parse_mode="HTML",
            reply_markup=_receipt_approval_keyboard(),
        )


# ── Free-text handler (awaiting_csv_upload state) ─────────────────────────────

async def handle_csv_upload_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Inform the user to send a file attachment instead of text.
    Called when bot_data['awaiting_csv_upload'] is True and a text message arrives.
    """
    await update.message.reply_text(
        "📎 テキストではなく CSV ファイルを添付してください。/ Please attach a CSV file, not text."
    )
