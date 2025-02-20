import os
import sqlite3
import base64
import hashlib
import datetime
import threading
import requests
import secrets
import logging
from flask import Flask, request, render_template, jsonify, redirect, session, url_for
from openai import OpenAI
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from cryptography.fernet import Fernet

app = Flask(__name__)
# セッション管理用 secret_key を環境変数またはランダム生成
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))

# ログ設定
logging.basicConfig(level=logging.INFO)

# 環境変数から各種キーを取得
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID")
LINE_LOGIN_CHANNEL_SECRET = os.getenv("LINE_LOGIN_CHANNEL_SECRET")
LINE_LOGIN_REDIRECT_URI = os.getenv("LINE_LOGIN_REDIRECT_URI", "https://yourdomain.com/callback")

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

# SQLiteデータベースのセットアップ（排他制御付き）
db_lock = threading.Lock()
with db_lock:
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
        logging.error("Missing X-Line-Signature")
        return "Missing Signature", 400

    try:
        threading.Thread(target=handle_webhook, args=(body, signature)).start()
        return "OK", 200
    except Exception as e:
        logging.exception("Webhook handling error")
        return str(e), 500

def handle_webhook(body, signature):
    try:
        handler.handle(body, signature)
    except Exception as e:
        logging.exception("Error in handler.handle")

# LINE Login用エンドポイント（CSRF対策として state を生成）
@app.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    scope = "openid profile"
    line_auth_url = (
        "https://access.line.me/oauth2/v2.1/authorize"
        f"?response_type=code&client_id={LINE_LOGIN_CHANNEL_ID}"
        f"&redirect_uri={LINE_LOGIN_REDIRECT_URI}&state={state}&scope={scope}"
    )
    return redirect(line_auth_url)

# コールバックエンドポイント：state の検証と例外処理の追加
@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    stored_state = session.get("oauth_state")
    if not code or not state or state != stored_state:
        logging.error("Invalid state or missing code")
        return "認証に失敗しました", 400

    # セッションから state を削除して再利用防止
    session.pop("oauth_state", None)

    try:
        token_url = "https://api.line.me/oauth2/v2.1/token"
        headers = { "Content-Type": "application/x-www-form-urlencoded" }
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LINE_LOGIN_REDIRECT_URI,
            "client_id": LINE_LOGIN_CHANNEL_ID,
            "client_secret": LINE_LOGIN_CHANNEL_SECRET
        }
        token_response = requests.post(token_url, headers=headers, data=data, timeout=10)
        token_response.raise_for_status()
        token_json = token_response.json()
        access_token = token_json.get("access_token")
        if not access_token:
            logging.error("Access token not found in response")
            return "認証に失敗しました", 400
    except Exception as e:
        logging.exception("Error during token retrieval")
        return "認証中にエラーが発生しました", 500

    try:
        profile_url = "https://api.line.me/v2/profile"
        profile_headers = { "Authorization": f"Bearer {access_token}" }
        profile_response = requests.get(profile_url, headers=profile_headers, timeout=10)
        profile_response.raise_for_status()
        profile_json = profile_response.json()
        user_id = profile_json.get("userId")
        if not user_id:
            logging.error("User ID not found in profile response")
            return "ユーザー情報の取得に失敗しました", 400
    except Exception as e:
        logging.exception("Error during profile retrieval")
        return "ユーザー情報の取得中にエラーが発生しました", 500

    session["user_id"] = user_id
    return redirect(url_for("miniapp_form"))

# ミニアプリのフォーム：セッションからユーザーIDを取得
@app.route("/miniapp", methods=["GET"])
def miniapp_form():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("miniapp_form.html", user_id=session["user_id"])

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    if user_message == "登録":
        reply = "こちらのリンクからユーザー情報を登録してください。\nhttps://line-uranai-bot.onrender.com/miniapp"
    elif user_message == "今月の運勢":
        user_info = get_user_info(user_id)
        if user_info:
            reply = get_fortune_response(user_info, topic="今月の運勢")
        else:
            reply = "まずは「登録」と送信し、情報を登録してください。"
    else:
        reply = "「登録」と送信すると、ユーザー情報の登録ページが開きます。"
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    except Exception as e:
        logging.exception("Error sending reply message")

def update_user_info(user_id, field, value):
    try:
        with db_lock:
            conn = sqlite3.connect("users.db", check_same_thread=False)
            c = conn.cursor()
            c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (encrypt_data(value), user_id))
            conn.commit()
            conn.close()
    except Exception as e:
        logging.exception("DB update error")

def get_user_info(user_id):
    try:
        with db_lock:
            conn = sqlite3.connect("users.db", check_same_thread=False)
            c = conn.cursor()
            c.execute("SELECT birthdate, birthtime, birthplace, name FROM users WHERE user_id=?", (user_id,))
            result = c.fetchone()
            conn.close()
        if result:
            return {
                "birthdate": decrypt_data(result[0]),
                "birthtime": decrypt_data(result[1]),
                "birthplace": decrypt_data(result[2]),
                "name": decrypt_data(result[3]),
            }
    except Exception as e:
        logging.exception("Error fetching user info")
    return None

