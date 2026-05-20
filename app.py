"""
小凡 LINE 機器人 — 主程式
思凡社會農場 AI 秘書

架構總覽：
─────────────────────────────────────────────────────────────────────
Flask（主執行緒）
  └─ /callback  ← LINE Webhook，必須在 3 秒內回應 OK
       ├─ 預解析 quotedMessageId（純 JSON parse，< 1ms）
       ├─ buffer_message() 存入監控暫存（list.append，< 1ms）
       └─ handler.handle() → 把訊息丟給 SDK handler 處理

SDK Handler（Flask worker thread，非同步處理）
  └─ handle_message() / handle_image()
       ├─ 呼叫 Claude API（1-5 秒，OK，不阻塞 Webhook）
       └─ send_reply() 回覆

背景執行緒 #1：Monitor Thread（每 N 秒醒來）
  └─ 取出 buffer → 分析 → Push 預警給老闆

背景執行緒 #2：APScheduler（時間到就執行）
  └─ Push 排程訊息到群組

關鍵設計：Webhook 端點只做「存入任務」，立刻回傳 OK，
         所有耗時操作（AI、Push）都在其他執行緒進行，
         絕不讓 LINE 收到超時。
─────────────────────────────────────────────────────────────────────
"""

import os
import sys
import json
import base64
import time
import requests
import anthropic
from flask import Flask, request, abort, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent, JoinEvent

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "selvans-secret-2024-change-me")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# 老闆 LINE User ID：優先讀環境變數，其次讀 config.json
# 在 Render → Environment 新增 BOSS_LINE_USER_ID 即可
BOSS_LINE_USER_ID = os.environ.get("BOSS_LINE_USER_ID", "").strip()

BASE_DIR = os.path.dirname(__file__)


# ── 設定檔 ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "text_model": "claude-3-5-haiku-20241022",
            "image_model": "claude-3-5-sonnet-20241022",
            "max_history_turns": 20,
            "image_window_minutes": 30,
            "strict_kb_mode": False,
            "boss_user_id": "",
            "monitor_interval_seconds": 300,
            "monitor_min_messages": 5,
            "scheduled_tasks": [],
        }


APP_CONFIG = load_config()

TRIGGER_KEYWORDS = [
    k.strip()
    for k in os.environ.get("TRIGGER_KEYWORDS", "小凡,思凡助理").split(",")
]


# ── 狀態（記憶體） ───────────────────────────────────────────────────────────

conversation_history: dict[str, list] = {}
quoted_image_map: dict[str, str] = {}
recent_images: dict[str, tuple[str, float]] = {}
debug_webhooks: list[dict] = []


# ── 工具函式 ─────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[APP] {msg}", flush=True, file=sys.stderr)


def get_boss_id() -> str:
    """
    取得老闆 LINE User ID。
    優先順序：環境變數 BOSS_LINE_USER_ID > config.json boss_user_id
    """
    return BOSS_LINE_USER_ID or APP_CONFIG.get("boss_user_id", "").strip()


def notify_boss(text: str):
    """
    主動發私訊給老闆。
    使用 Push Message API，與群組對話完全獨立。
    """
    boss_id = get_boss_id()
    if not boss_id:
        log("notify_boss: boss_id 未設定，跳過推播")
        return
    try:
        push_message(boss_id, text)
        log(f"notify_boss: 推播成功 → {boss_id[:10]}...")
    except Exception as e:
        log(f"notify_boss ERROR: {type(e).__name__}: {e}")


