#!/usr/bin/env python3
import os, uuid, sqlite3, time
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template_string, request, jsonify, abort, redirect, url_for
)
from flask_socketio import SocketIO, emit, join_room as sio_join, leave_room as sio_leave

# ----------------------- CONFIG ---------------------------------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_CONTENT_LENGTH = 20 * 1024 * 1024
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")

# ----------------------- DB HELPERS -----------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS messages(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       sender_ip TEXT NOT NULL,
                       ts REAL NOT NULL,
                       content TEXT NOT NULL,
                       token TEXT,
                       room_code TEXT
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tokens(
                       token TEXT PRIMARY KEY,
                       name TEXT NOT NULL,
                       public_token TEXT NOT NULL
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS rooms(
                       code TEXT PRIMARY KEY,
                       name TEXT NOT NULL,
                       host_token TEXT NOT NULL,
                       created_ts REAL NOT NULL
                   )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS banned(token TEXT PRIMARY KEY)""")
    conn.commit()
    conn.close()

def store_message(sender_ip: str, content: str, token: str = None, room_code: str = None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (sender_ip, ts, content, token, room_code) VALUES (?,?,?,?,?)",
        (sender_ip, time.time(), content, token, room_code),
    )
    conn.commit()
    conn.close()

def recent_messages(limit: int = 50, room_code: str = None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if room_code:
        cur.execute(
            "SELECT sender_ip, ts, content, token FROM messages WHERE room_code=? ORDER BY ts DESC LIMIT ?",
            (room_code, limit),
        )
    else:
        cur.execute(
            "SELECT sender_ip, ts, content, token FROM messages ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return rows

def get_name_by_token(token: str):
    if not token:
        return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_token_name(token: str, name: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    if row:
        public_token = row[0]
        cur.execute("UPDATE tokens SET name=? WHERE token=?", (name, token))
    else:
        public_token = str(uuid.uuid4())[:8]
        cur.execute(
            "INSERT OR REPLACE INTO tokens (token, name, public_token) VALUES (?,?,?)",
            (token, name, public_token),
        )
    conn.commit()
    conn.close()

def get_public_token_by_token(token: str):
    if not token:
        return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def create_room_in_db(code: str, name: str, host_token: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rooms (code, name, host_token, created_ts) VALUES (?,?,?,?)",
        (code, name, host_token, time.time()),
    )
    conn.commit()
    conn.close()

def room_exists(code: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT code FROM rooms WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def is_banned(token: str):
    if not token:
        return False
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT token FROM banned WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

# ----------------------- APP SETUP -----------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# Live maps
sid_to_name = {}
sid_to_token = {}
sid_to_room = {}

# ----------------------- UTIL ----------------------------------------
def get_client_ip():
    xff = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr

def last_non_anon_name_for_ip(ip: str):
    """Find the most recent non-anon username that posted from this IP."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.name
        FROM messages m
        JOIN tokens t ON t.token = m.token
        WHERE m.sender_ip=?
        ORDER BY m.ts DESC
        LIMIT 50
        """,
        (ip,),
    )
    rows = cur.fetchall()
    conn.close()
    for (nm,) in rows:
        if nm and not nm.lower().startswith("anon"):
            return nm
    return None

# ----------------------- SOCKET.IO HANDLERS --------------------------
@socketio.on('register')
def ws_register(data):
    # requested values
    req_name = (data.get('name') or '').strip()
    provided_token = data.get('token')
    desired_room = data.get('room') or data.get('room_code') or None

    # token/name resolution
    if provided_token:
        stored_name = get_name_by_token(provided_token)
        if stored_name:
            name = stored_name
            token = provided_token
        else:
            name = req_name or "anon"
            token = str(uuid.uuid4())
            set_token_name(token, name)
    else:
        name = req_name or "anon"
        token = str(uuid.uuid4())
        set_token_name(token, name)

    # If anon => reassociate by IP to last known non-anon username
    client_ip = get_client_ip()
    if not name or name.lower().startswith("anon"):
        prev = last_non_anon_name_for_ip(client_ip)
        if prev:
            name = prev
            set_token_name(token, name)

    # ban check
    if is_banned(token):
        emit('welcome', {'name': name, 'token': token, 'public_token': get_public_token_by_token(token), 'banned': True})
        return

    # maps
    sid_to_name[request.sid] = name
    sid_to_token[request.sid] = token

    # join room if valid
    if desired_room and room_exists(desired_room):
        sid_to_room[request.sid] = desired_room
        sio_join(desired_room)

    public_token = get_public_token_by_token(token)
    emit('welcome', {'name': name, 'token': token, 'public_token': public_token})

    # history scoped to room (if any)
    recent = recent_messages(limit=50, room_code=sid_to_room.get(request.sid))
    lines = []
    for sender_ip, ts, txt, tok in recent:
        nickname = get_name_by_token(tok) or "anon"
        pub = get_public_token_by_token(tok) or "?"
        when = datetime.fromtimestamp(ts).strftime('%H:%M')
        lines.append(f"<span class='user' data-pub='{pub}'>{nickname}</span> - {when} - {txt}")
    emit('history', lines)

@socketio.on('msg')
def ws_msg(data):
    text = data if isinstance(data, str) else data.get('text', '')
    room = None
    if isinstance(data, dict):
        room = data.get('room')

    name = sid_to_name.get(request.sid, 'anon')
    token = sid_to_token.get(request.sid)
    client_ip = get_client_ip()

    # store and broadcast
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    pub = get_public_token_by_token(token) or "?"
    now = datetime.now().strftime('%H:%M')
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room:
        emit('chat_line', line, room=room)
    else:
        emit('chat_line', line, broadcast=True)

# ----------------------- ADMIN: helpers --------------------------------
def require_admin():
    if request.args.get('pass') != ADMIN_PASSWORD:
        abort(403)

def all_users_with_ips():
    """[(name, secret, public, [ips])]"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name, token, public_token FROM tokens")
    users = cur.fetchall()
    cur.execute("SELECT DISTINCT sender_ip, token FROM messages")
    ip_pairs = cur.fetchall()
    conn.close()
    token_to_ips = {}
    for ip, tok in ip_pairs:
        token_to_ips.setdefault(tok, []).append(ip)
    out = []
    for name, secret, public in users:
        out.append((name, secret, public, token_to_ips.get(secret, [])))
    return out

