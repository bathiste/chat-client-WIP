#!/usr/bin/env python3
"""
chat_web.py – Public website + real‑time chat (WebSocket) using
the same SQLite‑backed logic that the simple chat_relay script uses.

Requirements (install once):
    pip install flask flask-socketio eventlet

Run:
    python chat_web.py               # listens on 0.0.0.0:5000
    # or choose another port:
    python chat_web.py --port 8080

Then open a browser on any device (inside or outside your LAN) and go to:
    http://<HOST_IP>:5000/
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime

from flask import Flask, render_template_string, request, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room

# ----------------------------------------------------------------------
# ---- SQLite persistence (same format as the original chat_relay) -----
# ----------------------------------------------------------------------
DB_FILE = "chat_web.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
                       ip   TEXT PRIMARY KEY,
                       name TEXT NOT NULL
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_ip TEXT NOT NULL,
                       receiver  TEXT NOT NULL,
                       ts        REAL NOT NULL,
                       content   TEXT NOT NULL
                   )""")
    conn.commit()
    conn.close()


def get_name_by_ip(ip):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE ip=?", (ip,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_name_by_ip(ip, name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (ip, name) VALUES (?,?)", (ip, name))
    conn.commit()
    conn.close()


def store_message(sender_ip, receiver, content):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (sender_ip, receiver, ts, content) VALUES (?,?,?,?)",
        (sender_ip, receiver, time.time(), content),
    )
    conn.commit()
    conn.close()


def recent_public(limit=30):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """SELECT sender_ip, ts, content
           FROM messages
           WHERE receiver = ?
           ORDER BY ts DESC
           LIMIT ?""",
        ("PUBLIC", limit),
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return rows


# ----------------------------------------------------------------------
# Flask + SocketIO setup
# ----------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)               # needed for Flask sessions
socketio = SocketIO(app, async_mode="eventlet")   # eventlet works out‑of‑the‑box

# ----------------------------------------------------------------------
# HTML template (very small, no external CSS/JS)
# ----------------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
    <title>Public Chat</title>
    <style>
        body {font-family:Arial,Helvetica,sans-serif; margin:20px;}
        #chat {border:1px solid #ccc; height:400px; overflow-y:scroll; padding:5px;}
        #chat p {margin:0; padding:2px;}
        #msg {width:80%;}
    </style>
</head>
<body>
    {% if not session.get('name') %}
        <h2>Enter a nickname</h2>
        <form action="{{ url_for('set_name') }}" method="post">
            <input type="text" name="nickname" placeholder="your name" required>
            <button type="submit">Enter chat</button>
        </form>
    {% else %}
        <h2>Welcome, {{ session['name'] }}! <a href="{{ url_for('logout') }}">(change name)</a></h2>
        <div id="chat"></div>
        <input id="msg" autocomplete="off" placeholder="type a message"/>
        <button id="send">Send</button>

        <script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
        <script type="text/javascript">
            var socket = io();
            var chatDiv = document.getElementById('chat');
            var msgInput = document.getElementById('msg');
            var sendBtn  = document.getElementById('send');

            // scroll helper
            function scrollDown() {
                chatDiv.scrollTop = chatDiv.scrollHeight;
            }

            // receive a new chat line from the server
            socket.on('chat_line', function(line){
                var p = document.createElement('p');
                p.textContent = line;
                chatDiv.appendChild(p);
                scrollDown();
            });

            // on load, request the recent history
            socket.emit('request_history');

            // send a message when the button is pressed or Enter is hit
            function sendMessage(){
                var txt = msgInput.value.trim();
                if (txt.length===0) return;
                socket.emit('client_message', txt);
                msgInput.value = '';
            }

            sendBtn.onclick = sendMessage;
            msgInput.addEventListener('keypress', function(e){
                if (e.key === 'Enter') { e.preventDefault(); sendMessage(); }
            });
        </script>
    {% endif %}
</body>
</html>
"""

# ----------------------------------------------------------------------
# Flask routes
# ----------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)


@app.route("/set_name", methods=["POST"])
def set_name():
    nick = request.form.get("nickname", "").strip()
    if nick:
        session["name"] = nick
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ----------------------------------------------------------------------
# SocketIO events
# ----------------------------------------------------------------------
@socketio.on("connect")
def ws_connect():
    # When a new browser connects we give it the nickname stored in the Flask session
    nickname = session.get("name", "anon")
    # Store the client’s IP → nickname mapping in the DB (so we can reuse it later)
    client_ip = request.remote_addr
    stored = get_name_by_ip(client_ip)
    if stored:
        # we already know this IP – override the nickname with the persisted one
        session["name"] = stored
    else:
        set_name_by_ip(client_ip, nickname)

    # Optional: let the client know it is connected
    emit("chat_line", f"[INFO] You are connected as {session['name']}")


@socketio.on("request_history")
def ws_history():
    """Send the last N public messages to the newly‑connected client."""
    rows = recent_public(limit=30)
    for ip, ts, txt in rows:
        sender = get_name_by_ip(ip) or ip
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        line = f"[{sender}] - {when} - {txt}"
        emit("chat_line", line)


@socketio.on("client_message")
def ws_message(text):
    """A browser sent a new chat line."""
    nickname = session.get("name", "anon")
    client_ip = request.remote_addr

    # 1️⃣ Store the message (so it appears later for newcomers)
    store_message(sender_ip=client_ip, receiver="PUBLIC", content=text)

    # 2️⃣ Broadcast to **all** connected browsers (including the sender)
    when = datetime.now().strftime("%H:%M")
    line = f"[{nickname}] - {when} - {text}"
    emit("chat_line", line, broadcast=True)


# ----------------------------------------------------------------------
# Command‑line entry point
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Public chat website (Flask + SocketIO)")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="TCP port (default 5000)")
    args = parser.parse_args()

    init_db()
    print(f"[+] Starting web chat on http://{args.host}:{args.port}")
    # eventlet’s built‑in server is good enough for a demo
    socketio.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
