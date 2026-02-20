"""
main.py
MY-SECRETARY エージェントのエントリーポイント。
設定を読み込み、各クライアントを初期化して定期実行ループを起動する。
Telegram Bot をバックグラウンドで起動しつつ、Gmail を定期ポーリングする。
"""

import asyncio
import logging
import re
import yaml
from datetime import datetime
from pathlib import Path

from gmail_client import (
    authenticate,
    build_calendar_service,
    get_unread_emails,
    learn_contacts,
    learn_writing_style,
    _read_learning_flags,
    _update_learning_flags,
)
from daily_summary import send_daily_briefing
from gemini_client import init_client as init_gemini, generate_reply_draft
from telegram_bot import (
    build_application,
    send_notification,
    send_email_summary,
)
from classifier import (
    load_contacts,
    classify_batch,
    CATEGORY_URGENT,
    CATEGORY_NORMAL,
    extract_email_address,
)
from discord_client import DiscordMonitor

# ログ設定（INFOレベル、タイムスタンプ付き）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# プロジェクトルートからのパス定義
CONFIG_PATH   = Path(__file__).parent.parent / "config.yaml"
STATE_PATH    = Path(__file__).parent.parent / "STATE.md"
MEMORY_PATH   = Path(__file__).parent.parent / "MEMORY.md"
CONTACTS_PATH = Path(__file__).parent.parent / "contacts.md"


def load_config(path: Path) -> dict:
    """
    config.yaml を読み込んで辞書として返す。
    ファイルが存在しない場合は FileNotFoundError を発生させる。
    """
    if not path.exists():
        raise FileNotFoundError(f"config.yaml が見つかりません: {path}")

    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"設定読み込み完了: {path}")
    return config


def update_state(state_path: Path, key: str, value: str) -> None:
    """
    STATE.md の指定セクション（## キー）配下の内容を更新する。
    セクションが存在しない場合はファイル末尾に追記する。
    """
    try:
        content = state_path.read_text(encoding="utf-8") if state_path.exists() else ""

        # "## キー\n値" のパターンで行を置換
        pattern = rf"(## {re.escape(key)}\n)([^\n#]*)"
        if re.search(pattern, content):
            new_content = re.sub(pattern, rf"\g<1>{value}", content)
        else:
            # セクションが存在しない場合は末尾に追記
            new_content = content.rstrip() + f"\n\n## {key}\n{value}\n"

        state_path.write_text(new_content, encoding="utf-8")

    except Exception as e:
        logger.error(f"STATE.md 更新エラー: {e}")


def load_user_style(memory_path: Path) -> str:
    """
    MEMORY.md からユーザーの返信スタイル設定を読み込む。
    ファイルが存在しない・読み込めない場合は空文字列を返す。
    """
    try:
        if memory_path.exists():
            # 先頭 2000 字を返信スタイル・分類の参考として使用
            return memory_path.read_text(encoding="utf-8")[:2000]
    except Exception:
        pass
    return ""


def _is_quiet_hours(config: dict) -> bool:
    """
    config の quiet_hours 設定に基づいて現在が静止時間帯か判定する。
    start > end の場合（例: 23:00〜07:00）は日付をまたぐケースに対応する。
    """
    qh = config.get("quiet_hours", {})
    if not qh.get("enabled", False):
        return False

    try:
        now = datetime.now().time()
        start = datetime.strptime(qh["start"], "%H:%M").time()
        end = datetime.strptime(qh["end"], "%H:%M").time()

        if start <= end:
            return start <= now < end
        else:
            # 日付をまたぐ場合（例: 23:00〜07:00）
            return now >= start or now < end
    except Exception as e:
        logger.warning(f"quiet_hours 設定のパースに失敗: {e}")
        return False


def _is_learning_done(kind: str, memory_path: Path) -> bool:
    """
    MEMORY.md の自動学習フラグを確認し、7日以内に学習済みなら True を返す。
    kind: "contacts" or "style"
    """
    try:
        flags = _read_learning_flags(str(memory_path))
        date_key = "contacts_date" if kind == "contacts" else "style_date"
        date_str = flags.get(date_key, "")
        if not date_str:
            return False

        days_since = (
            datetime.now().date()
            - datetime.strptime(date_str, "%Y-%m-%d").date()
        ).days
        return days_since < 7

    except Exception as e:
        logger.warning(f"学習フラグ確認エラー ({kind}): {e}")
        return False


