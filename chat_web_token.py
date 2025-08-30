#!/usr/bin/env python3
"""
chat_web_token.py – Public‑facing web chat (Flask + SocketIO)

* Users register with a nickname.
* The server returns a **token** (UUID) that the browser stores in localStorage.
* On every reconnection the client sends the token, so the nickname is remembered.
* No IP‑based lookup – the only permanent mapping is token → nickname.
* All messages are stored in SQLite (so a newcomer sees recent history).

Run:
    python chat_web_token.py          # listens on 0.0.0.0:5000
Open a browser:
    http://<YOUR_PUBLIC_IP_OR_DOMAIN>:5000/
"""

import os
import uuid
import sqlite3
import time
from datetime import datetime

from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

# ----------------------------------------------------------------------
# SQLite helper (messages + tokens)
# ----------------------------------------------------------------------
DB_FILE = "chat_web_token.db"


def init_db():
    """Create the tables if they do not exist yet."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # ------------------------------------------------------------------
    # Messages table – stores every public chat line
    # ------------------------------------------------------------------
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_ip TEXT NOT NULL,
                       ts        REAL NOT NULL,
                       content   TEXT NOT NULL
                   )""")
    # ------------------------------------------------------------------
    # Tokens table – maps a persistent UUID to a nickname
    # ------------------------------------------------------------------
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
    """Return the newest N messages as (sender_ip, ts, content)."""
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
    rows.reverse()                # oldest → newest
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
socketio = SocketIO(app, async_mode="eventlet")

# ----------------------------------------------------------------------
# HTML page (very small, everything lives here)
# ----------------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
    <title>Public Chat</title>
    <style>
        body{font-family:Arial,Helvetica,sans-serif;margin:20px;}
        #chat{border:1px solid #ccc;height:400px;overflow-y:auto;padding:5px;}
        #chat p{margin:0;padding:2px;}
        #msg{width:80%;}
    </style>
</head>
<body>
    <h2>Public Chat</h2>

    <!-- Nickname entry – shown only the first time -->
    <div id="login">
        <input id="nick" placeholder="choose a nickname" autocomplete="off"/>
        <button id="enter">Enter Chat</button>
    </div>

    <!-- The chat UI – hidden until we have a nickname -->
    <div id="chatui" style="display:none;">
        <div id="chat"></div>
        <input id="msg" autocomplete="off" placeholder="type a message"/>
        <button id="send">Send</button>
    </div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
    const socket = io();

    // ------------------------------------------------------------------
    // Helper: write a line into the chat area
    // ------------------------------------------------------------------
    function addLine(txt){
        const p = document.createElement('p');
        p.textContent = txt;
        document.getElementById('chat').appendChild(p);
        document.getElementById('chat').scrollTop = document.getElementById('chat').scrollHeight;
    }

    // ------------------------------------------------------------------
    // 1️⃣  Login flow – ask for nickname, send REGISTER (with optional token)
    // ------------------------------------------------------------------
    document.getElementById('enter').onclick = function(){
        const nick = document.getElementById('nick').value.trim();
        if(!nick){return;}
        const storedToken = localStorage.getItem('chatToken');   // may be null
        if(storedToken){
            socket.emit('register', {name:nick, token:storedToken});
        }else{
            socket.emit('register', {name:nick});
        }
    };

    // ------------------------------------------------------------------
    // 2️⃣  Server replies with WELCOME (name + token)
    // ------------------------------------------------------------------
    socket.on('welcome', data=>{
        // data = {name: "bathist", token:"1234‑abcd"}
        localStorage.setItem('chatToken', data.token);   // remember it for next visit
        document.getElementById('login').style.display = 'none';
        document.getElementById('chatui').style.display = 'block';
        addLine("[INFO] You are known as " + data.name);
    });

    // ------------------------------------------------------------------
    // 3️⃣  Server sends recent history (array of lines)
    // ------------------------------------------------------------------
    socket.on('history', lines=>{
        lines.forEach(l=>addLine(l));
    });

    // ------------------------------------------------------------------
    // 4️⃣  New chat lines from everybody (including yourself)
    // ------------------------------------------------------------------
    socket.on('chat_line', line=>{
        addLine(line);
    });

    // ------------------------------------------------------------------
    // 5️⃣  Send a new message
    // ------------------------------------------------------------------
    document.getElementById('send').onclick = function(){
        const txt = document.getElementById('msg').value.trim();
        if(!txt){return;}
        socket.emit('msg', txt);
        document.getElementById('msg').value = '';
    };
    // also send on Enter key
    document.getElementById('msg').addEventListener('keypress', e=>{
        if(e.key==='Enter'){
            e.preventDefault();
            document.getElementById('send').click();
        }
    });
