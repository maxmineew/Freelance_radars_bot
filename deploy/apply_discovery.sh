#!/usr/bin/env bash
set -euo pipefail
cd /opt/leadbot
backup="/tmp/leadbot-discovery-backup-$(date +%Y%m%d%H%M%S).tar.gz"
applied=0
success=0
restore_services() {
    if [ "$applied" = 1 ] && [ "$success" = 0 ]; then
        tar -xzf "$backup" -C /opt/leadbot
        echo "Deployment rolled back"
    fi
    systemctl start leadbot-feedback.service leadbot.timer
}
trap restore_services EXIT
systemctl stop leadbot.timer leadbot-feedback.service
for attempt in $(seq 1 25); do
    state=$(systemctl show leadbot.service -p ActiveState --value)
    if [ "$state" != active ] && [ "$state" != activating ]; then break; fi
    if [ "$attempt" = 25 ]; then echo "Lead collection still running; deployment deferred"; exit 1; fi
    sleep 2
done
backup_files=(run.py requirements.txt leadbot/config.py leadbot/watcher.py leadbot/collector.py)
for file in README.md leadbot/discovery.py; do
    if [ -f "$file" ]; then backup_files+=("$file"); fi
done
tar -czf "$backup" "${backup_files[@]}"
applied=1
tar -xzf /tmp/leadbot-discovery-update.tar.gz
chown leadbot:leadbot run.py requirements.txt README.md leadbot/discovery.py leadbot/config.py leadbot/watcher.py leadbot/collector.py deploy/test_discovery.py
venv/bin/pip install -q -r requirements.txt
runuser -u leadbot -- venv/bin/python -m compileall -q leadbot run.py
runuser -u leadbot -- venv/bin/python -m deploy.test_discovery
success=1
echo "DEPLOYMENT_OK"
