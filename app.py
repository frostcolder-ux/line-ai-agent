import os
import json
import base64
import requests
import anthropic
from flask import Flask, request, abort
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

# LINE config
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# Anthropic client
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Trigger keywords (only gates TEXT messages in groups)
TRIGGER_KEYWORDS = [k.strip() for k in os.environ.get("TRIGGER_KEYWORDS", "小凡,思凡助理").split(",")]

# Per-user conversation history (in-memory)
conversation_history: dict[str, list] = {}

# Raw webhook pre-parse: text_msg_id → quoted_image_msg_id
# Populated in /callback before SDK processes the event
quoted_image_map: dict[str, str] = {}


def load_system_prompt() -> str:
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "selvans.txt")
    try:
        with open(prompt_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "你是一位友善的 AI 助理，請用繁體中文回答問題。"


SYSTEM_PROMPT = load_system_prompt()


def is_triggered(text: str) -> bool:
    return any(kw in text for kw in TRIGGER_KEYWORDS)


def is_group_source(event: MessageEvent) -> bool:
    return event.source.type in ("group", "room")


def strip_keywords(text: str) -> str:
    """Remove trigger keywords and @mention prefix from user query."""
    import re
    for kw in TRIGGER_KEYWORDS:
        text = text.replace(kw, "")
    text = re.sub(r"@\S+", "", text).strip()
    return text or "請幫我分析這張照片。"


def get_ai_reply(user_id: str, user_message: str) -> str:
    history = conversation_history.setdefault(user_id, [])
    query = strip_keywords(user_message)
    history.append({"role": "user", "content": query})
    if len(history) > 20:
        history = history[-20:]
        conversation_history[user_id] = history
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": reply_text})
    return reply_text


def download_image(message_id: str) -> tuple[bytes, str]:
    """Download image bytes from LINE content API."""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError(f"Not an image content-type: {content_type}")
    return resp.content, content_type


def analyze_image(user_id: str, image_bytes: bytes, media_type: str, query: str) -> str:
    """Send image + question to Claude Vision and return reply."""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": query},
                ],
            }
        ],
    )
    reply_text = response.content[0].text
    # Save text summary to history (avoid storing giant base64)
    history = conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": f"[照片] {query}"})
    history.append({"role": "assistant", "content": reply_text})
    if len(history) > 20:
        conversation_history[user_id] = history[-20:]
    return reply_text


def send_reply(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # ── Pre-parse raw JSON to extract quotedMessageId ─────────────────────────
    # LINE SDK may not expose this field; we read it directly from the payload.
    try:
        payload = json.loads(body)
        for evt in payload.get("events", []):
            msg = evt.get("message", {})
            if msg.get("type") == "text":
                quoted_id = msg.get("quotedMessageId")
                if quoted_id:
                    quoted_image_map[msg["id"]] = quoted_id
    except Exception:
        pass  # Never let pre-parsing break webhook handling

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = event.message.text
    user_id = event.source.user_id

    # In groups: require trigger keyword
    if is_group_source(event) and not is_triggered(user_text):
        return

    # ── Check if this text message is a reply to an image ────────────────────
    # Pop so the dict doesn't grow unbounded
    quoted_id = quoted_image_map.pop(event.message.id, None)

    if quoted_id:
        try:
            image_bytes, media_type = download_image(quoted_id)
            query = strip_keywords(user_text) or "請分析這張照片中植物的病蟲害狀況。"
            reply_text = analyze_image(user_id, image_bytes, media_type, query)
            send_reply(event.reply_token, reply_text)
            return
        except Exception as e:
            # If image download fails, let the user know
            send_reply(event.reply_token, f"抱歉，無法讀取那張照片（可能太舊或不是圖片）。請直接傳照片給我分析！（{type(e).__name__}）")
            return

    # ── Plain text reply ──────────────────────────────────────────────────────
    reply_text = get_ai_reply(user_id, user_text)
    send_reply(event.reply_token, reply_text)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    """User sent a photo directly — always analyze, no keyword needed."""
    user_id = event.source.user_id
    try:
        image_bytes, media_type = download_image(event.message.id)
        query = "請分析這張照片中植物的狀況，判斷是否有病蟲害、營養缺乏或其他問題，並給出具體建議。如果不是植物，請描述你看到的內容並友善回應。"
        reply_text = analyze_image(user_id, image_bytes, media_type, query)
    except Exception as e:
        reply_text = f"抱歉，照片讀取失敗，請再試一次。（{type(e).__name__}）"
    send_reply(event.reply_token, reply_text)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
