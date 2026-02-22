"""
web_server.py
MY-SECRETARY Web ダッシュボードサーバー。
FastAPI + Jinja2 で管理 UI を提供し、承認フロー・状態確認をブラウザから操作できる。
"""

import asyncio
import copy
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent

# プロジェクトルートパス（contacts.md / MEMORY.md の参照に使用）
_PROJECT_ROOT = Path(__file__).parent.parent
_CONTACTS_PATH = _PROJECT_ROOT / "contacts.md"
_MEMORY_PATH   = _PROJECT_ROOT / "MEMORY.md"

app = FastAPI(title="MY-SECRETARY Dashboard")
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

# メインループから渡された共有 bot_data への参照
_bot_data: dict = {}

# 処理済みイベントカウンタ（起動からの累計）
_processed_count: int = 0

# ライブフィードバッファ（最大 100 件保持）
_live_feed: list[dict] = []
_MAX_FEED = 100

# WebSocket 接続中のクライアント一覧
_ws_clients: list[WebSocket] = []


def init(bot_data: dict) -> None:
    """
    main.py から呼び出す初期化関数。
    telegram_app.bot_data への参照を受け取る（Python dict は参照渡し）。
    """
    global _bot_data
    _bot_data = bot_data
    logger.info("web_server: bot_data 参照を取得しました")


async def start(host: str = "0.0.0.0", port: int = 8080) -> None:
    """
    uvicorn を asyncio タスクとして起動する。
    main.py で asyncio.create_task(web_server.start()) として呼び出す。
    """
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",  # uvicorn のログは warning 以上のみ
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info(f"Web ダッシュボードを起動中: http://{host}:{port}")
    await server.serve()


def push_event(event_type: str, message: str, data: dict | None = None) -> None:
    """
    ライブフィードにイベントを追加し、接続中の WebSocket クライアントにブロードキャストする。
    main.py や telegram_bot.py から呼び出すことでリアルタイム更新が可能。
    """
    global _processed_count

    entry = {
        "type": event_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    }

    _live_feed.append(entry)
    if len(_live_feed) > _MAX_FEED:
        _live_feed.pop(0)

    if event_type == "processed":
        _processed_count += 1

    # 非同期ブロードキャスト（接続中クライアントへ）
    asyncio.create_task(_broadcast(entry))


async def _broadcast(entry: dict) -> None:
    """接続中の全 WebSocket クライアントにイベントをブロードキャストする。"""
    import json
    disconnected = []
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(entry, ensure_ascii=False))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        _ws_clients.remove(ws)


