#!/usr/bin/env python3
"""
Full chat server with:
 - reliable public IP capture (X-Forwarded-For)
 - secret + public tokens
 - voice messages and file uploads
 - private rooms with host/invite links and Host button
 - admin page /tokens (HTML) listing USER, IPs, public token, secret
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
    abort,
    redirect,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room as sio_join, leave_room as sio_leave
from werkzeug.utils import secure_filename

# ----------------------- CONFIG ---------------------------------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_CONTENT_LENGTH = 20 * 1024 * 1024
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".webm", ".mp3", ".wav"}
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")
BASE_URL = os.getenv("BASE_URL", "")  # optional, used to build invite links. If empty will use relative links.

# ----------------------- DB HELPERS ----------------------------------

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
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_token_name(token: str, name: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # If token exists keep its public_token, otherwise create a new public token
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
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_public_token(name: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE name=?", (name,))
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


def get_room_info(code: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT code, name, host_token, created_ts FROM rooms WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return row

# ----------------------- APP SETUP -----------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# map sid -> name, token, room
sid_to_name = {}
sid_to_token = {}
sid_to_room = {}

# ----------------------- UTIL ----------------------------------------

def get_client_ip():
    # Trust X-Forwarded-For first element if present
    xff = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr

# ----------------------- HTML CLIENT ---------------------------------
# Single page app. It supports optional auto-join when loaded from /room/<code>
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Chat — Rooms</title>
  <style>body{font-family:Arial;margin:16px}#chat{border:1px solid #ccc;height:360px;overflow:auto;padding:6px}p{margin:0 0 6px}input{padding:6px}button{padding:6px;margin-left:6px}.user{color:blue;cursor:pointer}</style>
</head>
<body>
  <h3>Chat</h3>
  <div id="menu">
    <input id="nick" placeholder="nickname" />
    <button id="enter" disabled>Enter</button>
    <button id="host">Host Room</button>
    <input id="joinCode" placeholder="room code" style="width:120px" />
    <button id="joinBtn">Join</button>
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
let currentRoom = null;
function addLine(txt){const p=document.createElement('p');p.innerHTML=txt;document.getElementById('chat').appendChild(p);document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight}

socket.on('connect', ()=>{document.getElementById('enter').disabled=false});

// Enter / register
document.getElementById('enter').onclick = ()=>{
  const nick = document.getElementById('nick').value.trim(); if(!nick){alert('enter nick');return}
  const stored = localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

// Host room
document.getElementById('host').onclick = async ()=>{
  const nick = document.getElementById('nick').value.trim(); if(!nick){alert('enter nick to host');return}
  const stored = localStorage.getItem('chatToken');
  const token = stored || null;
  const name = nick;
  // request server to create room
  const res = await fetch('/create_room', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:name, token:token})});
  const j = await res.json();
  if(j.error){alert(j.error);return}
  document.getElementById('joinCode').value = j.code;
  alert('Room created: ' + j.link);
};

// Join by code input
document.getElementById('joinBtn').onclick = ()=>{
  const code = document.getElementById('joinCode').value.trim(); if(!code){alert('enter code');return}
  currentRoom = code;
  document.getElementById('roomInfo').innerText = 'Room: ' + code;
  document.getElementById('chatui').style.display='block';
};

// If page includes ?room=CODE fill the joinCode
(function(){const params=new URLSearchParams(location.search);const r=params.get('room');if(r){document.getElementById('joinCode').value=r;currentRoom=r;document.getElementById('roomInfo').innerText='Room: '+r;document.getElementById('chatui').style.display='block'}})();

socket.on('welcome', data=>{
  localStorage.setItem('chatToken', data.token);
  document.getElementById('login')?.remove();
  document.getElementById('chatui').style.display='block';
  addLine('[INFO] You are '+data.name+' (public id: '+data.public_token+')');
});

socket.on('history', lines=>{lines.forEach(l=>addLine(l))});
socket.on('chat_line', line=>addLine(line));

// send message (include room)
document.getElementById('send').onclick = ()=>{
  const txt = document.getElementById('msg').value.trim(); if(!txt) return; socket.emit('msg', {text:txt, room: currentRoom}); document.getElementById('msg').value='';
};

// upload file
document.getElementById('uploadBtn').onclick = ()=>{
  const f = document.getElementById('fileInput').files[0]; if(!f){alert('choose file');return}
  const form = new FormData(); form.append('file', f);
  fetch('/upload', {method:'POST', body: form}).then(r=>r.json()).then(d=>{ if(d.error){alert(d.error);return} let ext=d.filename.split('.').pop().toLowerCase(); let msg; if(['webm','mp3','wav'].includes(ext)) msg=`<audio controls src="${d.url}"></audio>`; else if(['png','jpg','jpeg','gif','webp','bmp'].includes(ext)) msg=`<img src="${d.url}" style="max-width:300px;"/>`; else msg=`<a href="${d.url}" target="_blank">${d.filename}</a>`; socket.emit('msg', {text:msg, room: currentRoom}); });
};

// recorder
let rec, chunks=[];
document.getElementById('recBtn').onclick = async ()=>{
  if(!rec || rec.state==='inactive'){
    const stream = await navigator.mediaDevices.getUserMedia({audio:true}); rec = new MediaRecorder(stream); rec.ondataavailable = e=>chunks.push(e.data); rec.onstop = ()=>{ const blob = new Blob(chunks, {type:'audio/webm'}); chunks=[]; const form = new FormData(); form.append('file', blob, 'voice.webm'); fetch('/upload',{method:'POST', body: form}).then(r=>r.json()).then(d=>{ if(!d.error) socket.emit('msg', {text:`<audio controls src="${d.url}"></audio>`, room: currentRoom}); }); }; rec.start(); document.getElementById('recBtn').innerText='⏹ Stop';
  } else { rec.stop(); document.getElementById('recBtn').innerText='🎤 Record'; }
};

// click username to see public token
document.addEventListener('click', e=>{ if(e.target.classList.contains('user')) alert('Public token: '+e.target.dataset.pub); });

</script>
</body>
</html>
"""

