#!/usr/bin/env bash
set -euo pipefail
cd /opt/leadbot
backup="/tmp/leadbot-before-llm-fix-$(date +%Y%m%d%H%M%S).tar.gz"
success=0
applied=0
restart_services() {
    if [ "$applied" = 1 ] && [ "$success" = 0 ]; then
        tar -xzf "$backup" -C /opt/leadbot
        echo "CODE_ROLLED_BACK"
    fi
    systemctl start leadbot-feedback.service leadbot.timer
    if [ "$success" = 1 ]; then systemctl start leadbot-retry.timer; fi
}
trap restart_services EXIT
systemctl stop leadbot.timer leadbot-feedback.service
if systemctl cat leadbot-retry.timer >/dev/null 2>&1; then systemctl stop leadbot-retry.timer leadbot-retry.service; fi
systemctl stop leadbot.service
tar -czf "$backup" run.py leadbot README.md deploy/setup.sh
# Preserve an internally consistent database backup and release only interrupted run claims.
venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
c=sqlite3.connect('leads.db')
destination=Path('/tmp') / ('leadbot-before-llm-fix-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '.db')
with sqlite3.connect(destination) as backup:
    c.backup(backup)
destination.chmod(0o600)
c.execute("UPDATE runs SET finished_at=?,error=? WHERE finished_at IS NULL",
          (datetime.now(timezone.utc).isoformat(),'Прогон остановлен для обновления очереди LLM'))
c.commit()
c.close()
PY
applied=1
tar -xzf /tmp/leadbot-llm-update.tar.gz
chown -R leadbot:leadbot leadbot
chown leadbot:leadbot run.py README.md deploy/test_llm_queue.py deploy/test_discovery.py
runuser -u leadbot -- venv/bin/python -m compileall -q leadbot run.py
runuser -u leadbot -- venv/bin/python -m deploy.test_llm_queue
runuser -u leadbot -- venv/bin/python -m deploy.test_discovery
runuser -u leadbot -- venv/bin/python -c 'from leadbot import db; c=db.connect(); print("QUEUE_SCHEMA_READY"); c.close()'
cp deploy/leadbot-retry.service deploy/leadbot-retry.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable leadbot-retry.timer
success=1
echo "LLM_FIX_DEPLOYED"
