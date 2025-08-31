#!/usr/bin/env python3
"""
chat_app.py — single-file Flask + Flask-SocketIO chat with:
 - public + private rooms (/room/<code>)
 - IP-based anon -> last-known-username reassociation
 - SQLite logging (tokens, messages, rooms, bans)
 - file uploads persisted to disk (images/audio/other)
 - voice recorder uploads
 - admin login (env-configurable username + password), hashed password check
 - admin dashboard (View / Manage / Logs) with search + pagination/filtering
 - ban/unban, kick, move users (visible admin actions)
 - back buttons and helpful admin utilities
 - no 2FA (per request)
Install: pip install flask flask-socketio eventlet werkzeug
Run: python chat_app.py
"""

import os
import sqlite3
import uuid
import time
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple

from flask import (
    Flask,
    render_template_string,
    request,
    jsonify,
    redirect,
    url_for,
    session,
    flash,
    abort,
    send_from_directory,
)
from flask_socketio import SocketIO, emit, join_room as sio_join, leave_room as sio_leave
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------- CONFIG ----------------------------
DB_FILE = os.getenv("CHAT_DB", "chat_app.sqlite3")
UPLOAD_DIR = Path(os.getenv("CHAT_UPLOADS", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".webm", ".mp3", ".wav", ".ogg", ".pdf", ".txt"}

PORT = int(os.getenv("PORT", 5000))
SECRET_KEY = os.getenv("CHAT_SECRET", "change_me_now")
ADMIN_USER = os.getenv("CHAT_ADMIN_USER", "root")
# Accept either a plain password in env (ADMIN_PASS) or a hashed password in ADMIN_PASS_HASH.
ADMIN_PASS_ENV = os.getenv("CHAT_ADMIN_PASS", "root")  # kept for convenience; hashed immediately
ADMIN_PASS_HASH = os.getenv("CHAT_ADMIN_PASS_HASH", None)

# If an explicit hashed password is not provided, hash ADMIN_PASS_ENV at startup.
if ADMIN_PASS_HASH:
    ADMIN_PASS_HASHED = ADMIN_PASS_HASH
else:
    ADMIN_PASS_HASHED = generate_password_hash(ADMIN_PASS_ENV)

# Flask + SocketIO
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, async_mode="eventlet")

