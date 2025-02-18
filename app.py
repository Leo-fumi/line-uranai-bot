from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
import threading

app = Flask(__name__)

# 環境変数からLINE APIキーを取得
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/", methods=["GET"])
def home():
    return "LINE占いBotは稼働中！"

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    # Webhookの処理を非同期で実行
    threading.Thread(target=handle_webhook, args=(body, signature)).start()

    # すぐに200を返す
    return "OK", 200

def handle_webhook(body, signature):
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    reply = "こんにちは！占いBotです。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

