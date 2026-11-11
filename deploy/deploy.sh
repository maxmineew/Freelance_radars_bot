#!/usr/bin/env bash
# Запуск с вашего компьютера (Git Bash), из папки проекта:  bash deploy/deploy.sh root@IP_СЕРВЕРА
# Копирует код, сессию наблюдателя и ключи на сервер (базу не трогает) и включает таймер. Пароль спросит два раза.
set -euo pipefail
HOST="${1:?Укажите сервер: bash deploy/deploy.sh root@IP}"
cd "$(dirname "$0")/.."

# .env для сервера: без пароля от Aeza, с ключами Telegram API из переменных среды Windows
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
grep -v '^my_aeza_net' .env > "$TMP/.env"
# На сервере нет входа в Claude CLI по подписке — оценка через API-цепочку (Groq/Anthropic/Gemini)
sed -i 's/^LLM_BACKEND=.*/LLM_BACKEND=api/' "$TMP/.env"
grep -q '^LLM_BACKEND=' "$TMP/.env" || echo "LLM_BACKEND=api" >> "$TMP/.env"
for v in TG_API_ID TG_API_HASH; do
    grep -q "^$v=" "$TMP/.env" || echo "$v=${!v:?$v не найден ни в .env, ни в переменных среды}" >> "$TMP/.env"
done

FILES="run.py requirements.txt sources.txt chats.txt leadbot deploy"
# leads.db не копируем: на сервере своя база с накопленными отметками, затирать её нельзя
# watcher.session копируем только при первой установке: на сервере сессия накапливает кеш чатов, затирать её нельзя
if [ -f watcher.session ] && ! ssh "$HOST" 'test -f /opt/leadbot/watcher.session' < /dev/null; then
    FILES="$FILES watcher.session"
fi

tar czf - --exclude=__pycache__ $FILES -C "$TMP" .env | ssh "$HOST" \
    "mkdir -p /opt/leadbot && tar xzf - -C /opt/leadbot && find /opt/leadbot/deploy -maxdepth 1 -type f -exec sed -i 's/\r\$//' {} + && bash /opt/leadbot/deploy/setup.sh"
