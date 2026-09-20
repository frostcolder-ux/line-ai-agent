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
import datetime as _dt
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

import access  # 誰可以使喚小凡：老闆、已核准的群組、其他人

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "selvans-secret-2024-change-me")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# 老闆 LINE User ID：優先讀環境變數，其次讀 config.json
# 在 Render → Environment 新增 BOSS_LINE_USER_ID 即可
BOSS_LINE_USER_ID = os.environ.get("BOSS_LINE_USER_ID", "").strip()

# 群組裡說「小凡，這個交給總管：…」→ 直接落地成交辦，給老闆本機的代理人處理
AGENT_HANDOFF_MARK = os.environ.get("AGENT_HANDOFF_MARK", "交給總管").strip() or "交給總管"

BASE_DIR = os.path.dirname(__file__)


# ── 設定檔 ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "text_model": "claude-haiku-4-5-20251001",
            "image_model": "claude-sonnet-4-6",
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


# 陌生人私訊時的固定回覆。小凡是內部助理，對外只給官網。
OUTSIDE_REPLY = """我是小凡，思凡自然農園的內部小幫手，只在農園的工作群組裡服務。

想認識思凡的香草和社會農場，或是要訂購、談合作：
https://www.selvansimpact.com

從官網的「洽談合作」留言，我們會回覆你。"""

# 群組被核准之後才發這一則。以前是一進群組就發，等於還沒確認身分就先自我介紹一輪。
GROUP_WELCOME = """大家好！我是小凡 🌿
思凡社會農場的 AI 小幫手，很高興加入這個群組！

你可以這樣呼叫我：
• 說「小凡」+ 問題 → 我來回答
• 傳照片後說「小凡幫我看看」→ 分析植物狀況
• 「小凡 這週誰還沒交週報」→ 查週報進度
• 「小凡 大溪這週採收多少」→ 查真實採收量
• 「小凡 開投票 開會時間 10點/14點/16點」→ 發起投票

有任何農業問題歡迎隨時問我！🌱"""
# 同一個人一天只回一次，免得被當成聊天機器人一直丟訊息。
# 記在記憶體就好：Render 重啟後最多多回一次，不值得為它寫一張表。
_outside_replied: dict[str, str] = {}
# 還沒核准的群組，一天也只提醒一次
_pending_nudged: dict[str, str] = {}


def group_display_name(group_id: str) -> str:
    """查群組名稱，查不到就用 ID 末八碼——老闆要看得懂這是哪一個群組。"""
    try:
        with ApiClient(configuration) as api_client:
            return MessagingApi(api_client).get_group_summary(group_id).group_name
    except Exception as e:
        log(f"group_display_name error: {type(e).__name__}: {e}")
        return group_id[-8:]


def leave_group(group_id: str):
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).leave_group(group_id)
        log(f"left group {group_id[-8:]}")
    except Exception as e:
        log(f"leave_group error: {type(e).__name__}: {e}")


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


def group_member_name(group_id: str, user_id: str) -> str:
    """查群組成員的顯示名稱。

    交辦紀錄裡的「誰」如果是 Ub20f553c… 那種 id，統計「誰一直在丟工作」
    就等於沒統計。呼叫端（work_capture）有快取，同一個人只會查一次。
    """
    with ApiClient(configuration) as api_client:
        profile = MessagingApi(api_client).get_group_member_profile(
            group_id, user_id)
        return getattr(profile, "display_name", "") or ""


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

    model = APP_CONFIG.get("text_model", "claude-haiku-4-5-20251001")
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
        model=APP_CONFIG.get("image_model", "claude-sonnet-4-6"),
        max_tokens=1024,
        system=build_system_prompt(query=query),
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
            # 只收已核准群組：沒核准的群組一個字都不留，更不會送進 AI 分析
            if (msg.get("type") == "text"
                    and src.get("type") == "group"
                    and not is_triggered(msg.get("text", ""))
                    and access.is_approved(src.get("groupId") or src.get("group_id", ""))):
                ctx_key = src.get("groupId") or src.get("group_id", "unknown")
                user_id = src.get("userId") or src.get("user_id", "unknown")
                text = msg.get("text", "")
                # 投票進行中：先嘗試把這則訊息計為一票（不影響監控暫存）
                try:
                    import poll
                    if poll.has_open_poll(ctx_key):
                        poll.record_vote(ctx_key, user_id, text)
                except Exception as e:
                    log(f"poll vote capture error: {e}")

                # 直接寫進資料庫佇列，不放記憶體——這支跑在會休眠也會重啟的
                # 免費方案上，囤在記憶體的訊息撐不到下一次分析。
                # 顯示名稱不在這裡查（那是一次 LINE API 呼叫，webhook 要快），
                # 分析時才補。
                try:
                    import work_capture
                    when = evt.get("timestamp")
                    said_at = (
                        _dt.datetime.fromtimestamp(when / 1000).strftime(
                            "%Y-%m-%d %H:%M:%S") if when else "")
                    work_capture.queue_message(
                        ctx_key, monitor._group_name(ctx_key,
                                                     lambda: APP_CONFIG),
                        user_id, "", text, said_at)
                except Exception as e:
                    log(f"queue_message error: {e}")

    except Exception as e:
        log(f"Pre-parse error: {e}")

    # ── 步驟 3：交給 LINE SDK 處理（在 Flask worker thread 執行）──
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


