"""Отправка владельцу через Bot API: карточки, ответы на команды, сообщения о сбоях."""
import html
import time
from datetime import datetime, timedelta, timezone
import httpx
from . import db
from .config import BOT_TOKEN, OWNER_CHAT_ID, EXCHANGE_CHANNELS

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MSK = timezone(timedelta(hours=3))
SERVICES = {"voice_agent": "🎙 Голосовой агент", "chatbot": "💬 Чат-бот", "ai_assistant": "🤖 AI-ассистент",
            "automation": "⚙️ Автоматизация", "other": "❔ Другое"}
BUTTONS = {"take": "✅ Беру", "skip": "⏭ Мимо", "wrong": "🎯 Неточно"}
# Второй ряд кнопок: причина после «Неточно», этап работы после «Беру»
REASONS = {"ad": "📢 Реклама", "stale": "⌛ Устарел", "service": "🧩 Не моя услуга", "notclient": "🙅 Не клиент"}
STAGES = {"wrote": "✉️ Написал", "replied": "💬 Ответил", "deal": "🤝 Сделка", "lost": "❌ Отказ"}
# Кнопки под полем ввода: подпись -> команда
MENU = {"📊 Статистика": "stats", "📡 Источники": "sources", "✅ Мои лиды": "leads",
        "📬 Пришли карточки": "cards", "🎯 Прогон по источнику": "pick", "▶️ Прогон": "run"}
LEGACY_MENU = {"ℹ️ Статус": "status"}  # кнопка со старой клавиатуры, если она ещё не обновилась
COMMANDS = {"stats": "Статистика и точность", "sources": "Точность по источникам", "leads": "Взятые лиды и их этапы",
            "cards": "Прислать ещё раз карточки без отметки", "pick": "Прогон по одному каналу или чату",
            "status": "Последний и следующий прогон", "run": "Запустить прогон сейчас", "help": "Что умеет бот"}


def esc(s):
    return html.escape(str(s or ""))


def api(method, **params):
    """Вызов Bot API. Лимит запросов, сбой сети и ошибку сервера пережидает и повторяет."""
    for attempt in range(4):
        last = attempt == 3
        try:
            r = httpx.post(f"{API}/{method}", json=params, timeout=params.get("timeout", 0) + 20)
            data = r.json()
        except (httpx.HTTPError, ValueError) as ex:
            if last:
                raise RuntimeError(f"Telegram {method}: {type(ex).__name__}") from None  # в тексте ошибки httpx есть токен
            time.sleep(2 * 2 ** attempt)
            continue
        if data.get("ok"):
            return data["result"]
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            time.sleep((data.get("parameters") or {}).get("retry_after", 2 * 2 ** attempt))
            continue
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")


def try_api(method, **params):
    """Вызов, отказ в котором не ошибка: ответ на старое нажатие, правка уже такого же сообщения."""
    try:
        return api(method, **params)
    except RuntimeError:
        return None


def send(text, **params):
    return api("sendMessage", chat_id=OWNER_CHAT_ID, text=text[:4096], parse_mode="HTML",
               disable_web_page_preview=True, **params)


def menu_keyboard():
    labels = list(MENU)
    return {"keyboard": [[{"text": t} for t in labels[i:i + 2]] for i in range(0, len(labels), 2)],
            "resize_keyboard": True, "is_persistent": True}


def card_keyboard(key, feedback=None, reason=None, stage=None):
    """Кнопки карточки; выбранная помечается точкой, выбор можно поменять."""
    def cells(buttons, chosen):
        return [{"text": ("● " if a == chosen else "") + label, "callback_data": f"{a}|{key}"}
                for a, label in buttons.items()]
    extra = cells(REASONS, reason) if feedback == "wrong" else cells(STAGES, stage) if feedback == "take" else []
    return {"inline_keyboard": [cells(BUTTONS, feedback)] + [extra[i:i + 2] for i in range(0, len(extra), 2)]}


