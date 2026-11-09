# OpenAI через подписку ChatGPT

Используется установленный на сервере Codex CLI (`codex exec`). Авторизация:

```sh
runuser -u leadbot -- codex login --device-auth
runuser -u leadbot -- codex login status
```

После успешного входа и проверки классификации задайте в `/opt/leadbot/.env`:

```dotenv
LLM_BACKEND=codex_cli
CODEX_CLI=/usr/bin/codex
CODEX_CLI_TIMEOUT=180
```

`CODEX_MODEL` необязателен: без него используется модель по умолчанию Codex.
API-ключи OpenAI исключаются из окружения дочернего процесса, вход принудительно
ограничен ChatGPT. При ошибке модель не переключается на платный API или Anthropic;
очередь сохраняется для повтора. Применяются лимиты подписки Codex.

CLI запускается в пустой временной папке, без пользовательской конфигурации,
с выключенными shell/apply_patch/web_search и sandbox read-only.

Перед изменением рабочей копии сохраните резервную копию `.env`,
`leadbot/classifier.py` и `leadbot/pending.py`. Скрипт
`prepare_codex_backend.py /opt/leadbot` вносит только изменения провайдера,
сохраняя различия между локальной и серверной версиями. Дополнительно скопируйте
`leadbot/codex_backend.py`. Затем перезапустите службы leadbot.

Документация: https://learn.chatgpt.com/docs/auth
