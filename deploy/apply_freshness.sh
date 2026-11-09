#!/usr/bin/env bash
set -euo pipefail
cd /opt/leadbot
backup="/tmp/leadbot-before-freshness-$(date +%Y%m%d%H%M%S).tar.gz"
files="leadbot/config.py leadbot/collector.py leadbot/pending.py leadbot/telegram_out.py run.py deploy/test_llm_queue.py deploy/test_radar.py"
success=0
applied=0
restore() {
    if [ "$applied" = 1 ] && [ "$success" = 0 ]; then
        tar -xzf "$backup" -C /opt/leadbot
        echo FRESHNESS_ROLLED_BACK
    fi
    systemctl start leadbot.timer leadbot-retry.timer leadbot-feedback.service leadbot-radar.service
}
trap restore EXIT
systemctl stop leadbot.timer leadbot-retry.timer leadbot-radar.service
for attempt in $(seq 1 25); do
    if runuser -u leadbot -- venv/bin/python -c 'from leadbot import db; c=db.connect(); busy=db.active_run(c); c.close(); raise SystemExit(1 if busy else 0)'; then break; fi
    if [ "$attempt" = 25 ]; then echo RUN_STILL_ACTIVE; exit 1; fi
    sleep 2
done
systemctl stop leadbot-feedback.service
tar -czf "$backup" $files
applied=1
tar -xzf /tmp/leadbot-freshness-update.tar.gz $files
chown leadbot:leadbot $files
runuser -u leadbot -- venv/bin/python -m compileall -q leadbot run.py
runuser -u leadbot -- venv/bin/python -m unittest deploy.test_llm_queue deploy.test_discovery deploy.test_radar
success=1
echo FRESHNESS_DEPLOYED
