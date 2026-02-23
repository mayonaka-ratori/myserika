# User Manual / ユーザーマニュアル

## my-secretary — AI Personal Secretary Bot

---

## 1. Getting Started / はじめに

### What this bot does / このBotでできること

my-secretary is your personal AI secretary. Once running, it automatically:

my-secretaryはあなた専用のAI秘書です。起動すると自動的に以下を行います：

- Monitors your Gmail and Discord every 5 minutes / 5分ごとにGmailとDiscordを監視
- Classifies messages by urgency / メッセージを重要度で分類
- Generates reply drafts using Gemini AI / Gemini AIで返信案を自動生成
- Sends notifications to your Telegram / Telegramに通知を送信
- Manages your tasks and deadlines / タスクと締切を管理
- Processes receipt photos for expense tracking / レシート写真から経費を記録

### First-time Setup / 初回セットアップ

1. **Install** / インストール
   ```bash
   git clone https://github.com/mayonaka-ratori/myserika.git
   cd my-secretary
   pip install -r requirements.txt
   cp config.yaml.example config.yaml
Configure / 設定

Edit config.yaml with your API keys / API キーを設定
Required: Gmail API credentials, Gemini API key, Telegram bot token
Optional: Discord bot token, Google Calendar credentials
Launch / 起動

Copycd src
python main.py
On first launch, a browser opens for Gmail OAuth. After authenticating, the bot starts automatically.

初回起動時にブラウザが開きGmail認証を求められます。認証後、Botが自動的に起動します。

2. Telegram Commands / Telegramコマンド
Email / メール
Command    What it does
/check    Immediately check for new emails / メールを即時チェック
/pending    Show emails waiting for your approval / 承認待ちメール一覧
/search <keyword>    Search emails from the last 30 days / 過去30日のメール検索
Schedule / スケジュール
Command    What it does
/schedule    Show today's calendar events / 今日の予定
/schedule tomorrow    Show tomorrow's events / 明日の予定
Tasks / タスク
Command    What it does
/todo <task> [date]    Add a new task. Date is optional. / タスク追加（期限は任意）
/tasks    Show all active tasks with action buttons / タスク一覧
/tasks today    Show only today's tasks / 今日のタスクのみ
/tasks overdue    Show overdue tasks / 期限超過タスク
/done <number>    Mark a task as complete / タスクを完了にする
Date formats accepted / 使える日付表現:

tomorrow, 明日
next Monday, 来週月曜
3/15, 2026-03-15
Examples / 使用例:

/todo Submit tax documents 3/15
/todo デザイン案を送る 明日
/todo Buy office supplies
Expense / 経費
Command    What it does
/expense    Open expense management menu / 経費管理メニューを開く
From the menu, you can:

📸 Scan Receipt — Send a receipt photo to extract details via OCR
📊 Monthly Summary — View this month's expense breakdown
📥 Import MF CSV — Upload MoneyForward CSV for matching
🔍 Review Unmatched — Match expenses with bank transactions
📋 Annual Report — View yearly totals + download CSV
System / システム
Command    What it does
/status    Show bot uptime, email count, API usage / システム状態
/stats    Daily statistics report / 日次統計
/stats weekly    Weekly statistics report / 週次統計
/contacts    Show important contacts / 重要連絡先
/quiet [hours]    Pause notifications (default: 1 hour) / 通知一時停止
/resume    Resume notifications / 通知再開
/help    Show all commands / コマンド一覧
3. Email Approval Flow / メール承認フロー
When a new email arrives that needs a reply, you'll receive a Telegram notification with the draft and these buttons:

返信が必要なメールが届くと、返信案とボタン付きの通知がTelegramに届きます：

✅ Approve & Send — Sends the draft as-is and marks the email as read / そのまま送信して既読にする
✏️ Revise — Type your revision instructions, Gemini regenerates the draft / 修正指示を入力すると再生成
❌ Reject — Discard the draft, email stays unread / 返信案を破棄、メールは未読のまま
📖 Read Only — No reply needed, just mark as read / 返信不要、既読にする
4. Discord Reply Assistant / Discord返信アシスタント
When someone mentions you or sends a DM on Discord, the bot:

Discordであなたへのメンション・DMを受信すると：

Generates a reply draft matching your writing style / あなたの文体に合った返信案を生成
Sends it to Telegram for approval / Telegramに承認依頼を送信
You choose: ✅ Send / 📝 Edit / ❌ Ignore
If you don't reply within 2 hours, a reminder is sent / 2時間未返信でリマインダー通知
The bot learns your Discord writing style (tone, emoji usage, formality level) from your message history.

Botはあなたの過去のメッセージから文体（口調、絵文字の使い方、敬語レベル）を学習します。

5. Receipt & Expense Management / レシート・経費管理
Scanning a Receipt / レシート読み取り
Send a photo of a receipt to the Telegram bot / レシートの写真をTelegramに送信
Gemini OCR extracts: date, store, amount, items / Gemini OCRが日付・店名・金額・品目を抽出
Auto-categorizes for tax filing (青色申告) / 確定申告用に自動仕訳
Choose: ✅ Save / 📝 Edit Category / ❌ Discard
MoneyForward CSV Matching / MoneyForward照合
Export CSV from MoneyForward ME app
Use /expense → 📥 Import MF CSV → send the CSV file
Bot imports transactions and runs matching:
Certain (date ±1 day + amount + name match) → auto-matched / 自動照合
Likely (date ±2 days + amount match) → asks for confirmation / 確認依頼
Uncertain (amount only) → manual review / 手動レビュー
Tax Categories / 勘定科目
The bot recognizes these categories for freelancer tax filing:

Category / 勘定科目    Examples / 例
通信費    Mobile, Wi-Fi, server, domain
旅費交通費    Train, bus, taxi, Suica
消耗品費    Stationery, cables, USB
接待交際費    Business meals, gifts
会議費    Cafe meetings
地代家賃    Office, coworking space
水道光熱費    Electricity, gas, water
広告宣伝費    Ads, business cards
外注費    Outsourced design, development
新聞図書費    Books, Kindle, tech subscriptions
研修費    Seminars, online courses
雑費    Other
6. Web Dashboard / Webダッシュボード
Access at http://localhost:8080 while the bot is running.

Bot起動中に http://localhost:8080 でアクセスできます。

Available views / 利用可能な画面:

📧 Emails — Email log with classification, approval status, and real-time feed
📋 Tasks — Kanban board (Todo / In Progress / Done) with priority badges
💰 Expenses — Expense list, category chart, CSV upload, manual entry
👥 Contacts — Important contact management
⚙️ Settings — Bot configuration and API usage stats
7. Daily Briefing / 日次ブリーフィング
Every morning at 08:00 JST, the bot sends a summary to Telegram:

毎朝8時にTelegramにサマリーが届きます：

📅 Today's calendar events / 今日の予定
📋 Top 3 priority tasks / 重要タスクTOP3
⚠️ Overdue tasks (if any) / 期限超過タスク
💰 Expense summary (at month start/end) / 経費サマリー（月初・月末）
8. Troubleshooting / トラブルシューティング
Bot doesn't start / Botが起動しない

Check that config.yaml exists and all API keys are set
Verify credentials.json is in the project root
Check logs/secretary.log for error details
No email notifications / メール通知が来ない

Verify Gmail OAuth: delete token.json and restart to re-authenticate
Check quiet_hours setting in config — notifications pause during quiet hours
Run /check to trigger an immediate email check
Discord not connecting / Discordが接続しない

Verify bot token in config
Check that the bot has been invited to your server with proper permissions
Required permissions: Read Messages, Read Message History, Send Messages
Receipt OCR inaccurate / レシートOCRが不正確

Use a clear, well-lit photo
Ensure the entire receipt is visible
Use 📝 Edit Category to correct the auto-classification
Web dashboard not accessible / Webダッシュボードにアクセスできない

Check web.enabled: true in config
Default URL: http://localhost:8080
Check if another process is using port 8080