# ─────────────────────────────────────────────
# エンドポイント
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """ダッシュボードのメインページを返す。"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    """
    ダッシュボード用の概要 JSON を返す。
    pending_approvals, calendar, live_feed などの現在状態を含む。
    """
    pending: dict = _bot_data.get("pending_approvals", {})

    # 承認待ちメール一覧を整形
    pending_list = []
    for email_id, item in pending.items():
        email = item.get("email", {})
        pending_list.append({
            "id": email_id,
            "subject": email.get("subject", "（件名なし）"),
            "sender": email.get("sender", ""),
            "category": item.get("category", ""),
            "draft": item.get("draft", ""),
            "body_preview": email.get("body", "")[:200],
        })

    # カレンダー予定（calendar_client がある場合のみ）
    today_events = []
    calendar_client = _bot_data.get("calendar_client")
    if calendar_client is not None:
        try:
            events = calendar_client.get_today_events()
            for ev in events:
                today_events.append({
                    "title": ev["title"],
                    "start": ev["start"].strftime("%H:%M") if ev["start"] else "",
                    "end": ev["end"].strftime("%H:%M") if ev["end"] else "",
                    "is_all_day": ev["is_all_day"],
                    "location": ev.get("location", ""),
                })
        except Exception as e:
            logger.warning(f"カレンダー取得エラー: {e}")

    # DB ベースの本日統計
    db = _bot_data.get("db")
    daily_stats: dict = await db.get_daily_stats() if db else {}

    # Discord 通知数
    discord_count = 0
    discord_client = _bot_data.get("discord_client")
    if discord_client is not None:
        try:
            from discord_client import get_discord_stats
            stats = get_discord_stats(discord_client)
            discord_count = stats.get("mention_count", 0) + stats.get("dm_count", 0)
        except Exception:
            pass

    return {
        "processed_count": daily_stats.get("total_processed", _processed_count),
        "pending_count": len(pending),
        "pending_emails": pending_list,
        "today_events": today_events,
        "live_feed": list(reversed(_live_feed))[:20],  # 最新 20 件（新着順）
        "uptime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "daily_stats": daily_stats,
        "discord_count": discord_count,
    }


@app.post("/api/approve/{email_id}")
async def api_approve(email_id: str) -> dict[str, str]:
    """
    指定メールの返信案を承認して Gmail 経由で送信する。
    telegram_bot.py の approve ロジックと同じ処理を実行する。
    """
    pending: dict = _bot_data.get("pending_approvals", {})

    if email_id not in pending:
        raise HTTPException(status_code=404, detail="承認待ちメールが見つかりません")

    item = pending[email_id]
    email = item.get("email", {})
    draft = item.get("draft", "")

    # 返信先アドレスを抽出
    from classifier import extract_email_address
    to_addr = extract_email_address(email.get("sender", ""))

    if not to_addr:
        raise HTTPException(status_code=400, detail="送信先アドレスが取得できませんでした")

    # 件名に Re: を付与
    original_subject = email.get("subject", "")
    reply_subject = (
        original_subject if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}"
    )

    # Gmail 経由で送信
    from gmail_client import send_email, mark_as_read
    gmail_service = _bot_data.get("gmail_service")
    success = send_email(gmail_service, to=to_addr, subject=reply_subject, body=draft)

    if not success:
        raise HTTPException(status_code=500, detail="メール送信に失敗しました")

    # 元メールを既読にして pending から削除
    try:
        mark_as_read(gmail_service, email_id)
    except Exception as e:
        logger.warning(f"既読処理エラー（送信は成功）: {e}")

    del pending[email_id]

    db = _bot_data.get("db")
    if db:
        await db.update_email_status(email_id, "approved")

    push_event(
        "processed",
        f"✅ 承認送信: {original_subject[:40]}",
        {"email_id": email_id, "to": to_addr},
    )
    logger.info(f"Web ダッシュボードから承認送信: {email_id} → {to_addr}")

    return {"status": "ok", "message": f"{to_addr} に送信しました"}


@app.post("/api/reject/{email_id}")
async def api_reject(email_id: str) -> dict[str, str]:
    """
    指定メールの返信案を却下して pending から削除する。
    メールは送信されない。
    """
    pending: dict = _bot_data.get("pending_approvals", {})

    if email_id not in pending:
        raise HTTPException(status_code=404, detail="承認待ちメールが見つかりません")

    item = pending.pop(email_id)
    subject = item.get("email", {}).get("subject", "")

    db = _bot_data.get("db")
    if db:
        await db.update_email_status(email_id, "rejected")

    push_event(
        "rejected",
        f"❌ 却下: {subject[:40]}",
        {"email_id": email_id},
    )
    logger.info(f"Web ダッシュボードから却下: {email_id}")

    return {"status": "ok", "message": "却下しました"}


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """
    ライブフィード用 WebSocket エンドポイント。
    接続中のクライアントにイベントをブロードキャストする。
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info(f"WebSocket 接続: {websocket.client} (計 {len(_ws_clients)} 接続)")

    try:
        # 接続直後に既存フィードを送信（最新 20 件）
        import json
        for entry in list(reversed(_live_feed))[:20]:
            await websocket.send_text(json.dumps(entry, ensure_ascii=False))

        # クライアントからのメッセージを待ち続ける（切断まで維持）
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket エラー: {e}")
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
        logger.info(f"WebSocket 切断 (残 {len(_ws_clients)} 接続)")


# ─────────────────────────────────────────────
# 追加エンドポイント
# ─────────────────────────────────────────────

@app.get("/api/emails")
async def api_emails(status: str | None = None, date: str | None = None) -> list[dict]:
    """DB からメール分類履歴を取得する（直近50件）。"""
    db = _bot_data.get("db")
    if not db:
        return []
    return await db.get_emails(status=status, date_str=date, limit=50)


@app.get("/api/emails/pending")
async def api_emails_pending() -> list[dict]:
    """承認待ちメール一覧をインメモリから取得する。"""
    pending: dict = _bot_data.get("pending_approvals", {})
    result = []
    for email_id, item in pending.items():
        email = item.get("email", {})
        result.append({
            "id": email_id,
            "subject": email.get("subject", ""),
            "sender": email.get("sender", ""),
            "category": item.get("category", ""),
            "draft": item.get("draft", ""),
            "body_preview": (email.get("body", "") or email.get("snippet", ""))[:200],
        })
    return result


@app.post("/api/emails/{email_id}/approve")
async def api_emails_approve(email_id: str) -> dict[str, str]:
    """指定メールの返信案を承認して Gmail 経由で送信する。"""
    return await api_approve(email_id)