def row_keyboard(row):
    return card_keyboard(f"{row['channel']}|{row['msg_id']}", row["feedback"], row["reason"], row["stage"])


def send_card(row, card):
    """Отправляет карточку, возвращает номер сообщения."""
    minutes = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(row["posted_at"])).total_seconds() / 60))
    freshness = f"{minutes} мин назад" if minutes < 60 else f"{minutes // 60} ч {minutes % 60} мин назад"
    text = (
        hot_text(row)
        + f"<b>Опубликовано:</b> {freshness}\n"
        f"<b>Лид {card['score']}/10</b> · {SERVICES.get(card.get('service'), '')}\n"
        f"<b>Ниша:</b> {esc((card.get('niche') or '')[:100])}\n"
        f"<b>Потребность:</b> {esc((card.get('need') or '')[:300])}\n"
        f"<b>Сигнал:</b> «{esc((card.get('pain_signal') or '')[:150])}»\n"
        + f"<b>Бюджет:</b> {esc((card.get('budget_hint') or 'неизвестен')[:120])}\n"
        + qualification_text(card)
        + f"\n<b>Заход:</b> {esc((card.get('hook') or '')[:400])}\n\n"
        + (f"🔗 <a href=\"{esc(row['order_url'])}\">Заказ на {esc(row['exchange'])}</a>\n" if row["order_url"] else "")
        + (f"<a href=\"{row['url']}\">Открыть пост</a> · @{row['channel']}" if row["channel"] not in EXCHANGE_CHANNELS else "")
    )
    return send(text, reply_markup=row_keyboard(row))["message_id"]


HOT = {"phone": "📞 в заявке есть телефон — он в исходном посте",
       "yandex": "🟡 заказ из Яндекс Услуг"}


def hot_text(row):
    flags = (row["hot"] if "hot" in row.keys() else None) or ""
    marks = [HOT[f] for f in flags.split(",") if f in HOT]
    return f"🔥 <b>Приоритет:</b> {'; '.join(marks)}\n" if marks else ""


def qualification_text(card):
    if "request_type" not in card:
        return ""  # Old saved cards remain readable.
    fit = {"high": "высокое", "medium": "среднее", "low": "низкое", "unknown": "неизвестно"}
    request = {"direct": "ищет исполнителя", "pain": "описывает свою проблему",
               "discussion": "обсуждение", "unknown": "неизвестен"}
    urgency = {"urgent": "срочно", "soon": "есть ближайший срок", "unspecified": "не указана"}
    role = {"buyer": "заказчик", "intermediary": "посредник", "provider": "исполнитель", "unknown": "неизвестна"}
    return (
        f"<b>Соответствие:</b> {fit.get(card.get('service_fit'), 'неизвестно')}"
        + (f" — {esc(card['fit_reason'][:240])}" if card.get("fit_reason") else "") + "\n"
        + f"<b>Запрос:</b> {request.get(card.get('request_type'), 'неизвестен')}\n"
        + f"<b>Срочность:</b> {urgency.get(card.get('urgency'), 'не указана')}"
        + (f" — {esc(card['deadline_hint'][:120])}" if card.get("deadline_hint") else "") + "\n"
        + f"<b>Роль автора:</b> {role.get(card.get('author_role'), 'неизвестна')}\n"
        + (f"<b>Следующий шаг:</b> {esc(card['next_step'][:300])}\n" if card.get("next_step") else ""))


def alert(c, key, text):
    """Сообщение о сбое. Один и тот же сбой — не чаще раза в сутки, иначе семь прогонов в день дадут семь сообщений."""
    today = f"{datetime.now(MSK):%Y-%m-%d}"
    try:
        if db.get_meta(c, f"alert:{key}") == today:
            return
        send("⚠️ " + esc(text))
        db.set_meta(c, f"alert:{key}", today)
    except Exception as ex:
        print("alert error:", type(ex).__name__, ex, flush=True)