# ---------------------------- DATABASE ---------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS tokens(
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            public_token TEXT NOT NULL,
            created_ts REAL NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_ip TEXT NOT NULL,
            ts REAL NOT NULL,
            content TEXT NOT NULL,
            token TEXT,
            room_code TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS rooms(
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host_token TEXT,
            created_ts REAL NOT NULL
        )"""
    )
    cur.execute("""CREATE TABLE IF NOT EXISTS banned(token TEXT PRIMARY KEY)""")
    conn.commit()
    conn.close()


def db_run(query: str, params: tuple = (), fetch: bool = False):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(query, params)
    data = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return data


# ------------------------- TOKEN HELPERS -------------------------
def ensure_token_record(token: str, name: str):
    """Create or update token->name mapping and ensure public token exists."""
    rows = db_run("SELECT public_token FROM tokens WHERE token=?", (token,), fetch=True)
    if rows:
        db_run("UPDATE tokens SET name=? WHERE token=?", (name, token))
    else:
        public = str(uuid.uuid4())[:8]
        db_run(
            "INSERT OR REPLACE INTO tokens (token,name,public_token,created_ts) VALUES (?,?,?,?)",
            (token, name, public, time.time()),
        )


def get_name_by_token(token: str) -> Optional[str]:
    if not token:
        return None
    rows = db_run("SELECT name FROM tokens WHERE token=?", (token,), fetch=True)
    return rows[0][0] if rows else None


def get_public_by_token(token: str) -> Optional[str]:
    if not token:
        return None
    rows = db_run("SELECT public_token FROM tokens WHERE token=?", (token,), fetch=True)
    return rows[0][0] if rows else None


# ------------------------- MESSAGE HELPERS -----------------------
def store_message(sender_ip: str, content: str, token: str = None, room_code: str = None):
    db_run(
        "INSERT INTO messages (sender_ip, ts, content, token, room_code) VALUES (?,?,?,?,?)",
        (sender_ip, time.time(), content, token, room_code),
    )


def recent_messages(limit: int = 200, room_code: Optional[str] = None):
    if room_code:
        rows = db_run(
            "SELECT sender_ip, ts, content, token FROM messages WHERE room_code=? ORDER BY ts DESC LIMIT ?",
            (room_code, limit),
            fetch=True,
        )
    else:
        rows = db_run(
            "SELECT sender_ip, ts, content, token FROM messages ORDER BY ts DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
    rows.reverse()
    return rows


# --------------------------- ROOM HELPERS ------------------------
def create_room(code: str, name: str = "", host_token: str = ""):
    db_run(
        "INSERT OR REPLACE INTO rooms (code, name, host_token, created_ts) VALUES (?,?,?,?)",
        (code, name or code, host_token, time.time()),
    )


def get_all_rooms():
    rows = db_run("SELECT code, name, host_token, created_ts FROM rooms", fetch=True)
    return rows or []


def room_exists(code: str) -> bool:
    rows = db_run("SELECT code FROM rooms WHERE code=?", (code,), fetch=True)
    return bool(rows)


# ---------------------------- BANS -------------------------------
def ban_token(token: str):
    db_run("INSERT OR REPLACE INTO banned(token) VALUES (?)", (token,))


def unban_token(token: str):
    db_run("DELETE FROM banned WHERE token=?", (token,))


def is_banned(token: str) -> bool:
    rows = db_run("SELECT token FROM banned WHERE token=?", (token,), fetch=True)
    return bool(rows)


# ---------------------------- IP LINKS ---------------------------
def ips_for_token(token: str) -> List[str]:
    rows = db_run("SELECT DISTINCT sender_ip FROM messages WHERE token=?", (token,), fetch=True)
    return [r[0] for r in rows] if rows else []


def linked_names_for_ip(ip: str) -> List[str]:
    rows = db_run(
        "SELECT DISTINCT t.name FROM messages m JOIN tokens t ON t.token=m.token WHERE m.sender_ip=?", (ip,), fetch=True
    )
    return [r[0] for r in rows] if rows else []


def last_non_anon_name_for_ip(ip: str) -> Optional[str]:
    rows = db_run(
        "SELECT t.name FROM messages m JOIN tokens t ON t.token=m.token WHERE m.sender_ip=? ORDER BY m.ts DESC LIMIT 50",
        (ip,),
        fetch=True,
    )
    if rows:
        for (nm,) in rows:
            if nm and not nm.lower().startswith("anon"):
                return nm
    return None


# --------------------------- LIVE MAPS ---------------------------
sid_to_name = {}  # sid -> display name
sid_to_token = {}  # sid -> secret token
sid_to_room = {}  # sid -> room code or None

# --------------------------- UTIL --------------------------------
def get_client_ip():
    xff = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr


def allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXT


def save_upload(file_storage):
    """Save an uploaded FileStorage to disk in UPLOAD_DIR, return public URL path."""
    filename = secure_filename(file_storage.filename)
    if not filename:
        filename = "file"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        # allow saving but mark extension — block by default
        raise ValueError("file type not allowed")
    unique = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{filename}"
    path = UPLOAD_DIR / unique
    file_storage.save(path)
    return url_for("uploaded_file", filename=unique, _external=True)


# ------------------------ SOCKET.IO HANDLERS -----------------------
@socketio.on("connect")
def on_connect():
    emit("connect_ack", {"ok": True})


@socketio.on("register")
def on_register(data):
    # data: {name, token?, room?}
    req_name = (data.get("name") or "").strip()
    provided_token = data.get("token")
    desired_room = data.get("room") or data.get("room_code") or None

    client_ip = get_client_ip()

    # resolve token and name
    if provided_token:
        stored_name = get_name_by_token(provided_token)
        if stored_name:
            name = stored_name
            token = provided_token
        else:
            name = req_name or "anon"
            token = str(uuid.uuid4())
            ensure_token_record(token, name)
    else:
        name = req_name or "anon"
        token = str(uuid.uuid4())
        ensure_token_record(token, name)

    # if anon-ish, try to re-associate by IP
    if not name or name.lower().startswith("anon"):
        prev = last_non_anon_name_for_ip(client_ip)
        if prev:
            name = prev
            ensure_token_record(token, name)

    if is_banned(token):
        emit("welcome", {"name": name, "token": token, "public_token": get_public_by_token(token), "banned": True})
        return

    sid = request.sid
    sid_to_name[sid] = name
    sid_to_token[sid] = token
    sid_to_room[sid] = None

    # join room if requested and exists
    if desired_room and room_exists(desired_room):
        sid_to_room[sid] = desired_room
        sio_join(desired_room)

    pub = get_public_by_token(token)
    emit("welcome", {"name": name, "token": token, "public_token": pub})

    # send recent history scoped to room
    history = recent_messages(limit=200, room_code=sid_to_room.get(sid))
    lines = []
    for sender_ip, ts, txt, tok in history:
        nickname = get_name_by_token(tok) or "anon"
        pubt = get_public_by_token(tok) or "?"
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"<span class='user' data-pub='{pubt}'>{nickname}</span> - {when} - {txt}")
    emit("history", lines)


@socketio.on("msg")
def on_msg(data):
    # data: string or {text, room}
    text = data if isinstance(data, str) else data.get("text", "")
    room = None
    if isinstance(data, dict):
        room = data.get("room")
    sid = request.sid
    token = sid_to_token.get(sid)
    name = sid_to_name.get(sid, "anon")
    client_ip = get_client_ip()
    # store
    store_message(sender_ip=client_ip, content=text, token=token, room_code=room)
    # broadcast
    pub = get_public_by_token(token) or "?"
    now = datetime.now().strftime("%H:%M")
    line = f"<span class='user' data-pub='{pub}'>{name}</span> - {now} - {text}"
    if room:
        emit("chat_line", line, room=room)
    else:
        emit("chat_line", line, broadcast=True)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    sid_to_name.pop(sid, None)
    sid_to_token.pop(sid, None)
    sid_to_room.pop(sid, None)


# --------------------------- ADMIN HELPERS ------------------------
def admin_required():
    return session.get("admin") is True


# ------------------------- ADMIN TEMPLATES ------------------------
ADMIN_LOGIN_HTML = """
<!doctype html>
<html>
<head><title>Admin Login</title>
<style>body{font-family:Arial;margin:20px}form{max-width:360px}input{display:block;margin:8px 0;padding:8px;width:100%}button{padding:8px}</style></head>
<body>
  <h2>Admin Login</h2>
  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div style="color:red">{{ msgs[0] }}</div>
    {% endif %}
  {% endwith %}
  <form method="post">
    <input name="username" placeholder="username" required />
    <input name="password" type="password" placeholder="password" required />
    <button type="submit">Login</button>
  </form>
  <p><a href="{{ url_for('index') }}">Back to chat</a></p>