@app.post("/api/emails/{email_id}/dismiss")
async def api_emails_dismiss(email_id: str) -> dict[str, str]:
    """指定メールを閲覧のみとしてマークし、既読にする。"""
    pending: dict = _bot_data.get("pending_approvals", {})
    if email_id not in pending:
        raise HTTPException(status_code=404, detail="承認待ちメールが見つかりません")

    item = pending.pop(email_id)
    subject = item.get("email", {}).get("subject", "")

    from gmail_client import mark_as_read
    gmail_service = _bot_data.get("gmail_service")
    try:
        mark_as_read(gmail_service, email_id)
    except Exception as e:
        logger.warning(f"既読処理エラー: {e}")

    db = _bot_data.get("db")
    if db:
        await db.update_email_status(email_id, "read_only")

    push_event("dismissed", f"📖 閲覧のみ: {subject[:40]}", {"email_id": email_id})
    return {"status": "ok", "message": "閲覧のみに変更しました"}


@app.get("/api/contacts")
async def api_contacts() -> dict:
    """contacts.md の連絡先一覧を JSON で返す。"""
    from classifier import load_contacts
    try:
        contacts = load_contacts(str(_CONTACTS_PATH))
        return {"contacts": contacts, "count": len(contacts)}
    except Exception as e:
        logger.warning(f"contacts.md 読み込みエラー: {e}")
        return {"contacts": {}, "count": 0}


@app.get("/api/memory")
async def api_memory_get() -> dict[str, str]:
    """MEMORY.md の全文を返す。"""
    try:
        content = _MEMORY_PATH.read_text(encoding="utf-8") if _MEMORY_PATH.exists() else ""
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/memory")
async def api_memory_put(content: str = Body(..., embed=True)) -> dict[str, str]:
    """MEMORY.md の内容を更新する。リクエストボディ: {"content": "新しい内容"}"""
    try:
        _MEMORY_PATH.write_text(content, encoding="utf-8")
        logger.info("MEMORY.md を Web ダッシュボードから更新しました")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/api-usage")
async def api_api_usage() -> dict:
    """Gemini API 使用量（インメモリ + DB 集計）を返す。"""
    gemini_client = _bot_data.get("gemini_client")
    usage = {}
    if gemini_client:
        from gemini_client import get_api_usage
        usage = get_api_usage(gemini_client)

    db = _bot_data.get("db")
    db_stats: dict = await db.get_daily_stats() if db else {}

    return {
        "realtime": usage,
        "today_db": db_stats,
    }


@app.get("/api/calendar/today")
async def api_calendar_today() -> list[dict]:
    """今日のカレンダー予定一覧を返す。"""
    calendar_client = _bot_data.get("calendar_client")
    if calendar_client is None:
        return []
    try:
        events = calendar_client.get_today_events()
        return [
            {
                "id": ev.get("id", ""),
                "title": ev["title"],
                "start": ev["start"].strftime("%H:%M") if ev.get("start") and not ev["is_all_day"] else "",
                "end": ev["end"].strftime("%H:%M") if ev.get("end") and not ev["is_all_day"] else "",
                "is_all_day": ev["is_all_day"],
                "location": ev.get("location", ""),
                "status": ev.get("status", ""),
                "attendees": ev.get("attendees", []),
            }
            for ev in events
        ]
    except Exception as e:
        logger.warning(f"カレンダー取得エラー: {e}")
        raise HTTPException(status_code=503, detail=f"カレンダー取得失敗: {e}")


@app.get("/api/discord/stats")
async def api_discord_stats() -> dict:
    """Discord の未読メンション数・DM 数などの統計を返す。"""
    discord_client = _bot_data.get("discord_client")
    if discord_client is None:
        return {"enabled": False}
    try:
        from discord_client import get_discord_stats
        stats = get_discord_stats(discord_client)
        return {"enabled": True, **stats}
    except Exception as e:
        logger.warning(f"Discord 統計取得エラー: {e}")
        return {"enabled": True, "error": str(e)}


# ─────────────────────────────────────────────
# 追加エンドポイント（Phase 2）
# ─────────────────────────────────────────────

@app.put("/api/emails/{email_id}/edit-reply")
async def api_edit_reply(email_id: str, draft: str = Body(..., embed=True)) -> dict[str, str]:
    """返信案をインライン編集する。インメモリ pending_approvals と DB を同時更新する。"""
    pending: dict = _bot_data.get("pending_approvals", {})
    if email_id in pending:
        pending[email_id]["draft"] = draft

    db = _bot_data.get("db")
    if db:
        await db.update_email_draft(email_id, draft)

    logger.info(f"返信案を更新: {email_id}")
    return {"status": "ok"}