def load_file(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def is_triggered(text: str) -> bool:
    return any(kw in text for kw in TRIGGER_KEYWORDS)


def source_key(event: MessageEvent) -> str:
    src = event.source
    return getattr(src, "group_id", None) or getattr(src, "room_id", None) or src.user_id


def strip_keywords(text: str) -> str:
    import re
    for kw in TRIGGER_KEYWORDS:
        text = text.replace(kw, "")
    return re.sub(r"@\S+", "", text).strip()


# ── System Prompt 組合 ────────────────────────────────────────────────────────

def get_system_prompt() -> str:
    return load_file(
        os.path.join(BASE_DIR, "prompts", "selvans.txt"),
        default="你是一位友善的 AI 助理，請用繁體中文回答問題。",
    )


def build_system_prompt(query: str = "") -> str:
    """
    動態組合系統提示：人格設定 + 知識庫（依嚴格模式切換）。

    query：使用者的問題。有向量搜尋時只注入最相關段落；
           無向量搜尋時退回截斷全文模式。
    """
    from knowledge_manager import search_relevant_chunks, has_vector_search
    base = get_system_prompt()

    kb_content = search_relevant_chunks(query)   # 有 query → 語意搜尋；無 → 全文

    if not kb_content.strip():
        return base

    mode_note = "（語意搜尋：最相關段落）" if (query and has_vector_search()) else ""
    log(f"build_system_prompt: KB {len(kb_content)} chars {mode_note}")

    if APP_CONFIG.get("strict_kb_mode", False):
        return (
            base
            + "\n\n【嚴格知識庫模式】\n"
            + "以下是你唯一可使用的知識來源，不得自行推測或使用知識庫以外的資訊。\n"
            + "若找不到答案，請明確告知：「知識庫中沒有相關資料，建議詢問農場工作人員。」\n\n"
            + "## 知識庫內容\n\n"
            + kb_content
        )
    else:
        return base + "\n\n## 農場知識庫（優先參考）\n\n" + kb_content


# ── LINE Push Message ─────────────────────────────────────────────────────────

def push_message(target_id: str, text: str):
    """主動推播訊息給指定 User ID 或 Group ID（不需要 reply_token）。"""
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message_with_http_info(
            PushMessageRequest(to=target_id, messages=[TextMessage(text=text)])
        )


# ── AI 回覆（含 Tool Use） ────────────────────────────────────────────────────

def get_ai_reply(user_id: str, user_message: str) -> str:
    """
    呼叫 Claude 進行文字回覆，支援 Function Calling。
    若 tool use 失敗，自動降級為純文字回覆。
    """
    history = conversation_history.setdefault(user_id, [])
    query = strip_keywords(user_message) or "你好"
    history.append({"role": "user", "content": query})

    max_turns = APP_CONFIG.get("max_history_turns", 20)
    if len(history) > max_turns:
        history = history[-max_turns:]
        conversation_history[user_id] = history

    model = APP_CONFIG.get("text_model", "claude-3-5-haiku-20241022")
    system = build_system_prompt(query=query)   # 傳入問題 → 語意搜尋最相關段落

    # 優先嘗試 Tool Use
    try:
        from tools_handler import run_with_tools
        reply_text, updated_history = run_with_tools(claude, model, system, history)
        conversation_history[user_id] = updated_history
        log(f"Tool use reply OK for {user_id}")
        return reply_text
    except Exception as tool_err:
        log(f"Tool use FAILED ({type(tool_err).__name__}: {tool_err}), falling back to plain text")

    # 降級：純文字回覆（不帶 tools）
    response = claude.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=history,
    )
    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": reply_text})
    return reply_text


# ── 圖片下載 & 分析 ───────────────────────────────────────────────────────────

