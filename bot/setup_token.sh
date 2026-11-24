#!/bin/bash
# Asks for the bot token (hidden input), finds the channels where the bot is admin, writes /opt/northgate/.env and starts the service.
set -e
read -s -p "Paste the NEW bot token and press Enter: " TOKEN; echo
PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(9))")
CH=$(TOKEN="$TOKEN" python3 - <<'PY'
import os,requests
t=os.environ["TOKEN"]; ids={}
requests.get(f"https://api.telegram.org/bot{t}/deleteWebhook",timeout=30)
r=requests.get(f"https://api.telegram.org/bot{t}/getUpdates",params={"allowed_updates":'["my_chat_member","channel_post"]'},timeout=30).json()
if not r.get("ok"): raise SystemExit("Token error: "+str(r.get("description")))
for u in r["result"]:
    m=u.get("my_chat_member") or u.get("channel_post") or {}
    c=m.get("chat",{})
    if c.get("type")=="channel": ids[c["id"]]=c.get("title","")
for i,tl in ids.items(): print(f"# {i} {tl}",file=open("/dev/stderr","w"))
print(",".join(str(i) for i in ids))
PY
)
if [ -z "$CH" ]; then echo; echo "No channels found. Write any short message in EACH of the 3 channels (you can delete it later), then run this script again."; exit 1; fi
cat > /opt/northgate/.env <<ENV
BOT_TOKEN=$TOKEN
CHANNELS=$CH
MANAGER=alexanorthgate
PANEL_USER=admin
PANEL_PASSWORD=$PASS
PORT=8080
CONTENT_DIR=/opt/northgate/repo/content
DB_PATH=/opt/northgate/state.db
ENV
chmod 600 /opt/northgate/.env
cp /opt/northgate/repo/bot/northgate.service /etc/systemd/system/northgate.service
systemctl daemon-reload && systemctl enable --now northgate >/dev/null 2>&1 && systemctl restart northgate
ufw status 2>/dev/null | grep -q active && ufw allow 8080/tcp >/dev/null
echo; echo "CHANNELS FOUND: $CH"
echo "PANEL: http://$(curl -s ifconfig.me):8080   login: admin   password: $PASS"
