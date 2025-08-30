#!/usr/bin/env python3
"""
Public chat with Flask + SocketIO
Features: nickname, secret/public tokens, file uploads, image preview, voice messages, admin token list.
"""

import os, uuid, sqlite3, time
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    render_template_string,
    request,
    jsonify,
    send_from_directory,
    abort
)
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_CONTENT_LENGTH = 20 * 1024 * 1024   # 20 MB per upload
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".webm", ".mp3", ".wav"}
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")

# ----------------------------------------------------------------------
# SQLite helpers
# ----------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_ip TEXT NOT NULL,
                       ts        REAL NOT NULL,
                       content   TEXT NOT NULL,
                       token     TEXT
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tokens(
                       token        TEXT PRIMARY KEY,
                       name         TEXT NOT NULL,
                       public_token TEXT NOT NULL
                   )""")
    conn.commit()
    conn.close()


def store_message(sender_ip: str, content: str, token: str = None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (sender_ip, ts, content, token) VALUES (?,?,?,?)",
        (sender_ip, time.time(), content, token),
    )
    conn.commit()
    conn.close()


def recent_messages(limit: int = 30):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """SELECT sender_ip, ts, content, token
           FROM messages
           ORDER BY ts DESC
           LIMIT ?""",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return rows


def get_name_by_token(token: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_token_name(token: str, name: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    public_token = str(uuid.uuid4())[:8]
    cur.execute(
        "INSERT OR REPLACE INTO tokens (token, name, public_token) VALUES (?,?,?)",
        (token, name, public_token),
    )
    conn.commit()
    conn.close()


def get_public_token(name: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE name=?", (name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ----------------------------------------------------------------------
# Flask + SocketIO setup
# ----------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# Map socket-id → nickname & token
sid_to_name = {}
sid_to_token = {}

# ----------------------------------------------------------------------
# HTML/JS UI (unchanged except truncated for brevity)
# ----------------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html><head><title>Chat with Voice</title></head><body>
<p>Chat client UI here (truncated, same as before)</p>
</body></html>
"""

# ----------------------------------------------------------------------
# Socket.IO events
# ----------------------------------------------------------------------
@socketio.on('register')
def ws_register(data):
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')

    if provided_token:
        stored_name = get_name_by_token(provided_token)
        if stored_name:
            name = stored_name
            token = provided_token
        else:
            name = name_requested or "anon"
            token = str(uuid.uuid4())
            set_token_name(token, name)
    else:
        name = name_requested or "anon"
        token = str(uuid.uuid4())
        set_token_name(token, name)

    sid_to_name[request.sid] = name
    sid_to_token[request.sid] = token
    public_token = get_public_token(name)

    emit('welcome', {'name': name, 'token': token, 'public_token': public_token})

    recent = recent_messages(limit=30)
    lines=[]
    for sender_ip, ts, txt, tok in recent:
        nickname = get_name_by_token(tok) or sender_ip
        pub = get_public_token(nickname) or "?"
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"<span class='user' data-pub='{pub}'>{nickname}</span> - {when} - {txt}")
    emit('history', lines)


@socketio.on('msg')
def ws_msg(text):
    name = sid_to_name.get(request.sid, "anon")
    token = sid_to_token.get(request.sid)
    client_ip = request.remote_addr
    store_message(sender_ip=client_ip, content=text, token=token)
    pub = get_public_token(name) or "?"
    now = datetime.now().strftime("%H:%M")
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    emit('chat_line', line, broadcast=True)


# ----------------------------------------------------------------------
# File upload
# ----------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify(error="no file part"), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="no selected file"), 400

    original_name = secure_filename(file.filename)
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error="disallowed type"), 400

    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    save_path = UPLOAD_FOLDER / unique_name
    try:
        file.save(save_path)
    except Exception as e:
        return jsonify(error=str(e)), 500

    file_url = f"/uploads/{unique_name}"
    return jsonify(url=file_url, filename=original_name)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ----------------------------------------------------------------------
# Admin route: list all tokens and IPs
# ----------------------------------------------------------------------
@app.route("/tokens")
def list_tokens():
    password = request.args.get("pass")
    if password != ADMIN_PASSWORD:
        abort(403)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name, token, public_token FROM tokens")
    tokens = cur.fetchall()
    cur.execute("SELECT DISTINCT sender_ip, token FROM messages")
    ips = cur.fetchall()
    conn.close()

    # Map tokens to IPs
    token_to_ips = {}
    for ip, tok in ips:
        token_to_ips.setdefault(tok, []).append(ip)

    result = []
    for name, secret, public in tokens:
        result.append({
            "name": name,
            "secret": secret,
            "public": public,
            "ips": token_to_ips.get(secret, [])
        })
    return jsonify(result)


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