async def check_and_process_emails(
    gmail_service, gemini_client, telegram_app, config: dict
) -> None:
    """
    メールチェック〜分類〜返信案生成〜Telegram 通知までの一連の処理を実行する。
    処理フロー:
      0. retry_queue 内のメールを先に再処理
      1. get_unread_emails() で未読メールを取得
      2. classify_batch() で4カテゴリに分類
      3. 要返信メールに対して返信案を生成して pending_approvals に格納
         （__RETRY__ カテゴリや "__RETRY__" ドラフトは retry_queue に追加）
      4. Telegram にサマリーを送信
      5. STATE.md の最終チェック時刻を更新
    """
    chat_id = config["telegram"]["chat_id"]
    bot = telegram_app.bot

    logger.info("メールチェック開始")

    try:
        contacts = load_contacts(str(CONTACTS_PATH))
        user_style = load_user_style(MEMORY_PATH)
        pending_approvals = telegram_app.bot_data.setdefault("pending_approvals", {})
        retry_queue: list = telegram_app.bot_data.setdefault("retry_queue", [])

        # ステップ0: retry_queue の再処理
        if retry_queue:
            logger.info(f"retry_queue 内の {len(retry_queue)} 件のメールを再処理します")
            retry_emails = list(retry_queue)
            telegram_app.bot_data["retry_queue"] = []

            retry_classified = classify_batch(
                retry_emails, gemini_client, contacts, memory_context=user_style
            )
            new_retry: list = []

            for result in retry_classified:
                category = result.get("category", "")
                if category == "__RETRY__":
                    new_retry.append(result.get("email", {}))
                    continue
                if category not in (CATEGORY_URGENT, CATEGORY_NORMAL):
                    continue

                email = result.get("email", {})
                email_id = result.get("email_id", "")
                if not email_id or email_id in pending_approvals:
                    continue

                sender_addr = extract_email_address(email.get("sender", ""))
                contact_info = contacts.get(sender_addr, {})
                sender_info = (
                    f"{contact_info.get('name', '')}（{contact_info.get('relationship', '不明')}）"
                    if contact_info
                    else email.get("sender", "")
                )

                draft = generate_reply_draft(gemini_client, email, user_style, sender_info)
                if draft == "__RETRY__":
                    new_retry.append(email)
                    continue

                pending_approvals[email_id] = {
                    "email": email,
                    "draft": draft,
                    "category": category,
                }

            if new_retry:
                telegram_app.bot_data["retry_queue"] = new_retry
                logger.info(f"retry_queue に {len(new_retry)} 件を残しました（次回再試行）")

        # ステップ1: 未読メールを取得
        emails = get_unread_emails(gmail_service)

        # 最終チェック時刻を更新
        update_state(STATE_PATH, "最終チェック時刻", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        if not emails:
            logger.info("新着メールなし")
            return

        # ステップ2: 連絡先を読み込んで分類を実行（MEMORY.md の内容を参考情報として渡す）
        classified = classify_batch(emails, gemini_client, contacts, memory_context=user_style)

        # ステップ3: 要返信メールの返信案を生成して pending_approvals に格納
        new_drafts = 0
        new_retry_emails: list = []

        for result in classified:
            category = result.get("category", "")

            # API レート制限で分類できなかったメールを retry_queue に追加
            if category == "__RETRY__":
                new_retry_emails.append(result.get("email", {}))
                continue

            if category not in (CATEGORY_URGENT, CATEGORY_NORMAL):
                continue  # 返信不要なメールはスキップ

            email = result.get("email", {})
            email_id = result.get("email_id", "")

            if not email_id or email_id in pending_approvals:
                continue  # ID なし・既に処理済みはスキップ

            # contacts.md から送信者情報を取得（返信案生成に使用）
            sender_addr = extract_email_address(email.get("sender", ""))
            contact_info = contacts.get(sender_addr, {})
            sender_info = (
                f"{contact_info.get('name', '')}（{contact_info.get('relationship', '不明')}）"
                if contact_info
                else email.get("sender", "")
            )

            logger.info(f"返信案生成中: {email.get('subject', '')}")
            draft = generate_reply_draft(gemini_client, email, user_style, sender_info)

            # 返信案生成もレート制限で失敗した場合は retry_queue に追加
            if draft == "__RETRY__":
                new_retry_emails.append(email)
                continue

            # 承認待ちリストに追加（Telegram コールバックで参照）
            pending_approvals[email_id] = {
                "email": email,
                "draft": draft,
                "category": category,
            }
            new_drafts += 1

        # retry_queue に追加して Telegram 通知
        if new_retry_emails:
            existing_retry = telegram_app.bot_data.setdefault("retry_queue", [])
            existing_retry.extend(new_retry_emails)
            logger.warning(f"{len(new_retry_emails)} 件を retry_queue に追加（API レート制限）")
            try:
                await send_notification(
                    bot,
                    chat_id,
                    f"⚠️ Gemini API が一時的に制限中です。後で {len(new_retry_emails)} 件を再試行します。",
                )
            except Exception:
                pass

        # ステップ4: Telegram にサマリーを送信
        await send_email_summary(bot, chat_id, classified)

        logger.info(
            f"処理完了: {len(classified)} 件分類, 返信案 {new_drafts} 件生成, "
            f"承認待ち合計 {len(pending_approvals)} 件"
        )

    except Exception as e:
        logger.error(f"メール処理中にエラー: {e}", exc_info=True)
        try:
            await send_notification(bot, chat_id, f"⚠️ エラーが発生しました：{e}")
        except Exception:
            pass  # 通知自体が失敗してもクラッシュさせない


async def daily_briefing_scheduler(
    gmail_service, calendar_service, gemini_client, telegram_app, config: dict
) -> None:
    """
    毎日 config の daily_summary.send_time（デフォルト 08:00 JST）に
    日次ブリーフィングを送信するスケジューラー。
    1 分ごとに現在時刻をチェックし、設定時刻に一致したら送信する。
    同日内に二重送信しないよう最終送信日を記録する。
    """
    send_time_str = config.get("daily_summary", {}).get("send_time", "08:00")
    send_hour, send_minute = map(int, send_time_str.split(":"))
    chat_id = config["telegram"]["chat_id"]
    last_sent_date = None

    logger.info(f"日次ブリーフィングスケジューラー起動: 毎日 {send_time_str} JST に送信")

    while True:
        await asyncio.sleep(60)  # 1 分ごとにチェック

        now = datetime.now()
        today = now.date()

        if (
            now.hour == send_hour
            and now.minute == send_minute
            and last_sent_date != today
        ):
            logger.info("日次ブリーフィングを送信します...")
            contacts = load_contacts(str(CONTACTS_PATH))
            pending = telegram_app.bot_data.get("pending_approvals", {})

            try:
                discord_client = telegram_app.bot_data.get("discord_client")
                await send_daily_briefing(
                    bot=telegram_app.bot,
                    chat_id=chat_id,
                    gmail_service=gmail_service,
                    calendar_service=calendar_service,
                    gemini_client=gemini_client,
                    contacts=contacts,
                    pending_approvals=pending,
                    discord_client=discord_client,
                )
                last_sent_date = today
            except Exception as e:
                logger.error(f"日次ブリーフィングスケジューラーエラー: {e}")


async def main_loop(config: dict) -> None:
    """
    メインの定期実行ループ。
    config の check_interval_minutes 間隔で check_and_process_emails() を呼び出す。
    Telegram Bot をバックグラウンドで起動し、Ctrl+C で終了する。
    quiet_hours の時間帯はメールチェックをスキップする。
    """
    interval_sec = config["gmail"]["check_interval_minutes"] * 60
    chat_id = config["telegram"]["chat_id"]

    # Gmail 認証（初回はブラウザが起動する）
    # config.yaml の相対パスはプロジェクトルート基準で解決する
    project_root = CONFIG_PATH.parent
    credentials_path = str(project_root / config["gmail"]["credentials_path"])
    token_path = str(project_root / config["gmail"]["token_path"])

    logger.info("Gmail 認証中...")
    gmail_service = authenticate(credentials_path, token_path)

    # Google Calendar サービス初期化（token.json に calendar.readonly スコープが必要）
    # 初回は token.json を削除して再起動 → ブラウザで再認証することで有効化される
    logger.info("Google Calendar サービス初期化中...")
    try:
        calendar_service = build_calendar_service(credentials_path, token_path)
    except Exception as e:
        logger.warning(
            f"Google Calendar 初期化失敗（日次ブリーフィングで予定は非表示）: {e}"
        )
        calendar_service = None

    # Gemini クライアント初期化
    logger.info("Gemini クライアント初期化中...")
    gemini_client = init_gemini(
        api_key=config["gemini"]["api_key"],
        model_name=config["gemini"]["model"],
    )

    # Telegram Bot 初期化
    logger.info("Telegram Bot 初期化中...")
    telegram_app = build_application(config["telegram"]["bot_token"])

    # bot_data に共有リソースを格納（コールバックハンドラーからも参照できる）
    telegram_app.bot_data.update({
        "gmail_service": gmail_service,
        "gemini_client": gemini_client,
        "chat_id": chat_id,
        "pending_approvals": {},
        "awaiting_revision": None,
        "awaiting_discord_reply": None,
        "retry_queue": [],
        # 🔄 再チェックボタン用: コールバックから呼び出せるようにする
        "_recheck_fn": check_and_process_emails,
        "config": config,
        "discord_client": None,
    })

    # Discord クライアント初期化（エラーでも Gmail 機能は継続）
    discord_monitor = None
    discord_cfg = config.get("discord", {})
    if discord_cfg.get("bot_token"):
        try:
            discord_monitor = DiscordMonitor(
                config=discord_cfg,
                telegram_bot=telegram_app.bot,
                chat_id=chat_id,
                gemini_client=gemini_client,
            )
            asyncio.create_task(discord_monitor.start(discord_cfg["bot_token"]))
            asyncio.create_task(discord_monitor.run_summary_scheduler())
            telegram_app.bot_data["discord_client"] = discord_monitor
            logger.info("Discord クライアント起動タスクを開始しました")
        except Exception as e:
            logger.warning(f"Discord 初期化失敗（Gmail機能は継続）: {e}")

    # 起動時に学習済みフラグを確認してから自動学習を実行（毎起動での無駄な実行を防ぐ）
    logger.info("学習済みフラグを確認して自動学習を開始...")
    try:
        if not _is_learning_done("contacts", MEMORY_PATH):
            logger.info("連絡先を学習中...")
            learn_contacts(gmail_service, str(CONTACTS_PATH), memory_path=str(MEMORY_PATH))
        else:
            logger.info("連絡先は学習済みのためスキップします")

        if not _is_learning_done("style", MEMORY_PATH):
            logger.info("返信スタイルを学習中...")
            learn_writing_style(gmail_service, str(MEMORY_PATH), gemini_client=gemini_client)
        else:
            logger.info("返信スタイルは学習済みのためスキップします")

        logger.info("自動学習チェック完了")
    except Exception as e:
        logger.warning(f"自動学習中にエラーが発生しました（処理は継続）: {e}")

    # Telegram Bot をバックグラウンドで起動（ポーリング開始）
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    # 日次ブリーフィングスケジューラーをバックグラウンドタスクとして起動
    asyncio.create_task(
        daily_briefing_scheduler(
            gmail_service, calendar_service, gemini_client, telegram_app, config
        )
    )

    logger.info(
        f"MY-SECRETARY 起動完了。"
        f"{config['gmail']['check_interval_minutes']} 分ごとにメールをチェックします。"
    )

    # 起動通知を Telegram に送信
    try:
        await send_notification(
            telegram_app.bot,
            chat_id,
            (
                f"🤖 <b>MY-SECRETARY 起動しました</b>\n"
                f"{config['gmail']['check_interval_minutes']} 分ごとにメールをチェックします。\n"
                f"/status で承認待ち件数を確認できます。"
            ),
        )
    except Exception as e:
        logger.warning(f"起動通知の送信に失敗: {e}")

    try:
        while True:
            # quiet hours の時間帯はメールチェックをスキップ
            if _is_quiet_hours(config):
                logger.info("静止時間帯のためメールチェックをスキップします")
                await asyncio.sleep(interval_sec)
                continue

            await check_and_process_emails(
                gmail_service, gemini_client, telegram_app, config
            )
            logger.info(
                f"{config['gmail']['check_interval_minutes']} 分後に次のチェックを実行します。"
            )
            await asyncio.sleep(interval_sec)

    except KeyboardInterrupt:
        logger.info("Ctrl+C を検出。終了処理中...")

    except Exception as e:
        logger.error(f"予期しないエラーで停止: {e}", exc_info=True)
        try:
            await send_notification(
                telegram_app.bot, chat_id, f"⚠️ システムエラーで停止しました：{e}"
            )
        except Exception:
            pass

    finally:
        # Discord Bot を安全に停止
        if discord_monitor and not discord_monitor.is_closed():
            logger.info("Discord Bot 停止中...")
            try:
                await discord_monitor.close()
            except Exception as e:
                logger.error(f"Discord Bot 停止エラー: {e}")

        # Telegram Bot を安全に停止
        logger.info("Telegram Bot 停止中...")
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as e:
            logger.error(f"Bot 停止エラー: {e}")

        logger.info("MY-SECRETARY 停止完了")


if __name__ == "__main__":
    # 設定読み込み → 各クライアント初期化 → メインループ起動
    config = load_config(CONFIG_PATH)
    asyncio.run(main_loop(config))