@app.post("/api/trigger-check")
async def api_trigger_check() -> dict[str, str]:
    """メールチェックを即時トリガーする。main.py の asyncio.Event を set() する。"""
    event = _bot_data.get("_manual_check_event")
    if event is None:
        raise HTTPException(status_code=503, detail="チェック機能が初期化されていません")
    event.set()
    logger.info("Web ダッシュボードから手動メールチェックをトリガーしました")
    return {"status": "ok", "message": "メールチェックをトリガーしました"}


def _mask_secrets(d: dict) -> None:
    """設定辞書の機密フィールドをマスクする（再帰的に処理）。"""
    SECRET_KEYS = {"api_key", "bot_token", "chat_id"}
    for k, v in d.items():
        if isinstance(v, dict):
            _mask_secrets(v)
        elif k in SECRET_KEYS and isinstance(v, str) and v:
            d[k] = (v[:4] + "****" + v[-2:]) if len(v) > 6 else "****"
        elif k in SECRET_KEYS and isinstance(v, int):
            d[k] = "****"


@app.get("/api/config")
async def api_config() -> dict:
    """config.yaml の内容を返す。APIキー等の機密フィールドはマスクする。"""
    config = _bot_data.get("config", {})
    masked = copy.deepcopy(config)
    _mask_secrets(masked)
    return masked


@app.post("/api/reset-learning")
async def api_reset_learning() -> dict[str, str]:
    """contacts.md の自動学習済み連絡先セクションをリセットする。"""
    if not _CONTACTS_PATH.exists():
        return {"status": "ok", "message": "contacts.md が存在しません"}

    content = _CONTACTS_PATH.read_text(encoding="utf-8")
    marker = "## 自動学習済み連絡先"
    idx = content.find(marker)
    if idx != -1:
        content = content[:idx] + marker + "\n\n（リセット済み）\n"
        _CONTACTS_PATH.write_text(content, encoding="utf-8")
        logger.info("contacts.md の自動学習セクションをリセットしました")

    return {"status": "ok", "message": "学習データをリセットしました"}


@app.put("/api/contacts/{email:path}/priority")
async def api_update_contact_priority(
    email: str, priority: str = Body(..., embed=True)
) -> dict[str, str]:
    """contacts.md の指定メールアドレスの優先度を更新する。"""
    if priority not in ("高", "中", "低"):
        raise HTTPException(status_code=400, detail="優先度は 高/中/低 で指定してください")

    if not _CONTACTS_PATH.exists():
        raise HTTPException(status_code=404, detail="contacts.md が見つかりません")

    content = _CONTACTS_PATH.read_text(encoding="utf-8")
    email_lower = email.lower()
    content_lower = content.lower()

    # メールアドレスの位置を探す
    email_pos = content_lower.find(email_lower)
    if email_pos == -1:
        raise HTTPException(status_code=404, detail=f"メールアドレス {email} が見つかりません")

    # 該当ブロックの開始位置（直前の ### ヘッダー）を探す
    block_start = 0
    for m in re.finditer(r"^###\s+", content[:email_pos], re.MULTILINE):
        block_start = m.start()

    # 該当ブロックの終了位置（次の ### ヘッダー、またはファイル末尾）
    next_block = re.search(r"^###\s+", content[email_pos + len(email_lower):], re.MULTILINE)
    block_end = (
        email_pos + len(email_lower) + next_block.start()
        if next_block
        else len(content)
    )

    block_content = content[block_start:block_end]
    new_block = re.sub(r"(優先度[：:]\s*)(?:高|中|低)", r"\g<1>" + priority, block_content)

    if new_block == block_content:
        raise HTTPException(status_code=404, detail="優先度行が見つかりませんでした")

    new_content = content[:block_start] + new_block + content[block_end:]
    _CONTACTS_PATH.write_text(new_content, encoding="utf-8")
    logger.info(f"連絡先優先度を更新: {email} → {priority}")
    return {"status": "ok"}


# ─────────────────────────────────────────────
# タスク管理エンドポイント / Task management endpoints
# ─────────────────────────────────────────────

# リクエストボディ定義 / Request body schemas
class TaskCreateBody(BaseModel):
    """タスク作成リクエストボディ / Request body for task creation."""
    title: str
    description: str | None = None
    priority: str | None = "medium"   # urgent / high / medium / low
    due_date: str | None = None       # "YYYY-MM-DD" 形式 / "YYYY-MM-DD" format