def reply_to_outsider(event: MessageEvent, user_id: str):
    """陌生人私訊：回一則固定說明，同一個人一天只回一次。"""
    today = _dt.date.today().isoformat()
    if _outside_replied.get(user_id) == today:
        return
    _outside_replied[user_id] = today
    try:
        # 用回覆不用推播：回覆不計入官方帳號的訊息則數
        send_reply(event.reply_token, OUTSIDE_REPLY)
        log(f"outsider replied: {user_id[:10]}...")
    except Exception as e:
        log(f"outsider reply error: {type(e).__name__}: {e}")


def start_serving(group_id: str, name: str):
    """核准之後才做的事：開週排程、在群組自我介紹。沒核准的群組不該收到任何推播。"""
    try:
        import scheduler_tasks
        scheduler_tasks.add_group_task(push_message, group_id, name)
    except Exception as e:
        log(f"approve schedule error: {type(e).__name__}: {e}")


def nudge_pending_group(event: MessageEvent, group_id: str):
    """還沒核准的群組裡有人叫小凡：說明原因＋提醒老闆。同一個群組一天一次。"""
    today = _dt.date.today().isoformat()
    if _pending_nudged.get(group_id) == today:
        return
    _pending_nudged[group_id] = today
    name = group_display_name(group_id)
    row = access.remember_pending(group_id, name)
    try:
        send_reply(event.reply_token, "這個群組還沒開通，我先不插話。請思凡在這裡說一句「小凡 核准」，我就開始幫忙。")
    except Exception as e:
        log(f"pending nudge reply error: {type(e).__name__}: {e}")
    notify_boss(
        f"「{name}」裡有人叫我，但這個群組還沒核准。\n"
        f"代號 {row['code']}\n\n"
        f"回「核准 {row['code']}」開通，或直接在那個群組說「小凡 核准」。"
    )


def handle_boss_command(event: MessageEvent, action: str, code: str) -> bool:
    """老闆在私訊裡的群組管理指令（核准 A7K2／拒絕 A7K2／群組清單）。有處理就回 True。"""
    if action == "list":
        send_reply(event.reply_token, access.summary())
        return True

    row = access.find_by_code(code)
    if not row:
        send_reply(event.reply_token, f"找不到代號 {code}。傳「群組清單」看目前有哪些。")
        return True

    if action == "approve":
        access.decide(code, "approved")
        start_serving(row["group_id"], row["name"])
        try:
            push_message(row["group_id"], GROUP_WELCOME)
        except Exception as e:
            log(f"approve welcome error: {type(e).__name__}: {e}")
        send_reply(event.reply_token, f"好，「{row['name']}」開始服務了。")
        return True

    access.decide(code, "rejected")
    leave_group(row["group_id"])
    send_reply(event.reply_token, f"我已經退出「{row['name']}」。")
    return True


