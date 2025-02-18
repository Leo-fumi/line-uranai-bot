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
       - 次に入力すべき項目を案内し、ユーザーからの入力をそのままDBに保存
       - 全部そろったら「登録完了」と伝える

    2. ユーザー情報が完了している場合：
       - ユーザーが「今月の運勢」と入力 -> 占い結果を返す
       - それ以外 -> ヘルプ的メッセージを返す
    """
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    user_info = get_user_info(user_id)

    # まだDBにレコード自体がなければ作っておく（全項目"未入力"の状態）
    if not user_info:
        initialize_user_info(user_id)
        user_info = get_user_info(user_id)

    # すべての項目が登録済みかどうか
    if is_user_info_complete(user_info):
        # すべて登録済み -> 「今月の運勢」かどうかで分岐
        if user_message == "今月の運勢":
            reply = get_fortune_response(user_info)
        else:
            reply = (
                "登録済みです。占いをご希望の場合は「今月の運勢」と入力してください。\n"
                "情報を変更したい場合は、管理者にお問い合わせください。"
            )
    else:
        # まだ登録が完了していない場合
        next_field = get_next_missing_field(user_info)
        # まず「今月の運勢」と言われても、情報が揃っていないなら先に登録案内
        if user_message == "今月の運勢":
            reply = f"まだ登録が完了していません。まずは{FIELD_PROMPTS[next_field]}"
        else:
            # 現在の「次に入力すべきフィールド」に対してバリデーション＋登録
            field_stored, error_msg = store_user_input(user_id, next_field, user_message)
            if not field_stored:
                # バリデーション失敗等の場合
                reply = error_msg
            else:
                # 正しく保存できたら、次の項目を確認
                user_info = get_user_info(user_id)
                if is_user_info_complete(user_info):
                    reply = (
                        "すべての登録が完了しました！\n"
                        "占いをご希望の場合は「今月の運勢」と入力してください。"
                    )
                else:
                    # まだ次がある
                    new_next_field = get_next_missing_field(user_info)
                    reply = FIELD_PROMPTS[new_next_field]

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def initialize_user_info(user_id):
    """まだレコードがない場合、'未入力'状態で作成"""
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
    """まだ '未入力' のフィールドのうち、FIELDS_ORDERで先頭のものを返す"""
    for field in FIELDS_ORDER:
        if user_info[field] == "未入力":
            return field
    return None  # 全部埋まっていれば None

def store_user_input(user_id, field, value):
    """
    ユーザーが入力した文字列を、指定フィールドにバリデーションをかけつつ保存。
    戻り値:
      (True, None)  -> 正常に保存完了
      (False, エラーメッセージ) -> バリデーション失敗など
    """
    value = value.strip()
    if field == "birthdate":
        # YYYY-MM-DD形式かチェック
        try:
            datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return (False, "生年月日の形式が正しくありません。YYYY-MM-DD 形式で入力してください。")
    elif field == "birthtime":
        # HH:MM形式か簡易チェック
        # 厳密にパースしたい場合は datetime.strptime(value, "%H:%M") など
        parts = value.split(":")
        if len(parts) != 2:
            return (False, "生まれた時間の形式が正しくありません。HH:MM 形式で入力してください。")
        # さらに数値として範囲チェックしたければ追加
        # 例: 0 <= hour <= 23, 0 <= minute <= 59
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return (False, "時刻が不正です。0〜23時、0〜59分で指定してください。")
        except ValueError:
            return (False, "生まれた時間の形式が正しくありません。HH:MM 形式で入力してください。")

    # birthplace, name は特にバリデーションなし
    # DBに保存
    update_user_info(user_id, field, value)
    return (True, None)

def save_user_info(user_id, birthdate, birthtime, birthplace, name):
    """ユーザー情報をデータベースに保存 (初回または上書き)"""
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
    """特定のフィールドを更新"""
    with db_lock:
        conn = sqlite3.connect("users.db", check_same_thread=False)
        c = conn.cursor()
        c.execute(f"""
            UPDATE users SET {field} = ? WHERE user_id = ?
        """, (encrypt_data(value), user_id))
        conn.commit()
        conn.close()
    
def get_user_info(user_id):
    """データベースからユーザー情報を取得し、復号化する"""
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

def get_fortune_response(user_info):
    # OpenAI APIキーを設定
    openai.api_key = OPENAI_API_KEY
    
    # プロンプトの組み立て
    prompt = f"""
    以下のユーザー情報を基に占いを行ってください。

    生年月日: {user_info['birthdate']}
    生まれた時間: {user_info['birthtime']}
    生まれた市区町村: {user_info['birthplace']}
    氏名: {user_info['name']}

    西洋占星術、東洋占星術、数秘術、姓名判断（熊崎式姓名判断）を統合的に用いて占ってください。
    占いの結果（X）と西洋占星術の結果（A）、東洋占星術の結果（B）、数秘術（C）、姓名判断（D）は、年齢（a）を使って以下の数式に従ってください。
    
    X = (100-a)*A + a*B + 10*C + 13*D

    なお、占いの結果にはどの占星術を用いたかは含めないでください。
    
    「あなたは今、迷っているようですね。」「あなたにもうすぐ幸運が訪れようとしています」のように読者に語りかけ、
    まるで目の前で読者を鑑定しているかのような口調で結果を出してください。

    今月の運勢のみを表示してください。
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",  # GPT-4を使えるなら "gpt-4"
            messages=[
                {"role": "system", "content": prompt}
            ]
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        # ここで例外をログに出しておくと Render のログでも確認できる
        print(f"OpenAI API Error: {e}")
        return "占い結果を取得できませんでした。時間をおいて再度お試しください。"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