</body>
</html>
"""

ADMIN_MENU_HTML = """
<!doctype html>
<html><head><title>Admin Dashboard</title>
<style>body{font-family:Arial;margin:20px}a.btn{display:inline-block;padding:8px 12px;margin:6px;border:1px solid #999;border-radius:6px;text-decoration:none;color:black}</style></head>
<body>
  <h2>Admin Dashboard</h2>
  <p>
    <a class="btn" href="{{ url_for('admin_view') }}">View Users & Rooms</a>
    <a class="btn" href="{{ url_for('admin_manage') }}">Manage Users</a>
    <a class="btn" href="{{ url_for('admin_logs') }}">Logs</a>
    <a class="btn" href="{{ url_for('admin_logout') }}">Logout</a>
  </p>
  <p><a href="{{ url_for('index') }}">⬅ Back to Chat</a></p>
</body></html>
"""

ADMIN_VIEW_HTML = """
<!doctype html>
<html><head><title>Admin • View</title>
<style>body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px}.code{font-family:monospace;background:#f9f9f9;padding:2px 6px;border-radius:4px}</style></head>
<body>
<h2>Users & Rooms</h2>
<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>

<h3>Live Participants</h3>
<table>
<tr><th>sid (short)</th><th>Name</th><th>Public</th><th>Secret</th><th>IP(s)</th><th>Room</th></tr>
{% for sid, v in live.items() %}
<tr>
  <td class="code">{{ sid[:8] }}</td>
  <td>{{ v.name }}</td>
  <td class="code">{{ v.public or '-' }}</td>
  <td class="code">{{ v.secret or '-' }}</td>
  <td>{{ v.ips|join(', ') if v.ips else '-' }}</td>
  <td>{{ v.room or 'Lobby' }}</td>
</tr>
{% endfor %}
</table>

<h3>Registered Users</h3>
<table>
<tr><th>Name</th><th>Public</th><th>Secret</th><th>IPs</th></tr>
{% for name, secret, public, ips in users %}
<tr>
  <td>{{ name }}</td>
  <td class="code">{{ public }}</td>
  <td class="code">{{ secret }}</td>
  <td>{{ ips|join(', ') if ips else '-' }}</td>
</tr>
{% endfor %}
</table>

<h3>Linked usernames by IP</h3>
<table>
<tr><th>IP</th><th>Usernames</th></tr>
{% for ip, names in linked.items() %}
<tr><td>{{ ip }}</td><td>{{ names|join(', ') }}</td></tr>
{% endfor %}
</table>

<h3>Rooms</h3>
<table>
<tr><th>Code</th><th>Name</th><th>Host</th><th>Created</th><th>Live participants</th></tr>
{% for r in rooms %}
<tr>
  <td class="code">{{ r[0] }}</td>
  <td>{{ r[1] }}</td>
  <td class="code">{{ r[2] or '-' }}</td>
  <td>{{ (r[3] and (datetime.fromtimestamp(r[3]).strftime('%Y-%m-%d %H:%M'))) or '-' }}</td>
  <td>
    {% for p in (live_by_room.get(r[0]) or []) %}
      <div>{{ p[0] }} <small class="code">{{ p[1][:8] }}</small></div>
    {% endfor %}
  </td>
</tr>
{% endfor %}
</table>

<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>
</body></html>
"""

ADMIN_MANAGE_HTML = """
<!doctype html>
<html><head><title>Admin • Manage</title><style>body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px}</style></head><body>
<h2>Manage Users</h2>
<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>

