#!/usr/bin/env python3
import os, uuid, sqlite3, time, base64
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, abort
from flask_socketio import SocketIO, emit, join_room as sio_join, leave_room as sio_leave

# ---------------- CONFIG ----------------
DB_FILE = "chat_web_token.db"
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")
MAX_CONTENT_LENGTH = 20*1024*1024
banned_tokens = set()

# ---------------- DB ----------------
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

def store_message(sender_ip, content, token=None, room_code=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (sender_ip, ts, content, token, room_code) VALUES (?,?,?,?,?)",
                (sender_ip, time.time(), content, token, room_code))
    conn.commit()
    conn.close()

def recent_messages(limit=50, room_code=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if room_code:
        cur.execute("SELECT sender_ip, ts, content, token FROM messages WHERE room_code=? ORDER BY ts DESC LIMIT ?",
                    (room_code, limit))
    else:
        cur.execute("SELECT sender_ip, ts, content, token FROM messages ORDER BY ts DESC LIMIT ?",
                    (limit,))
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return rows

def get_name_by_token(token):
    if not token: return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_token_name(token, name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE tokens SET name=? WHERE token=?", (name, token))
    else:
        public_token = str(uuid.uuid4())[:8]
        cur.execute("INSERT OR REPLACE INTO tokens (token, name, public_token) VALUES (?,?,?)",
                    (token, name, public_token))
    conn.commit()
    conn.close()

def get_public_token_by_token(token):
    if not token: return None
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT public_token FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def create_room_in_db(code, name, host_token):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO rooms (code, name, host_token, created_ts) VALUES (?,?,?,?)",
                (code, name, host_token, time.time()))
    conn.commit()
    conn.close()

def room_exists(code):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT code FROM rooms WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def get_all_rooms():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT code, name, host_token, created_ts FROM rooms")
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------------- APP ----------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

sid_to_name = {}
sid_to_token = {}
sid_to_room = {}

def get_client_ip():
    xff = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr

# ---------------- SOCKET.IO ----------------
@socketio.on('register')
def ws_register(data):
    name_requested = (data.get('name') or '').strip()
    provided_token = data.get('token')
    desired_room = data.get('room') or data.get('room_code') or None

    if provided_token in banned_tokens:
        emit('welcome', {'name':'BANNED','token':'','public_token':'BANNED'})
        return

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

    if desired_room and room_exists(desired_room):
        sid_to_room[request.sid] = desired_room
        sio_join(desired_room)

    public_token = get_public_token_by_token(token)
    emit('welcome', {'name': name, 'token': token, 'public_token': public_token})

    recent = recent_messages(limit=50, room_code=sid_to_room.get(request.sid))
    lines=[]
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
    if isinstance(data,dict):
        room = data.get('room')
    token = sid_to_token.get(request.sid)
    if token in banned_tokens: return
    name = sid_to_name.get(request.sid,'anon')
    client_ip = get_client_ip()
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    pub = get_public_token_by_token(token) or "?"
    now = datetime.now().strftime('%H:%M')
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room:
        emit('chat_line', line, room=room)
    else:
        emit('chat_line', line, broadcast=True)

# ---------------- ADMIN API ----------------
@app.route('/api/admin_overview')
def api_admin_overview():
    password = request.args.get('pass')
    if password != ADMIN_PASSWORD: abort(403)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT name, token, public_token FROM tokens')
    tokens = cur.fetchall()
    cur.execute('SELECT DISTINCT sender_ip, token FROM messages')
    ips = cur.fetchall()
    token_to_ips={}
    for ip,tok in ips: token_to_ips.setdefault(tok,[]).append(ip)
    users=[{'name':n,'secret':s,'public':p,'ips':token_to_ips.get(s,[])} for n,s,p in tokens]
    rooms=[{'code':r[0],'name':r[1],'host':r[2],'created':datetime.fromtimestamp(r[3]).strftime('%Y-%m-%d %H:%M')} for r in get_all_rooms()]
    conn.close()
    return jsonify(users=users,rooms=rooms)

@app.route('/api/admin_user')
def api_admin_user():
    password=request.args.get('pass'); token=request.args.get('token')
    if password != ADMIN_PASSWORD: abort(403)
    conn=sqlite3.connect(DB_FILE)
    cur=conn.cursor()
    cur.execute('SELECT name, token, public_token FROM tokens WHERE token=?',(token,))
    row=cur.fetchone()
    cur.execute('SELECT DISTINCT sender_ip FROM messages WHERE token=?',(token,))
    ips=[r[0] for r in cur.fetchall()]
    conn.close()
    if not row: return jsonify(error="not found"),404
    return jsonify(name=row[0], secret=row[1], public=row[2], ips=ips)

@app.route('/api/admin_ban')
def api_admin_ban():
    password=request.args.get('pass'); token=request.args.get('token')
    if password != ADMIN_PASSWORD: abort(403)
    banned_tokens.add(token)
    for sid,tok in list(sid_to_token.items()):
        if tok==token: socketio.disconnect(sid)
    return '',200

@app.route('/api/admin_logs')
def api_admin_logs():
    password=request.args.get('pass')
    if password != ADMIN_PASSWORD: abort(403)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT sender_ip, ts, content, token, room_code FROM messages ORDER BY ts DESC")
    rows = cur.fetchall()
    logs=[]
    for sender_ip, ts, content, token, room_code in rows:
        name = get_name_by_token(token) or "anon"
        public = get_public_token_by_token(token) or "?"
        logs.append({
            "name": name,
            "public": public,
            "ip": sender_ip,
            "time": datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M'),
            "room": room_code or "Public",
            "content": content
        })
    conn.close()
    return jsonify(logs)

# ---------------- INDEX ----------------
INDEX_HTML = """<!doctype html>
<html>
<head>
<title id="chatHeader">Public Chat</title>
<style>
body{font-family:Arial;margin:20px;}
#chat{border:1px solid #ccc;height:400px;overflow:auto;padding:5px;}
#chat p{margin:0;padding:2px;word-break:break-word;}
#msg{width:60%;}
.user{color:blue;cursor:pointer;}
</style>
</head>
<body>
<h2 id="chatHeader">Public Chat</h2>
<div id="menu">
  <input id="nick" placeholder="nickname"/>
  <button id="enter" disabled>Enter</button>
  <button id="host">Host Room</button>
  <input id="joinCode" placeholder="room code" style="width:120px"/>
  <button id="joinBtn">Join</button>
</div>
<div id="chatui" style="display:none;margin-top:10px">
  <div id="roomInfo"></div>
  <div id="chat"></div>
  <div style="margin-top:6px">
    <input id="msg" placeholder="message"/>
    <button id="send">Send</button>
    <button id="recBtn">🎤 Record</button>
    <input type="file" id="fileInput"/>
    <button id="uploadBtn">Upload</button>
  </div>
</div>
<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
const socket=io(); let currentRoom=null;
function addLine(txt){const p=document.createElement('p'); p.innerHTML=txt; document.getElementById('chat').appendChild(p); document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight;}
function updateHeader(){document.getElementById('chatHeader').innerText=currentRoom?`Private Chat [${currentRoom}]`:'Public Chat';}
socket.on('connect',()=>{document.getElementById('enter').disabled=false;});
document.getElementById('enter').onclick=()=>{
  const nick=document.getElementById('nick').value.trim(); if(!nick){alert('enter nick');return;}
  const stored=localStorage.getItem('chatToken');
  if(stored){socket.emit('register',{name:nick,token:stored,room:currentRoom});}
  else{socket.emit('register',{name:nick,room:currentRoom});}
};
document.getElementById('host').onclick=async()=>{
  const nick=document.getElementById('nick').value.trim(); if(!nick){alert('enter nick');return;}
  const stored=localStorage.getItem('chatToken')||null;
  const res=await fetch('/create_room',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nick,token:stored})});
  const j=await res.json(); currentRoom=j.code; document.getElementById('joinCode').value=j.code; updateHeader(); alert('Room created: '+j.link);
};
document.getElementById('joinBtn').onclick=()=>{
  const code=document.getElementById('joinCode').value.trim(); if(!code){alert('enter code');return;}
  currentRoom=code; document.getElementById('chatui').style.display='block'; updateHeader();
};
socket.on('welcome',data=>{localStorage.setItem('chatToken',data.token); document.getElementById('chatui').style.display='block'; addLine(`[INFO] You are ${data.name} (public id: ${data.public_token})`);});
socket.on('history',lines=>{lines.forEach(l=>addLine(l));});
socket.on('chat_line',line=>addLine(line));
document.getElementById('send').onclick=()=>{
  const txt=document.getElementById('msg').value.trim(); if(!txt)return; socket.emit('msg',{text:txt,room:currentRoom}); document.getElementById('msg').value='';
};
document.getElementById('msg').addEventListener('keypress',e=>{if(e.key==='Enter'){document.getElementById('send').click();}});
document.addEventListener('click',e=>{if(e.target.classList.contains('user')){alert('Public token: '+e.target.dataset.pub);}});
document.getElementById('uploadBtn').onclick=()=>{
  const f=document.getElementById('fileInput').files[0]; if(!f){alert('choose file');return;}
  const form=new FormData(); form.append('file',f);
  fetch('/upload',{method:'POST',body:form}).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;} socket.emit('msg',{text:`<a>${d.filename}</a><br><img src="${d.url}"/>`,room:currentRoom});});
};
let rec,chunks=[];
document.getElementById('recBtn').onclick=async()=>{
  if(!rec||rec.state==='inactive'){let stream=await navigator.mediaDevices.getUserMedia({audio:true}); rec=new MediaRecorder(stream);
  rec.ondataavailable=e=>chunks.push(e.data);
  rec.onstop=async()=>{const blob=new Blob(chunks,{type:'audio/webm'}); chunks=[]; const form=new FormData(); form.append('file',blob,'voice.webm');
  const res=await fetch('/upload',{method:'POST',body:form}); const d=await res.json();
  if(!d.error){socket.emit('msg',{text:`<audio controls src="${d.url}"></audio>`,room:currentRoom});}};
  rec.start(); document.getElementById('recBtn').innerText='⏹ Stop';} else { rec.stop(); document.getElementById('recBtn').innerText='🎤 Record';}
};
</script>
</body></html>"""

@app.route('/')
def index(): return render_template_string(INDEX_HTML)

# ---------------- UPLOAD ----------------
@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f: return jsonify(error="No file uploaded")
    data = f.read()
    mimetype = f.mimetype or "application/octet-stream"
    b64 = base64.b64encode(data).decode('utf-8')
    url = f"data:{mimetype};base64,{b64}"
    return jsonify(filename=f.filename, url=url)

# ---------------- RUN ----------------
if __name__=="__main__":
    init_db()
    port=int(os.getenv('PORT',5000))
    socketio.run(app, host='0.0.0.0', port=port)
