from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "LINE占いBotは稼働中！"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