</script>
</body>
</html>
"""

# ----------------------------------------------------------------------
# Socket.IO events
# ----------------------------------------------------------------------
@socketio.on('connect')
def ws_connect():
    # Nothing special – just wait for the client to register
    pass


@socketio.on('register')
def ws_register(data):
    """
    Client sends:
        { name: "chosen nickname", token: "optional‑uuid" }
    Server replies with:
        emit('welcome', { name: <actual name>, token: <token to store> })
        emit('history', [ "<msg line>", … ])   # recent public messages
    """
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')

    # --------------------------------------------------------------
    # Decide which nickname to use and which token to return
    # --------------------------------------------------------------
    if provided_token:
        stored_name = get_name_by_token(provided_token)
        if stored_name:
            name = stored_name
            token = provided_token            # keep the old token
        else:
            # unknown token → treat as a fresh registration
            name = name_requested or "anon"
            token = str(uuid.uuid4())
            set_token_name(token, name)
    else:
        name = name_requested or "anon"
        token = str(uuid.uuid4())
        set_token_name(token, name)

    # --------------------------------------------------------------
    # Send the acknowledgement + recent chat history
    # --------------------------------------------------------------
    emit('welcome', {'name': name, 'token': token})

    # Build recent messages in the required format:
    #   [alice] - 10:42 - hello world!
    recent = recent_messages(limit=30)
    lines = []
    for sender_ip, ts, txt in recent:
        # look up the name that belongs to the sender_ip
        sender_name = get_name_by_token_for_ip(sender_ip) or sender_ip
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{sender_name}] - {when} - {txt}")
    emit('history', lines)


def get_name_by_token_for_ip(ip: str) -> str | None:
    """
    Helper used only for displaying historic lines.
    It looks up the nickname that belongs to the *most recent* token used
    by this IP (if any).  If none is found we just return the raw IP.
    """
    # We have no direct IP→token mapping, but we can scan the tokens table:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT token FROM tokens")
    tokens = [row[0] for row in cur.fetchall()]
    conn.close()

    # Very cheap linear search – the DB is tiny for a demo.
    # Each token row already stores the nickname, so we can match the IP
    # by searching the messages table for the most recent row that used that IP.
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    for token in tokens:
        cur.execute(
            """SELECT sender_ip FROM messages
               WHERE sender_ip = ?
               ORDER BY ts DESC LIMIT 1""",
            (ip,),
        )
        if cur.fetchone():
            # we found at least one message from this IP; fetch its nickname
            cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
            row = cur.fetchone()
            if row:
                conn.close()
                return row[0]
    conn.close()
    return None


@socketio.on('msg')
def ws_msg(text):
    """
    A client sent a new chat line.
    We store it, then broadcast it to everybody (including the sender).
    """
    # The socket.io library gives us a `request.sid` that uniquely
    # identifies the connection.  We stored the nickname in a
    # side‑channel (see `register`) – retrieve it from the session dict.
    if 'nickname' not in request.namespace:
        # safety net – a client should have registered first.
        return
    name = request.namespace['nickname']
    client_ip = request.remote_addr

    # store in the DB (so newcomers can see it later)
    store_message(sender_ip=client_ip, content=text)

    now = datetime.now().strftime("%H:%M")
    line = f"[{name}] - {now} - {text}"
    emit('chat_line', line, broadcast=True)


# ----------------------------------------------------------------------
# Keep track of the nickname that belongs to each Socket.IO session
# ----------------------------------------------------------------------
@socketio.on('register')
def remember_nickname(data):
    """
    This second handler runs *after* the one above.  Flask‑SocketIO
    calls all handlers that match the same event name, in the order they
    were registered.  Here we simply stash the nickname in the session
    dict so the ``msg`` handler can retrieve it later.
    """
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')
    if provided_token:
        stored_name = get_name_by_token(provided_token)
        request.namespace['nickname'] = stored_name or name_requested
    else:
        request.namespace['nickname'] = name_requested


# ----------------------------------------------------------------------
# Flask route – serve the single HTML page
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    # Change the port if you want (e.g. 8080, 443, …)
    socketio.run(app, host="0.0.0.0", port=5000)