class TaskUpdateBody(BaseModel):
    """タスク部分更新リクエストボディ / Request body for partial task update.
    None フィールドは更新されない。/ None fields are not updated."""
    title: str | None = None
    status: str | None = None         # todo / in_progress / done / cancelled
    priority: str | None = None       # urgent / high / medium / low
    due_date: str | None = None       # "YYYY-MM-DD" or "" to clear / "" でクリア


@app.get("/api/tasks")
async def api_tasks_list(
    status: str | None = None,
    priority: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """タスク一覧を返す。status / priority / limit でフィルタリング可能。
    / Return task list. Supports filtering by status, priority, and limit."""
    db = _bot_data.get("db")
    if not db:
        return []
    return await db.get_tasks(status=status, priority=priority, limit=limit)


@app.post("/api/tasks")
async def api_tasks_create(body: TaskCreateBody) -> dict[str, Any]:
    """新規タスクを作成する。ソースは 'manual' として保存する。
    / Create a new task. Source is saved as 'manual'."""
    db = _bot_data.get("db")
    if not db:
        raise HTTPException(status_code=503, detail="DB が初期化されていません / DB not initialized")

    task_id = await db.save_task(
        title=body.title,
        description=body.description or "",
        source="manual",
        priority=body.priority or "medium",
        due_date=body.due_date or "",
    )

    push_event("info", f"📋 タスク追加: {body.title[:40]}", {"task_id": task_id})
    logger.info(f"タスク作成: id={task_id}, title={body.title}")
    return {"status": "ok", "id": task_id}


# NOTE: /api/tasks/stats を /api/tasks/{task_id} より先に定義することで
#       FastAPI が "stats" を task_id として誤認識しないようにする。
# NOTE: Define /api/tasks/stats BEFORE /api/tasks/{task_id} so FastAPI
#       does not treat the literal "stats" as a path parameter.
@app.get("/api/tasks/stats")
async def api_tasks_stats() -> dict[str, Any]:
    """タスクの統計情報を返す（total / ステータス別 / 期限超過数）。
    / Return task statistics (total, by status, overdue count)."""
    db = _bot_data.get("db")
    if not db:
        return {"total": 0, "todo": 0, "in_progress": 0, "done": 0, "cancelled": 0, "overdue": 0}
    return await db.get_task_stats()


@app.put("/api/tasks/{task_id}")
async def api_tasks_update(task_id: int, body: TaskUpdateBody) -> dict[str, str]:
    """タスクのフィールドを部分更新する（status / priority / title / due_date）。
    / Partially update task fields (status, priority, title, due_date)."""
    db = _bot_data.get("db")
    if not db:
        raise HTTPException(status_code=503, detail="DB が初期化されていません / DB not initialized")

    updated = False

    # ステータス更新 / Update status
    if body.status is not None:
        valid_statuses = {"todo", "in_progress", "done", "cancelled"}
        if body.status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"無効なステータス / Invalid status: {body.status}")
        await db.update_task_status(task_id, body.status)
        updated = True

    # 優先度更新 / Update priority
    if body.priority is not None:
        valid_priorities = {"urgent", "high", "medium", "low"}
        if body.priority not in valid_priorities:
            raise HTTPException(status_code=400, detail=f"無効な優先度 / Invalid priority: {body.priority}")
        await db.update_task_priority(task_id, body.priority)
        updated = True

    # タイトル更新 / Update title
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="タイトルは空にできません / Title cannot be empty")
        await db.update_task_title(task_id, body.title.strip())
        updated = True

    # 期日更新（空文字列で NULL クリア）/ Update due_date (empty string clears to NULL)
    if body.due_date is not None:
        await db.update_task_due_date(task_id, body.due_date)
        updated = True

    if not updated:
        raise HTTPException(status_code=400, detail="更新フィールドが指定されていません / No fields to update")

    logger.info(f"タスク更新: id={task_id}, fields={body.model_fields_set}")
    return {"status": "ok"}


@app.delete("/api/tasks/{task_id}")
async def api_tasks_delete(task_id: int) -> dict[str, str]:
    """タスクを削除する。
    / Delete a task."""
    db = _bot_data.get("db")
    if not db:
        raise HTTPException(status_code=503, detail="DB が初期化されていません / DB not initialized")

    await db.delete_task(task_id)
    push_event("info", f"🗑️ タスク削除: id={task_id}", {"task_id": task_id})
    logger.info(f"タスク削除: id={task_id}")
    return {"status": "ok"}
