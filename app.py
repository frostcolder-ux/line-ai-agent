import os
import base64
import time
import anthropic
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

app = Flask(__name__)

# LINE config
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# Anthropic client
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Trigger keywords
TRIGGER_KEYWORDS = [k.strip() for k in os.environ.get("TRIGGER_KEYWORDS", "小凡,思凡助理").split(",")]

# Per-user conversation history (in-memory)
conversation_history: dict[str, list] = {}

# Track users who recently triggered the bot (for group image handling)
# Format: {user_id: timestamp}
triggered_users: dict[str, float] = {}
TRIGGER_WINDOW_SECONDS = 600  # 10 minutes


# Load system prompt
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


def get_ai_reply(user_id: str, user_message: str) -> str:
    history = conversation_history.setdefault(user_id, [])

    # Strip trigger keywords from the actual query
    query = user_message
    for kw in TRIGGER_KEYWORDS:
        query = query.replace(kw, "").strip()
    if not query:
        query = "你好"

    history.append({"role": "user", "content": query})

    # Keep last 10 turns to avoid token overflow
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


def analyze_image(user_id: str, image_bytes: bytes) -> str:
    """Send image to Claude Vision for plant disease/pest analysis."""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    history = conversation_history.setdefault(user_id, [])

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        },
        {
            "type": "text",
            "text": "請分析這張照片中植物的狀況，判斷是否有病蟲害、營養缺乏或其他問題，並提供具體的建議處理方式。如果照片不是植物，請描述你看到的內容並提供相關協助。",
        },
    ]

    history.append({"role": "user", "content": user_content})

    # Keep last 20 messages
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

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = event.message.text
    user_id = event.source.user_id

    if not is_triggered(user_text):
        return

    # Record that this user triggered the bot (for group image handling)
    triggered_users[user_id] = time.time()

    reply_text = get_ai_reply(user_id, user_text)
    send_reply(event.reply_token, reply_text)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    user_id = event.source.user_id

    # In group/room chats, only respond if user triggered the bot recently
    if is_group_source(event):
        last_trigger = triggered_users.get(user_id, 0)
        if time.time() - last_trigger > TRIGGER_WINDOW_SECONDS:
            return  # Ignore images from non-triggered users in groups

    # Download image content
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(event.message.id)

    reply_text = analyze_image(user_id, image_bytes)
    send_reply(event.reply_token, reply_text)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