def linked_usernames_by_ip():
    """{ip: [distinct names]}"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT sender_ip FROM messages")
    ips = [r[0] for r in cur.fetchall()]
    res = {}
    for ip in ips:
        cur.execute(
            """SELECT DISTINCT t.name
               FROM messages m JOIN tokens t ON t.token=m.token
               WHERE m.sender_ip=?""",
            (ip,),
        )
        res[ip] = [r[0] for r in cur.fetchall()]
    conn.close()
    return res

def live_participants_by_room():
    """{room_code: [(name, token)]} from current connections"""
    rooms = {}
    for sid, room in sid_to_room.items():
        if not room:
            continue
        rooms.setdefault(room, []).append((sid_to_name.get(sid, 'anon'), sid_to_token.get(sid)))
    return rooms

# ----------------------- ADMIN: routes ---------------------------------
ADMIN_MENU_HTML = """
<!doctype html>
<html><head><title>Admin</title>
<style>body{font-family:Arial;margin:20px}a.btn{display:inline-block;padding:8px 12px;margin:6px;border:1px solid #999;border-radius:6px;text-decoration:none}</style>
</head><body>
<h2>Admin Dashboard</h2>
<p>
  <a class="btn" href="{{ url_for('admin_view', _external=False) }}?pass={{ pw }}">View Users & Rooms</a>
  <a class="btn" href="{{ url_for('admin_manage', _external=False) }}?pass={{ pw }}">Manage Users</a>
  <a class="btn" href="{{ url_for('admin_logs', _external=False) }}?pass={{ pw }}">View Messages & Files</a>
</p>
<p><a class="btn" href="{{ url_for('index') }}">⬅ Back to Chat</a></p>
</body></html>
"""

@app.route('/admin')
def admin_menu():
    require_admin()
    return render_template_string(ADMIN_MENU_HTML, pw=ADMIN_PASSWORD)

ADMIN_VIEW_HTML = """
<!doctype html>
<html><head><title>Admin • View</title>
<style>
body{font-family:Arial;margin:20px}
table{border-collapse:collapse;width:100%;margin:10px 0}
th,td{border:1px solid #ccc;padding:6px;vertical-align:top}
small{color:#666}
a.btn{display:inline-block;padding:6px 10px;margin:6px;border:1px solid #999;border-radius:6px;text-decoration:none}
.code{font-family:monospace}
</style></head><body>
<h2>Users & Rooms</h2>
<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>

<h3>Users</h3>
<table>
<tr><th>Name</th><th>Public</th><th>Secret</th><th>IPs</th></tr>
{% for name, secret, public, ips in users %}
<tr>
  <td>{{ name }}</td>
  <td class="code">{{ public }}</td>
  <td class="code">{{ secret }}</td>
  <td>{{ ', '.join(ips) if ips else '-' }}</td>
</tr>
{% endfor %}
</table>

<h3>Linked Usernames by IP</h3>
<table>
<tr><th>IP</th><th>Usernames (distinct)</th></tr>
{% for ip, names in linked.items() %}
<tr><td>{{ ip }}</td><td>{{ ', '.join(names) }}</td></tr>
{% endfor %}
</table>

<h3>Rooms (live)</h3>
<table>
<tr><th>Room Code</th><th>Participants</th></tr>
{% for code, parts in live.items() %}
<tr>
  <td class="code">{{ code }}</td>
  <td>
    {% for nm, tok in parts %}
      <div>{{ nm }} <small>(token: <span class="code">{{ tok }}</span>)</small></div>
    {% endfor %}
  </td>
</tr>
{% else %}
<tr><td colspan="2"><i>No active rooms</i></td></tr>
{% endfor %}
</table>

<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>
</body></html>
"""

@app.route('/admin/view')
def admin_view():
    require_admin()
    users = all_users_with_ips()
    linked = linked_usernames_by_ip()
    live = live_participants_by_room()
    return render_template_string(ADMIN_VIEW_HTML, users=users, linked=linked, live=live, pw=ADMIN_PASSWORD)

ADMIN_MANAGE_HTML = """
<!doctype html>
<html><head><title>Admin • Manage</title>
<style>
body{font-family:Arial;margin:20px}
table{border-collapse:collapse;width:100%;margin:10px 0}
th,td{border:1px solid #ccc;padding:6px}
a.btn,button{display:inline-block;padding:6px 10px;margin:6px;border:1px solid #999;border-radius:6px;text-decoration:none;background:#f6f6f6;cursor:pointer}
input{padding:6px}
.code{font-family:monospace}
</style></head><body>
<h2>Manage Users</h2>
<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>

<h3>Ban / Unban</h3>
<table>
<tr><th>Name</th><th>Token</th><th>Actions</th></tr>
{% for name, token, public, ips in users %}
<tr>
  <td>{{ name }}</td>
  <td class="code">{{ token }}</td>
  <td>
    <button onclick="ban('{{ token }}')">Ban</button>
    <button onclick="unban('{{ token }}')">Unban</button>
  </td>
</tr>
{% endfor %}
</table>

<h3>Room Control</h3>
<p>
  Token: <input id="mvTok" placeholder="secret token" style="width:360px"/>
  Room code (leave blank to kick): <input id="mvRoom" placeholder="ROOMCODE" style="width:160px"/>
  <button onclick="move()">Apply</button>
</p>

<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>

<script>
function ban(tok){ fetch('/admin/ban?pass={{ pw }}&token='+encodeURIComponent(tok)).then(()=>alert('Banned')); }
function unban(tok){ fetch('/admin/unban?pass={{ pw }}&token='+encodeURIComponent(tok)).then(()=>alert('Unbanned')); }
function move(){
  const tok = document.getElementById('mvTok').value.trim();
  const room = document.getElementById('mvRoom').value.trim();
  if(!tok){ alert('enter token'); return; }
  const url = '/admin/force_move?pass={{ pw }}&token='+encodeURIComponent(tok)+'&room='+encodeURIComponent(room);
  fetch(url).then(r=>r.text()).then(t=>alert(t));
}
</script>
</body></html>
"""

@app.route('/admin/manage')
def admin_manage():
    require_admin()
    users = all_users_with_ips()
    return render_template_string(ADMIN_MANAGE_HTML, users=users, pw=ADMIN_PASSWORD)

ADMIN_LOGS_HTML = """
<!doctype html>
<html><head><title>Admin • Logs</title>
<style>
body{font-family:Arial;margin:20px}
pre{white-space:pre-wrap;border:1px solid #ccc;padding:10px}
a.btn{display:inline-block;padding:6px 10px;margin:6px;border:1px solid #999;border-radius:6px;text-decoration:none}
</style></head><body>
<h2>Messages & Files</h2>
<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>
<pre>
{% for ip, ts, content, token, room in messages %}
[{{ datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') }}] IP: {{ ip }} | Room: {{ room or "-" }} | Token: {{ token }}
{{ content }}

{% endfor %}
</pre>
<p><a class="btn" href="{{ url_for('admin_menu') }}?pass={{ pw }}">⬅ Back</a></p>
</body></html>
"""

@app.route('/admin/logs')
def admin_logs():
    require_admin()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT sender_ip, ts, content, token, room_code FROM messages ORDER BY ts ASC")
    messages = cur.fetchall()
    conn.close()
    return render_template_string(ADMIN_LOGS_HTML, messages=messages, datetime=datetime, pw=ADMIN_PASSWORD)

@app.route('/admin/ban')
def admin_ban():
    require_admin()
    token = request.args.get('token')
    if not token: abort(400)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO banned(token) VALUES (?)", (token,))
    conn.commit()
    conn.close()
    return "ok"

@app.route('/admin/unban')
def admin_unban():
    require_admin()
    token = request.args.get('token')
    if not token: abort(400)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM banned WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return "ok"

@app.route('/admin/force_move')
def admin_force_move():
    require_admin()
    token = request.args.get('token')
    room = (request.args.get('room') or '').strip()  # blank => kick
    if not token: abort(400)

    # find all sids for this token
    targets = [sid for sid, tok in sid_to_token.items() if tok == token]
    if not targets:
        return "no active session for token"

    count = 0
    for sid in targets:
        # leave current room if any
        current = sid_to_room.get(sid)
        if current:
            sio_leave(current, sid=sid)
        if room:
            # only move if room exists
            if room_exists(room):
                sio_join(room, sid=sid)
                sid_to_room[sid] = room
                socketio.emit('chat_line', f"<i>[ADMIN moved you to room {room}]</i>", room=room)
            else:
                # if room not in DB, create a minimal record with name=code
                create_room_in_db(room, room, sid_to_token.get(sid) or '')
                sio_join(room, sid=sid)
                sid_to_room[sid] = room
                socketio.emit('chat_line', f"<i>[ADMIN moved you to room {room}]</i>", room=room)
        else:
            # kicked: clear mapping
            sid_to_room[sid] = None
            socketio.emit('chat_line', f"<i>[ADMIN removed a user from room]</i>", room=current) if current else None
        count += 1
    return f"moved/kicked {count} session(s)"

# Legacy simple token dump retained
@app.route('/tokens')
def list_tokens():
    require_admin()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT name, token, public_token FROM tokens')
    tokens = cur.fetchall()
    cur.execute('SELECT DISTINCT sender_ip, token FROM messages')
    ips = cur.fetchall()
    conn.close()
    token_to_ips = {}
    for ip, tok in ips:
        token_to_ips.setdefault(tok, []).append(ip)
    rows = []
    for name, secret, public in tokens:
        ips_list = token_to_ips.get(secret, [])
        rows.append({'name': name, 'secret': secret, 'public': public, 'ips': ips_list})
    html = ['<html><head><title>Tokens</title></head><body><h2>Tokens</h2><pre>']
    for r in rows:
        html.append(
            f"USER: {r['name']}\n"
            f"  IPs: {', '.join(r['ips']) or '-'}\n"
            f"  public token: {r['public']}\n"
            f"  secret token: {r['secret']}\n\n"
        )
    html.append('<p><a href="/admin?pass=' + ADMIN_PASSWORD + '">⬅ Back</a></p></pre></body></html>')
    return ''.join(html)

# ----------------------- CHAT UI --------------------------------------
INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Chat — Rooms</title>
  <style>
    body{font-family:Arial;margin:16px}
    #chat{border:1px solid #ccc;height:360px;overflow:auto;padding:6px}
    p{margin:0 0 6px}
    input{padding:6px}
    button{padding:6px;margin-left:6px}
    .user{color:blue;cursor:pointer}
  </style>
</head>
<body>
  <h3 id="title">Chat</h3>
  <div id="menu">
    <input id="nick" placeholder="nickname" />
    <button id="enter" disabled>Enter</button>
    <button id="host">Host Room</button>
    <input id="joinCode" placeholder="room code" style="width:120px" />
    <button id="joinBtn">Join</button>
    <a href="/admin?pass={{ admin_pass }}" style="margin-left:12px">Admin</a>
  </div>

  <div id="chatui" style="display:none;margin-top:10px">
    <div id="roomInfo"></div>
    <div id="chat"></div>
    <div style="margin-top:6px">
      <input id="msg" placeholder="message" style="width:60%" />
      <button id="send">Send</button>
      <button id="recBtn">🎤 Record</button>
      <input type="file" id="fileInput" />
      <button id="uploadBtn">Upload</button>
    </div>
  </div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
const socket = io();
let currentRoom = '';
const DEFAULT_ROOM = "{{ default_room or '' }}";

function addLine(txt){
  const p=document.createElement('p');p.innerHTML=txt;
  const c=document.getElementById('chat');c.appendChild(p);c.scrollTop=c.scrollHeight;
}
function setTitle(){
  document.getElementById('title').innerText = currentRoom ? ('Private Chat room code - ' + currentRoom) : 'Public Chat';
}

socket.on('connect', ()=>{document.getElementById('enter').disabled=false});

// Enter / register
document.getElementById('enter').onclick = ()=>{
  const nick = document.getElementById('nick').value.trim();
  if(!nick){alert('enter nick');return}
  const stored = localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

// Host room (creates client-side code; server side record optional)
document.getElementById('host').onclick = ()=>{
  const nick = document.getElementById('nick').value.trim(); if(!nick){alert('enter nick to host');return}
  const code = Math.random().toString(36).slice(2,8).toUpperCase();
  currentRoom = code;
  document.getElementById('joinCode').value = code;
  document.getElementById('roomInfo').innerText = 'Room: ' + code + '  (Share link: ' + location.origin + '/room/' + code + ')';
  document.getElementById('chatui').style.display='block';
  setTitle();
};

// Join by code input
document.getElementById('joinBtn').onclick = ()=>{
  const code = document.getElementById('joinCode').value.trim(); if(!code){alert('enter code');return}
  currentRoom = code;
  document.getElementById('roomInfo').innerText = 'Room: ' + code + '  (Link: ' + location.origin + '/room/' + code + ')';
  document.getElementById('chatui').style.display='block';
  setTitle();
  // re-register into that room so server sends room history if exists
  const nick = document.getElementById('nick').value.trim() || 'anon';
  const stored = localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

// If DEFAULT_ROOM provided by /room/<code>, auto-join
(function(){
  if(DEFAULT_ROOM){
    currentRoom = DEFAULT_ROOM;
    document.getElementById('joinCode').value = DEFAULT_ROOM;
    document.getElementById('roomInfo').innerText = 'Room: ' + DEFAULT_ROOM + '  (Link: ' + location.origin + '/room/' + DEFAULT_ROOM + ')';
    document.getElementById('chatui').style.display='block';
    setTitle();
  }
})();

socket.on('welcome', data=>{
  localStorage.setItem('chatToken', data.token);
  document.getElementById('chatui').style.display='block';
  addLine('[INFO] You are '+data.name+' (public id: '+data.public_token+')');
  if(DEFAULT_ROOM && currentRoom===DEFAULT_ROOM){
    // already set; nothing else
  }
});

socket.on('history', lines=>{lines.forEach(l=>addLine(l))});
socket.on('chat_line', line=>addLine(line));

// send message (include room)
document.getElementById('send').onclick = ()=>{
  const txt = document.getElementById('msg').value.trim(); if(!txt) return;
  socket.emit('msg', {text:txt, room: currentRoom}); document.getElementById('msg').value='';
};
document.getElementById('msg').addEventListener('keypress',e=>{if(e.key==='Enter'){document.getElementById('send').click();}});

// click username to see public token
document.addEventListener('click', e=>{ if(e.target.classList.contains('user')) alert('Public token: '+e.target.dataset.pub); });

// "Upload" as Data URL (image/audio become embeddable; no server store)
document.getElementById('uploadBtn').onclick = ()=>{
  const f = document.getElementById('fileInput').files[0]; if(!f){alert('choose file');return}
  const reader = new FileReader();
  reader.onload = e=>{
    const url = e.target.result;
    let msg;
    if(f.type.startsWith('image/')) msg = `<img src="${url}" style="max-width:300px;"/>`;
    else if(f.type.startsWith('audio/')) msg = `<audio controls src="${url}"></audio>`;
    else msg = `<a href="${url}" target="_blank">${f.name}</a>`;
    socket.emit('msg', {text:msg, room: currentRoom});
  };
  reader.readAsDataURL(f);
};

// voice recorder -> DataURL
let rec, chunks=[];
document.getElementById('recBtn').onclick = async ()=>{
  if(!rec || rec.state==='inactive'){
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    rec = new MediaRecorder(stream);
    rec.ondataavailable = e=>chunks.push(e.data);
    rec.onstop = ()=>{
      const blob = new Blob(chunks, {type:'audio/webm'}); chunks=[];
      const fr = new FileReader();
      fr.onload = e=>{
        const msg = `<audio controls src="${e.target.result}"></audio>`;
        socket.emit('msg', {text:msg, room: currentRoom});
      };
      fr.readAsDataURL(blob);
    };
    rec.start(); document.getElementById('recBtn').innerText='⏹ Stop';
  } else { rec.stop(); document.getElementById('recBtn').innerText='🎤 Record'; }
};
</script>
</body>
</html>
"""

# ----------------------- ROUTES ---------------------------------------
@app.route('/')
def index():
    return render_template_string(INDEX_HTML, default_room='', admin_pass=ADMIN_PASSWORD)

@app.route('/room/<code>')
def room_page(code):
    # Render same page but with default_room pre-set
    return render_template_string(INDEX_HTML, default_room=code, admin_pass=ADMIN_PASSWORD)

# Optional stub upload (not used when sending Data URLs, but kept for compatibility)
@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f:
        return jsonify(error="No file uploaded")
    return jsonify(filename=f.filename, url=f"/uploads/{f.filename}")

# ----------------------- RUN ------------------------------------------
if __name__ == '__main__':
    init_db()
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
