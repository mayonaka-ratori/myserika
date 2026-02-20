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
from datetime import datetime

import discord
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)


class DiscordMonitor(discord.Client):
    """
    Discord のメッセージを監視して Telegram に転送するクライアント。
    - メンション・DM: 即時通知
    - 監視チャンネル: バッファに蓄積して定期サマリー
    """

    def __init__(self, config: dict, telegram_bot: Bot, chat_id: str, gemini_client):
        intents = discord.Intents.default()
        intents.message_content = True  # Developer Portal で Privileged Intent を有効化すること
        intents.messages = True
        super().__init__(intents=intents)

        self.config = config
        self.telegram_bot = telegram_bot
        self.chat_id = chat_id
        self.gemini_client = gemini_client

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
        """メンション通知を Telegram に送信し pending_discord_messages に格納する。"""
        msg_key = f"msg_{message.id}"
        server_name = message.guild.name if message.guild else "不明"
        channel_name = getattr(message.channel, "name", "不明")
        sender_name = message.author.display_name
        content = message.content

        self.pending_discord_messages[msg_key] = {
            "type": "mention",
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "sender_name": sender_name,
            "content": content,
            "server_name": server_name,
            "channel_name": channel_name,
        }
        self.unread_mention_count += 1

        text = (
            f"🔔 <b>Discord でメンションされました</b>\n\n"
            f"サーバー: {html.escape(server_name)}\n"
            f"チャンネル: #{html.escape(channel_name)}\n"
            f"送信者: {html.escape(sender_name)}\n"
            f"─────────────────\n"
            f"{html.escape(content)}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 返信", callback_data=f"discord_reply:{msg_key}"),
                InlineKeyboardButton("👀 既読のみ", callback_data=f"discord_dismiss:{msg_key}"),
            ]
        ])

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
        """DM 通知を Telegram に送信し pending_discord_messages に格納する。"""
        msg_key = f"msg_{message.id}"
        sender_name = message.author.display_name
        content = message.content

        self.pending_discord_messages[msg_key] = {
            "type": "dm",
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "sender_name": sender_name,
            "content": content,
            "server_name": None,
            "channel_name": None,
        }
        self.unread_dm_count += 1

        text = (
            f"💬 <b>Discord DM が届きました</b>\n\n"
            f"送信者: {html.escape(sender_name)}\n"
            f"─────────────────\n"
            f"{html.escape(content)}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 返信", callback_data=f"discord_reply:{msg_key}"),
                InlineKeyboardButton("👀 既読のみ", callback_data=f"discord_dismiss:{msg_key}"),
            ]
        ])

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
            from gemini_client import _call_model
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None, _call_model, self.gemini_client, prompt
            )
            return summary.strip()
        except Exception as e:
            logger.error(f"Gemini サマリー生成エラー: {e}")
            return f"（要約の生成に失敗しました: {e}）"

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
