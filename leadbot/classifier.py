import json
import time
from datetime import datetime, timedelta, timezone
import httpx
from .config import MODEL, GEMINI_API_KEY, GROQ_API_KEY
from .anonymize import anonymize

SYSTEM = """Ты аналитик лидогенерации AI-студии. Студия делает: голосовых AI-агентов (приоритет №1),
чат-ботов и AI-ассистентов, текстовых AI-агентов, автоматизацию бизнес-процессов.
Тебе дают ОБЕЗЛИЧЕННЫЙ пост из Telegram-канала. Определи, есть ли в нём потенциальный клиент:
бизнес/человек, который сам формулирует потребность или боль, решаемую нашими услугами
(ищет исполнителя, жалуется на пропущенные звонки/заявки/рутину, масштабирует продажи/поддержку).
НЕ лид: сам исполнитель/фрилансер предлагает услуги, реклама курсов, штатные вакансии не по теме,
общие новости, экспертный контент.
Тоже НЕ лид (is_lead=false):
- продающий или экспертный пост, который описывает типовую проблему ниши («администратор не перезванивает,
  салон теряет 42 000 ₽ с клиента») и подводит к решению, сервису или услуге: так пишут продавцы, а не клиенты;
- рассказ о собственном кейсе, опыте или результатах внедрения («прозвонил роботом, готовлю кейс»):
  это исполнитель или конкурент.
Лид только тогда, когда автор говорит о СВОЁМ бизнесе и своей нерешённой задаче или прямо ищет исполнителя.
Перед текстом указаны дата публикации и сегодняшняя дата. Если срок из текста («старт в понедельник»,
«нужно к 25-му», «до конца недели») отсчитан от даты публикации и к сегодняшнему дню уже прошёл —
заказ устарел: is_lead=false.
Ответь ТОЛЬКО JSON:
{"is_lead":bool,"score":0-10,"niche":"...","need":"что нужно, 1 фраза","pain_signal":"цитата/сигнал, до 150 символов",
 "service":"voice_agent|chatbot|ai_assistant|automation|other","budget_hint":"если есть, иначе ''",
 "hook":"первая фраза для контакта, 1-2 предложения, по делу, без спама"}
score: 9-10 явный запрос исполнителя, 6-8 явная боль, меньше 6 слабый сигнал. Для голосовых агентов +1."""

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MSK = timezone(timedelta(hours=3))
_client = None


_DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _day(d):
    return f"{d:%d.%m.%Y}, {_DAYS[d.weekday()]}"


def examples(c, takes=5, wrongs=8):
    """Свежие отметки владельца как примеры для модели. Текстов постов в базе нет, поэтому примеры —
    из карточек: ниша, потребность и обезличенная цитата."""
    def lines(where, limit, tag=False):
        out = []
        for r in c.execute(f"SELECT card, reason FROM posts WHERE {where} AND card IS NOT NULL"
                           " ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ?", (limit,)):
            card = json.loads(r["card"])
            why = f"[{_REASONS[r['reason']]}] " if tag and r["reason"] in _REASONS else ""
            out.append(f"- {why}ниша: {card.get('niche')}; потребность: {card.get('need')}; "
                       f"сигнал: «{card.get('pain_signal')}»")
        return out
    good, bad = lines("feedback='take'", takes), lines("feedback='wrong'", wrongs, tag=True)
    if not (good or bad):
        return ""
    return ("\n\nВладелец студии размечает твои прошлые карточки. Учитывай это при оценке похожих постов."
            + ("\nВзял в работу (такое нужно):\n" + "\n".join(good) if good else "")
            + ("\nОтметил как твою ошибку (такое не присылать):\n" + "\n".join(bad) if bad else ""))


_REASONS = {"ad": "это реклама", "stale": "заказ устарел", "service": "не услуга студии",
            "notclient": "автор не клиент"}


def classify(text: str, exchange: str | None = None, chat: str | None = None, copies: int = 0,
             posted_at: str | None = None, examples: str = ""):
    global _client
    system = SYSTEM + examples
    _client = _client or httpx.Client(timeout=60)
    content = anonymize(text)[:3000]
    if posted_at:
        posted = datetime.fromisoformat(posted_at).astimezone(MSK)
        today = datetime.now(MSK)
        content = (f"[Опубликовано: {_day(posted)}. Сегодня: {_day(today)}, прошло дней: {(today - posted).days}. "
                   f"Сначала проверь срок из текста: если он уже прошёл, это не лид.]\n{content}")
    if copies > 0 and not exchange:
        content = (f"[Этот же текст опубликован ещё в {copies} источниках: скорее всего рассылка или реклама, "
                   f"лидом считай только если это явный заказ.]\n{content}")
    if chat:
        content = f"[Сообщение участника чата «{chat}».]\n{content}"
    if exchange:
        content = (f"[Заказ размещён на бирже {exchange}. Это всё равно лид, оценивай по сути задачи; "
                   f"hook пиши как текст отклика на бирже.]\n{content}")
    if GROQ_API_KEY:
        url, headers = GROQ_URL, {"Authorization": f"Bearer {GROQ_API_KEY}"}
        body = {
            # запас по токенам: у рассуждающих моделей рассуждение тоже расходует этот лимит
            "model": MODEL, "max_completion_tokens": 2000, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        if MODEL.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"
    else:
        url, headers = GEMINI_URL.format(model=MODEL), {"x-goog-api-key": GEMINI_API_KEY}
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": content}]}],
            "generationConfig": {"maxOutputTokens": 500, "responseMimeType": "application/json",
                                 "thinkingConfig": {"thinkingBudget": 0}},
        }
    for attempt in range(4):
        r = _client.post(url, headers=headers, json=body)
        if r.status_code not in (429, 500, 503) or attempt == 3:
            break
        time.sleep(5 * 2 ** attempt)  # лимит запросов в минуту или перегрузка: ждём и повторяем
    if r.status_code != 200:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:300]}")
    data = r.json()
    if GROQ_API_KEY:
        raw = data["choices"][0]["message"]["content"].strip()
    else:
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return json.loads(raw)
