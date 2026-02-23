# Technical Specification / 技術仕様書

## my-secretary v1.0

---

## 1. System Architecture / システムアーキテクチャ

### 1.1 Design Philosophy / 設計思想

my-secretary is designed as a single-process, event-driven application optimized for personal use on a local machine. Key architectural decisions:

my-secretaryは個人利用に最適化されたシングルプロセス・イベント駆動型アプリケーションです。主要な設計判断：

- **Single-process model**: All components (Gmail polling, Telegram bot, Discord bot, Web server) run as concurrent async tasks within one Python process. This eliminates IPC complexity and simplifies deployment.
- **SQLite as sole data store**: No external database server required. The `aiosqlite` library provides non-blocking access. Suitable for personal-scale data volumes (<100K records).
- **Gemini as unified AI backend**: All AI tasks (classification, reply generation, task extraction, receipt OCR, style learning, expense categorization) use a single Gemini 2.5 Flash endpoint, minimizing API key management.
- **MEMORY.md as learning store**: Human-readable markdown file for AI personalization data. Easily auditable and editable by the user.
- **Telegram as primary control interface**: All user interactions flow through Telegram inline buttons, providing a mobile-first UX without building a custom app.

### 1.2 Component Diagram / コンポーネント図

┌──────────────────────────────────────────────────────────┐ │ main.py │ │ ┌─────────────┐ ┌──────────────┐ ┌────────────────┐ │ │ │ Gmail Poll │ │ Telegram Bot │ │ Discord Bot │ │ │ │ (5 min loop) │ │ (webhook) │ │ (gateway) │ │ │ └──────┬───────┘ └──────┬───────┘ └──────┬─────────┘ │ │ │ │ │ │ │ ┌──────▼─────────────────▼─────────────────▼─────────┐ │ │ │ Shared State (bot_data) │ │ │ │ pending_approvals, pending_receipts, │ │ │ │ pending_discord_messages, awaiting_* flags │ │ │ └──────┬─────────────────┬─────────────────┬─────────┘ │ │ │ │ │ │ │ ┌──────▼───────┐ ┌─────▼──────┐ ┌──────▼─────────┐ │ │ │ classifier.py│ │task_manager│ │expense_manager │ │ │ │ gemini_client│ │ .py │ │ .py │ │ │ └──────┬───────┘ └─────┬──────┘ └──────┬─────────┘ │ │ │ │ │ │ │ ┌──────▼─────────────────▼─────────────────▼─────────┐ │ │ │ database.py (aiosqlite) │ │ │ └──────┬─────────────────────────────────────────────┘ │ │ │ │ │ ┌──────▼───────┐ ┌──────────────┐ │ │ │ web_server.py│ │daily_summary │ │ │ │ (FastAPI) │ │ .py │ │ │ └──────────────┘ └──────────────┘ │ └──────────────────────────────────────────────────────────┘


### 1.3 Concurrency Model / 並行処理モデル

All I/O-bound operations use Python's `asyncio`. The main process runs these concurrent tasks:

すべてのI/O操作はPythonの`asyncio`を使用。メインプロセスで以下のタスクが並行実行されます：

| Task | Interval | Description |
|------|----------|-------------|
| Gmail polling | 5 min (configurable) | Checks unread emails, classifies, generates drafts |
| Telegram bot | Event-driven | Processes commands, callbacks, text messages, photos |
| Discord bot | Event-driven | Monitors mentions/DMs, style learning on startup |
| Discord reminder loop | 10 min | Checks for unreplied Discord messages |
| Task reminder loop | Same as Gmail | Checks for upcoming task deadlines |
| Web server | Always-on | FastAPI on port 8080 |
| Daily summary | Once/day at 08:00 | Morning briefing via Telegram |

**Important**: Gemini API calls are synchronous (`google-genai` SDK). All Gemini calls are wrapped in `asyncio.to_thread()` to prevent event loop blocking.

---

## 2. Database Schema / データベーススキーマ

### 2.1 Overview

Database: SQLite via `aiosqlite`
Location: `data/secretary.db`
Initialization: `database.py:init_db()` — creates all tables with `CREATE TABLE IF NOT EXISTS` and applies `ALTER TABLE` migrations for schema evolution.

### 2.2 Tables

#### emails
Stores all processed emails and their classification/reply status.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| message_id | TEXT UNIQUE | Gmail message ID |
| sender | TEXT | Sender email address |
| subject | TEXT | Email subject |
| body_preview | TEXT | First 200 chars of body |
| category | TEXT | Classification: 要返信（重要）/要返信（通常）/閲覧のみ/無視 |
| reply_draft | TEXT | Gemini-generated reply draft |
| status | TEXT | pending/approved/rejected/read_only |
| language | TEXT | Detected language: ja/en |
| created_at | TEXT | ISO 8601 timestamp |
| processed_at | TEXT | When classification completed |