def decide_group(event: MessageEvent, group_id: str, decision: str):
    """老闆直接在那個群組裡說「小凡 核准」或「小凡 拒絕」。"""
    row = access.remember_pending(group_id, group_display_name(group_id))
    if decision == "approve":
        access.decide(row["code"], "approved")
        start_serving(group_id, row["name"])
        # 自我介紹用回覆發，不用推播：同樣一則訊息，回覆不計入訊息則數
        send_reply(event.reply_token, GROUP_WELCOME)
        return
    access.decide(row["code"], "rejected")
    try:
        send_reply(event.reply_token, "好，那我先離開了。需要我的時候再把我加回來。")
    except Exception as e:
        log(f"reject reply error: {type(e).__name__}: {e}")
    leave_group(group_id)


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
    role      = access.role_of(event.source, get_boss_id())

    # ── 身分先擋在最前面（見 access.py）──
    # 陌生人：只回一則固定說明，不進 AI、不查資料、不寫任何東西。
    # 還沒核准的群組：完全安靜，等老闆核准。
    if role == access.OUTSIDE:
        group_id = getattr(event.source, "group_id", None)
        # 老闆人就在那個還沒核准的群組裡，直接說「小凡 核准」也算數，
        # 不必切回私訊去找代號
        if group_id and get_boss_id() and user_id == get_boss_id():
            decision = access.parse_group_decision(strip_keywords(user_text))
            if decision:
                decide_group(event, group_id, decision)
                return
        # 還沒核准的群組裡有人叫小凡：講一句為什麼不回答，並通知老闆。
        #
        # ★ 為什麼不能默默不理
        #   config.json 只留得住一個群組（其他群組是小凡自己加的，而 Render 重啟
        #   就把那些紀錄洗掉）。所以白名單上線時，有些還在用的工作群組不會被自動核准。
        #   如果那裡的小凡只是安靜，沒有人知道發生什麼事，也沒人知道怎麼救。
        if group_id and is_triggered(user_text):
            nudge_pending_group(event, group_id)
            return
        # 私訊來的陌生人回一則說明（不管他有沒有叫「小凡」，他是特地來傳訊息的）
        if not group_id:
            reply_to_outsider(event, user_id)
        return

    # 老闆的管理指令：核准／拒絕群組、看清單。要先於觸發詞判斷，
    # 這樣他直接回「核准 A7K2」就好，不必每次都寫「小凡」。
    if role == access.BOSS:
        command = access.parse_boss_command(strip_keywords(user_text) or user_text)
        if command and handle_boss_command(event, *command):
            return

    if not is_triggered(user_text):
        return

    query = strip_keywords(user_text) or "請分析這張照片中植物的病蟲害狀況。"

    # ── Priority 0a：「交給總管」──
    # 老闆本機有個代理人（思凡總管），它透過 brand-db 來拉交辦。明講交給總管的
    # 直接落地成一筆交辦（不等 5 分鐘一批的分析、也不靠模型判斷是不是交辦），
    # 原句裡的「總管」就是 brand-db 那端認出要優先處理的記號。
    #
    # ★ 只有老闆說的才真的交給代理人
    #   群組裡的夥伴也會看到這個用法。代理人拿到交辦就會去做事，所以別人說的
    #   只存成一般交辦等老闆確認——記號（總管兩個字）不留在存下來的原句裡，
    #   brand-db 那端就不會把它當成要優先執行的那種。
    if AGENT_HANDOFF_MARK in query:
        to_agent = role == access.BOSS
        try:
            import monitor
            import work_capture
            task = query.split(AGENT_HANDOFF_MARK, 1)[1].lstrip("：:，, ").strip() or query
            gid = getattr(event.source, "group_id", "") or ""
            who = work_capture.resolve_name(gid, user_id, group_member_name) if gid else "老闆"
            work_capture.save_many([{
                "group_id": gid,
                "group_name": monitor._group_name(gid, lambda: APP_CONFIG) if gid else "私訊",
                "asker": who,
                "said_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "raw": user_text if to_agent else user_text.replace(AGENT_HANDOFF_MARK, "").strip(),
                "title": task[:60], "detail": task,
                "kind": "other", "urgency": "mid", "confidence": "explicit",
            }])
            send_reply(event.reply_token, "收到，轉給總管了。做完會回報給老闆。" if to_agent
                       else "收到，我記下來了，會讓老闆確認。")
        except Exception as e:
            log(f"agent handoff error: {type(e).__name__}: {e}")
            send_reply(event.reply_token, "記這一筆的時候出錯了，沒有記到，請稍後再說一次。")
        return

    # ── Priority 0：開會投票指令（純 bot 端，需群組情境）──
    try:
        import poll
        q = query.strip()
        if q.startswith("開投票") or q.startswith("發起投票"):
            body = q.replace("發起投票", "", 1).replace("開投票", "", 1).strip()
            topic, options = poll.parse_open_command(body)
            if topic is None:
                send_reply(event.reply_token, options)  # 錯誤訊息
            else:
                send_reply(event.reply_token, poll.open_poll(ctx_key, topic, options))
            return
        if q in ("結算", "投票結算", "公告結果", "結束投票"):
            send_reply(event.reply_token, poll.close_poll(ctx_key))
            return
        if q in ("投票狀況", "投票狀態", "目前票數", "看投票"):
            send_reply(event.reply_token, poll.status_text(ctx_key))
            return
    except Exception as e:
        log(f"poll command error: {type(e).__name__}: {e}")

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
            # 錯誤內容只寫進 log。把 exception 原文回到聊天室，等於把內部結構講給看得到的人聽
            send_reply(event.reply_token, "抱歉，我這邊出了點狀況，沒有處理完。請再說一次，或稍後再試。")
        except Exception:
            pass


