#!/usr/bin/env python3
"""
Public chat (Flask + SocketIO) – now with image / file uploads.

Features
--------
* Persistent nickname via a UUID token (stored in localStorage)
* All messages are timestamped and shown as:
      [alice] - 10:42 - hello world!
* Users can upload files:
      – Images are shown inline (<img src="…">)
      – Other files appear as a clickable link.
* Files are saved in the local “uploads/” directory and served
  via /uploads/<filename>.
* No IP‑based nickname mapping – each socket‑id has its own name.

Run:
    python chat_web_token_with_uploads.py   # listens on 0.0.0.0:5000
Open a browser:
    http://<HOST_IP_OR_DOMAIN>:5000/
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

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)      # create if it doesn't exist
MAX_CONTENT_LENGTH = 10 * 1024 * 1024   # 10 MiB max per upload

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# --------------------------------------------------------------
# SQLite helpers (messages + tokens)
# --------------------------------------------------------------
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


# --------------------------------------------------------------
# Flask + SocketIO setup
# --------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# simple mapping: socket‑id (sid) → nickname
sid_to_name = {}

# --------------------------------------------------------------
# HTML page (all in one file)
# --------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
    <title>Public Chat + Uploads</title>
    <style>
        body{font-family:Arial,Helvetica,sans-serif;margin:20px;}
        #chat{border:1px solid #ccc;height:400px;overflow-y:auto;padding:5px;}
        #chat p{margin:0;padding:2px;word-break:break-word;}
        #msg{width:70%;}
        button:disabled{opacity:0.5;}
        #uploadSection{margin-top:10px;}
    </style>
</head>
<body>
    <h2>Public Chat</h2>

    <div id="login">
        <input id="nick" placeholder="choose a nickname" autocomplete="off"/>
        <button id="enter" disabled>Enter Chat</button>
    </div>

    <div id="chatui" style="display:none;">
        <div id="chat"></div>

        <input id="msg" autocomplete="off" placeholder="type a message"/>
        <button id="send">Send</button>

        <!-- ----------- upload UI ----------- -->
        <div id="uploadSection">
            <input type="file" id="fileInput"/>
            <button id="uploadBtn">Upload</button>
        </div>
    </div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
    const socket = io();

    // ------------------------------------------------------------------
    // Helper: put a line into the chat window.
    // We use `innerHTML` so that an <img> tag (or a link) renders.
    // ------------------------------------------------------------------
    function addLine(txt){
        const p = document.createElement('p');
        p.innerHTML = txt;          // <-- renders HTML safely (but still raw)
        document.getElementById('chat').appendChild(p);
        document.getElementById('chat').scrollTop =
            document.getElementById('chat').scrollHeight;
    }

    // ------------------------------------------------------------------
    // Enable the “Enter Chat” button only after the socket is ready
    // ------------------------------------------------------------------
    socket.on('connect', ()=>{
        console.log('[socket] connected');
        document.getElementById('enter').disabled = false;
    });

    // ------------------------------------------------------------------
    // Register (nickname + optional token)
    // ------------------------------------------------------------------
    document.getElementById('enter').onclick = function(){
        const nick = document.getElementById('nick').value.trim();
        if(!nick){ alert('Please type a nickname'); return; }
        const storedToken = localStorage.getItem('chatToken');   // may be null
        if(storedToken){
            socket.emit('register', {name:nick, token:storedToken});
        }else{
            socket.emit('register', {name:nick});
        }
    };

    // ------------------------------------------------------------------
    // Server says “welcome” → we store the token and show the UI
    // ------------------------------------------------------------------
    socket.on('welcome', data=>{
        console.log('[socket] welcome', data);
        localStorage.setItem('chatToken', data.token);
        document.getElementById('login').style.display = 'none';
        document.getElementById('chatui').style.display = 'block';
        addLine("[INFO] You are known as " + data.name);
    });

    // ------------------------------------------------------------------
    // Recent history (array of pre‑formatted lines)
    // ------------------------------------------------------------------
    socket.on('history', lines=>{
        lines.forEach(l=>addLine(l));
    });

    // ------------------------------------------------------------------
    // New chat line from anyone (including yourself)
    // ------------------------------------------------------------------
    socket.on('chat_line', line=>addLine(line));

    // ------------------------------------------------------------------
    // Send a text message
    // ------------------------------------------------------------------
    document.getElementById('send').onclick = function(){
        const txt = document.getElementById('msg').value.trim();
        if(!txt){return;}
        socket.emit('msg', txt);
        document.getElementById('msg').value = '';
    };
    document.getElementById('msg').addEventListener('keypress', e=>{
        if(e.key === 'Enter'){
            e.preventDefault();
            document.getElementById('send').click();
        }
    });

    // ------------------------------------------------------------------
    // ----------- FILE UPLOAD ----------
    // ------------------------------------------------------------------
    document.getElementById('uploadBtn').onclick = function(){
        const fileInput = document.getElementById('fileInput');
        if (!fileInput.files.length){
            alert('Select a file first');
            return;
        }
        const file = fileInput.files[0];
        const form = new FormData();
        form.append('file', file);

        fetch('/upload', {
            method: 'POST',
            body: form
        })
        .then(resp => resp.json())
        .then(data => {
            if (data.error){
                alert('Upload error: ' + data.error);
                return;
            }
            // data.url is the public URL of the uploaded file
            // If it is an image we embed it, otherwise we link to it.
            const ext = data.filename.split('.').pop().toLowerCase();
            const imageExts = ['png','jpg','jpeg','gif','webp','bmp'];
            let msg;
            if (imageExts.includes(ext)){
                msg = `<img src="${data.url}" style="max-width:300px;"/>`;
            }else{
                msg = `<a href="${data.url}" target="_blank">`+
                      `${data.filename}</a>`;
            }
            // Broadcast the message as a **regular chat line** so everybody sees it.
            socket.emit('msg', msg);
        })
        .catch(err => {
            console.error('Upload failed', err);
            alert('Upload failed');
        });
    };
</script>
</body>
</html>
"""

