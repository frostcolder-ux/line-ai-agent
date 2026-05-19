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
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "selvans-secret-2024-change-me")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

BASE_DIR = os.path.dirname(__file__)

# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "text_model": "claude-haiku-4-5-20251001",
            "image_model": "claude-sonnet-4-5-20251001",
            "max_history_turns": 20,
            "image_window_minutes": 30,
        }

APP_CONFIG = load_config()

TRIGGER_KEYWORDS = [k.strip() for k in os.environ.get("TRIGGER_KEYWORDS", "小凡,思凡助理").split(",")]

# ── State ────────────────────────────────────────────────────────────────────

conversation_history: dict[str, list] = {}
quoted_image_map: dict[str, str] = {}
recent_images: dict[str, tuple[str, float]] = {}
debug_webhooks: list[dict] = []

# ── Prompts ──────────────────────────────────────────────────────────────────

def load_file(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default

def get_system_prompt() -> str:
    return load_file(
        os.path.join(BASE_DIR, "prompts", "selvans.txt"),
        default="你是一位友善的 AI 助理，請用繁體中文回答問題。"
    )

def build_system_prompt() -> str:
    """Build full system prompt with optional knowledge base injection."""
    from knowledge_manager import get_all_content
    base = get_system_prompt()
    kb_content = get_all_content()

    if not kb_content.strip():
        return base

    strict = APP_CONFIG.get("strict_kb_mode", False)
    if strict:
        return (
            base
            + "\n\n"
            + "【嚴格知識庫模式】\n"
            + "以下是你唯一可以使用的知識來源。回答問題時必須完全依據以下資料，"
            + "不得使用任何知識庫以外的資訊或自行推測。\n"
            + "若使用者的問題在以下資料中找不到答案，請明確告知：「知識庫中沒有這個問題的相關資料，"
            + "建議您直接詢問農場工作人員。」\n\n"
            + "## 知識庫內容\n\n"
            + kb_content
        )
    else:
        return (
            base
            + "\n\n"
            + "## 農場知識庫（優先參考）\n\n"
            + kb_content
        )

# Keep a hot-loaded reference updated by admin panel
SYSTEM_PROMPT = get_system_prompt()

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[APP] {msg}", flush=True, file=sys.stderr)


def is_triggered(text: str) -> bool:
    return any(kw in text for kw in TRIGGER_KEYWORDS)


def source_key(event: MessageEvent) -> str:
    src = event.source
    return getattr(src, "group_id", None) or getattr(src, "room_id", None) or src.user_id


def strip_keywords(text: str) -> str:
    import re
    for kw in TRIGGER_KEYWORDS:
        text = text.replace(kw, "")
    text = re.sub(r"@\S+", "", text).strip()
    return text


def get_ai_reply(user_id: str, user_message: str) -> str:
    history = conversation_history.setdefault(user_id, [])
    query = strip_keywords(user_message) or "你好"
    history.append({"role": "user", "content": query})

    max_turns = APP_CONFIG.get("max_history_turns", 20)
    if len(history) > max_turns:
        history = history[-max_turns:]
        conversation_history[user_id] = history

    response = claude.messages.create(
        model=APP_CONFIG.get("text_model", "claude-haiku-4-5-20251001"),
        max_tokens=1024,
        system=build_system_prompt(),
        messages=history,
    )
    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": reply_text})
    return reply_text


def download_image(message_id: str) -> tuple[bytes, str]:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError(f"Not an image: {content_type}")
    return resp.content, content_type


def analyze_image(user_id: str, image_bytes: bytes, media_type: str, query: str) -> str:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    image_system = build_system_prompt()

    response = claude.messages.create(
        model=APP_CONFIG.get("image_model", "claude-sonnet-4-5-20251001"),
        max_tokens=1024,
        system=image_system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": query or "請分析這張照片中植物的狀況，判斷是否有病蟲害或其他問題，並給出具體建議。"},
            ],
        }],
    )
    reply_text = response.content[0].text
    history = conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": f"[照片] {query}"})
    history.append({"role": "assistant", "content": reply_text})
    if len(history) > APP_CONFIG.get("max_history_turns", 20):
        conversation_history[user_id] = history[-APP_CONFIG.get("max_history_turns", 20):]
    return reply_text