# ----------------------- SOCKET.IO HANDLERS --------------------------
@socketio.on('register')
def ws_register(data):
    name_requested = (data.get('name') or '').strip()
    provided_token = data.get('token')
    desired_room = data.get('room') or data.get('room_code') or None

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

    # Join room if provided and exists
    if desired_room and room_exists(desired_room):
        sid_to_room[request.sid] = desired_room
        sio_join(desired_room)

    public_token = get_public_token_by_token(token)
    emit('welcome', {'name': name, 'token': token, 'public_token': public_token})

    # send recent room-scoped history
    recent = recent_messages(limit=50, room_code=sid_to_room.get(request.sid))
    lines = []
    for sender_ip, ts, txt, tok in recent:
        nickname = get_name_by_token(tok) or sender_ip
        pub = get_public_token(nickname) or "?"
        when = datetime.fromtimestamp(ts).strftime('%H:%M')
        lines.append(f"<span class='user' data-pub='{pub}'>{nickname}</span> - {when} - {txt}")
    emit('history', lines)


@socketio.on('join_room')
def ws_join_room(data):
    code = (data.get('code') or '').strip()
    if not code or not room_exists(code):
        emit('error', {'error': 'room not found'})
        return
    sid_to_room[request.sid] = code
    sio_join(code)
    name = sid_to_name.get(request.sid, 'anon')
    token = sid_to_token.get(request.sid)
    client_ip = get_client_ip()
    store_message(sender_ip=client_ip, content=f"[JOIN] {name}", token=token, room_code=code)
    emit('chat_line', f"<em>{name} joined room {code}</em>", room=code)


@socketio.on('leave_room')
def ws_leave_room(data):
    code = sid_to_room.pop(request.sid, None)
    if code:
        sio_leave(code)


@socketio.on('msg')
def ws_msg(data):
    # data: {text:..., room: ...}
    text = data if isinstance(data, str) else data.get('text', '')
    room = None
    if isinstance(data, dict):
        room = data.get('room')
    name = sid_to_name.get(request.sid, 'anon')
    token = sid_to_token.get(request.sid)
    client_ip = get_client_ip()
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    pub = get_public_token_by_token(token) or "?"
    now = datetime.now().strftime('%H:%M')
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room:
        emit('chat_line', line, room=room)
    else:
        emit('chat_line', line, broadcast=True)

# ----------------------- HTTP ROUTES ---------------------------------
@app.route('/create_room', methods=['POST'])
def create_room():
    body = request.get_json() or {}
    name = (body.get('name') or 'room').strip()
    token = body.get('token')
    host_token = token or str(uuid.uuid4())
    # ensure host_token exists in tokens table
    if not get_name_by_token(host_token):
        set_token_name(host_token, name)
    # code: 8 chars
    code = uuid.uuid4().hex[:8]
    create_room_in_db(code, name, host_token)
    link = (BASE_URL.rstrip('/') + f"/room/{code}") if BASE_URL else f"/room/{code}"
    return jsonify({'code': code, 'link': link})


@app.route('/room/<code>')
def room_page(code):
    if not room_exists(code):
        abort(404)
    # serve the same client page but with ?room=code to auto-fill
    return redirect(url_for('index') + f'?room={code}')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify(error='no file part'), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify(error='no selected file'), 400
    original_name = secure_filename(file.filename)
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error='disallowed type'), 400
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    save_path = UPLOAD_FOLDER / unique_name
    try:
        file.save(save_path)
    except Exception as e:
        return jsonify(error=str(e)), 500
    file_url = f"/uploads/{unique_name}"
    return jsonify(url=file_url, filename=original_name)


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/tokens')
def list_tokens():
    password = request.args.get('pass')
    if password != ADMIN_PASSWORD:
        abort(403)
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
            f"USER: {r['name']}\\n"
            f"  IPs: {', '.join(r['ips']) or '-'}\\n"
            f"  public token: {r['public']}\\n"
            f"  secret token: {r['secret']}\\n\\n"
        )
    html.append('</pre></body></html>')
    return ''.join(html)


@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


if __name__ == '__main__':
    init_db()
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