@app.route("/save_user_info", methods=["POST"])
def save_user_info():
    data = request.json
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"status": "error", "message": "ユーザー認証が行われていません"}), 400

    birthdate = data.get("birthdate")
    birthtime = data.get("birthtime")
    birthplace = data.get("birthplace")
    name = data.get("name")
    if not (birthdate and birthtime and birthplace and name):
        return jsonify({"status": "error", "message": "全てのフィールドの入力が必要です"}), 400

    try:
        birthdate_enc = encrypt_data(birthdate)
        birthtime_enc = encrypt_data(birthtime)
        birthplace_enc = encrypt_data(birthplace)
        name_enc = encrypt_data(name)
        with db_lock:
            conn = sqlite3.connect("users.db", check_same_thread=False)
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO users (user_id, birthdate, birthtime, birthplace, name)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, birthdate_enc, birthtime_enc, birthplace_enc, name_enc))
            conn.commit()
            conn.close()
    except Exception as e:
        logging.exception("Error saving user info")
        return jsonify({"status": "error", "message": "ユーザー情報の保存中にエラーが発生しました"}), 500

    return jsonify({"status": "success", "message": "ユーザー情報を保存しました"})

def split_message(text, max_length=2000):
    """テキストを max_length ごとに分割してリストとして返す"""
    return [text[i:i+max_length] for i in range(0, len(text), max_length)]

def send_long_text(reply_token, text):
    """長文を複数の TextSendMessage として送信する"""
    messages = [TextSendMessage(text=chunk) for chunk in split_message(text)]
    line_bot_api.reply_message(reply_token, messages)

def get_fortune_response(user_info, topic):
    """ユーザー情報とトピックをもとに、詳細な占い結果を生成する"""
    #openai.api_key = OPENAI_API_KEY
    client = OpenAI(api_key=OPENAI_API_KEY)

    # プロンプトを充実させ、複数段落に渡る詳細な占い結果を生成するよう指示
    prompt = f"""
        【役割定義】
        あなたは占いの専門家です。利用者の生年月日、出生時間、市区町村、氏名という個人情報をもとに、
        西洋占星術、東洋占星術、数秘術、姓名判断（熊崎式）を統合して鑑定します。ただし、どの占術を用いたかは出力しないでください。
        
        【タスク記述】
        以下のアルゴリズム（X = (100-a)A + aB + 10C + 13D、ここで a は年齢、A～D は各占い結果の数値）
        に基づき、運勢の総合数値を算出した上で、利用者に向けた具体的な占い結果を生成してください。
        数値計算の過程や各占星術の内訳は出力せず、占い結果というナラティブな文章にのみ反映してください。
        
        【出力制約】
        ・出力は日本語で、親しみやすく、かつ具体的に占い結果を伝えること。
        ・以下のような構造（例）を参考に、見出しやセクションを含む詳細な文章とすること。
        
        ＜出力例＞
        まず、あなたの運気の流れを見ていきましょう。あなたの生年月日から導き出したエネルギーの流れを総合的に計算し、今後の方向性を探ります。
        
        あなたは今、大きな変化の中にいるようですね。2024年6月から2025年2月にかけて、あなたの運気は「安定と挑戦が交錯する時期」です。流れに身を任せるだけではなく、自ら新しい扉を開く勇気が必要になりそうです。
        
        🔮 これからの運勢 🔮
        
        仕事・キャリア
        あなたの知識や経験が評価されるタイミングが訪れます。時には大胆な選択が新たなチャンスを呼びます。
        
        人間関係
        信頼できる人との出会いや、共感できる価値観の共有が今後の人生に大きな影響を与えるでしょう。
        
        金運
        収入は安定する一方で出費が増えやすい時期です。計画的な判断が求められます。
        
        健康
        体調管理に注意し、ストレス解消や適度な休養を心がける必要があります。
        
        💡 これからの指針 💡
        あなたは今、「選択の年」に差し掛かっています。自分の直感を信じ、焦らずに次のステージへと歩みを進めてください。特に2025年初頭の決断が今後に大きく影響するでしょう。
        
        最後に、あなたは次のステージへ進む準備が整っているのです。自信を持って進んでいきましょう。✨
        
        【入力パラメータ】
        生年月日： {user_info['birthdate']}
        生まれた時間： {user_info['birthtime']}
        生まれた市区町村： {user_info['birthplace']}
        氏名： {user_info['name']}
        希望テーマ： {topic}
        
        【注意事項】
        ・計算過程や各占術の詳細は出力しない。
        ・「計算」という言葉を使わない。
        ・利用者に語りかけるような、親しみと説得力ある口調で記述すること。
        """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # GPT-4利用可能なら "gpt-4" に変更してください
            messages=[
                {"role": "system", "content": "あなたは占いの専門家で、あらゆる占いに精通しています。運勢を占ってください"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,         # 応答内容の一貫性・安定性向上
            top_p=0.9,
            max_tokens=1500,
            frequency_penalty=0.2,
            presence_penalty=0.1
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"OpenAI API Error: {e}")
        return "占い結果を取得できませんでした。時間をおいて再度お試しください。"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
