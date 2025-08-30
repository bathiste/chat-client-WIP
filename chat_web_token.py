#!/usr/bin/env python3
"""
Chat web app (public, token‑based persistence) – fixed
so the “Enter Chat” button works and the nickname storage uses a
simple sid → name dictionary (compatible with current Flask‑SocketIO).
"""

import os, uuid, sqlite3, time
from datetime import datetime
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

# ----------------------------------------------------------------------
# SQLite helper
# ----------------------------------------------------------------------
DB_FILE = "chat_web_token.db"


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
# Flask + SocketIO
# ----------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, async_mode="eventlet")

# Global dict: socket‑id (sid) → nickname
sid_to_name = {}


# ----------------------------------------------------------------------
# HTML page (all in‑line)
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
        button:disabled{opacity:0.5;}
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
    </div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
    const socket = io();

    function addLine(txt){
        const p = document.createElement('p');
        p.textContent = txt;
        document.getElementById('chat').appendChild(p);
        document.getElementById('chat').scrollTop = document.getElementById('chat').scrollHeight;
    }

    // Enable the button *after* socket connection is ready
    socket.on('connect', ()=>{
        console.log('[socket] connected');
        document.getElementById('enter').disabled = false;
    });

    // -------------------- Register --------------------
    document.getElementById('enter').onclick = function(){
        const nick = document.getElementById('nick').value.trim();
        if(!nick){ alert('Pick a nickname'); return; }
        const storedToken = localStorage.getItem('chatToken');   // may be null
        if(storedToken){
            socket.emit('register', {name:nick, token:storedToken});
        }else{
            socket.emit('register', {name:nick});
        }
    };

    // -------------------- Welcome + token --------------------
    socket.on('welcome', data=>{
        console.log('[socket] welcome', data);
        localStorage.setItem('chatToken', data.token);   // remember for next visit
        document.getElementById('login').style.display = 'none';
        document.getElementById('chatui').style.display = 'block';
        addLine("[INFO] You are known as " + data.name);
    });

    // -------------------- Recent history --------------------
    socket.on('history', lines=>{
        lines.forEach(l=>addLine(l));
    });

    // -------------------- New chat line --------------------
    socket.on('chat_line', line=>addLine(line));

    // -------------------- Send a message --------------------
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
</script>
</body>
</html>
"""

# ----------------------------------------------------------------------
# Socket.IO events
# ----------------------------------------------------------------------
@socketio.on('register')
def ws_register(data):
    """
    Client sends: {name:"nickname", token:"optional‑uuid"}
    Server replies with:
        emit('welcome', {name:<actual_name>, token:<token>})
        emit('history', [ "<msg line>", … ])
    """
    name_requested = data.get('name', '').strip()
    provided_token = data.get('token')

    # --------------------------------------------------------------
    # Decide nickname & token
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # Remember the nickname for this socket (sid → name)
    # --------------------------------------------------------------
    sid_to_name[request.sid] = name

    # --------------------------------------------------------------
    # Send welcome packet
    # --------------------------------------------------------------
    emit('welcome', {'name': name, 'token': token})

    # --------------------------------------------------------------
    # Build recent‑message list in the requested format:
    #   [alice] - 10:42 - hello world!
    # --------------------------------------------------------------
    recent = recent_messages(limit=30)
    lines = []
    for sender_ip, ts, txt in recent:
        # Try to find a nickname that ever used this IP.
        # (The token table holds the current nicknames; historic IP→nick
        # mapping isn’t perfect, but good enough for a demo.)
        # For simplicity we just display the IP if we can’t find a name.
        nickname = get_name_by_token_for_ip(sender_ip) or sender_ip
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{nickname}] - {when} - {txt}")
    emit('history', lines)


def get_name_by_token_for_ip(ip: str):
    """
    Very small helper used only for rendering historic lines.
    It searches the `tokens` table for a nickname that has ever sent a
    message from the given IP.
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
    A client posted a new line.
    Store it, then broadcast it to everybody (including the sender).
    """
    # Which nickname belongs to THIS socket?
    name = sid_to_name.get(request.sid, "anon")
    client_ip = request.remote_addr

    store_message(sender_ip=client_ip, content=text)

    now = datetime.now().strftime("%H:%M")
    line = f"[{name}] - {now} - {text}"
    emit('chat_line', line, broadcast=True)


# ----------------------------------------------------------------------
# Flask route (serves the single page)
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000)
