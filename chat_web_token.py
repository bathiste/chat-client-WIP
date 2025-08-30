#!/usr/bin/env python3
"""
Public chat (Flask + SocketIO) – now works on Render/Fly.io/etc.
Features: nickname token, file uploads, image preview.
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
)
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)   # <‑‑ create if missing

MAX_CONTENT_LENGTH = 10 * 1024 * 1024   # 10 MiB per upload
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# ----------------------------------------------------------------------
# SQLite helpers (messages + tokens)
# ----------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_ip TEXT NOT NULL,
                       ts        REAL NOT NULL,
                       content   TEXT NOT NULL
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tokens(
                       token TEXT PRIMARY KEY,
                       name  TEXT NOT NULL
                   )""")
    conn.commit()
    conn.close()


def store_message(sender_ip: str, content: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (sender_ip, ts, content) VALUES (?,?,?)",
        (sender_ip, time.time(), content),
    )
    conn.commit()
    conn.close()


def recent_messages(limit: int = 30):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """SELECT sender_ip, ts, content
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
    cur.execute(
        "INSERT OR REPLACE INTO tokens (token, name) VALUES (?,?)",
        (token, name),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# Flask + SocketIO setup
# ----------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# Map socket‑id (sid) → nickname
sid_to_name = {}

# ----------------------------------------------------------------------
# HTML page (all in one file)
# ----------------------------------------------------------------------
INDEX_HTML = """< ![... same as before …] """   # (omitted for brevity – use the same HTML from the previous answer)

# ----------------------------------------------------------------------
# Socket.IO events
# ----------------------------------------------------------------------
@socketio.on('register')
def ws_register(data):
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')

    # ---- decide nickname & token ----
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

    # ---- remember nickname for this socket ----
    sid_to_name[request.sid] = name

    # ---- welcome packet ----
    emit('welcome', {'name': name, 'token': token})

    # ---- recent history (formatted) ----
    recent = recent_messages(limit=30)
    lines = []
    for sender_ip, ts, txt in recent:
        nickname = get_name_by_token_for_ip(sender_ip) or sender_ip
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{nickname}] - {when} - {txt}")
    emit('history', lines)


def get_name_by_token_for_ip(ip: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT token FROM messages WHERE sender_ip = ?""", (ip,))
    tokens = [row[0] for row in cur.fetchall()]
    conn.close()
    for token in tokens:
        name = get_name_by_token(token)
        if name:
            return name
    return None


@socketio.on('msg')
def ws_msg(text):
    name = sid_to_name.get(request.sid, "anon")
    client_ip = request.remote_addr
    store_message(sender_ip=client_ip, content=text)

    now = datetime.now().strftime("%H:%M")
    line = f"[{name}] - {now} - {text}"
    emit('chat_line', line, broadcast=True)


# ----------------------------------------------------------------------
# File‑upload route
# ----------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify(error="no file part"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="no selected file"), 400

    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    save_path = UPLOAD_FOLDER / unique_name
    try:
        file.save(save_path)
    except Exception as e:
        return jsonify(error=str(e)), 500

    file_url = f"/uploads/{unique_name}"
    print(f"[UPLOAD] saved {original_name} as {unique_name}")
    return jsonify(url=file_url, filename=original_name)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ----------------------------------------------------------------------
# Index route (serves the single‑page app)
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ----------------------------------------------------------------------
# Entry point – bind to the port that Render gives us
# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    print("[*] Starting chat server")
    print(f"[*] Listening on port {port}")
    socketio.run(app, host="0.0.0.0", port=port)
