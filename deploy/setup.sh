#!/usr/bin/env bash
# Выполняется на сервере (его вызывает deploy.sh): окружение, пользователь, таймер systemd.
set -euo pipefail
APP=/opt/leadbot
cd "$APP"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

id leadbot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin leadbot
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

chown -R leadbot:leadbot "$APP"
chmod 600 .env
[ -f watcher.session ] && chmod 600 watcher.session

cp deploy/leadbot.service deploy/leadbot.timer deploy/leadbot-feedback.service deploy/leadbot-retry.service deploy/leadbot-retry.timer /etc/systemd/system/
cp deploy/leadbot-radar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now leadbot.timer
systemctl enable --now leadbot-retry.timer
systemctl enable leadbot-feedback.service
systemctl restart leadbot-feedback.service
systemctl enable leadbot-radar.service
systemctl restart leadbot-radar.service

echo "--- Проверка доступа к Telegram с сервера"
curl -s -o /dev/null -m 15 -w "t.me: %{http_code}\n" https://t.me/s/jobs_start || echo "t.me: недоступен"
curl -s -o /dev/null -m 15 -w "api.telegram.org: %{http_code}\n" https://api.telegram.org || echo "api.telegram.org: недоступен"
echo "--- Таймер"
systemctl list-timers leadbot.timer --no-pager
