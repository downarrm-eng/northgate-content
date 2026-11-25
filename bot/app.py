"""Northgate content-plan panel + Telegram auto-publisher.

- Content (posts.json + img/) lives in a git repo that Claude updates; this app runs `git pull` every minute.
- In the web panel you press "Готов к публикации" on a post.
- At the post's time, the bot publishes it to every channel in CHANNELS with a "Contact manager" button.
"""
import json, os, sqlite3, subprocess, threading, time, urllib.parse
from datetime import datetime, timezone
from functools import wraps

import requests
import hashlib
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session

def env(k, d=None):
    v = os.environ.get(k, d)
    if v is None:
        raise SystemExit(f"Missing setting {k} in .env")
    return v

BOT_TOKEN = env("BOT_TOKEN")
CHANNELS = [c.strip() for c in env("CHANNELS").split(",") if c.strip()]
MANAGER = env("MANAGER", "alexanorthgate")
PANEL_USER = env("PANEL_USER", "admin")
PANEL_PASSWORD = env("PANEL_PASSWORD")
CONTENT_DIR = env("CONTENT_DIR", os.path.join(os.path.dirname(__file__), "content"))
DB_PATH = env("DB_PATH", os.path.join(os.path.dirname(__file__), "state.db"))
PORT = int(env("PORT", "8080"))
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
lock = threading.Lock()

# ---------- state ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""create table if not exists state(id text primary key, ready int default 0, status text default 'draft',
                 published_at text, error text)""")
    return c

def load_posts():
    with open(os.path.join(CONTENT_DIR, "posts.json"), encoding="utf-8") as f:
        posts = json.load(f)["posts"]
    with db() as c:
        for p in posts:
            if p.get("already_published"):
                c.execute("insert or ignore into state(id,status) values(?, 'published')", (p["id"],))
            else:
                c.execute("insert or ignore into state(id) values(?)", (p["id"],))
        st = {r[0]: {"ready": bool(r[1]), "status": r[2], "published_at": r[3], "error": r[4]}
              for r in c.execute("select * from state")}
    for p in posts:
        p.update(st.get(p["id"], {}))
    return sorted(posts, key=lambda p: p["publish_at"])

# ---------- telegram ----------
def button(p):
    if p.get("vacancy"):
        label, msg = f"✉️ Apply for {p['vacancy']}", f"Hello! I want to apply for vacancy {p['vacancy']}"
    else:
        label, msg = "✉️ Contact manager", "Hello! I have a question about a job in Europe"
    url = f"https://t.me/{MANAGER}?text=" + urllib.parse.quote(msg)
    return json.dumps({"inline_keyboard": [[{"text": label, "url": url}]]})

def tg(method, data=None, files=None):
    r = requests.post(API + method, data=data, files=files, timeout=300)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description')}")
    return j["result"]

def send_post(chat, p):
    kb = button(p)
    text = p.get("text", "")
    imgs = [os.path.join(CONTENT_DIR, "img", i) for i in p.get("images", [])]
    if p.get("kind") == "poll":
        poll = p["poll"]
        tg("sendPoll", {"chat_id": chat, "question": poll["question"][:300],
                        "options": json.dumps([{"text": o[:100]} for o in poll["options"]]), "reply_markup": kb})
        return
    if p.get("video"):
        with open(os.path.join(CONTENT_DIR, "img", p["video"]), "rb") as f:
            data = {"chat_id": chat, "supports_streaming": "true"}
            if len(text) <= 1024:
                data.update(caption=text, reply_markup=kb)
            tg("sendVideo", data, {"video": f})
        if len(text) > 1024:
            tg("sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb, "disable_web_page_preview": True})
        return
    if not imgs:
        tg("sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb, "disable_web_page_preview": True})
        return
    short = len(text) <= 1024
    if len(imgs) == 1:
        with open(imgs[0], "rb") as f:
            data = {"chat_id": chat}
            if short:
                data.update(caption=text, reply_markup=kb)
            tg("sendPhoto", data, {"photo": f})
        if not short:
            tg("sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb, "disable_web_page_preview": True})
        return
    media, files = [], {}
    for n, path in enumerate(imgs[:10]):
        m = {"type": "photo", "media": f"attach://f{n}"}
        if n == 0 and short:
            m["caption"] = text
        media.append(m)
        files[f"f{n}"] = open(path, "rb")
    try:
        tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}, files)
    finally:
        for f in files.values():
            f.close()
    tg("sendMessage", {"chat_id": chat, "text": "👇 Apply / ask our manager" if short else text,
                       "reply_markup": kb, "disable_web_page_preview": True})

def publish(p):
    errors = []
    for ch in CHANNELS:
        try:
            send_post(ch, p)
        except Exception as e:
            errors.append(f"{ch}: {e}")
    with db() as c:
        if errors:
            c.execute("update state set status='error', error=? where id=?", ("; ".join(errors), p["id"]))
        else:
            c.execute("update state set status='published', published_at=?, error=null where id=?",
                      (datetime.now(timezone.utc).isoformat(), p["id"]))

APP_MTIME = os.path.getmtime(__file__)

def worker():
    while True:
        try:
            subprocess.run(["git", "-C", CONTENT_DIR, "pull", "-q", "--ff-only"], timeout=60, capture_output=True)
        except Exception:
            pass
        if os.path.getmtime(__file__) != APP_MTIME:
            print("bot code updated, restarting", flush=True)
            os._exit(0)
        try:
            now = datetime.now(timezone.utc)
            with lock:
                for p in load_posts():
                    if p.get("ready") and p.get("status") == "draft" and datetime.fromisoformat(p["publish_at"]) <= now:
                        publish(p)
        except Exception as e:
            print("worker error:", e, flush=True)
        time.sleep(60)

# ---------- web panel ----------
app = Flask(__name__)
app.secret_key = hashlib.sha256(("northgate-session:" + BOT_TOKEN + PANEL_PASSWORD).encode()).digest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=60*60*24*90)

LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Northgate · вход</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0c203e;font-family:system-ui,sans-serif}
form{background:#fff;padding:28px;border-radius:16px;width:300px;display:flex;flex-direction:column;gap:12px}h1{margin:0 0 6px;font-size:20px;color:#0c203e}
input{font:inherit;padding:11px 12px;border:1px solid #ccc;border-radius:9px}button{font:inherit;font-weight:700;padding:12px;border:0;border-radius:9px;background:#c9a54c;color:#0c203e;cursor:pointer}
.e{color:#b3261e;font-size:13px}</style></head><body><form method="post" action="/login"><h1>NORTHGATE · вход</h1>{err}
<input name="u" placeholder="Логин" autocomplete="username" required><input name="p" type="password" placeholder="Пароль" autocomplete="current-password" required>
<button>Войти</button></form></body></html>"""

