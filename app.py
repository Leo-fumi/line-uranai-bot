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
db_lock = threading.Lock()  # DB操作の排他制御用ロック

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
    """
    1. ユーザー情報（生年月日/生まれた時間/市区町村/氏名）が未完了の場合：
       - 次に入力すべき項目を案内し、入力をそのままDBに保存
       - 全部揃ったら「登録完了」と伝える

    2. ユーザー情報が完了している場合：
       - ユーザーが「占い: 〇〇」と入力 -> 〇〇をトピックとして占い結果を返す
       - それ以外 -> ヘルプ的メッセージを返す
    """
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    user_info = get_user_info(user_id)

    # まだDBにレコードがなければ初期化（すべて "未入力" の状態）
    if not user_info:
        initialize_user_info(user_id)
        user_info = get_user_info(user_id)

    # すべての項目が登録済みかどうか
    if is_user_info_complete(user_info):
        # すべて登録済み → 占いのトピック指定かどうかで分岐
        if user_message.startswith("占い:"):
            topic = user_message.replace("占い:", "").strip()
            if topic:
                # ユーザーが指定したトピックに対して占いを実施
                fortune_text = get_fortune_response(user_info, topic)
                send_long_text(event.reply_token, fortune_text)
                return  # ここで終了
            else:
                reply = "占いたい内容を指定してください。例：「占い: 仕事運」"
        else:
            reply = (
                "占いたい内容を指定してください。\n"
                "例：「占い: 今月の運勢」「占い: 仕事運」「占い: 恋愛運」"
            )
    else:
        # 登録が完了していない場合
        next_field = get_next_missing_field(user_info)
        if user_message.startswith("占い:"):
            reply = f"まだ登録が完了していません。まずは {FIELD_PROMPTS[next_field]}"
        else:
            field_stored, error_msg = store_user_input(user_id, next_field, user_message)
            if not field_stored:
                reply = error_msg
            else:
                user_info = get_user_info(user_id)
                if is_user_info_complete(user_info):
                    reply = (
                        "すべての登録が完了しました！\n"
                        "占いたい内容を指定してください。\n"
                        "例：「占い: 今月の運勢」「占い: 仕事運」「占い: 恋愛運」"
                    )
                else:
                    new_next_field = get_next_missing_field(user_info)
                    reply = FIELD_PROMPTS[new_next_field]

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def initialize_user_info(user_id):
    """レコードがない場合、'未入力' 状態で初期化する"""
    with db_lock:
        conn = sqlite3.connect("users.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("""
            INSERT OR IGNORE INTO users (user_id, birthdate, birthtime, birthplace, name)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, encrypt_data("未入力"), encrypt_data("未入力"), encrypt_data("未入力"), encrypt_data("未入力")))
        conn.commit()
        conn.close()

def is_user_info_complete(user_info):
    """4項目すべてが '未入力' 以外になっていれば完了とみなす"""
    return all(
        user_info[field] != "未入力"
        for field in ["birthdate", "birthtime", "birthplace", "name"]
    )

FIELDS_ORDER = ["birthdate", "birthtime", "birthplace", "name"]
FIELD_PROMPTS = {
    "birthdate": "生年月日を YYYY-MM-DD 形式で入力してください。",
    "birthtime": "生まれた時間を HH:MM 形式で入力してください。",
    "birthplace": "生まれた市区町村を入力してください。",
    "name": "氏名を入力してください。"
}

def get_next_missing_field(user_info):
    """まだ '未入力' のフィールドのうち、先頭のものを返す"""
    for field in FIELDS_ORDER:
        if user_info[field] == "未入力":
            return field
    return None

def store_user_input(user_id, field, value):
    """
    入力された文字列を指定フィールドに保存（バリデーションあり）
    戻り値:
      (True, None)  → 保存成功
      (False, エラーメッセージ) → バリデーション失敗
    """
    value = value.strip()
    if field == "birthdate":
        try:
            datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return (False, "生年月日の形式が正しくありません。YYYY-MM-DD 形式で入力してください。")
    elif field == "birthtime":
        parts = value.split(":")
        if len(parts) != 2:
            return (False, "生まれた時間の形式が正しくありません。HH:MM 形式で入力してください。")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return (False, "時刻が不正です。0〜23時、0〜59分で指定してください。")
        except ValueError:
            return (False, "生まれた時間の形式が正しくありません。HH:MM 形式で入力してください。")

    update_user_info(user_id, field, value)
    return (True, None)

def save_user_info(user_id, birthdate, birthtime, birthplace, name):
    """ユーザー情報をDBに保存（初回または更新）"""
    with db_lock:
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
            encrypt_data(birthtime),
            encrypt_data(birthplace),
            encrypt_data(name)
        ))
        conn.commit()
        conn.close()

def update_user_info(user_id, field, value):
    """指定フィールドをDBで更新する"""
    with db_lock:
        conn = sqlite3.connect("users.db", check_same_thread=False)
        c = conn.cursor()
        c.execute(f"""
            UPDATE users SET {field} = ? WHERE user_id = ?
        """, (encrypt_data(value), user_id))
        conn.commit()
        conn.close()
    
def get_user_info(user_id):
    """DBからユーザー情報を取得し復号化する"""
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
    return None

def split_message(text, max_length=2000):
    """テキストを max_length ごとに分割してリストとして返す"""
    return [text[i:i+max_length] for i in range(0, len(text), max_length)]

def send_long_text(reply_token, text):
    """長文を複数の TextSendMessage として送信する"""
    messages = [TextSendMessage(text=chunk) for chunk in split_message(text)]
    line_bot_api.reply_message(reply_token, messages)

def get_fortune_response(user_info, topic):
    """ユーザー情報とトピックをもとに、詳細な占い結果を生成する"""
    openai.api_key = OPENAI_API_KEY

    # プロンプトを充実させ、複数段落に渡る詳細な占い結果を生成するよう指示
    prompt = f"""
    以下のユーザー情報を基に、西洋占星術、東洋占星術、数秘術、姓名判断（熊崎式姓名判断）を統合的に用いて占ってください。
    占いの結果（X）と西洋占星術の結果（A）、東洋占星術の結果（B）、数秘術（C）、姓名判断（D）は、年齢（a）を使って下記のような数式に従ってください。

    X=(100-a)*A+a*B+10*C+13*D

    なお、占いの結果にはどの占星術を用いたかは含めないでください。

    生年月日: {user_info['birthdate']}
    生まれた時間: {user_info['birthtime']}
    生まれた市区町村: {user_info['birthplace']}
    氏名: {user_info['name']}

    ユーザーは「{topic}」についての占いを希望しています。
    「あなたは今、迷っているようですね。」「あなたにもうすぐ幸運が訪れようとしています」のように読者に語りかけ、まるで目の前で読者を鑑定しているかのような口調で結果を出してください。
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",  # GPT-4利用可能なら "gpt-4" に変更してください
            messages=[
                {"role": "system", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"OpenAI API Error: {e}")
        return "占い結果を取得できませんでした。時間をおいて再度お試しください。"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