# --------------------------------------------------------------
# Socket.IO events
# --------------------------------------------------------------
@socketio.on('register')
def ws_register(data):
    """
    Client --> {name:"nick", token:"optional‑uuid"}
    Server --> welcome + recent history
    """
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')

    # ----- decide nickname & token -----
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

    # ----- remember nickname for this socket -----
    sid_to_name[request.sid] = name

    # ----- welcome packet -----
    emit('welcome', {'name': name, 'token': token})

    # ----- recent messages (already formatted) -----
    recent = recent_messages(limit=30)
    lines = []
    for sender_ip, ts, txt in recent:
        # try to resolve a nickname for the historic IP
        nickname = get_name_by_token_for_ip(sender_ip) or sender_ip
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{nickname}] - {when} - {txt}")
    emit('history', lines)


def get_name_by_token_for_ip(ip: str):
    """
    Very small helper used only to display historic lines.
    It looks for any token that has ever sent a message from the given IP.
    """
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """SELECT DISTINCT token FROM messages WHERE sender_ip = ?""",
        (ip,),
    )
    tokens = [row[0] for row in cur.fetchall()]
    conn.close()
    for token in tokens:
        name = get_name_by_token(token)
        if name:
            return name
    return None


@socketio.on('msg')
def ws_msg(text):
    """
    A client posted a new chat line.
    Store it and broadcast it to everybody.
    """
    name = sid_to_name.get(request.sid, "anon")
    client_ip = request.remote_addr

    store_message(sender_ip=client_ip, content=text)

    now = datetime.now().strftime("%H:%M")
    line = f"[{name}] - {now} - {text}"
    emit('chat_line', line, broadcast=True)


# --------------------------------------------------------------
# ---------- FILE UPLOAD ROUTE ----------
# --------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    """
    Accepts a single file via multipart/form‑data.
    Saves it under uploads/<uuid>_<secure_filename>.
    Returns JSON: {url: "/uploads/<filename>", filename: "<original name>"}
    """
    if "file" not in request.files:
        return jsonify(error="no file part"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="no selected file"), 400

    # Secure the filename and prefix with a random UUID to avoid collisions
    original_name = secure_filename(file.filename)
    ext = Path(original_name).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    save_path = UPLOAD_FOLDER / unique_name

    try:
        file.save(save_path)
    except Exception as e:
        return jsonify(error=str(e)), 500

    # Build the public URL (Flask will serve /uploads/<filename>)
    file_url = f"/uploads/{unique_name}"
    return jsonify(url=file_url, filename=original_name)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    """Serve files from the uploads folder."""
    return send_from_directory(UPLOAD_FOLDER, filename)


# --------------------------------------------------------------
# Flask route – serves the single page
# --------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# --------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000)
