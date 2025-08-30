#!/usr/bin/env python3
import os, uuid, sqlite3, time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, abort
from flask_socketio import SocketIO, emit, join_room as sio_join

# ---------------- CONFIG ----------------
DB_FILE = "chat_web_token.db"
UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
MAX_CONTENT_LENGTH = 20*1024*1024
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")

# ---------------- DB --------------------
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
    conn.commit(); conn.close()

def store_message(sender_ip, content, token=None, room_code=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (sender_ip, ts, content, token, room_code) VALUES (?,?,?,?,?)",
                (sender_ip, time.time(), content, token, room_code))
    conn.commit(); conn.close()

def recent_messages(limit=50, room_code=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if room_code:
        cur.execute("SELECT sender_ip, ts, content, token FROM messages WHERE room_code=? ORDER BY ts DESC LIMIT ?",
                    (room_code, limit))
    else:
        cur.execute("SELECT sender_ip, ts, content, token FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    rows = cur.fetchall(); conn.close(); rows.reverse(); return rows

def get_name_by_token(token):
    if not token: return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
    row = cur.fetchone(); conn.close()
    return row[0] if row else None

def set_token_name(token, name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    if row: public_token = row[0]; cur.execute("UPDATE tokens SET name=? WHERE token=?", (name, token))
    else: public_token = str(uuid.uuid4())[:8]; cur.execute("INSERT OR REPLACE INTO tokens (token,name,public_token) VALUES (?,?,?)",
                (token,name,public_token))
    conn.commit(); conn.close()

def get_public_token_by_token(token):
    if not token: return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone(); conn.close()
    return row[0] if row else None

def create_room_in_db(code, name, host_token):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO rooms (code,name,host_token,created_ts) VALUES (?,?,?,?)",
                (code,name,host_token,time.time()))
    conn.commit(); conn.close()

def room_exists(code):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT code FROM rooms WHERE code=?", (code,))
    row = cur.fetchone(); conn.close()
    return bool(row)

def is_banned(token):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT token FROM banned WHERE token=?", (token,))
    row = cur.fetchone(); conn.close()
    return bool(row)

# ---------------- APP -------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")
sid_to_name, sid_to_token, sid_to_room = {}, {}, {}

def get_client_ip():
    xff = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    return xff.split(',')[0].strip() if xff else request.remote_addr

# ---------------- SOCKET.IO ----------------
@socketio.on('register')
def ws_register(data):
    name_requested = (data.get('name') or '').strip()
    provided_token = data.get('token')
    desired_room = data.get('room') or data.get('room_code') or None

    if provided_token:
        stored_name = get_name_by_token(provided_token)
        if stored_name: name = stored_name; token = provided_token
        else: name = name_requested or "anon"; token = str(uuid.uuid4()); set_token_name(token,name)
    else: name = name_requested or "anon"; token = str(uuid.uuid4()); set_token_name(token,name)

    if is_banned(token):
        emit('welcome', {'name': name, 'token': token, 'public_token': get_public_token_by_token(token), 'banned':True})
        return

    sid_to_name[request.sid] = name; sid_to_token[request.sid] = token
    if desired_room and room_exists(desired_room): sid_to_room[request.sid] = desired_room; sio_join(desired_room)

    public_token = get_public_token_by_token(token)
    emit('welcome', {'name': name, 'token': token, 'public_token': public_token})

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
    text = data if isinstance(data,str) else data.get('text','')
    room = None
    if isinstance(data, dict): room = data.get('room')
    name = sid_to_name.get(request.sid,'anon')
    token = sid_to_token.get(request.sid)
    client_ip = get_client_ip()
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    pub = get_public_token_by_token(token) or "?"
    now = datetime.now().strftime('%H:%M')
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room: emit('chat_line', line, room=room)
    else: emit('chat_line', line, broadcast=True)

# ---------------- CHAT HTML ----------------
INDEX_HTML = """<!doctype html>
<html>
<head>
<title>Chat</title>
<style>
body{font-family:Arial;margin:16px}#chat{border:1px solid #ccc;height:360px;overflow:auto;padding:6px}p{margin:0 0 6px}input{padding:6px}button{padding:6px;margin-left:6px}.user{color:blue;cursor:pointer}
</style>
</head>
<body>
<h2 id="chatTitle">Public Chat</h2>
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
let currentRoom=null;
function addLine(txt){const p=document.createElement('p');p.innerHTML=txt;document.getElementById('chat').appendChild(p);document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight;}
function updateChatTitle(){document.getElementById('chatTitle').innerText=currentRoom?`Private Chat [${currentRoom}]`:'Public Chat';}
socket.on('connect',()=>{document.getElementById('enter').disabled=false;});
document.getElementById('enter').onclick=()=>{const nick=document.getElementById('nick').value.trim();if(!nick){alert('enter nick');return;}const stored=localStorage.getItem('chatToken');if(stored){socket.emit('register',{name:nick,token:stored,room:currentRoom});}else{socket.emit('register',{name:nick,room:currentRoom});}};
document.getElementById('host').onclick=()=>{const code=Math.random().toString(36).slice(2,8).toUpperCase();currentRoom=code;document.getElementById('joinCode').value=code;document.getElementById('roomInfo').innerText='Room: '+code;document.getElementById('chatui').style.display='block';updateChatTitle();};
document.getElementById('joinBtn').onclick=()=>{const code=document.getElementById('joinCode').value.trim();if(!code){alert('enter code');return;}currentRoom=code;document.getElementById('roomInfo').innerText='Room: '+code;document.getElementById('chatui').style.display='block';updateChatTitle();};
socket.on('welcome',data=>{localStorage.setItem('chatToken',data.token);document.getElementById('login')?.remove();document.getElementById('chatui').style.display='block';addLine('[INFO] You are '+data.name+' (public id: '+data.public_token+')');});
socket.on('history',lines=>{lines.forEach(l=>addLine(l));});
socket.on('chat_line',line=>addLine(line));
document.getElementById('send').onclick=()=>{const txt=document.getElementById('msg').value.trim();if(!txt)return;socket.emit('msg',{text:txt,room:currentRoom});document.getElementById('msg').value='';};
document.getElementById('msg').addEventListener('keypress',e=>{if(e.key==='Enter'){document.getElementById('send').click();}});
document.addEventListener('click',e=>{if(e.target.classList.contains('user')){alert('Public token: '+e.target.dataset.pub);}});
document.getElementById('uploadBtn').onclick=()=>{const f=document.getElementById('fileInput').files[0];if(!f){alert('choose file');return;}const reader=new FileReader();reader.onload=function(e){const msg=f.type.startsWith('image/')?`<img src="${e.target.result}" style="max-width:300px;"/>`:f.type.startsWith('audio/')?`<audio controls src="${e.target.result}"></audio>`:`<a href="${e.target.result}" target="_blank">${f.name}</a>`;socket.emit('msg',{text:msg,room:currentRoom});};reader.readAsDataURL(f);};
let rec,chunks=[];
document.getElementById('recBtn').onclick=async()=>{if(!rec||rec.state==='inactive'){const stream=await navigator.mediaDevices.getUserMedia({audio:true});rec=new MediaRecorder(stream);rec.ondataavailable=e=>chunks.push(e.data);rec.onstop=()=>{const blob=new Blob(chunks,{type:'audio/webm'});chunks=[];const reader=new FileReader();reader.onload=e=>{const msg=`<audio controls src="${e.target.result}"></audio>`;socket.emit('msg',{text:msg,room:currentRoom});};reader.readAsDataURL(blob);};rec.start();document.getElementById('recBtn').innerText='⏹ Stop';}else{rec.stop();document.getElementById('recBtn').innerText='🎤 Record';}};
</script>
</body></html>"""

@app.route('/')
def index(): return render_template_string(INDEX_HTML)

@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f: return jsonify(error="No file uploaded")
    return jsonify(filename=f.filename, url=f"/uploads/{f.filename}")

# ---------------- ADMIN -----------------
@app.route('/admin')
def admin_dashboard():
    password = request.args.get('pass')
    if password != ADMIN_PASSWORD: abort(403)
    conn = sqlite3.connect(DB_FILE); cur = conn.cursor()
    cur.execute("SELECT name, token, public_token FROM tokens"); users = cur.fetchall()
    cur.execute("SELECT code, name, host_token, created_ts FROM rooms"); rooms = cur.fetchall()
    cur.execute("SELECT sender_ip, ts, content, token, room_code FROM messages ORDER BY ts ASC"); messages = cur.fetchall()
    conn.close()
    html = """<!doctype html>
<html><head><title>Admin Dashboard</title>
<style>body{font-family:Arial;margin:20px;}button{padding:6px;margin:6px;}.section{margin-bottom:20px;}.hidden{display:none;}pre{white-space:pre-wrap;}</style></head>
<body>
<h2>Admin Dashboard</h2>
<button onclick="showSection('view')">View Users & Rooms</button>
<button onclick="showSection('manage')">Manage Users</button>
<button onclick="showSection('logs')">View Messages & Files</button>

<div id="view" class="section hidden">
<h3>Users</h3><ul>
{% for name,secret,public in users %}
<li><b>{{name}}</b> | Secret: {{secret}} | Public: {{public}} | IPs: {{get_ips(secret)|join(', ')}}</li>
{% endfor %}
</ul>
<h3>Rooms</h3><ul>
{% for code,name,host,ts in rooms %}
<li><b>{{name}}</b> | Code: {{code}} | Host: {{host}}</li>
{% endfor %}
</ul>
<button onclick="hideSection('view')">Back</button>
</div>

<div id="manage" class="section hidden">
<h3>Manage Users</h3><ul>
{% for name,secret,public in users %}
<li>{{name}} <button onclick="banUser('{{secret}}')">Ban</button> <button onclick="unbanUser('{{secret}}')">Unban</button></li>
{% endfor %}
</ul>
<button onclick="hideSection('manage')">Back</button>
</div>

<div id="logs" class="section hidden">
<h3>Messages & Files</h3>
<pre>{% for ip, ts, content, token, room in messages %}
[{{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}}] IP: {{ip}} | Room: {{room}} | Token: {{token}}
{{content}}
{% endfor %}</pre>
<button onclick="hideSection('logs')">Back</button>
</div>

<script>
function showSection(id){document.getElementById('view').classList.add('hidden');document.getElementById('manage').classList.add('hidden');document.getElementById('logs').classList.add('hidden');document.getElementById(id).classList.remove('hidden');}
function hideSection(id){document.getElementById(id).classList.add('hidden');}
function banUser(token){fetch('/admin/ban?pass={{ADMIN_PASSWORD}}&token='+token).then(r=>alert('Banned'));}
function unbanUser(token){fetch('/admin/unban?pass={{ADMIN_PASSWORD}}&token='+token).then(r=>alert('Unbanned'));}
</script>
</body></html>"""
    def get_ips(tok):
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT sender_ip FROM messages WHERE token=?", (tok,))
        ips = [r[0] for r in cur.fetchall()]
        conn.close()
        return ips
    return render_template_string(html, users=users, rooms=rooms, messages=messages,
                                  datetime=datetime, ADMIN_PASSWORD=ADMIN_PASSWORD, get_ips=get_ips)

@app.route('/admin/ban')
def admin_ban():
    password = request.args.get('pass'); token = request.args.get('token')
    if password != ADMIN_PASSWORD or not token: abort(403)
    conn = sqlite3.connect(DB_FILE); conn.execute("INSERT OR REPLACE INTO banned(token) VALUES (?)", (token,)); conn.commit(); conn.close()
    return "ok"

@app.route('/admin/unban')
def admin_unban():
    password = request.args.get('pass'); token = request.args.get('token')
    if password != ADMIN_PASSWORD or not token: abort(403)
    conn = sqlite3.connect(DB_FILE); conn.execute("DELETE FROM banned WHERE token=?", (token,)); conn.commit(); conn.close()
    return "ok"

# ---------------- RUN -----------------
if __name__=='__main__':
    init_db()
    port = int(os.getenv('PORT',5000))
    socketio.run(app, host='0.0.0.0', port=port)