def send_reply(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )

# ── Register admin blueprint ─────────────────────────────────────────────────

from admin_routes import admin_bp
app.register_blueprint(admin_bp)

# ── LINE Webhook ─────────────────────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        payload = json.loads(body)
        debug_webhooks.append(payload)
        if len(debug_webhooks) > 10:
            debug_webhooks.pop(0)

        for evt in payload.get("events", []):
            msg = evt.get("message", {})
            quoted_id = msg.get("quotedMessageId")
            log(f"EVENT type={evt.get('type')} msg_type={msg.get('type')} "
                f"msg_id={msg.get('id')} quotedMessageId={quoted_id}")
            if msg.get("type") == "text" and quoted_id:
                log(f"QUOTE-REPLY: text={msg['id']} → image={quoted_id}")
                quoted_image_map[msg["id"]] = quoted_id
    except Exception as e:
        log(f"Pre-parse error: {e}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = event.message.text
    user_id = event.source.user_id
    ctx_key = source_key(event)

    if not is_triggered(user_text):
        return

    query = strip_keywords(user_text) or "請分析這張照片中植物的病蟲害狀況。"

    # Priority 1: user quote-replied to an image
    quoted_id = quoted_image_map.pop(event.message.id, None)
    log(f"TEXT trigger: quoted_id={quoted_id} ctx={ctx_key}")

    if quoted_id:
        log(f"Downloading quoted image {quoted_id}")
        try:
            image_bytes, media_type = download_image(quoted_id)
            log(f"Downloaded {len(image_bytes)} bytes {media_type}")
            reply_text = analyze_image(user_id, image_bytes, media_type, query)
            send_reply(event.reply_token, reply_text)
            return
        except Exception as e:
            log(f"Quoted image failed: {e}")
            send_reply(event.reply_token, f"抱歉，無法讀取那張照片（{type(e).__name__}）。請直接傳照片給我！")
            return

    # Priority 2: recent image in this chat (within window)
    window_secs = APP_CONFIG.get("image_window_minutes", 30) * 60
    recent = recent_images.get(ctx_key)
    if recent:
        img_id, img_ts = recent
        age = time.time() - img_ts
        log(f"Recent image: id={img_id} age={age:.0f}s window={window_secs}s")
        if age <= window_secs:
            try:
                image_bytes, media_type = download_image(img_id)
                reply_text = analyze_image(user_id, image_bytes, media_type, query)
                send_reply(event.reply_token, reply_text)
                return
            except Exception as e:
                log(f"Recent image failed: {e}")

    # Priority 3: plain text reply
    log("No image found, text reply")
    reply_text = get_ai_reply(user_id, user_text)
    send_reply(event.reply_token, reply_text)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    """Store image for later triggered analysis. No auto-reply."""
    ctx_key = source_key(event)
    recent_images[ctx_key] = (event.message.id, time.time())
    log(f"Image stored (no auto-reply): id={event.message.id} ctx={ctx_key}")


# ── Utility routes ───────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "image_model": APP_CONFIG.get("image_model")}, 200


@app.route("/debug/webhook", methods=["GET"])
def debug_webhook():
    return jsonify({"count": len(debug_webhooks), "webhooks": debug_webhooks})


@app.route("/debug/state", methods=["GET"])
def debug_state():
    return jsonify({
        "config": APP_CONFIG,
        "recent_images": {k: {"id": v[0], "age_seconds": round(time.time() - v[1])} for k, v in recent_images.items()},
        "quoted_image_map": quoted_image_map,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
