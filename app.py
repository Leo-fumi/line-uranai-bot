import datetime
import openai
import os
import sqlite3
from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import threading
from cryptography.fernet import Fernet
import base64
import hashlib

app = Flask(__name__)

# 環境変数からAPIキーを取得
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    raise ValueError("暗号化キー（ENCRYPTION_KEY）が設定されていません！")

# AESキーを作成
def generate_key(secret_key):
    key = hashlib.sha256(secret_key.encode()).digest()
    return base64.urlsafe_b64encode(key)

cipher = Fernet(generate_key(ENCRYPTION_KEY))

# 暗号化・復号化関数
def encrypt_data(data):
    if not data:
        return None
    return cipher.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return None
    return cipher.decrypt(encrypted_data.encode()).decode()

# SQLiteデータベースのセットアップ
conn = sqlite3.connect("users.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        birthdate TEXT,
        birthtime TEXT,
        birthplace TEXT,
        name TEXT
    )
""")
conn.commit()
conn.close()

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/", methods=["GET"])
def home():
    return "LINE占いBotは稼働中！"

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    if not signature:
        return "Missing Signature", 400

    try:
        threading.Thread(target=handle_webhook, args=(body, signature)).start()
        return "OK", 200
    except Exception as e:
        return str(e), 500

def handle_webhook(body, signature):
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    if user_message.startswith("生年月日"):
        birthdate = user_message.replace("生年月日 ", "").strip()
        try:
            birthdate_obj = datetime.datetime.strptime(birthdate, "%Y-%m-%d").date()
            save_user_info(user_id, birthdate_obj, None, None, None)
            reply = "生まれた時間を入力してください（任意）。例: 14:30"
        except ValueError:
            reply = "生年月日の形式が正しくありません。例: 生年月日 1990-05-15"
    elif user_message.startswith("生まれた時間"):
        birthtime = user_message.replace("生まれた時間 ", "").strip()
        update_user_info(user_id, "birthtime", birthtime)
        reply = "生まれた市区町村を入力してください（任意）。例: 東京都渋谷区"
    elif user_message.startswith("生まれた市区町村"):
        birthplace = user_message.replace("生まれた市区町村 ", "").strip()
        update_user_info(user_id, "birthplace", birthplace)
        reply = "氏名を入力してください（任意）。例: 山田太郎"
    elif user_message.startswith("氏名"):
        name = user_message.replace("氏名 ", "").strip()
        update_user_info(user_id, "name", name)
        reply = "情報を登録しました。今月の運勢を知りたい場合は「今月の運勢」と送信してください。"
    elif user_message == "今月の運勢":
        user_info = get_user_info(user_id)
        if user_info:
            reply = get_fortune_response(user_info)
        else:
            reply = "まずは生年月日を登録してください。「生年月日 YYYY-MM-DD」と送信してください。"
    else:
        reply = "「生年月日 YYYY-MM-DD」と送信してください。"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def update_user_info(user_id, field, value):
    """データベース内の特定のフィールドを更新"""
    conn = sqlite3.connect("users.db", check_same_thread=False)
    c = conn.cursor()
    c.execute(f"""
        UPDATE users SET {field} = ? WHERE user_id = ?
    """, (encrypt_data(value), user_id))
    conn.commit()
    conn.close()

def save_user_info(user_id, birthdate, birthtime, birthplace, name):
    """ユーザー情報をデータベースに保存"""
    conn = sqlite3.connect("users.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, birthdate, birthtime, birthplace, name)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            birthdate=excluded.birthdate,
            birthtime=excluded.birthtime,
            birthplace=excluded.birthplace,
            name=excluded.name
    """, (
        user_id,
        encrypt_data(str(birthdate)),
        encrypt_data(birthtime) if birthtime else None,
        encrypt_data(birthplace) if birthplace else None,
        encrypt_data(name) if name else None
    ))
    conn.commit()
    conn.close()

from flask import send_file

@app.route("/download-db", methods=["GET"])
def download_db():
    return send_file("users.db", as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