<h3>Ban / Unban tokens</h3>
<form method="post" action="{{ url_for('admin_ban') }}">
  <label>Token to ban: <input name="token" placeholder="secret token" required/></label>
  <button type="submit">Ban</button>
</form>
<form method="post" action="{{ url_for('admin_unban') }}" style="margin-top:8px">
  <label>Token to unban: <input name="token" placeholder="secret token" required/></label>
  <button type="submit">Unban</button>
</form>

<h3>Active sessions (kick / move)</h3>
<table>
<tr><th>sid</th><th>name</th><th>token</th><th>room</th><th>actions</th></tr>
{% for sid, v in live.items() %}
<tr>
  <td class="code">{{ sid[:8] }}</td>
  <td>{{ v.name }}</td>
  <td class="code">{{ v.secret }}</td>
  <td>{{ v.room or 'Lobby' }}</td>
  <td>
    <form style="display:inline" method="post" action="{{ url_for('admin_kick') }}">
      <input type="hidden" name="sid" value="{{ sid }}"/><button type="submit">Kick</button>
    </form>
    <form style="display:inline" method="post" action="{{ url_for('admin_move') }}">
      <input type="hidden" name="sid" value="{{ sid }}"/><input name="room" placeholder="ROOMCODE"/><button type="submit">Move</button>
    </form>
  </td>
