# Chat App (Flask + Socket.IO)

A single-file real-time chat server built with **Flask** and **Flask-SocketIO**, featuring public chat, private rooms, file uploads, admin controls, IP-based name recovery, logging, and persistent storage.

---

## Features

### Core Chat
- Real-time messaging via Socket.IO.
- Public lobby + private rooms at `/room/<code>`.
- Persistent usernames with token-based identity.
- Automatic reassociation of anonymous users via last-known IP.
- Message history (200 recent messages per room).
- Per-message storage of sender IP, timestamp, and token.

### File Uploads
- Uploads saved to disk under `uploads/`.
- Supports **images**, **audio**, **GIFs**, **WebM**, **PDF**, **text**, and other allowed formats.
- Automatic secure filenames.
- Max upload size: **50 MB**.

### Admin System
- Admin login at `/admin`.
- Username/password from environment variables.
- Supports **hashed password** via environment.
- Admin dashboard includes:
  - **Live view** of sessions, rooms, IPs, usernames, tokens.
  - **User management**: ban/unban, kick sessions, move users to rooms.
  - **Full logs** with pagination + filtering:
    - by IP
    - by token
    - by room
    - by date
    - by text search

### Database
- SQLite database: `chat_app.sqlite3` (configurable).
- Tables:
  - `tokens` — stores usernames, secret tokens, and public display tokens.
  - `messages` — every message with metadata.
  - `rooms` — created or visited room codes.
  - `banned` — tokens banned from using the service.

### Security
- Admin password hashing (PBKDF2 via Werkzeug).
- Banned tokens blocked at login.
- Uploaded filename sanitization.
- No 2FA (by design).

### Other
- Voice recorder uploads supported.
- Simple templated UI for admin pages.
- Room persistence with host token storage.

---

## Environment Variables

| Variable | Description | Default |
|---------|-------------|---------|
| `CHAT_DB` | SQLite database path | `chat_app.sqlite3` |
| `CHAT_UPLOADS` | Upload directory | `uploads/` |
| `PORT` | Server port | `5000` |
| `CHAT_SECRET` | Flask session secret | `change_me_now` |
| `CHAT_ADMIN_USER` | Admin username | `root` |
| `CHAT_ADMIN_PASS` | Admin password (plaintext; hashed at startup) | `root` |
| `CHAT_ADMIN_PASS_HASH` | Pre-hashed admin password | *None* |

If `CHAT_ADMIN_PASS_HASH` is set, the application uses it directly and ignores `CHAT_ADMIN_PASS`.

---

## Install

```bash
pip install flask flask-socketio eventlet werkzeug
```

---

## Run

```bash
python chat_app.py
```

Then access:
- Chat lobby: `http://localhost:5000/`
- Admin panel: `http://localhost:5000/admin`

---

## Directory Structure
```
chat_app.py
uploads/          # uploaded files stored here
chat_app.sqlite3  # created on first run
```

---

## Room System
- `/room/<code>` joins or creates a named room.
- Rooms stored in DB with:
  - `code`
  - `name`
  - `host_token`
  - timestamp

---

## Logging
Every message is stored with:
- sender IP
- text content
- timestamp
- token
- room code

Admin can filter logs and paginate through them.

---

## Identity Model
Each user receives:
- **secret token** (persistent identity)
- **public token** (safe to show)
- **username** (modifiable via registration events)

Anonymous users attempt to rediscover their previous non-anon name based on last-known IP.

---

## Ban System
- Bans stored in SQLite.
- Bans prevent registration.
- Admin can ban/unban via `/admin/manage`.

---

## Live Session Tracking
Tracked in memory:
- `sid_to_name`
- `sid_to_token`
- `sid_to_room`

Used for admin live monitoring and session actions.

---

## Notes
- No 2FA by design.
- All admin features are synchronous and simple.
- Eventlet is required for Socket.IO async mode.

---

## License