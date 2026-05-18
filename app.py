import os
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
from linebot.v3.webhooks import MessageEvent, TextMessageContent

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

    reply_text = get_ai_reply(user_id, user_text)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