# ── 加入群組事件 ──────────────────────────────────────────────────────────────

@handler.add(JoinEvent)
def handle_join(event: JoinEvent):
    """
    小凡被加入群組時：先問過老闆再開始服務。

    以前是進群組就自我介紹、自動開週排程推播、通知老闆「已啟用」。
    問題是任何加小凡好友的人都能把它拉進自己的群組，等於外人可以
    讓它開始推播（吃訊息額度）並監聽那個群組的所有對話。

    現在改成：先安靜，私訊老闆一組代號，他回「核准 代號」才開始服務。
    """
    src = event.source
    group_id = getattr(src, "group_id", None)
    if not group_id:
        return

    log(f"Joined group: {group_id}")

    if access.is_approved(group_id):
        # 之前核准過又被重新加入（例如被踢出再加回來）：照舊開始服務
        try:
            send_reply(event.reply_token, GROUP_WELCOME)
        except Exception as e:
            log(f"Join welcome reply failed: {e}")
        return

    name = group_display_name(group_id)
    row = access.remember_pending(group_id, name)
    try:
        send_reply(event.reply_token, "大家好，我是小凡。我先跟思凡確認一下，確認完才開始幫忙。")
    except Exception as e:
        log(f"Join pending reply failed: {e}")

    notify_boss(
        f"小凡被加入群組「{name}」。\n"
        f"代號 {row['code']}\n\n"
        f"回「核准 {row['code']}」開始服務，回「拒絕 {row['code']}」我就退出。\n"
        f"在核准之前，我不會回話、不會推播，也不會看那個群組的訊息。"
    )


# ── 圖片訊息處理 ──────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    """只存圖片 ID，不自動回覆（避免群組每張圖都觸發）。"""
    # 陌生人與還沒核准的群組的照片不留，免得他們接著說「小凡幫我看看」
    if access.role_of(event.source, get_boss_id()) == access.OUTSIDE:
        return
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
        # 白名單有沒有生效、目前核准了幾個群組。部署後從外面看得出跑的是不是新版
        "whitelist":       True,
        "approved_groups": len(access.approved_ids()),
        # Render 會把這次部署的 commit 放進環境變數。有它才分得出「推上去了」
        # 和「部署好了」——免費方案建置要好幾分鐘，中間問 /health 還是舊版，
        # 很容易誤判成自動部署壞掉。
        "commit":          os.environ.get("RENDER_GIT_COMMIT", "")[:7],
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


@app.route("/admin/test-radar", methods=["POST"])
def test_radar():
    """手動立即觸發營運雷達（週報進度私訊 + 晨報），測試整條資料鏈是否通。"""
    from flask import session
    if not session.get("admin_logged_in"):
        return {"error": "unauthorized"}, 401
    import radar
    from data_store import list_tasks as _list_tasks
    which = (request.json or {}).get("which", "digest")
    try:
        if which == "morning":
            radar.daily_morning_report(notify_boss, _list_tasks)
        elif which == "friday":
            import scheduler_tasks
            gid = scheduler_tasks._primary_group_id(lambda: APP_CONFIG)
            if not gid:
                return {"success": False, "error": "找不到群組 ID"}, 400
            radar.friday_group_remind(push_message, gid)
        else:
            radar.weekly_boss_digest(notify_boss)
        return {"success": True, "triggered": which}
    except Exception as e:
        log(f"test-radar ERROR: {e}")
        return {"success": False, "error": str(e)}, 500


@app.route("/internal/radar-preview", methods=["GET"])
def radar_preview():
    """
    純文字預覽營運雷達內容（不發 LINE），方便快速確認資料是否接通。
    需帶 ?token= 等於 FARM_API_TOKEN（沿用同一把密鑰）。
    """
    import os as _os
    required = _os.environ.get("FARM_API_TOKEN", "").strip()
    if required and request.args.get("token", "") != required:
        return {"error": "unauthorized"}, 401
    import farm_bridge
    from data_store import list_tasks as _list_tasks
    status = farm_bridge.get_report_status()
    return jsonify({
        "status_raw": status,
        "boss_digest": farm_bridge.format_status_digest(status, for_boss=True),
        "group_reminder": farm_bridge.format_reminder(status),
        "pending_tasks": _list_tasks(status="pending"),
    })