#### tasks
AI-managed task list with auto-extraction from emails/Discord.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| title | TEXT | Task title |
| description | TEXT | Task details |
| source | TEXT | email/discord/calendar/manual |
| source_id | TEXT | Original message ID |
| priority | INTEGER | 1 (highest) to 5 (lowest) |
| status | TEXT | todo/in_progress/done/cancelled |
| due_date | TEXT | ISO 8601 date (nullable) |
| created_at | TEXT | ISO 8601 timestamp |
| updated_at | TEXT | Last modification |
| completed_at | TEXT | When marked done |
| reminded_at | TEXT | Last reminder sent |

#### expenses
Receipt and expense records for tax filing.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| date | TEXT | Purchase date YYYY-MM-DD |
| store_name | TEXT | Store/vendor name |
| amount | INTEGER | Total amount in JPY (tax included) |
| tax_amount | INTEGER | Tax portion (nullable) |
| category | TEXT | 勘定科目 (tax category) |
| subcategory | TEXT | Subcategory detail |
| payment_method | TEXT | cash/credit_card/bank_transfer/electronic |
| receipt_image_path | TEXT | Path to receipt image |
| moneyforward_matched | BOOLEAN | Whether matched with MF transaction |
| moneyforward_id | TEXT | Matched MF transaction ID |
| note | TEXT | User notes |
| source | TEXT | receipt_photo/manual/moneyforward_import |
| created_at | TEXT | ISO 8601 timestamp |
| updated_at | TEXT | Last modification |

#### moneyforward_transactions
Imported MoneyForward ME CSV data for expense matching.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| mf_id | TEXT UNIQUE | MoneyForward transaction ID (dedup key) |
| is_calculation_target | BOOLEAN | 計算対象 flag from CSV |
| date | TEXT | Transaction date |
| content | TEXT | Transaction description |
| amount | INTEGER | Amount in JPY (negative = expense) |
| source_account | TEXT | 保有金融機関 (bank/card name) |
| large_category | TEXT | 大項目 from MF |
| medium_category | TEXT | 中項目 from MF |
| memo | TEXT | User memo from MF |
| is_transfer | BOOLEAN | 振替 flag (excluded from matching) |
| matched_expense_id | INTEGER | FK to expenses.id |
| imported_at | TEXT | Import timestamp |

#### discord_messages
Tracks Discord mentions/DMs for reply management and reminders.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| message_id | TEXT UNIQUE | Discord message ID |
| channel_id | TEXT | Discord channel ID |
| guild_id | TEXT | Discord server ID |
| sender_id | TEXT | Sender's Discord user ID |
| sender_name | TEXT | Sender display name |
| content | TEXT | Message content |
| is_mention | BOOLEAN | Whether user was mentioned |
| is_dm | BOOLEAN | Whether it's a direct message |
| replied | BOOLEAN | Whether reply was sent |
| reply_content | TEXT | Content of sent reply |
| created_at | TEXT | Message timestamp |
| replied_at | TEXT | Reply timestamp |
| reminder_sent_at | TEXT | When reminder was sent |

#### notifications
General notification log.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| type | TEXT | Notification type |
| content | TEXT | Notification content |
| read | BOOLEAN | Read status |
| created_at | TEXT | ISO 8601 timestamp |

#### api_logs
API usage tracking for monitoring and cost awareness.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| service | TEXT | gemini/gmail/telegram/discord |
| endpoint | TEXT | Specific API endpoint called |
| tokens_used | INTEGER | Token count (for Gemini) |
| created_at | TEXT | ISO 8601 timestamp |

---

## 3. API Endpoints / APIエンドポイント一覧

Base URL: `http://localhost:8080`

### 3.1 System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | System status (uptime, counts, connections) |
| GET | `/api/config` | Current configuration (sensitive values masked) |
| GET | `/api/api-usage` | API call statistics by service |
| POST | `/api/trigger-check` | Trigger immediate email check |
| POST | `/api/reset-learning` | Reset MEMORY.md learning data |

### 3.2 Emails

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/emails` | Email list (query: status, category, limit) |
| GET | `/api/emails/pending` | Pending approval emails only |
| POST | `/api/emails/{id}/approve` | Approve and send reply |
| POST | `/api/emails/{id}/reject` | Reject reply draft |
| POST | `/api/emails/{id}/dismiss` | Mark as read only |
| PUT | `/api/emails/{id}/edit-reply` | Edit reply draft text |

### 3.3 Tasks

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks` | Task list (query: status, priority, limit) |
| POST | `/api/tasks` | Create new task |
| PUT | `/api/tasks/{id}` | Update task fields |
| DELETE | `/api/tasks/{id}` | Delete task |
| GET | `/api/tasks/stats` | Task statistics |

### 3.4 Expenses

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/expenses` | Expense list (query: month, category, limit) |
| POST | `/api/expenses` | Create expense manually |
| PUT | `/api/expenses/{id}` | Edit expense |
| GET | `/api/expenses/monthly/{year}/{month}` | Monthly summary JSON |
| GET | `/api/expenses/annual/{year}` | Annual summary JSON |
| GET | `/api/expenses/annual/{year}/csv` | Download annual CSV |
| POST | `/api/expenses/import-mf` | Upload MoneyForward CSV |

### 3.5 Other

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/contacts` | Contact list from contacts.md |
| GET | `/api/memory` | MEMORY.md contents |
| GET | `/api/calendar/today` | Today's calendar events |
| GET | `/api/discord/stats` | Discord monitoring statistics |

