"""Northgate content-plan panel + Telegram auto-publisher.

- Content (posts.json + img/) lives in a git repo that Claude updates; this app runs `git pull` every minute.
- In the web panel you press "Готов к публикации" on a post.
- At the post's time, the bot publishes it to every channel in CHANNELS with a "Contact manager" button.
"""
import json, os, sqlite3, subprocess, threading, time, urllib.parse
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

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
    r = requests.post(API + method, data=data, files=files, timeout=60)
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

def worker():
    while True:
        try:
            subprocess.run(["git", "-C", CONTENT_DIR, "pull", "-q", "--ff-only"], timeout=60, capture_output=True)
        except Exception:
            pass
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

def auth(f):
    @wraps(f)
    def w(*a, **k):
        a_ = request.authorization
        if not a_ or a_.username != PANEL_USER or a_.password != PANEL_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Northgate"'})
        return f(*a, **k)
    return w

@app.get("/")
@auth
def index():
    return send_from_directory(os.path.dirname(__file__), "panel.html")

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