@app.route("/internal/agent-result", methods=["POST"])
def internal_agent_result():
    """思凡總管（老闆本機的代理人）做完交辦後，透過這裡推播結果給老闆。

    只推給老闆一個人，不推群組——結果要不要轉達由老闆決定。
    需帶 X-Internal-Token 標頭（或 ?token=）等於 FARM_API_TOKEN。
    """
    import os as _os
    required = _os.environ.get("FARM_API_TOKEN", "").strip()
    if not required:
        return {"error": "FARM_API_TOKEN not configured"}, 503
    supplied = request.headers.get("X-Internal-Token", "") or request.args.get("token", "")
    if supplied != required:
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return {"error": "text required"}, 400
    if not get_boss_id():
        return {"error": "BOSS_LINE_USER_ID not configured"}, 503
    notify_boss("【總管回報】\n" + text[:4500])
    return {"ok": True}


@app.route("/internal/work-requests", methods=["GET"])
def internal_work_requests():
    """群組裡抓到的交辦，給老闆本機的 brand-db 來拉。

    為什麼是「來拉」不是「推過去」：brand-db 跑在他自己的電腦上
    （127.0.0.1:8765），雲端推不進去。跟 brand-db 拉 farm_reports
    即時數據同一個模式。

    不做「只給沒同步過的」——brand-db 端也會用指紋去重，整個時間窗
    都吐出去比較不怕它關機幾天。需帶 ?token= 等於 FARM_API_TOKEN。
    """
    import os as _os
    required = _os.environ.get("FARM_API_TOKEN", "").strip()
    if not required:
        # 這裡面有夥伴的名字與原話，沒設密鑰就不開放——不像唯讀的雷達預覽
        return {"error": "FARM_API_TOKEN not configured"}, 503
    if request.args.get("token", "") != required:
        return {"error": "unauthorized"}, 401

    import work_capture
    try:
        days = max(1, min(int(request.args.get("days", 14)), 365))
    except ValueError:
        days = 14

    # ★ 先把還沒分析的訊息消化掉再回。
    #   背景執行緒在會休眠的免費方案上不保證跑得到，但「老闆按同步」這個
    #   動作一定會送到這裡——把處理掛在這一下，整條路才有確定性。
    processed = {}
    if request.args.get("process", "1") != "0":
        try:
            import monitor
            processed = work_capture.process_pending(
                monitor.make_analyzer(claude, lambda: APP_CONFIG, notify_boss,
                                      group_member_name),
                group_name_fn=lambda g: monitor._group_name(
                    g, lambda: APP_CONFIG))
        except Exception as e:
            log(f"process_pending error: {type(e).__name__}: {e}")
            processed = {"error": str(e)}

    rows = work_capture.list_recent(days=days)
    return jsonify({"days": days, "count": len(rows), "requests": rows,
                    "processed": processed,
                    "pending": work_capture.pending_count(),
                    "stats": work_capture.stats()})


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

    # 0b. 白名單第一次上線時，把設定檔裡已經在用的群組直接列為已核准，
    #     否則現有的工作群組會全部被當成外人，小凡會整個安靜下來
    try:
        access.seed_from_config(APP_CONFIG)
    except Exception as e:
        log(f"access seed error: {type(e).__name__}: {e}")

    # 1. 背景監控執行緒（push_fn 改用 notify_boss，自動讀取 env var / config）
    monitor.start_monitor(
        claude_client=claude,
        get_config=lambda: APP_CONFIG,
        push_fn=notify_boss,   # ← 直接傳 notify_boss，內部自動找老闆 ID
        profile_fn=group_member_name,   # 把 user id 換成看得懂的名字
    )

    # 2. APScheduler 排程器（靜態任務）
    scheduler_tasks.setup_scheduler(
        push_fn=push_message,
        get_config=lambda: APP_CONFIG,
    )

    # 3. 營運雷達（讀農場回報系統真實資料 → 主動推播）
    from data_store import list_tasks as _list_tasks
    scheduler_tasks.setup_radar(
        notify_boss_fn=notify_boss,
        push_fn=push_message,
        list_tasks_fn=_list_tasks,
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