def logged_in():
    if session.get("ok"):
        return True
    a_ = request.authorization
    return bool(a_ and a_.username == PANEL_USER and a_.password == PANEL_PASSWORD)

def auth(f):
    @wraps(f)
    def w(*a, **k):
        if not logged_in():
            return jsonify(ok=False, error="login required"), 401
        return f(*a, **k)
    return w

@app.get("/login")
def login_page():
    return LOGIN_HTML.replace("{err}", "")

@app.post("/login")
def login_post():
    if request.form.get("u") == PANEL_USER and request.form.get("p") == PANEL_PASSWORD:
        session.permanent = True
        session["ok"] = True
        return redirect("/")
    time.sleep(1)
    return LOGIN_HTML.replace("{err}", '<div class="e">Неверный логин или пароль</div>'), 401

@app.get("/")
def index():
    if not logged_in():
        return redirect("/login")
    return send_from_directory(os.path.dirname(__file__), "panel.html")

@app.get("/script")
def script_page():
    if not logged_in():
        return redirect("/login")
    return send_from_directory(os.path.dirname(__file__), "script.html")

@app.get("/api/script")
@auth
def api_script():
    with open(os.path.join(CONTENT_DIR, "script.json"), encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.get("/files/<path:fn>")
@auth
def files(fn):
    return send_from_directory(os.path.join(CONTENT_DIR, "files"), fn, as_attachment=True)

@app.get("/img/<path:fn>")
@auth
def img(fn):
    return send_from_directory(os.path.join(CONTENT_DIR, "img"), fn)

@app.get("/api/posts")
@auth
def api_posts():
    return jsonify({"posts": load_posts(), "channels": CHANNELS, "server_time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/ready")
@auth
def api_ready():
    j = request.get_json(force=True)
    with db() as c:
        c.execute("update state set ready=? where id=? and status!='published'", (1 if j.get("ready") else 0, j["id"]))
        if j.get("ready"):
            c.execute("update state set status='draft', error=null where id=? and status='error'", (j["id"],))
    return jsonify(ok=True)

@app.post("/api/publish_now")
@auth
def api_publish_now():
    pid = request.get_json(force=True)["id"]
    with lock:
        p = next((x for x in load_posts() if x["id"] == pid), None)
        if not p or p.get("status") == "published":
            return jsonify(ok=False, error="not found or already published"), 400
        publish(p)
    return jsonify(ok=True)

@app.post("/api/mark_published")
@auth
def api_mark_published():
    pid = request.get_json(force=True)["id"]
    with db() as c:
        c.execute("update state set status='published', ready=0, published_at=? where id=?",
                  (datetime.now(timezone.utc).isoformat(), pid))
    return jsonify(ok=True)

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
