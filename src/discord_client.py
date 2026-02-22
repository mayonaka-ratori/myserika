"""
discord_client.py
Discord を監視して Telegram に通知するモジュール。
メンション・DM は即時通知し、監視チャンネルのメッセージは定期サマリーする。
discord.py の Client を継承した DiscordMonitor クラスを提供する。

⚠️ 事前設定:
  Discord Developer Portal の Bot 設定で「Message Content Intent」を有効化すること。
  有効化しないとメッセージ本文が取得できない。
"""

import asyncio
import html
import logging
import re
from datetime import datetime
from pathlib import Path

import discord
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

from gemini_client import _call_model, _parse_json_response, generate_discord_reply

logger = logging.getLogger(__name__)

# MEMORY.md のパス（プロジェクトルート）/ Path to MEMORY.md (project root)
_MEMORY_PATH = Path(__file__).parent.parent / "MEMORY.md"

# Discord スタイルセクションのマーカー / Section markers for Discord style in MEMORY.md
_STYLE_SECTION_HEADER = "## Discord コミュニケーションスタイル / Discord Communication Style"
_STYLE_FLAG_KEY       = "Discordスタイル学習日:"


class DiscordMonitor(discord.Client):
    """
    Discord のメッセージを監視して Telegram に転送するクライアント。
    - メンション・DM: 即時通知
    - 監視チャンネル: バッファに蓄積して定期サマリー
    """

    def __init__(self, config: dict, telegram_bot: Bot, chat_id: str, gemini_client,
                 task_manager=None, db=None):
        intents = discord.Intents.default()
        intents.message_content = True  # Enable Privileged Intent in Developer Portal
        intents.messages = True
        super().__init__(intents=intents)

        self.config = config
        self.telegram_bot = telegram_bot
        self.chat_id = chat_id
        self.gemini_client = gemini_client
        self.task_manager = task_manager
        self.db = db  # Database instance for Discord message persistence

        # guild_id → set[channel_id]（空 set は全チャンネル監視）
        self._monitored_guilds: dict[int, set[int]] = {}

        # channel_id → [{author, content, timestamp}, ...]
        self.message_buffer: dict[int, list[dict]] = {}

        # msg_key → {type, channel_id, user_id, sender_name, content, server_name, channel_name}
        self.pending_discord_messages: dict[str, dict] = {}

        self.unread_mention_count: int = 0
        self.unread_dm_count: int = 0

    async def on_ready(self) -> None:
        """
        Discord に接続完了したとき呼ばれる。
        config の server_name を guild_id に解決して _monitored_guilds を構築する。
        """
        logger.info(f"Discord Bot ログイン完了: {self.user} (ID: {self.user.id})")

        monitored = self.config.get("monitored_channels", [])
        for entry in monitored:
            server_name = entry.get("server_name", "")
            channel_ids = entry.get("channel_ids", [])

            # server_name から guild を検索
            guild = discord.utils.find(
                lambda g, sn=server_name: g.name == sn, self.guilds
            )
            if guild is None:
                logger.warning(
                    f"Discord: サーバー '{server_name}' が見つかりません。"
                    f"参加済みサーバー: {[g.name for g in self.guilds]}"
                )
                continue

            self._monitored_guilds[guild.id] = set(channel_ids)
            logger.info(
                f"Discord: サーバー '{server_name}' (ID:{guild.id}) を監視対象に追加。"
                f"チャンネルID: {channel_ids if channel_ids else '全チャンネル'}"
            )

        # 文体学習を非同期タスクとしてバックグラウンド起動（on_ready をブロックしない）
        # Launch style learning as a background task (does not block on_ready)
        if self.config.get("style_learning", False):
            _task = asyncio.create_task(self.initialize_style_learning())
            _task.add_done_callback(
                lambda t: logger.error(f"Style learning failed: {t.exception()}")
                if not t.cancelled() and t.exception() else None
            )

    async def on_message(self, message: discord.Message) -> None:
        """
        メッセージ受信時のエントリーポイント。
        自分発信は無視し、DM / メンション / 監視チャンネル の 3 分岐で処理する。
        """
        # 自分自身の発言は無視
        if message.author == self.user:
            return

        # DM チャンネル
        if isinstance(message.channel, discord.DMChannel):
            if self.config.get("dm_monitoring", True):
                await self._notify_dm(message)
            return

        # ギルドメッセージ: メンションチェック
        if self.user in message.mentions:
            if self.config.get("mention_instant_notify", True):
                await self._notify_mention(message)

        # 監視チャンネルへの蓄積（メンションの場合も蓄積する）
        if self._is_monitored_channel(message.channel):
            channel_id = message.channel.id
            self.message_buffer.setdefault(channel_id, []).append({
                "author": str(message.author.display_name),
                "content": message.content,
                "timestamp": message.created_at.strftime("%H:%M"),
            })

    # ─────────────────────────────────────────────────────────
    # Discord スタイル学習 / Discord writing style learning
    # ─────────────────────────────────────────────────────────

    def _read_discord_style_from_memory(self) -> str:
        """
        MEMORY.md から Discord スタイルセクションを抽出して返す。
        セクションが存在しない場合は空文字列を返す。
        / Extract the Discord style section from MEMORY.md.
        Returns empty string if the section does not exist.
        """
        if not _MEMORY_PATH.exists():
            return ""
        try:
            content = _MEMORY_PATH.read_text(encoding="utf-8")
            # セクション開始位置を探す / Find section start
            idx = content.find(_STYLE_SECTION_HEADER)
            if idx == -1:
                return ""
            # 次の ## セクションまでを抽出（またはファイル末尾）
            # Extract until the next ## section (or end of file)
            rest  = content[idx + len(_STYLE_SECTION_HEADER):]
            match = re.search(r"\n## ", rest)
            section = rest[:match.start()] if match else rest
            return section.strip()
        except Exception as e:
            logger.warning(f"MEMORY.md の Discord スタイル読み込みエラー / Failed to read Discord style: {e}")
            return ""

    def _write_discord_style_to_memory(self, channel_label: str, style_markdown: str) -> None:
        """
        Discord スタイルセクション内の指定チャンネルブロックを更新する。
        ブロックが存在しない場合はセクション末尾に追記する。
        / Update (or append) a channel block inside the Discord style section of MEMORY.md.

        channel_label: 例 "#general（新作打ち合わせ用）" or "DM: username"
        style_markdown: Gemini が生成したチャンネルのスタイル Markdown テキスト
        """
        if not _MEMORY_PATH.exists():
            logger.warning("MEMORY.md が見つかりません / MEMORY.md not found")
            return
        try:
            content = _MEMORY_PATH.read_text(encoding="utf-8")

            # ── Discord スタイルセクションを更新 / Update Discord style section ──
            sec_idx = content.find(_STYLE_SECTION_HEADER)
            if sec_idx == -1:
                # セクションがなければ末尾に追加 / Append section if not found
                content += f"\n\n{_STYLE_SECTION_HEADER}\n\n{style_markdown}\n"
            else:
                # セクション末尾を見つけてチャンネルブロックを置換 or 追記
                # Find section end and replace/append the channel block
                rest  = content[sec_idx + len(_STYLE_SECTION_HEADER):]
                nxt   = re.search(r"\n## ", rest)
                sec_body     = rest[:nxt.start()] if nxt else rest
                after_sec    = rest[nxt.start():] if nxt else ""

                # チャンネルブロック（### channel_label から次の ### まで）を置換
                # Replace existing channel block (### … → next ###)
                block_pattern = re.compile(
                    r"(### " + re.escape(channel_label) + r".*?)(?=\n### |\Z)",
                    re.DOTALL,
                )
                new_block = f"### {channel_label}\n{style_markdown}"
                if block_pattern.search(sec_body):
                    new_sec_body = block_pattern.sub(new_block, sec_body, count=1)
                else:
                    # ブロックが存在しない場合はセクション末尾に追記 / Append if block not found
                    new_sec_body = sec_body.rstrip() + f"\n\n### {channel_label}\n{style_markdown}\n"

                content = (
                    content[:sec_idx + len(_STYLE_SECTION_HEADER)]
                    + new_sec_body
                    + after_sec
                )

            # ── 自動学習フラグを更新 / Update style-learned flag ──
            today_str = datetime.now().strftime("%Y-%m-%d")
            flag_pattern = re.compile(rf"^({re.escape(_STYLE_FLAG_KEY)}\s*).*$", re.MULTILINE)
            if flag_pattern.search(content):
                content = flag_pattern.sub(rf"\g<1>{today_str}", content)
            else:
                # フラグ行がなければ自動学習フラグセクションに追記 / Append flag if not found
                content = content.replace(
                    "## 自動学習フラグ",
                    f"## 自動学習フラグ\n\n{_STYLE_FLAG_KEY} {today_str}",
                    1,
                )

            _MEMORY_PATH.write_text(content, encoding="utf-8")
            logger.info(f"MEMORY.md 更新: Discord スタイル ({channel_label}) / Updated Discord style for {channel_label}")

        except Exception as e:
            logger.error(f"MEMORY.md 書き込みエラー / Failed to write to MEMORY.md: {e}")

    async def learn_discord_style(
        self,
        channel_or_dm: discord.TextChannel | discord.DMChannel,
        max_messages: int = 100,
    ) -> None:
        """
        指定チャンネルまたは DM の履歴からオーナーの文体を学習して MEMORY.md に保存する。
        / Learn the owner's writing style from channel or DM history and save to MEMORY.md.

        - TextChannel の場合は Read Message History 権限を確認する。
          権限がない場合は warning を出してスキップする（クラッシュしない）。
          / For TextChannel, verify Read Message History permission.
          If not granted, log a warning and skip gracefully (no crash).
        - owner_user_id が未設定の場合はスキップする。
          / If owner_user_id is not configured, skip gracefully.
        """
        try:
            # ── 権限チェック (TextChannel のみ) / Permission check (TextChannel only) ──
            if isinstance(channel_or_dm, discord.TextChannel):
                perms = channel_or_dm.permissions_for(channel_or_dm.guild.me)
                if not perms.read_message_history:
                    logger.warning(
                        f"Discord スタイル学習: #{channel_or_dm.name} に "
                        f"Read Message History 権限がないためスキップ。"
                        f"/ Skipping #{channel_or_dm.name}: no Read Message History permission."
                    )
                    return

            # ── オーナー ID の確認 / Verify owner user ID ──
            owner_user_id = str(self.config.get("owner_user_id", "")).strip()
            if not owner_user_id:
                logger.warning(
                    "Discord スタイル学習: owner_user_id が設定されていないためスキップ。"
                    " config.yaml の discord.owner_user_id を設定してください。"
                    " / Skipping style learning: owner_user_id not configured."
                    " Please set discord.owner_user_id in config.yaml."
                )
                return

            # ── 履歴取得 / Fetch message history ──
            messages: list[discord.Message] = [
                msg async for msg in channel_or_dm.history(limit=max_messages)
            ]

            # オーナーのメッセージのみ抽出 / Filter for owner's messages only
            owner_msgs = [m for m in messages if str(m.author.id) == owner_user_id]

            if len(owner_msgs) < 3:
                logger.info(
                    f"Discord スタイル学習: サンプルが少ない（{len(owner_msgs)}件）ためスキップ。"
                    f" / Skipping: insufficient samples ({len(owner_msgs)} messages)."
                )
                return

            # ── Gemini プロンプト構築 / Build Gemini prompt ──
            sample_text = "\n".join(
                f"[{m.created_at.strftime('%H:%M')}] {m.content}"
                for m in owner_msgs[:50]  # 最大50件を分析対象に / Analyze up to 50 messages
            )

            if isinstance(channel_or_dm, discord.TextChannel):
                channel_label = f"#{channel_or_dm.name}（{channel_or_dm.guild.name}）"
            else:
                dm_user = getattr(channel_or_dm, "recipient", None)
                channel_label = f"DM: {dm_user.name if dm_user else 'unknown'}"

            prompt = (
                f"以下は Discord チャンネルでのユーザー自身の発言履歴です。\n"
                f"/ The following are the user's own messages in a Discord channel.\n\n"
                f"チャンネル / Channel: {channel_label}\n"
                f"対象件数 / Sample count: {len(owner_msgs)}件\n\n"
                f"【発言履歴 / Message history】\n{sample_text}\n\n"
                f"この発言パターンを分析して、このユーザーの Discord 文体プロファイルを生成してください。\n"
                f"/ Analyze these messages and generate a Discord writing style profile for this user.\n\n"
                f"必ず以下の JSON 形式のみで回答してください（説明文不要）:\n"
                f"/ Respond ONLY in the following JSON format (no explanation):\n"
                f'{{\n'
                f'  "tone": "casual/formal/mixed のいずれか / one of casual/formal/mixed",\n'
                f'  "avg_length": "平均文字数の概算（例: 約30字）/ approx avg length (e.g. \'約30字\')",\n'
                f'  "common_expressions": ["よく使う表現や絵文字のリスト / common expressions or emoji"],\n'
                f'  "reply_speed": "返信速度の傾向（例: 速い・普通・遅い）/ reply speed tendency",\n'
                f'  "notes": "その他の特徴（任意）/ other notes (optional)"\n'
                f'}}'
            )

            # ── Gemini で分析 / Analyze with Gemini ──
            loop   = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _call_model, self.gemini_client, prompt)
            parsed = _parse_json_response(result)

            # ── Markdown 形式に変換して MEMORY.md に書き込む / Convert to Markdown and write ──
            common_expr = "、".join(parsed.get("common_expressions", [])) or "（なし）"
            style_md = (
                f"- トーン / Tone: {parsed.get('tone', '-')}\n"
                f"- 平均メッセージ長 / Avg length: {parsed.get('avg_length', '-')}\n"
                f"- よく使う表現・絵文字 / Common expressions & emoji: {common_expr}\n"
                f"- 返信速度の傾向 / Reply speed: {parsed.get('reply_speed', '-')}\n"
                f"- 備考 / Notes: {parsed.get('notes', 'なし')}\n"
            )
            self._write_discord_style_to_memory(channel_label, style_md)
            logger.info(
                f"Discord スタイル学習完了: {channel_label} ({len(owner_msgs)}件分析)"
                f" / Style learning complete for {channel_label}"
            )

        except Exception as e:
            logger.error(
                f"Discord スタイル学習エラー（スキップ）: {channel_or_dm} / {e}"
                f" / Style learning error (skipping): {e}"
            )

    async def initialize_style_learning(self) -> None:
        """
        起動時に一度だけ文体学習を実行する。
        今日すでに学習済みの場合はスキップする（冪等性）。
        最大5チャンネル/DMを処理し、各チャンネルは20件まで（Gemini レート制限対策）。
        / Run style learning once on startup.
        Skip if already completed today (idempotent).
        Process up to 5 channels/DMs, 20 messages each (Gemini rate limit consideration).
        """
        # style_learning フラグを確認 / Check style_learning flag
        if not self.config.get("style_learning", False):
            logger.info("Discord スタイル学習: 無効化されています / Style learning is disabled")
            return

        # 今日すでに学習済みか確認 / Check if already learned today
        today_str = datetime.now().strftime("%Y-%m-%d")
        if _MEMORY_PATH.exists():
            try:
                content = _MEMORY_PATH.read_text(encoding="utf-8")
                flag_match = re.search(
                    rf"^{re.escape(_STYLE_FLAG_KEY)}\s*(.+)$", content, re.MULTILINE
                )
                if flag_match and flag_match.group(1).strip() == today_str:
                    logger.info(
                        f"Discord スタイル学習: 本日（{today_str}）は学習済みのためスキップ。"
                        f" / Already learned today ({today_str}), skipping."
                    )
                    return
            except Exception as e:
                logger.warning(f"学習済みフラグの確認エラー（継続）: {e} / Flag check error, continuing.")

        logger.info("Discord スタイル学習を開始します / Starting Discord style learning")

        # ── 対象チャンネルリストを構築（最大5件）/ Build target channel list (up to 5) ──
        targets: list[discord.TextChannel | discord.DMChannel] = []

        # 監視対象の TextChannel を追加（最大3件）/ Add monitored TextChannels (up to 3)
        for guild_id, channel_ids in self._monitored_guilds.items():
            guild = self.get_guild(guild_id)
            if guild is None:
                continue
            channels_to_check = (
                [guild.get_channel(cid) for cid in channel_ids if guild.get_channel(cid)]
                if channel_ids
                else [ch for ch in guild.text_channels if isinstance(ch, discord.TextChannel)]
            )
            for ch in channels_to_check:
                if ch and len(targets) < 3:
                    targets.append(ch)

        # DM チャンネルを追加（最大2件）/ Add DM channels (up to 2)
        for dm in self.private_channels:
            if isinstance(dm, discord.DMChannel) and len(targets) < 5:
                targets.append(dm)

        if not targets:
            logger.info(
                "Discord スタイル学習: 対象チャンネルが見つかりません / No target channels found."
            )
            return

        # ── 各チャンネルを処理 / Process each channel ──
        for ch in targets:
            label = (
                f"#{getattr(ch, 'name', '?')}（{getattr(getattr(ch, 'guild', None), 'name', 'DM')}）"
                if isinstance(ch, discord.TextChannel)
                else f"DM: {getattr(getattr(ch, 'recipient', None), 'name', 'unknown')}"
            )
            logger.info(f"スタイル学習中: {label} / Learning style for: {label}")
            await self.learn_discord_style(ch, max_messages=20)
            # Gemini レート制限対策: チャンネル間で待機 / Rate limit guard between channels
            await asyncio.sleep(5)

        logger.info(
            f"Discord スタイル学習完了: {len(targets)}チャンネル処理。"
            f" / Style learning complete: processed {len(targets)} channel(s)."
        )

    def _is_monitored_channel(self, channel) -> bool:
        """
        _monitored_guilds に基づいてチャンネルが監視対象か判定する。
        channel_ids が空セットのとき、そのサーバーの全チャンネルを対象とする。
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False

        if guild.id not in self._monitored_guilds:
            return False

        allowed_ids = self._monitored_guilds[guild.id]
        return len(allowed_ids) == 0 or channel.id in allowed_ids

    async def _notify_mention(self, message: discord.Message) -> None:
        """
        メンション通知を Telegram に送信し pending_discord_messages に格納する。
        reply_generation が有効な場合は Gemini で返信案を生成してメッセージに含める。
        / Send mention notification to Telegram and store in pending_discord_messages.
        If reply_generation is enabled, generate a reply draft via Gemini and include it.
        """
        msg_key = f"msg_{message.id}"
        server_name  = message.guild.name if message.guild else "不明"
        channel_name = getattr(message.channel, "name", "不明")
        sender_name  = message.author.display_name
        content      = message.content

        self.pending_discord_messages[msg_key] = {
            "type": "mention",
            "message_id": message.id,           # Needed for send_reply()
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "sender_name": sender_name,
            "content": content,
            "server_name": server_name,
            "channel_name": channel_name,
            "draft": "",          # Reply draft (filled below)
            "confidence": 0.0,    # Style match confidence
            "discord_db_id": None,  # Set after DB save
        }
        self.unread_mention_count += 1

        # Persist to DB for unreplied tracking
        if self.db is not None:
            try:
                guild_id = str(message.guild.id) if message.guild else ""
                db_id = await self.db.save_discord_message(
                    message_id=str(message.id),
                    channel_id=str(message.channel.id),
                    guild_id=guild_id,
                    sender_id=str(message.author.id),
                    sender_name=sender_name,
                    content=content,
                    is_mention=True,
                    is_dm=False,
                )
                self.pending_discord_messages[msg_key]["discord_db_id"] = db_id
            except Exception as e:
                logger.warning(f"Failed to save Discord mention to DB: {e}")

        # タスク自動抽出 / Auto-extract tasks from mention
        if self.task_manager is not None:
            try:
                extracted = await self.task_manager.extract_tasks_from_discord(
                    sender=sender_name, content=content
                )
                for task in extracted:
                    due_icon = f" / 期限推定: {task['due_date'][:10]}" if task.get('due_date') else ""
                    source_label = f"（Discord メンション: {sender_name} より{due_icon}）"
                    priority_icon = {
                        "urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"
                    }.get(task.get("priority", "medium"), "🟡")
                    task_text = (
                        f"📌 <b>新しいタスクを検出</b>\n"
                        f"{priority_icon} {html.escape(task['title'])}\n"
                        f"{html.escape(source_label)}"
                    )
                    task_keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ 追加する", callback_data=f"task_confirm:{task['id']}"),
                        InlineKeyboardButton("❌ 無視する", callback_data=f"task_ignore:{task['id']}"),
                    ]])
                    await self.telegram_bot.send_message(
                        chat_id=self.chat_id, text=task_text,
                        parse_mode="HTML", reply_markup=task_keyboard
                    )
            except Exception as e:
                logger.warning(f"Discord タスク抽出エラー（スキップ）/ Discord task extraction error: {e}")

        # ── 返信案生成（reply_generation が有効な場合）/ Generate reply draft if enabled ──
        draft_text  = ""
        confidence  = 0.0
        if self.config.get("reply_generation", False):
            try:
                # 会話コンテキスト取得（最新11件を取得し、受信メッセージ自身を除外）
                # Fetch last 11 messages and exclude the triggering message itself
                history_raw: list[discord.Message] = [
                    m async for m in message.channel.history(limit=11)
                ]
                context_msgs = [m for m in history_raw if m.id != message.id][:10]
                conv_history = [
                    {
                        "author": m.author.display_name,
                        "content": m.content,
                        "timestamp": m.created_at.strftime("%H:%M"),
                    }
                    for m in reversed(context_msgs)  # 古い順に並べる / Oldest first
                ]

                discord_style = self._read_discord_style_from_memory()

                # Gemini は同期関数のため run_in_executor で非同期化 / Run sync Gemini call in executor
                loop   = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    generate_discord_reply,
                    self.gemini_client,
                    sender_name,
                    content,
                    f"#{channel_name}",
                    conv_history,
                    discord_style,
                )
                draft_text = result.get("reply_text", "")
                confidence = result.get("confidence", 0.0)

                # __RETRY__ は API 制限を示す / __RETRY__ indicates rate limit
                if draft_text == "__RETRY__":
                    logger.warning("Discord 返信案: API 制限のため生成スキップ / Reply draft skipped due to rate limit")
                    draft_text = ""

                # pending に返信案を格納（DR-2 が送信ハンドラで利用）/ Store for DR-2 send handler
                if draft_text:
                    self.pending_discord_messages[msg_key]["draft"]      = draft_text
                    self.pending_discord_messages[msg_key]["confidence"] = confidence

            except Exception as e:
                logger.warning(
                    f"Discord 返信案生成エラー（スキップ・既存フローで継続）: {e}"
                    f" / Reply draft error (skipping, falling back to default flow): {e}"
                )
                draft_text = ""

        # ── Telegram 通知メッセージと承認ボタンを構築 / Build Telegram notification ──
        if draft_text:
            # 返信案あり: 拡張フォーマット / Draft available: enhanced format
            confidence_pct = int(confidence * 100)
            text = (
                f"💬 <b>Discord 返信案</b>\n\n"
                f"──────────────────\n"
                f"サーバー: {html.escape(server_name)} / チャンネル: #{html.escape(channel_name)}\n"
                f"送信者: {html.escape(sender_name)}\n"
                f"──────────────────\n"
                f"{html.escape(content)}\n"
                f"──────────────────\n"
                f"返信案（信頼度: {confidence_pct}%）:\n"
                f"{html.escape(draft_text)}\n"
                f"──────────────────"
            )
            # DR-2 が discord_draft_send / discord_draft_edit のハンドラを実装する
            # DR-2 will implement handlers for discord_draft_send / discord_draft_edit
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 送信", callback_data=f"discord_draft_send:{msg_key}"),
                InlineKeyboardButton("📝 編集", callback_data=f"discord_draft_edit:{msg_key}"),
                InlineKeyboardButton("❌ 無視", callback_data=f"discord_dismiss:{msg_key}"),
            ]])
        else:
            # 返信案なし: 既存フォーマット / No draft: existing format
            text = (
                f"🔔 <b>Discord でメンションされました</b>\n\n"
                f"サーバー: {html.escape(server_name)}\n"
                f"チャンネル: #{html.escape(channel_name)}\n"
                f"送信者: {html.escape(sender_name)}\n"
                f"─────────────────\n"
                f"{html.escape(content)}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 返信", callback_data=f"discord_reply:{msg_key}"),
                InlineKeyboardButton("👀 既読のみ", callback_data=f"discord_dismiss:{msg_key}"),
            ]])

        try:
            await self.telegram_bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(f"Discord メンション通知を Telegram に送信: {msg_key}")
        except Exception as e:
            logger.error(f"Discord メンション通知送信エラー: {e}")

    async def _notify_dm(self, message: discord.Message) -> None:
        """
        DM 通知を Telegram に送信し pending_discord_messages に格納する。
        reply_generation が有効な場合は Gemini で返信案を生成してメッセージに含める。
        / Send DM notification to Telegram and store in pending_discord_messages.
        If reply_generation is enabled, generate a reply draft via Gemini and include it.
        """
        msg_key     = f"msg_{message.id}"
        sender_name = message.author.display_name
        content     = message.content

        self.pending_discord_messages[msg_key] = {
            "type": "dm",
            "message_id": message.id,           # Needed for send_reply()
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "sender_name": sender_name,
            "content": content,
            "server_name": None,
            "channel_name": None,
            "draft": "",          # Reply draft (filled below)
            "confidence": 0.0,    # Style match confidence
            "discord_db_id": None,  # Set after DB save
        }
        self.unread_dm_count += 1

        # Persist to DB for unreplied tracking
        if self.db is not None:
            try:
                db_id = await self.db.save_discord_message(
                    message_id=str(message.id),
                    channel_id=str(message.channel.id),
                    guild_id="",
                    sender_id=str(message.author.id),
                    sender_name=sender_name,
                    content=content,
                    is_mention=False,
                    is_dm=True,
                )
                self.pending_discord_messages[msg_key]["discord_db_id"] = db_id
            except Exception as e:
                logger.warning(f"Failed to save Discord DM to DB: {e}")

        # タスク自動抽出 / Auto-extract tasks from DM
        if self.task_manager is not None:
            try:
                extracted = await self.task_manager.extract_tasks_from_discord(
                    sender=sender_name, content=content
                )
                for task in extracted:
                    due_icon = f" / 期限推定: {task['due_date'][:10]}" if task.get('due_date') else ""
                    source_label = f"（Discord DM: {sender_name} より{due_icon}）"
                    priority_icon = {
                        "urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"
                    }.get(task.get("priority", "medium"), "🟡")
                    task_text = (
                        f"📌 <b>新しいタスクを検出</b>\n"
                        f"{priority_icon} {html.escape(task['title'])}\n"
                        f"{html.escape(source_label)}"
                    )
                    task_keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ 追加する", callback_data=f"task_confirm:{task['id']}"),
                        InlineKeyboardButton("❌ 無視する", callback_data=f"task_ignore:{task['id']}"),
                    ]])
                    await self.telegram_bot.send_message(
                        chat_id=self.chat_id, text=task_text,
                        parse_mode="HTML", reply_markup=task_keyboard
                    )
            except Exception as e:
                logger.warning(f"Discord タスク抽出エラー（スキップ）/ Discord task extraction error: {e}")

        # ── 返信案生成（reply_generation が有効な場合）/ Generate reply draft if enabled ──
        draft_text = ""
        confidence = 0.0
        if self.config.get("reply_generation", False):
            try:
                # 会話コンテキスト取得（DM チャンネルの最新11件）
                # Fetch last 11 DM messages, exclude the triggering message
                history_raw: list[discord.Message] = [
                    m async for m in message.channel.history(limit=11)
                ]
                context_msgs = [m for m in history_raw if m.id != message.id][:10]
                conv_history = [
                    {
                        "author": m.author.display_name,
                        "content": m.content,
                        "timestamp": m.created_at.strftime("%H:%M"),
                    }
                    for m in reversed(context_msgs)  # 古い順 / Oldest first
                ]

                discord_style = self._read_discord_style_from_memory()

                # Gemini は同期関数のため run_in_executor で非同期化 / Run sync Gemini call in executor
                loop   = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    generate_discord_reply,
                    self.gemini_client,
                    sender_name,
                    content,
                    "DM",
                    conv_history,
                    discord_style,
                )
                draft_text = result.get("reply_text", "")
                confidence = result.get("confidence", 0.0)

                if draft_text == "__RETRY__":
                    logger.warning("Discord DM 返信案: API 制限のためスキップ / DM reply draft skipped (rate limit)")
                    draft_text = ""

                if draft_text:
                    self.pending_discord_messages[msg_key]["draft"]      = draft_text
                    self.pending_discord_messages[msg_key]["confidence"] = confidence

            except Exception as e:
                logger.warning(
                    f"Discord DM 返信案生成エラー（スキップ）: {e}"
                    f" / DM reply draft error (skipping): {e}"
                )
                draft_text = ""

        # ── Telegram 通知メッセージと承認ボタンを構築 / Build Telegram notification ──
        if draft_text:
            # 返信案あり: 拡張フォーマット / Draft available: enhanced format
            confidence_pct = int(confidence * 100)
            text = (
                f"💬 <b>Discord 返信案（DM）</b>\n\n"
                f"──────────────────\n"
                f"送信者: {html.escape(sender_name)}\n"
                f"──────────────────\n"
                f"{html.escape(content)}\n"
                f"──────────────────\n"
                f"返信案（信頼度: {confidence_pct}%）:\n"
                f"{html.escape(draft_text)}\n"
                f"──────────────────"
            )
            # DR-2 が discord_draft_send / discord_draft_edit のハンドラを実装する
            # DR-2 will implement handlers for discord_draft_send / discord_draft_edit
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 送信", callback_data=f"discord_draft_send:{msg_key}"),
                InlineKeyboardButton("📝 編集", callback_data=f"discord_draft_edit:{msg_key}"),
                InlineKeyboardButton("❌ 無視", callback_data=f"discord_dismiss:{msg_key}"),
            ]])
        else:
            # 返信案なし: 既存フォーマット / No draft: existing format
            text = (
                f"💬 <b>Discord DM が届きました</b>\n\n"
                f"送信者: {html.escape(sender_name)}\n"
                f"─────────────────\n"
                f"{html.escape(content)}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 返信", callback_data=f"discord_reply:{msg_key}"),
                InlineKeyboardButton("👀 既読のみ", callback_data=f"discord_dismiss:{msg_key}"),
            ]])

        try:
            await self.telegram_bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(f"Discord DM 通知を Telegram に送信: {msg_key}")
        except Exception as e:
            logger.error(f"Discord DM 通知送信エラー: {e}")

    async def run_summary_scheduler(self) -> None:
        """
        summary_interval_minutes ごとに message_buffer を Gemini で要約して
        Telegram に送信し、バッファをクリアする。
        """
        interval_min = self.config.get("summary_interval_minutes", 360)
        logger.info(f"Discord サマリースケジューラー起動: {interval_min}分ごとに実行")

        while True:
            await asyncio.sleep(interval_min * 60)

            if not self.message_buffer:
                logger.debug("Discord: サマリー対象メッセージなし")
                continue

            buffer_snapshot = dict(self.message_buffer)
            self.message_buffer.clear()

            for channel_id, messages in buffer_snapshot.items():
                if not messages:
                    continue

                channel = self.get_channel(channel_id)
                channel_name = getattr(channel, "name", str(channel_id))
                guild = getattr(channel, "guild", None)
                server_name = guild.name if guild else "不明"
                count = len(messages)

                messages_text = "\n".join(
                    f"[{m['timestamp']}] {m['author']}: {m['content']}"
                    for m in messages
                )

                summary = await self._summarize_with_gemini(
                    channel_name=channel_name,
                    server_name=server_name,
                    count=count,
                    messages_text=messages_text,
                )

                text = (
                    f"📋 <b>Discord チャンネルサマリー</b>\n"
                    f"#{html.escape(channel_name)}（{html.escape(server_name)}）"
                    f"  {count}件\n\n"
                    f"{html.escape(summary)}"
                )

                try:
                    await self.telegram_bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    logger.info(
                        f"Discord サマリー送信: #{channel_name} {count}件"
                    )
                except Exception as e:
                    logger.error(f"Discord サマリー送信エラー: {e}")

    async def _summarize_with_gemini(
        self,
        channel_name: str,
        server_name: str,
        count: int,
        messages_text: str,
    ) -> str:
        """Gemini でチャンネルメッセージを要約する内部ヘルパー。"""
        prompt = (
            f"以下の Discord チャンネルのメッセージを要約してください。\n"
            f"チャンネル: #{channel_name}（{server_name}）\n"
            f"対象件数: {count}件\n\n"
            f"{messages_text}\n\n"
            f"200字以内で重要なポイントをまとめてください。"
        )

        try:
            loop = asyncio.get_running_loop()
            summary = await loop.run_in_executor(
                None, _call_model, self.gemini_client, prompt
            )
            return summary.strip()
        except Exception as e:
            logger.error(f"Gemini サマリー生成エラー: {e}")
            return f"（要約の生成に失敗しました: {e}）"

    async def send_reply(self, channel_id: int, message_id: int, content: str) -> bool:
        """Post a reply to a specific Discord message (mention context).
        Falls back to channel.send() if fetch_message() fails (e.g. message deleted).
        Returns True on success, False on failure.
        """
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                logger.error(f"Discord: channel {channel_id} not found")
                return False
            try:
                original = await channel.fetch_message(message_id)
                await original.reply(content)
            except discord.NotFound:
                # Original message was deleted; fall back to plain channel send
                logger.warning(
                    f"Discord: original message {message_id} not found, "
                    "falling back to channel.send()"
                )
                await channel.send(content)
            except discord.Forbidden:
                logger.error(f"Discord: no permission to reply in channel {channel_id}")
                return False
            logger.info(f"Discord: sent reply in channel {channel_id} (msg {message_id})")
            return True
        except Exception as e:
            logger.error(f"Discord send_reply error: {e}")
            return False

    async def track_unreplied_mentions(self) -> list[dict]:
        """Query DB for mentions/DMs older than reply_reminder_hours with replied=0
        and no previous reminder. Returns list of discord_messages rows."""
        if self.db is None:
            return []
        hours = self.config.get("reply_reminder_hours", 2)
        try:
            return await self.db.get_unreplied_messages(older_than_hours=hours)
        except Exception as e:
            logger.error(f"Discord: track_unreplied_mentions error: {e}")
            return []

    async def run_unreplied_reminder_loop(self) -> None:
        """Periodically check for unreplied Discord messages and send Telegram reminders.
        Runs every reply_reminder_hours, independent of the summary scheduler.
        """
        hours = self.config.get("reply_reminder_hours", 2)
        interval_sec = hours * 3600

        # Wait one interval before first check to avoid firing immediately on startup
        await asyncio.sleep(interval_sec)

        while True:
            try:
                unreplied = await self.track_unreplied_mentions()
                for row in unreplied:
                    await self._send_unreplied_reminder(row)
            except Exception as e:
                logger.error(f"Discord unreplied reminder loop error: {e}")
            await asyncio.sleep(interval_sec)

    async def _send_unreplied_reminder(self, row: dict) -> None:
        """Send a Telegram reminder for a single unreplied Discord message.
        Updates reminder_sent_at in DB after sending to prevent duplicate reminders.
        """
        db_id       = row["id"]
        sender_name = row.get("sender_name", "Someone")
        content     = row.get("content", "")
        created_at  = row.get("created_at", "")
        is_dm       = bool(row.get("is_dm", 0))

        # Calculate elapsed hours
        try:
            from datetime import timezone
            created_dt = datetime.fromisoformat(created_at)
            elapsed_sec = (datetime.now() - created_dt).total_seconds()
            elapsed_hours = int(elapsed_sec // 3600)
            hours_label = f"{elapsed_hours} hour{'s' if elapsed_hours != 1 else ''}"
        except Exception:
            hours_label = "a while"

        # Truncate long content for preview
        preview = content[:120] + "..." if len(content) > 120 else content
        context_label = "DMed you" if is_dm else "mentioned you"

        text = (
            f"⏰ <b>Discord Unreplied Reminder</b>\n\n"
            f"{html.escape(sender_name)} {context_label} {hours_label} ago:\n"
            f"<i>'{html.escape(preview)}'</i>"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💬 Generate Reply Now",
                callback_data=f"discord_unreplied_generate:{db_id}",
            ),
            InlineKeyboardButton(
                "👀 Mark as Read",
                callback_data=f"discord_mark_read:{db_id}",
            ),
        ]])

        try:
            await self.telegram_bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            # Prevent duplicate reminders for this message
            await self.db.update_discord_reminder_sent(db_id)
            logger.info(f"Discord unreplied reminder sent for db_id={db_id}")
        except Exception as e:
            logger.error(f"Discord unreplied reminder send error (db_id={db_id}): {e}")

    async def send_to_channel(self, channel_id: int, content: str) -> bool:
        """
        指定チャンネルにメッセージを送信する。
        Telegram の「💬 返信」ボタンから呼ばれる。
        成功時 True、失敗時 False を返す。
        """
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                logger.error(f"Discord: チャンネル {channel_id} が見つかりません")
                return False
            await channel.send(content)
            logger.info(f"Discord チャンネル {channel_id} に返信送信")
            return True
        except Exception as e:
            logger.error(f"Discord チャンネル送信エラー: {e}")
            return False

    async def send_dm(self, user_id: int, content: str) -> bool:
        """
        指定ユーザーに DM を送信する。
        Telegram の「💬 返信」ボタンから呼ばれる。
        成功時 True、失敗時 False を返す。
        """
        try:
            user = await self.fetch_user(user_id)
            await user.send(content)
            logger.info(f"Discord DM 送信: user_id={user_id}")
            return True
        except Exception as e:
            logger.error(f"Discord DM 送信エラー: {e}")
            return False


def get_discord_stats(discord_client) -> dict:
    """
    日次サマリー用: 未読メンション数・DM数を返す。
    discord_client が None の場合はゼロを返す。
    """
    if discord_client is None:
        return {"mention_count": 0, "dm_count": 0}
    return {
        "mention_count": discord_client.unread_mention_count,
        "dm_count": discord_client.unread_dm_count,
    }
