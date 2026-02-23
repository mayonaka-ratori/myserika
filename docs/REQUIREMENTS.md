# Requirements Specification / 要件定義書

## my-secretary v1.0

---

## 1. Functional Requirements / 機能要件

### FR-001: Gmail Monitoring & Classification / Gmail監視・分類

| Item | Description |
|------|-------------|
| **ID** | FR-001 |
| **Priority** | Must |
| **Description (EN)** | The system shall poll Gmail for unread emails at a configurable interval (default: 5 minutes) and classify each email into one of four categories using Gemini AI. |
| **Description (JP)** | 設定可能な間隔（デフォルト5分）でGmailの未読メールを取得し、Gemini AIで4カテゴリに分類する。 |
| **Categories** | 要返信（重要）/ 要返信（通常）/ 閲覧のみ / 無視 |
| **Acceptance Criteria** | Emails are fetched, classified, stored in DB, and notified via Telegram within one polling cycle. |
| **Status** | ✅ Implemented |

### FR-002: Reply Draft Generation / 返信案生成

| Item | Description |
|------|-------------|
| **ID** | FR-002 |
| **Priority** | Must |
| **Description (EN)** | For emails classified as requiring reply, the system shall generate a context-aware reply draft using Gemini, detecting the language (Japanese/English) and applying appropriate style and signature rules. |
| **Description (JP)** | 要返信メールに対し、言語（日本語/英語）を自動判定し、適切なスタイル・署名ルールでGeminiが返信案を生成する。 |
| **Rules** | Japanese: no signature. English: "Best regards, [Name]" signature. Style learned from MEMORY.md. |
| **Status** | ✅ Implemented |

### FR-003: Telegram Approval Flow / Telegram承認フロー

| Item | Description |
|------|-------------|
| **ID** | FR-003 |
| **Priority** | Must |
| **Description (EN)** | The system shall send email notifications with reply drafts to Telegram, providing inline buttons for Approve (send reply + mark read), Revise (text input → regenerate), Reject (discard draft), and Read Only (mark read, no reply). |
| **Description (JP)** | メール通知と返信案をTelegramに送信し、承認（送信+既読）、修正（テキスト入力→再生成）、却下（破棄）、閲覧のみ（既読化）のインラインボタンを提供する。 |
| **Status** | ✅ Implemented |

### FR-004: Discord Monitoring / Discord監視

| Item | Description |
|------|-------------|
| **ID** | FR-004 |
| **Priority** | Should |
| **Description (EN)** | The system shall monitor configured Discord channels and DMs for mentions and direct messages, sending digest notifications to Telegram at a configurable interval (default: 6 hours). |
| **Description (JP)** | 設定されたDiscordチャンネルとDMのメンション・ダイレクトメッセージを監視し、設定間隔（デフォルト6時間）でTelegramにダイジェスト通知する。 |
| **Status** | ✅ Implemented |

### FR-005: Discord Reply Assistant / Discord返信アシスタント

| Item | Description |
|------|-------------|
| **ID** | FR-005 |
| **Priority** | Should |
| **Description (EN)** | The system shall learn the user's Discord writing style from message history, generate reply drafts matching that style, and provide a Telegram approval flow (Send/Edit/Ignore) for Discord replies. Unreplied mentions/DMs shall trigger reminders after a configurable period (default: 2 hours). |
| **Description (JP)** | ユーザーのDiscordメッセージ履歴から文体を学習し、その文体に合った返信案を生成。Telegramで承認フロー（送信/編集/無視）を提供。未返信のメンション/DMは設定時間後（デフォルト2時間）にリマインドする。 |
| **Status** | ✅ Implemented |

### FR-006: Google Calendar Integration / Googleカレンダー連携

| Item | Description |
|------|-------------|
| **ID** | FR-006 |
| **Priority** | Should |
| **Description (EN)** | The system shall retrieve today's and tomorrow's calendar events, prioritize emails from meeting participants, suggest available times, and include schedules in the daily briefing. |
| **Description (JP)** | 当日・翌日のカレンダー予定を取得し、会議参加者からのメールを優先、空き時間を提案、日次ブリーフィングに予定を表示する。 |
| **Status** | ✅ Implemented |

### FR-007: AI Task Manager / AIタスクマネージャー

| Item | Description |
|------|-------------|
| **ID** | FR-007 |
| **Priority** | Should |
| **Description (EN)** | The system shall automatically extract tasks from emails and Discord messages using Gemini, auto-assign priority based on deadline/sender/calendar, provide Telegram commands for manual task management (/todo, /tasks, /done), send deadline reminders, and include top tasks in the daily briefing. |
| **Description (JP)** | Geminiでメール・DiscordメッセージからタスクをAI自動抽出し、締切・送信者・カレンダーに基づき優先度を自動設定。Telegramコマンドで手動管理（/todo, /tasks, /done）、締切リマインダー送信、日次ブリーフィングにタスクTOP3を表示。 |
| **Status** | ✅ Implemented |

### FR-008: Expense & Receipt Management / 経費・レシート管理

