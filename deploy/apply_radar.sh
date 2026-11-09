#!/usr/bin/env bash
set -euo pipefail
cd /opt/leadbot
backup="/tmp/leadbot-before-radar-$(date +%Y%m%d%H%M%S).tar.gz"
success=0
applied=0
had_radar=0
if systemctl cat leadbot-radar.service >/dev/null 2>&1; then had_radar=1; fi
restore() {
    if [ "$applied" = 1 ] && [ "$success" = 0 ]; then
        systemctl stop leadbot-radar.service || true
        tar -xzf "$backup" -C /opt/leadbot
        if [ "$had_radar" = 0 ]; then
            systemctl disable leadbot-radar.service || true
            rm -f /etc/systemd/system/leadbot-radar.service
        else
            cp deploy/leadbot-radar.service /etc/systemd/system/
        fi
        systemctl daemon-reload
        echo "RADAR_CODE_ROLLED_BACK"
    fi
    systemctl start leadbot-feedback.service leadbot.timer leadbot-retry.timer
    if [ "$success" = 1 ] || [ "$had_radar" = 1 ]; then systemctl start leadbot-radar.service; fi
}
trap restore EXIT
systemctl stop leadbot.timer leadbot-retry.timer
if [ "$had_radar" = 1 ]; then systemctl stop leadbot-radar.service; fi
# Do not interrupt a user's manual run or an ongoing card delivery.
for attempt in $(seq 1 25); do
    if runuser -u leadbot -- venv/bin/python -c 'from leadbot import db; c=db.connect(); busy=db.active_run(c); c.close(); raise SystemExit(1 if busy else 0)'; then break; fi
    if [ "$attempt" = 25 ]; then echo "RUN_STILL_ACTIVE_DEPLOYMENT_DEFERRED"; exit 1; fi
    sleep 2
done
systemctl stop leadbot-feedback.service
tar -czf "$backup" run.py leadbot README.md deploy/setup.sh deploy/test_llm_queue.py deploy/test_discovery.py deploy/leadbot*.service
venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
destination = Path('/tmp') / ('leadbot-before-radar-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '.db')
with sqlite3.connect('leads.db') as source, sqlite3.connect(destination) as target:
    source.backup(target)
destination.chmod(0o600)
print('DATABASE_BACKUP_READY')
PY
applied=1
tar -xzf /tmp/leadbot-radar-update.tar.gz
chown -R leadbot:leadbot leadbot
chown leadbot:leadbot run.py README.md deploy/test_llm_queue.py deploy/test_discovery.py deploy/test_radar.py
runuser -u leadbot -- venv/bin/python -m compileall -q leadbot run.py
runuser -u leadbot -- venv/bin/python -m unittest deploy.test_llm_queue deploy.test_discovery deploy.test_radar
runuser -u leadbot -- venv/bin/python - <<'PY'
from leadbot import db
c = db.connect()
if not db.get_meta(c, 'radar:history_repaired'):
    c.execute('UPDATE chats SET last_id=0')
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('radar:history_repaired',?)", (db.now(),))
    c.commit()
print('RADAR_SCHEMA_READY')
c.close()
PY
cp deploy/leadbot-radar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable leadbot-radar.service
systemctl start leadbot-radar.service
sleep 3
systemctl is-active --quiet leadbot-radar.service
success=1
echo "RADAR_DEPLOYED"