def download_image(message_id: str) -> tuple[bytes, str]:
    """從 LINE Content API 下載圖片，回傳 (bytes, media_type)。"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError(f"Not an image: {content_type}")
    return resp.content, content_type


def analyze_image(user_id: str, image_bytes: bytes, media_type: str, query: str) -> str:
    """用 Claude Vision 分析圖片（使用 Sonnet，準確度較高）。"""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = claude.messages.create(
        model=APP_CONFIG.get("image_model", "claude-3-5-sonnet-20241022"),
        max_tokens=1024,
        system=build_system_prompt(query=query),   # 圖片問題也做語意搜尋
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": query or "請分析這張照片中植物的狀況，判斷是否有病蟲害或其他問題，並給出具體建議。",
                },
            ],
        }],
    )
    reply_text = response.content[0].text

    # 存入對話記憶
    history = conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": f"[照片] {query}"})
    history.append({"role": "assistant", "content": reply_text})
    max_turns = APP_CONFIG.get("max_history_turns", 20)
    if len(history) > max_turns:
        conversation_history[user_id] = history[-max_turns:]

    return reply_text


def send_reply(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


# ── 後台管理 Blueprint ────────────────────────────────────────────────────────

from admin_routes import admin_bp
app.register_blueprint(admin_bp)


# ── LINE Webhook ──────────────────────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    """
    LINE Webhook 入口。
    ★ 核心設計：此函式必須在 3 秒內回傳 OK，不做任何耗時操作。

    這裡只做兩件極快的事：
      1. 預解析 JSON 取得 quotedMessageId（純記憶體操作，< 1ms）
      2. buffer_message() 把訊息存入監控暫存（list.append，< 1ms）

    實際的 AI 呼叫由 Flask worker thread 的 handler.handle() 處理，
    不影響本函式回傳 OK 的時間。
    """
    import monitor

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # ── 步驟 1：預解析取得 quotedMessageId ──
    try:
        payload = json.loads(body)
        debug_webhooks.append(payload)
        if len(debug_webhooks) > 20:
            debug_webhooks.pop(0)

        for evt in payload.get("events", []):
            msg = evt.get("message", {})
            src = evt.get("source", {})
            msg_id    = msg.get("id")
            quoted_id = msg.get("quotedMessageId")

            # ── User ID Debug：每則訊息都印出，方便在 Render Log 查詢 ──
            sender_uid = src.get("userId") or src.get("user_id", "?")
            group_id   = src.get("groupId") or src.get("group_id", "")
            print(
                f"[USER_ID_DEBUG] Sender: {sender_uid} | "
                f"Group: {group_id or '(private)'} | "
                f"type: {msg.get('type', evt.get('type', '?'))}",
                flush=True, file=sys.stderr
            )

            log(f"EVENT type={evt.get('type')} msg_type={msg.get('type')} "
                f"msg_id={msg_id} quoted={quoted_id}")

            # 存入 quotedImageMap（供後續圖片分析使用）
            if msg.get("type") == "text" and quoted_id:
                quoted_image_map[msg_id] = quoted_id
                log(f"QUOTE-MAP: {msg_id} → {quoted_id}")

            # ── 步驟 2：把「未觸發關鍵字」的訊息存入監控暫存 ──
            # 只監控群組訊息（src_type = group），保護個人隱私
            if (msg.get("type") == "text"
                    and src.get("type") == "group"
                    and not is_triggered(msg.get("text", ""))):
                ctx_key = src.get("groupId") or src.get("group_id", "unknown")
                user_id = src.get("userId") or src.get("user_id", "unknown")
                monitor.buffer_message(ctx_key, user_id, msg.get("text", ""))

    except Exception as e:
        log(f"Pre-parse error: {e}")

    # ── 步驟 3：交給 LINE SDK 處理（在 Flask worker thread 執行）──
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ── 文字訊息處理 ──────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    """
    處理有觸發關鍵字的文字訊息。
    優先順序：
      1. 引用回覆照片（quotedMessageId）
      2. 最近 30 分鐘內的照片
      3. 純文字對話（支援 Function Calling）
    """
    user_text = event.message.text
    user_id   = event.source.user_id
    ctx_key   = source_key(event)

    if not is_triggered(user_text):
        return

    query = strip_keywords(user_text) or "請分析這張照片中植物的病蟲害狀況。"

    try:
        # ── Priority 1：引用回覆照片 ──
        quoted_id = quoted_image_map.pop(event.message.id, None)
        log(f"TEXT trigger: quoted_id={quoted_id} ctx={ctx_key}")

        if quoted_id:
            try:
                image_bytes, media_type = download_image(quoted_id)
                log(f"Downloaded quoted image: {len(image_bytes)} bytes {media_type}")
                reply_text = analyze_image(user_id, image_bytes, media_type, query)
                send_reply(event.reply_token, reply_text)
                return
            except Exception as e:
                log(f"Quoted image error: {type(e).__name__}: {e}")
                send_reply(event.reply_token, "抱歉，無法讀取那張照片，請直接傳照片給我！")
                return

        # ── Priority 2：最近傳過的照片 ──
        window_secs = APP_CONFIG.get("image_window_minutes", 30) * 60
        recent = recent_images.get(ctx_key)
        if recent:
            img_id, img_ts = recent
            age = time.time() - img_ts
            log(f"Recent image: age={age:.0f}s window={window_secs}s")
            if age <= window_secs:
                try:
                    image_bytes, media_type = download_image(img_id)
                    reply_text = analyze_image(user_id, image_bytes, media_type, query)
                    send_reply(event.reply_token, reply_text)
                    return
                except Exception as e:
                    log(f"Recent image error: {type(e).__name__}: {e}")

        # ── Priority 3：純文字對話（含 Function Calling）──
        log("Text reply with tool use enabled")
        reply_text = get_ai_reply(user_id, user_text)
        send_reply(event.reply_token, reply_text)

    except Exception as e:
        log(f"handle_message FATAL: {type(e).__name__}: {e}")
        try:
            send_reply(event.reply_token,
                       f"⚠️ 錯誤：{type(e).__name__}\n{str(e)[:120]}")
        except Exception:
            pass


# ── 加入群組事件 ──────────────────────────────────────────────────────────────

@handler.add(JoinEvent)
def handle_join(event: JoinEvent):
    """
    小凡被加入群組時自動執行：
    1. 在群組發歡迎訊息
    2. 自動把此群組加入週排程推播
    3. Push 通知老闆
    """
    import scheduler_tasks

    src = event.source
    group_id = getattr(src, "group_id", None)
    if not group_id:
        return

    log(f"Joined group: {group_id}")

    # 1. 群組歡迎訊息
    welcome = (
        "大家好！我是小凡 🌿\n"
        "思凡社會農場的 AI 小幫手，很高興加入這個群組！\n\n"
        "你可以這樣呼叫我：\n"
        "• 說「小凡」+ 問題 → 我來回答\n"
        "• 傳照片後說「小凡幫我看看」→ 分析植物狀況\n"
        "• 說「小凡記錄採收...」→ 紀錄農場資料\n\n"
        "有任何農業問題歡迎隨時問我！🌱"
    )
    try:
        send_reply(event.reply_token, welcome)
    except Exception as e:
        log(f"Join welcome reply failed: {e}")

    # 2. 自動加入週排程
    try:
        added = scheduler_tasks.add_group_task(push_message, group_id)
        log(f"Auto scheduled task added: {added}")
    except Exception as e:
        log(f"Auto schedule failed: {e}")

    # 3. 主動私訊通知老闆（使用 notify_boss，自動讀取 env var 或 config）
    notify_boss(f"📢 小凡已加入新群組！\n群組 ID：{group_id}\n週排程推播已自動啟用 ✅")


# ── 圖片訊息處理 ──────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    """只存圖片 ID，不自動回覆（避免群組每張圖都觸發）。"""
    ctx_key = source_key(event)
    recent_images[ctx_key] = (event.message.id, time.time())
    log(f"Image stored: id={event.message.id} ctx={ctx_key}")


# ── 工具路由 ─────────────────────────────────────────────────────────────────

@app.route("/debug/models", methods=["GET"])
def debug_models():
    """列出 Anthropic API 上可用的模型，幫助確認正確的模型名稱。"""
    try:
        models = claude.models.list()
        return jsonify({
            "available_models": [m.id for m in models.data],
            "current_text_model": APP_CONFIG.get("text_model"),
            "current_image_model": APP_CONFIG.get("image_model"),
        })
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "text_model":      APP_CONFIG.get("text_model"),
        "image_model":     APP_CONFIG.get("image_model"),
        "boss_configured": bool(get_boss_id()),
        "boss_source":     "env_var" if BOSS_LINE_USER_ID else ("config" if APP_CONFIG.get("boss_user_id") else "not_set"),
    }, 200


@app.route("/admin/test-scheduler", methods=["POST"])
def test_scheduler():
    """手動立即觸發所有排程任務，測試推播是否正常。"""
    from flask import session
    if not session.get("admin_logged_in"):
        return {"error": "unauthorized"}, 401
    import scheduler_tasks
    cfg   = load_config()
    tasks = cfg.get("scheduled_tasks", [])
    results = []
    for task in tasks:
        if not task.get("enabled") or not task.get("group_id"):
            continue
        try:
            push_message(task["group_id"], task["message"])
            results.append({"task": task["name"], "status": "sent"})
            log(f"Test scheduler: sent to {task['name']}")
        except Exception as e:
            results.append({"task": task["name"], "status": f"error: {e}"})
    return jsonify({"triggered": len(results), "results": results})


@app.route("/admin/test-boss-notify", methods=["POST"])
def test_boss_notify():
    """測試發私訊給老闆，確認 BOSS_LINE_USER_ID 設定是否正確。"""
    from flask import session
    if not session.get("admin_logged_in"):
        return {"error": "unauthorized"}, 401
    boss_id = get_boss_id()
    if not boss_id:
        return jsonify({
            "success": False,
            "error": "老闆 ID 未設定",
            "hint": "請在 Render → Environment 新增 BOSS_LINE_USER_ID，或在後台設定 boss_user_id"
        }), 400
    try:
        notify_boss("✅ 測試成功！小凡可以正常發送私訊給老闆 🎉\n（這是從後台觸發的測試訊息）")
        log(f"test-boss-notify: sent to {boss_id[:10]}...")
        return jsonify({"success": True, "boss_id": boss_id[:10] + "...", "source": "env_var" if BOSS_LINE_USER_ID else "config"})
    except Exception as e:
        log(f"test-boss-notify ERROR: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/debug/webhook", methods=["GET"])
def debug_webhook():
    return jsonify({"count": len(debug_webhooks), "webhooks": debug_webhooks})


@app.route("/debug/state", methods=["GET"])
def debug_state():
    return jsonify({
        "config": APP_CONFIG,
        "recent_images": {
            k: {"id": v[0], "age_seconds": round(time.time() - v[1])}
            for k, v in recent_images.items()
        },
        "quoted_image_map": quoted_image_map,
        "active_conversations": len(conversation_history),
    })


@app.route("/admin/push-test", methods=["POST"])
def push_test():
    """後台測試 Push Message 功能（需登入 admin）。"""
    from flask import session
    if not session.get("admin_logged_in"):
        return {"error": "unauthorized"}, 401
    target = request.json.get("target_id", "").strip()
    text   = request.json.get("text", "小凡測試推播 ✓").strip()
    if not target:
        return {"error": "target_id required"}, 400
    try:
        push_message(target, text)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}, 500


# ── 啟動背景服務 ──────────────────────────────────────────────────────────────

def _start_background_services():
    """在 Flask 啟動後立刻啟動監控執行緒與排程器。"""
    import monitor
    import scheduler_tasks

    # 0. 初始化資料庫（有 DATABASE_URL 才會執行）
    from db import init_db
    init_db()

    # 1. 背景監控執行緒（push_fn 改用 notify_boss，自動讀取 env var / config）
    monitor.start_monitor(
        claude_client=claude,
        get_config=lambda: APP_CONFIG,
        push_fn=notify_boss,   # ← 直接傳 notify_boss，內部自動找老闆 ID
    )

    # 2. APScheduler 排程器
    scheduler_tasks.setup_scheduler(
        push_fn=push_message,
        get_config=lambda: APP_CONFIG,
    )

    log("Background services started ✓")


# 使用 with_appcontext 確保背景服務只啟動一次
# （gunicorn multi-worker 下每個 worker 各自啟動自己的背景服務）
with app.app_context():
    _start_background_services()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