| Item | Description |
|------|-------------|
| **ID** | FR-008 |
| **Priority** | Should |
| **Description (EN)** | The system shall process receipt photos via Gemini Vision OCR, auto-categorize expenses for Japanese freelancer tax filing (青色申告), import MoneyForward ME CSV with 3-tier matching (certain/likely/uncertain), and generate monthly/annual reports with CSV export. |
| **Description (JP)** | Gemini Vision OCRでレシート写真を処理し、フリーランスの青色申告用に自動仕訳。MoneyForward ME CSVを3段階照合（確実/可能性高/不明）でインポートし、月次・年次レポートをCSVエクスポート付きで生成。 |
| **Status** | ✅ Implemented |

### FR-009: Web Dashboard / Webダッシュボード

| Item | Description |
|------|-------------|
| **ID** | FR-009 |
| **Priority** | Should |
| **Description (EN)** | The system shall provide a web-based dashboard (FastAPI + Jinja2) with views for emails, tasks (kanban board), expenses (list + category chart + CSV upload), contacts, and system settings. Full REST API with Swagger documentation. |
| **Description (JP)** | FastAPI + Jinja2によるWebダッシュボードを提供。メール、タスク（カンバンボード）、経費（一覧+カテゴリグラフ+CSVアップロード）、連絡先、システム設定の画面。Swaggerドキュメント付きREST API。 |
| **Status** | ✅ Implemented |

### FR-010: Daily Briefing / 日次ブリーフィング

| Item | Description |
|------|-------------|
| **ID** | FR-010 |
| **Priority** | Should |
| **Description (EN)** | The system shall send a morning briefing via Telegram at a configurable time (default: 08:00 JST) containing today's calendar events, top 3 priority tasks, overdue tasks, and conditional expense summaries (month start/end). |
| **Description (JP)** | 設定時刻（デフォルト08:00 JST）にTelegramで朝のブリーフィングを送信。当日の予定、重要タスクTOP3、期限超過タスク、月初/月末の経費サマリーを含む。 |
| **Status** | ✅ Implemented |

---

## 2. Non-Functional Requirements / 非機能要件

### NFR-001: Reliability / 信頼性

| Item | Description |
|------|-------------|
| **ID** | NFR-001 |
| **Requirement (EN)** | The system shall handle API failures (Gemini 429 rate limit, Gmail auth expiry, Discord disconnects) gracefully with retry logic and error notifications via Telegram. The bot shall not crash on any single API failure. |
| **Requirement (JP)** | API障害（Gemini 429レート制限、Gmail認証期限切れ、Discord切断）をリトライロジックとTelegramエラー通知で適切に処理する。単一のAPI障害でBotがクラッシュしないこと。 |
| **Status** | ✅ Implemented |

### NFR-002: Performance / パフォーマンス

| Item | Description |
|------|-------------|
| **ID** | NFR-002 |
| **Requirement (EN)** | All Gemini API calls shall be non-blocking (wrapped in asyncio.to_thread). Receipt images shall be resized to max 1024px before API submission. The event loop shall never be blocked for more than 100ms by application code. |
| **Requirement (JP)** | Gemini API呼び出しは全てノンブロッキング（asyncio.to_thread使用）。レシート画像はAPI送信前に最大1024pxにリサイズ。アプリケーションコードによるイベントループのブロックは100ms以内。 |
| **Status** | ✅ Implemented |

### NFR-003: Security / セキュリティ

| Item | Description |
|------|-------------|
| **ID** | NFR-003 |
| **Requirement (EN)** | API keys and tokens shall never be committed to the repository. All database queries shall use parameterized statements. File uploads shall be size-limited (10 MB for CSV). Web UI shall escape all user-supplied content. Only the configured Telegram chat_id shall have bot access. |
| **Requirement (JP)** | APIキー・トークンはリポジトリにコミットしない。DB操作はパラメータ化クエリ。ファイルアップロードはサイズ制限（CSV 10MB）。Web UIはユーザー入力をエスケープ。設定されたTelegram chat_idのみBotアクセス可。 |
| **Status** | ✅ Implemented |

### NFR-004: Maintainability / 保守性

| Item | Description |
|------|-------------|
| **ID** | NFR-004 |
| **Requirement (EN)** | Telegram bot handlers shall be organized into domain-specific modules (email, discord, task, expense). Shared utilities shall be extracted to a common module. All public database methods shall have docstrings. Code comments shall be in English. |
| **Requirement (JP)** | Telegramハンドラはドメイン別モジュール（メール、Discord、タスク、経費）に分割。共通ユーティリティは共通モジュールに抽出。DBの公開メソッドにはdocstring。コードコメントは英語。 |
| **Status** | ✅ Implemented |

---

## 3. System Constraints / システム制約

| Constraint | Description |
|------------|-------------|
| **Runtime** | Python 3.11+ on Windows (primary) / Linux / macOS |
| **Database** | SQLite only — no external database server |
| **AI Provider** | Google Gemini 2.5 Flash (single provider) |
| **Deployment** | Local machine, single user, single process |
| **Network** | Requires internet for Gmail, Gemini, Telegram, Discord APIs |
| **Authentication** | Gmail/Calendar: OAuth2. Gemini: API key. Telegram/Discord: Bot token. |
| **Localization** | Japanese primary, English secondary. Bilingual documentation. |