</tr>
{% endfor %}
</table>

<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>
</body></html>
"""

ADMIN_LOGS_HTML = """
<!doctype html>
<html><head><title>Admin • Logs</title>
<style>body{font-family:Arial;margin:20px}form.inline{display:flex;gap:8px;align-items:center;margin-bottom:12px}pre{white-space:pre-wrap;background:#f9f9f9;padding:12px;border:1px solid #ddd}</style></head>
<body>
<h2>Message Logs</h2>
<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>

<form method="get" action="{{ url_for('admin_logs') }}" class="inline">
  <input name="q" placeholder="search text or filename" value="{{ q or '' }}"/>
  <input name="room" placeholder="room" value="{{ room or '' }}"/>
  <input name="token" placeholder="token" value="{{ token or '' }}"/>
  <input name="ip" placeholder="ip" value="{{ ip or '' }}"/>
  <label>from <input type="date" name="from" value="{{ date_from or '' }}"/></label>
  <label>to <input type="date" name="to" value="{{ date_to or '' }}"/></label>
  <button type="submit">Filter</button>
</form>

<div>Showing page {{ page }} / {{ total_pages }} ({{ total }} results)</div>

<pre>
{% for ip, ts, content, token, room in results %}
[{{ datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') }}] IP: {{ ip }} | Room: {{ room or '-' }} | Token: {{ token }}
{{ content }}

{% endfor %}
</pre>

<div>
{% if page > 1 %}
  <a href="{{ url_for('admin_logs') }}?{{ qs_prev }}">⬅ Prev</a>
{% endif %}
{% if page < total_pages %}
  <a href="{{ url_for('admin_logs') }}?{{ qs_next }}">Next ➡</a>
{% endif %}
</div>

<p><a href="{{ url_for('admin_dashboard') }}">⬅ Back</a></p>
</body></html>
"""

# ------------------------- ADMIN ROUTES ---------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username")
        p = request.form.get("password")
        if u == ADMIN_USER and check_password_hash(ADMIN_PASS_HASHED, p):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials")
        return redirect(url_for("admin_login"))
    return render_template_string(ADMIN_LOGIN_HTML)


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    return render_template_string(ADMIN_MENU_HTML)


@app.route("/admin/view")
def admin_view():
    if not admin_required():
        return redirect(url_for("admin_login"))
    # registered users + ips
    users = db_run("SELECT name, token, public_token FROM tokens", fetch=True) or []
    ip_pairs = db_run("SELECT DISTINCT sender_ip, token FROM messages", fetch=True) or []
    token_to_ips = {}
    for ip, tok in ip_pairs:
        token_to_ips.setdefault(tok, []).append(ip)
    users_with_ips = [(n, t, p, token_to_ips.get(t, [])) for n, t, p in users]

    # linked names by ip
    ips = db_run("SELECT DISTINCT sender_ip FROM messages", fetch=True) or []
    linked = {}
    for (ip,) in ips:
        linked[ip] = linked_names_for_ip(ip)

    rooms = get_all_rooms()

    live = {}
    for sid, name in sid_to_name.items():
        secret = sid_to_token.get(sid)
        public = get_public_by_token(secret)
        ip_list = ips_for_token(secret)
        live[sid] = type("V", (), {"name": name, "secret": secret, "public": public, "ips": ip_list, "room": sid_to_room.get(sid)})

    live_by_room = {}
    for sid, v in live.items():
        room = v.room or "Lobby"
        live_by_room.setdefault(room, []).append((v.name, v.secret or ""))

    return render_template_string(
        ADMIN_VIEW_HTML, users=users_with_ips, linked=linked, rooms=rooms, live=live, live_by_room=live_by_room, datetime=datetime
    )


@app.route("/admin/manage")
def admin_manage():
    if not admin_required():
        return redirect(url_for("admin_login"))
    live = {}
    for sid, name in sid_to_name.items():
        secret = sid_to_token.get(sid)
        public = get_public_by_token(secret)
        ip_list = ips_for_token(secret)
        live[sid] = type("V", (), {"name": name, "secret": secret, "public": public, "ips": ip_list, "room": sid_to_room.get(sid)})
    return render_template_string(ADMIN_MANAGE_HTML, live=live)


@app.route("/admin/logs")
def admin_logs():
    if not admin_required():
        return redirect(url_for("admin_login"))

    # filtering & pagination
    q = request.args.get("q", "").strip()
    room = request.args.get("room", "").strip()
    token = request.args.get("token", "").strip()
    ip = request.args.get("ip", "").strip()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    page = max(1, int(request.args.get("page", "1")))
    per_page = 30

    # build where
    where_clauses = []
    params = []
    if q:
        where_clauses.append("content LIKE ?")
        params.append(f"%{q}%")
    if room:
        where_clauses.append("room_code=?")
        params.append(room)
    if token:
        where_clauses.append("token=?")
        params.append(token)
    if ip:
        where_clauses.append("sender_ip=?")
        params.append(ip)
    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            where_clauses.append("ts >= ?")
            params.append(time.mktime(dt.timetuple()))
        except Exception:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            where_clauses.append("ts < ?")
            params.append(time.mktime(dt.timetuple()))
        except Exception:
            pass

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    count_row = db_run(f"SELECT COUNT(*) FROM messages {where_sql}", tuple(params), fetch=True)
    total = count_row[0][0] if count_row else 0
    total_pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    rows = db_run(
        f"SELECT sender_ip, ts, content, token, room_code FROM messages {where_sql} ORDER BY ts DESC LIMIT ? OFFSET ?",
        tuple(params) + (per_page, offset),
        fetch=True,
    ) or []
    # render with next/prev qs
    from urllib.parse import urlencode

    def qs_for(p):
        qd = {"q": q, "room": room, "token": token, "ip": ip, "from": date_from, "to": date_to, "page": p}
        return urlencode({k: v for k, v in qd.items() if v})

    qs_prev = qs_for(page - 1)
    qs_next = qs_for(page + 1)
    return render_template_string(
        ADMIN_LOGS_HTML,
        results=rows,
        page=page,
        total=total,
        total_pages=total_pages,
        qs_prev=qs_prev,
        qs_next=qs_next,
        q=q,
        room=room,
        token=token,
        ip=ip,
        date_from=date_from,
        date_to=date_to,
        datetime=datetime,
    )


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


# ----------------------- ADMIN ACTIONS ----------------------------
@app.route("/admin/ban", methods=["POST"])
def admin_ban():
    if not admin_required():
        return redirect(url_for("admin_login"))
    token = request.form.get("token")
    if not token:
        flash("token required")
        return redirect(url_for("admin_manage"))
    ban_token(token)
    # disconnect sessions for that token
    to_disconnect = [sid for sid, tok in sid_to_token.items() if tok == token]
    for sid in to_disconnect:
        try:
            socketio.disconnect(sid)
        except Exception:
            pass
    flash("banned")
    return redirect(url_for("admin_manage"))


@app.route("/admin/unban", methods=["POST"])
def admin_unban():
    if not admin_required():
        return redirect(url_for("admin_login"))
    token = request.form.get("token")
    if not token:
        flash("token required")
        return redirect(url_for("admin_manage"))
    unban_token(token)
    flash("unbanned")
    return redirect(url_for("admin_manage"))


@app.route("/admin/kick", methods=["POST"])
def admin_kick():
    if not admin_required():
        return redirect(url_for("admin_login"))
    sid = request.form.get("sid")
    if not sid:
        flash("sid required")
        return redirect(url_for("admin_manage"))
    try:
        room = sid_to_room.get(sid)
        if room:
            try:
                sio_leave(room, sid=sid)
            except Exception:
                pass
        socketio.disconnect(sid)
        flash("kicked")
    except Exception as e:
        flash(f"error: {e}")
    return redirect(url_for("admin_manage"))


@app.route("/admin/move", methods=["POST"])
def admin_move():
    if not admin_required():
        return redirect(url_for("admin_login"))
    sid = request.form.get("sid")
    room = (request.form.get("room") or "").strip()
    if not sid:
        flash("sid required")
        return redirect(url_for("admin_manage"))
    try:
        current = sid_to_room.get(sid)
        if current:
            try:
                sio_leave(current, sid=sid)
            except Exception:
                pass
        if not room:
            sid_to_room[sid] = None
            flash("removed from room")
        else:
            if not room_exists(room):
                create_room(room, room, "")
            sio_join(room, sid=sid)
            sid_to_room[sid] = room
            flash("moved")
    except Exception as e:
        flash(f"error: {e}")
    return redirect(url_for("admin_manage"))


# ----------------------- UPLOAD ROUTES ---------------------------
@app.route("/upload", methods=["POST"])
def upload():
    """
    Accepts multipart/form-data file field 'file'.
    Saves file to UPLOAD_DIR and returns JSON {filename, url}
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file"), 400
    try:
        url = save_upload(f)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(filename=f.filename, url=url)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# ---------------------- CHAT UI / ROOM ----------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Chat — Rooms</title>
  <style>
    body{font-family:Arial;margin:16px}
    #chat{border:1px solid #ccc;height:420px;overflow:auto;padding:6px}
    p{margin:0 0 6px}
    input{padding:6px}
    button{padding:6px;margin-left:6px}
    .user{color:blue;cursor:pointer}
    .menu{margin-bottom:8px}
    .info{color:#666;font-size:90%}
  </style>
</head>
<body>
  <h3 id="title">Public Chat</h3>
  <div class="menu" id="menu">
    <input id="nick" placeholder="nickname" />
    <button id="enter" disabled>Enter</button>
    <button id="host">Host Room</button>
    <input id="joinCode" placeholder="room code" style="width:120px" />
    <button id="joinBtn">Join</button>
    <a href="/admin" style="margin-left:12px">Admin</a>
  </div>

  <div id="chatui" style="display:none;margin-top:10px">
    <div id="roomInfo" class="info"></div>
    <div id="chat"></div>
    <div style="margin-top:6px">
      <input id="msg" placeholder="message" style="width:58%" />
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
  const p=document.createElement('p'); p.innerHTML=txt;
  document.getElementById('chat').appendChild(p);
  document.getElementById('chat').scrollTop=document.getElementById('chat').scrollHeight;
}
function setTitle(){
  document.getElementById('title').innerText = currentRoom ? ('Private Chat room code - ' + currentRoom) : 'Public Chat';
}
socket.on('connect', ()=>{ document.getElementById('enter').disabled=false; });

document.getElementById('enter').onclick = ()=> {
  const nick = document.getElementById('nick').value.trim();
  if(!nick){ alert('enter nick'); return; }
  const stored = localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

document.getElementById('host').onclick = ()=>{
  const nick=document.getElementById('nick').value.trim() || 'anon';
  const code = Math.random().toString(36).slice(2,8).toUpperCase();
  currentRoom = code;
  document.getElementById('joinCode').value = code;
  document.getElementById('roomInfo').innerText = 'Room: ' + code + ' (link: ' + location.origin + '/room/' + code + ')';
  document.getElementById('chatui').style.display='block';
  setTitle();
  const stored=localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

document.getElementById('joinBtn').onclick = ()=>{
  const code = document.getElementById('joinCode').value.trim();
  if(!code){ alert('enter code'); return; }
  currentRoom = code;
  document.getElementById('roomInfo').innerText = 'Room: ' + code + ' (link: ' + location.origin + '/room/' + code + ')';
  document.getElementById('chatui').style.display='block';
  setTitle();
  const nick = document.getElementById('nick').value.trim() || 'anon';
  const stored = localStorage.getItem('chatToken');
  const payload = stored ? {name:nick, token:stored, room: currentRoom} : {name:nick, room: currentRoom};
  socket.emit('register', payload);
};

(function(){
  if(DEFAULT_ROOM){
    currentRoom = DEFAULT_ROOM;
    document.getElementById('joinCode').value = DEFAULT_ROOM;
    document.getElementById('roomInfo').innerText = 'Room: ' + DEFAULT_ROOM + ' (link: ' + location.origin + '/room/' + DEFAULT_ROOM + ')';
    document.getElementById('chatui').style.display='block';
    setTitle();
  }
})();

socket.on('welcome', data=>{
  localStorage.setItem('chatToken', data.token);
  document.getElementById('chatui').style.display='block';
  addLine('[INFO] You are '+data.name+' (public id: '+data.public_token+')' + (data.banned ? ' [BANNED]' : ''));
});
socket.on('history', lines=>{ lines.forEach(l=>addLine(l)); });
socket.on('chat_line', line=>addLine(line));

document.getElementById('send').onclick = ()=>{
  const txt = document.getElementById('msg').value.trim(); if(!txt) return;
  socket.emit('msg', {text:txt, room: currentRoom}); document.getElementById('msg').value = '';
};
document.getElementById('msg').addEventListener('keypress', e=>{ if(e.key==='Enter'){ document.getElementById('send').click(); } });

document.addEventListener('click', e=>{ if(e.target.classList.contains('user')) alert('Public token: '+e.target.dataset.pub); });

// Upload flow: send file to /upload, server returns URL; then emit message with tag
document.getElementById('uploadBtn').onclick = async ()=>{
  const f = document.getElementById('fileInput').files[0];
  if(!f){ alert('choose file'); return; }
  const form = new FormData(); form.append('file', f);
  const res = await fetch('/upload', {method:'POST', body: form});
  const j = await res.json();
  if(j.error){ alert(j.error); return; }
  const ext = f.name.split('.').pop().toLowerCase();
  let msg;
  if(f.type.startsWith('image/')) msg = `<img src="${j.url}" style="max-width:300px"/>`;
  else if(f.type.startsWith('audio/')) msg = `<audio controls src="${j.url}"></audio>`;
  else msg = `<a href="${j.url}" target="_blank">${j.filename}</a>`;
  socket.emit('msg', {text: msg, room: currentRoom});
};

// recorder -> upload blob to /upload
let rec, chunks = [];
document.getElementById('recBtn').onclick = async ()=>{
  if(!rec || rec.state==='inactive'){
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    rec = new MediaRecorder(stream);
    rec.ondataavailable = e=>chunks.push(e.data);
    rec.onstop = async ()=>{
      const blob = new Blob(chunks, {type: 'audio/webm'}); chunks=[];
      const form = new FormData(); form.append('file', blob, 'voice.webm');
      const res = await fetch('/upload', {method:'POST', body: form});
      const j = await res.json();
      if(j.error){ alert(j.error); return; }
      const msg = `<audio controls src="${j.url}"></audio>`;
      socket.emit('msg', {text: msg, room: currentRoom});
    };
    rec.start(); document.getElementById('recBtn').innerText='⏹ Stop';
  } else { rec.stop(); document.getElementById('recBtn').innerText='🎤 Record'; }
};
</script>
</body>
</html>
"""

# ------------------------- ROUTES & START --------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML, default_room="", admin_pass=ADMIN_USER)


@app.route("/room/<code>")
def room_route(code):
    # persist room server-side
    if not room_exists(code):
        create_room(code, code, "")
    return render_template_string(INDEX_HTML, default_room=code, admin_pass=ADMIN_USER)


if __name__ == "__main__":
    init_db()
    print(f"Starting server on 0.0.0.0:{PORT}  (admin_user={ADMIN_USER})")
    socketio.run(app, host="0.0.0.0", port=PORT)