### 3.6 Documentation

| Path | Description |
|------|-------------|
| `/docs` | Swagger UI (interactive API explorer) |
| `/redoc` | ReDoc (alternative API docs) |
| `/openapi.json` | OpenAPI 3.0 schema |

---

## 4. AI Pipeline / AIパイプライン

### 4.1 Email Classification Pipeline

Input: Raw email (sender, subject, body_preview) │ ├─→ contacts.md lookup (known sender? priority level?) │ ├─→ Gemini classification prompt │ System: "You are an email classifier..." │ Input: sender, subject, body (200 chars) │ Output: {category, confidence, reason} │ ├─→ If category == 要返信: generate reply draft │ System: "Generate a reply in {detected_language}..." │ Context: MEMORY.md style preferences, sender relationship │ Output: reply text │ └─→ Save to DB + notify Telegram


### 4.2 Task Extraction Pipeline

Input: Email body or Discord message │ ├─→ Gemini task detection prompt │ "Does this message contain action items, requests, or deadlines?" │ Output: [{title, description, due_date_estimate}] or [] │ ├─→ Auto-prioritize each task │ Input: deadline proximity, sender importance, calendar availability │ Output: priority (1-5) │ └─→ Save to DB + notify Telegram with confirm/ignore buttons


### 4.3 Receipt OCR Pipeline

Input: Photo (Telegram) │ ├─→ Resize to max 1024px (Pillow) │ ├─→ Gemini Vision OCR (Japanese prompt) │ Output: {date, store_name, items[], total, tax, payment_method} │ ├─→ Validate date format (YYYY-MM-DD regex) │ ├─→ Auto-categorize │ Step 1: Rule-based keyword matching (CATEGORY_KEYWORDS dict) │ Step 2: DB lookup (same store → same category) │ Step 3: Gemini classification (fallback) │ └─→ Show result in Telegram with Save/Edit/Discard buttons


### 4.4 Discord Style Learning Pipeline

Trigger: Bot startup (one-time, flag-controlled) │ ├─→ For each monitored channel/DM (max 5): │ Fetch last 100 messages │ Filter: user's own messages only (max 20) │ ├─→ Gemini style analysis │ Output: {tone, avg_length, common_expressions, emoji_patterns, per_person_style} │ └─→ Write to MEMORY.md "Discord Communication Style" section


### 4.5 MoneyForward Matching Pipeline

Input: CSV file (Telegram document upload) │ ├─→ Encoding detection (UTF-8 → Shift-JIS → CP932) │ ├─→ Parse CSV columns: │ 計算対象, 日付, 内容, 金額（円）, 保有金融機関, 大項目, 中項目, メモ, 振替, ID │ ├─→ Import to DB (dedup by MF ID, skip transfers) │ ├─→ Match against expenses: │ Certain: ±1 day + exact amount + name match → auto-match │ Likely: ±2 days + exact amount → user confirmation │ Uncertain: amount match only → manual review │ └─→ Report results in Telegram


---

## 5. Security Considerations / セキュリティ

- **API keys**: Stored in `config.yaml` (gitignored). Never committed to repository.
- **OAuth tokens**: `token.json` and `credentials.json` are gitignored.
- **Receipt images**: Stored in `data/receipts/` (gitignored). Never uploaded to any external service except Gemini API for OCR.
- **Web dashboard**: Binds to `0.0.0.0:8080` by default. No authentication — intended for local network only. For remote access, use a reverse proxy with authentication.
- **SQL injection**: All database queries use parameterized statements via aiosqlite.
- **XSS prevention**: Template output is escaped via Jinja2 auto-escaping and JavaScript `esc()` helper.
- **File upload limits**: CSV uploads capped at 10 MB. Receipt images resized to max 1024px before processing.
- **Telegram chat_id**: Only the configured chat_id can interact with the bot. Other users are ignored.

---

## 6. Dependencies / 依存ライブラリ

| Package | Version | Purpose |
|---------|---------|---------|
| google-genai | latest | Gemini 2.5 Flash API (text + vision) |
| google-api-python-client | latest | Gmail API |
| google-auth-oauthlib | latest | Google OAuth2 flow |
| google-auth-httplib2 | latest | Google Auth HTTP transport |
| python-telegram-bot | 20+ | Telegram Bot API (async) |
| discord.py | latest | Discord Gateway + REST API |
| fastapi | latest | Web dashboard framework |
| uvicorn[standard] | latest | ASGI server for FastAPI |
| jinja2 | latest | HTML template engine |
| aiosqlite | latest | Async SQLite wrapper |
| pyyaml | latest | YAML config parser |
| Pillow | latest | Image processing (receipt resize) |
| tzdata | latest | Timezone data for Windows |

---

## 7. Configuration Reference / 設定リファレンス

See `config.yaml.example` for the complete configuration template with all available options and their defaults.

全設定項目とデフォルト値は `config.yaml.example` を参照してください。
