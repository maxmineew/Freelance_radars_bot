"""Тексты ответов на команды бота: статистика, источники, взятые лиды, состояние."""
import json
from datetime import datetime, timedelta, timezone
from . import db
from .collector import load_sources
from .watcher import load_chats
from .config import MODEL, RUN_HOURS, RETENTION_DAYS
from .telegram_out import esc, REASONS, STAGES, MSK

HELP = (
    "Бот ищет в каналах и чатах тех, кому нужны услуги студии, и присылает карточки лидов.\n\n"
    "<b>Под карточкой</b>\n"
    "✅ Беру — лид в работе; ниже появятся этапы: написал, ответил, сделка, отказ\n"
    "⏭ Мимо — лид настоящий, но не нужен\n"
    "🎯 Неточно — бот ошибся; ниже выберите причину\n\n"
    "<b>Кнопки внизу</b>\n"
    "📊 Статистика — карточки, отметки и точность\n"
    "📡 Источники — какие каналы дают лиды, а какие нет\n"
    "✅ Мои лиды — взятые лиды и их этапы\n"
    "📬 Пришли карточки — ещё раз прислать свежие карточки без отметки\n"
    "🎯 Прогон по источнику — выбрать канал или чат и проверить только его\n"
    "▶️ Прогон — запустить вне расписания\n\n"
    "/status — последний и следующий прогон, состояние радара"
)


def _msk(iso):
    return f"{datetime.fromisoformat(iso).astimezone(MSK):%d.%m %H:%M}"


def _marks(c, where="", args=()):
    m = c.execute(f"""SELECT COUNT(*) sent, SUM(feedback='take') take, SUM(feedback='skip') skip,
        SUM(feedback='wrong') wrong FROM posts WHERE status='sent' {where}""", args).fetchone()
    return m["sent"], m["take"] or 0, m["skip"] or 0, m["wrong"] or 0


def stats(c):
    day_start = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    lines = ["<b>Статистика</b>"]
    for title, since in (("Сегодня", day_start), ("7 дней", day_start - timedelta(days=6)), ("Всего", None)):
        # у карточек, отправленных до появления sent_at, берём дату записи
        sent, take, skip, wrong = _marks(c, "AND COALESCE(sent_at, created_at) >= ?" if since else "",
                                         (since.isoformat(),) if since else ())
        marked = take + skip + wrong
        lines.append(f"{title}: карточек {sent} · ✅ {take} · ⏭ {skip} · 🎯 {wrong}"
                     + (f" · точность {take / marked:.0%}" if marked else ""))
    sent, take, skip, wrong = _marks(c)
    if sent - take - skip - wrong:
        lines.append(f"Без отметки: {sent - take - skip - wrong}")
    reasons = dict(c.execute("SELECT reason, COUNT(*) FROM posts WHERE feedback='wrong' AND reason IS NOT NULL"
                             " GROUP BY reason").fetchall())
    if reasons:
        lines.append("\n<b>Причины «Неточно»</b>\n" + " · ".join(
            f"{label} {reasons[r]}" for r, label in REASONS.items() if r in reasons))
    if take:
        stages = dict(c.execute("SELECT COALESCE(stage, ''), COUNT(*) FROM posts WHERE feedback='take'"
                                " GROUP BY 1").fetchall())
        lines.append("\n<b>Взятые лиды</b>\n" + " · ".join(
            f"{label} {stages.get(s, 0)}" for s, label in {"": "Без этапа", **STAGES}.items()))
    return "\n".join(lines)


