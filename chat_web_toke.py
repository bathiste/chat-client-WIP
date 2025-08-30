#!/usr/bin/env python3
import os, uuid, sqlite3, time, random, string, base64
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, abort
from flask_socketio import SocketIO, emit, join_room as sio_join
from werkzeug.utils import secure_filename

# ---------------- CONFIG ----------------
DB_FILE = "chat_web_token.db"
MAX_CONTENT_LENGTH = 20 * 1024 * 1024
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASS", "changeme")

# ---------------- DB HELPERS ----------------
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
        cur.execute("SELECT sender_ip, ts, content, token FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
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

def generate_room_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ---------------- APP SETUP ----------------
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
    desired_room = data.get('room') or None

    if provided_token and get_name_by_token(provided_token):
        name = get_name_by_token(provided_token)
        token = provided_token
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
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    pub = get_public_token_by_token(token) or "?"
    now = datetime.now().strftime('%H:%M')
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room:
        emit('chat_line', line, room=room)
    else:
        emit('chat_line', line, broadcast=True)

# ---------------- CREATE ROOM ----------------
@app.route('/create_room', methods=['POST'])
def create_room():
    data = request.json
    name = (data.get('name') or '').strip() or "anon"
    token = data.get('token') or str(uuid.uuid4())
    room_code = generate_room_code()
    create_room_in_db(room_code, name, token)
    return jsonify(code=room_code, link=f"/?room={room_code}")

# ---------------- UPLOAD HANDLER ----------------
@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f:
        return jsonify(error="No file uploaded")
    content = f.read()
    ext = os.path.splitext(f.filename)[1].lower()
    if ext in ['.png','.jpg','.jpeg','.gif','.bmp','.webp']:
        b64 = base64.b64encode(content).decode()
        url = f"data:image/{ext[1:]};base64,{b64}"
    elif ext in ['.webm','.mp3','.wav']:
        b64 = base64.b64encode(content).decode()
        url = f"data:audio/{ext[1:]};base64,{b64}"
    else:
        return jsonify(error="Unsupported file type")
    return jsonify(filename=f.filename, url=url)

# ---------------- INDEX HTML ----------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <title>Chat with Voice & Rooms</title>
  <style>
    body{font-family:Arial;margin:20px;}
    #chat{border:1px solid #ccc;height:400px;overflow-y:auto;padding:5px;}
    #chat p{margin:0;padding:2px;word-break:break-word;}
    #msg{width:70%;}
    .user{color:blue;cursor:pointer;}
    #menu{margin-bottom:10px;}
    #joinCode{width:120px;}
  </style>
</head>
<body>
<h2>Public Chat</h2>
<div id="menu">
  <input id="nick" placeholder="nickname"/>
  <button id="enter" disabled>Enter</button>
  <button id="host">Host Room</button>
  <input id="joinCode" placeholder="room code"/>
  <button id="joinBtn">Join</button>
</div>
<div id="chatui" style="display:none;">
  <div id="chat"></div>
  <input id="msg" placeholder="message"/>
  <button id="send">Send</button>
  <button id="recBtn">🎤 Record</button>
  <div id="uploadSection">
    <input type="file" id="fileInput"/>
    <button id="uploadBtn">Upload</button>
  </div>
</div>
<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.1/socket.io.min.js"></script>
<script>
const socket = io();
let currentRoom = null;

function addLine(txt){
  const p=document.createElement('p');
  p.innerHTML=txt;
  document.getElementById('chat').appendChild(p);
  document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight;
}

socket.on('connect',()=>{document.getElementById('enter').disabled=false;});
document.getElementById('enter').onclick=()=>{
  const nick=document.getElementById('nick').value.trim();
  if(!nick){alert('enter nick');return;}
  const stored=localStorage.getItem('chatToken');
  socket.emit('register',{name:nick, token:stored, room:currentRoom});
};

document.getElementById('host').onclick=async()=>{
  const nick=document.getElementById('nick').value.trim();
  if(!nick){alert('enter nick');return;}
  const token = localStorage.getItem('chatToken') || null;
  const res = await fetch('/create_room',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:nick,token:token})});
  const j = await res.json();
  currentRoom=j.code;
  document.getElementById('joinCode').value=j.code;
  alert('Room created: '+j.link);
};

document.getElementById('joinBtn').onclick=()=>{
  const code = document.getElementById('joinCode').value.trim();
  if(!code){alert('enter code');return;}
  currentRoom=code;
  document.getElementById('chatui').style.display='block';
};

socket.on('welcome',data=>{
  localStorage.setItem('chatToken',data.token);
  document.getElementById('chatui').style.display='block';
  addLine(`[INFO] You are ${data.name} (public id: ${data.public_token})`);
});

socket.on('history',lines=>{lines.forEach(l=>addLine(l));});
socket.on('chat_line',line=>addLine(line));

document.getElementById('send').onclick=()=>{
  const txt=document.getElementById('msg').value.trim();
  if(!txt)return;
  socket.emit('msg',{text:txt, room:currentRoom});
  document.getElementById('msg').value='';
};
document.getElementById('msg').addEventListener('keypress',e=>{if(e.key==='Enter'){document.getElementById('send').click();}});

// click username
document.addEventListener('click',e=>{if(e.target.classList.contains('user')){alert('Public token: '+e.target.dataset.pub);}});

// upload file
document.getElementById('uploadBtn').onclick=()=>{
  const f=document.getElementById('fileInput').files[0];
  if(!f){alert('choose file');return;}
  const form=new FormData();form.append('file',f);
  fetch('/upload',{method:'POST',body:form}).then(r=>r.json()).then(d=>{
    if(d.error){alert(d.error);return;}
    socket.emit('msg',{text:`<a>${d.filename}</a><br><img src="${d.url}"/>`, room:currentRoom});
  });
};

// voice recorder
let rec,chunks=[];
document.getElementById('recBtn').onclick=async()=>{
  if(!rec||rec.state==='inactive'){
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    rec=new MediaRecorder(stream);
    rec.ondataavailable=e=>chunks.push(e.data);
    rec.onstop=async()=>{
      const blob=new Blob(chunks,{type:'audio/webm'});chunks=[];
      const form=new FormData();form.append('file',blob,'voice.webm');
      const res = await fetch('/upload',{method:'POST',body:form});
      const d = await res.json();
      if(!d.error){socket.emit('msg',{text:`<audio controls src="${d.url}"></audio>`, room:currentRoom});}
    };
    rec.start();
    document.getElementById('recBtn').innerText='⏹ Stop';
  } else { rec.stop(); document.getElementById('recBtn').innerText='🎤 Record'; }
};
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

if __name__ == '__main__':
    init_db()
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT',5000)))