def sources(c):
    titles = dict(c.execute("SELECT name, title FROM chats").fetchall())

    def name(ch):
        return esc(titles[ch]) if ch in titles else "@" + esc(ch)
    rows = c.execute("""SELECT channel, SUM(status='sent') sent, SUM(feedback='take') take,
        SUM(feedback='skip') skip, SUM(feedback='wrong') wrong FROM posts GROUP BY channel
        ORDER BY take DESC, sent DESC""").fetchall()
    if not rows:
        return "Данных по источникам пока нет."
    lines = [f"<b>Источники за {RETENTION_DAYS} дней</b>"]
    with_cards = [r for r in rows if r["sent"]]
    for r in with_cards[:25]:
        lines.append(f"{name(r['channel'])} — карточек {r['sent']} · ✅ {r['take'] or 0}"
                     f" · ⏭ {r['skip'] or 0} · 🎯 {r['wrong'] or 0}")
    if len(with_cards) > 25:
        lines.append(f"…и ещё {len(with_cards) - 25}")
    bad = [r for r in with_cards if not r["take"] and (r["skip"] or 0) + (r["wrong"] or 0) >= 5]
    if bad:
        lines.append("\n<b>Пять и больше отметок, ни одной «Беру»</b>\n" + ", ".join(name(r["channel"]) for r in bad))
    empty = [r for r in rows if not r["sent"]]
    if empty:
        lines.append(f"\n<b>Ни одной карточки: {len(empty)}</b>\n" + ", ".join(name(r["channel"]) for r in empty[:40])
                     + (" …" if len(empty) > 40 else ""))
    return "\n".join(lines)


def leads(c):
    rows = c.execute("SELECT * FROM posts WHERE feedback='take'"
                     " ORDER BY COALESCE(stage_at, sent_at, created_at) DESC LIMIT 20").fetchall()
    if not rows:
        return "Взятых лидов пока нет. Нажмите «✅ Беру» под карточкой, и лид появится здесь."
    lines = ["<b>Взятые лиды</b>"]
    for r in rows:
        card = json.loads(r["card"] or "{}")
        since = r["stage_at"] or r["sent_at"] or r["created_at"]
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).days
        lines.append(f"• <a href=\"{esc(r['order_url'] or r['url'])}\">{esc(card.get('niche') or r['channel'])}</a>"
                     f" — {esc(card.get('need'))}\n  {STAGES.get(r['stage'], 'Без этапа')} · {days} дн.")
    return "\n".join(lines)


def next_run():
    now = datetime.now(MSK)
    for day in (0, 1):
        for h in RUN_HOURS:
            t = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                return t


def status(c):
    lines = ["<b>Статус</b>"]
    if db.active_run(c):
        lines.append("Сейчас идёт прогон.")
    last = c.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        took = datetime.fromisoformat(last["finished_at"]) - datetime.fromisoformat(last["started_at"])
        lines.append(f"Последний прогон: {_msk(last['started_at'])} МСК, {int(took.total_seconds() // 60)} мин")
        if last["error"]:
            lines.append(f"⚠️ Завершился ошибкой: {esc(last['error'])}")
        if last["summary"]:
            lines.append(esc(last["summary"]))
    else:
        lines.append("Прогонов в журнале пока нет.")
    lines.append(f"\nСледующий по расписанию: {next_run():%d.%m %H:%M} МСК")
    n = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    lines.append(f"Источники: каналов {len(load_sources())}, чатов {len(load_chats())}")
    heartbeat = db.get_meta(c, "radar:heartbeat")
    if heartbeat:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
        state = db.get_meta(c, "radar:state")
        label = {"connected": "наблюдает", "paused": "уступил сессию прогону",
                 "waiting": "ждёт окончания паузы Telegram",
                 "reconnecting": "переподключается", "stopped": "остановлен"}.get(state, "неизвестен")
        lines.append(f"Радар: {label if age < 180 else 'нет свежего сигнала'} · {_msk(heartbeat)} МСК")
    lines.append(f"Записей в базе: {n} · модель: {esc(MODEL)}")
    queued = c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
    lines.append(f"Ожидают оценки LLM: {queued}; радар обрабатывает очередь круглосуточно, резервный повтор — по таймеру")
    last_model = db.get_meta(c, "llm:last_model")
    if last_model:
        lines.append(f"Последняя успешная оценка: {esc(last_model)}")
    return "\n".join(lines)
