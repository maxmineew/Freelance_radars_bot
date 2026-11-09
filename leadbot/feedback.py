"""Слушатель: сразу отвечает на кнопки карточек и команды, напоминает о взятых лидах.
Очередь обновлений Telegram читает только он. Запуск: python -m leadbot.feedback
(на сервере — служба leadbot-feedback.service)"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from . import db, reports, targets, telegram_out as tg
from .config import ROOT, OWNER_CHAT_ID, REMIND_AFTER_HOURS, RUN_HOURS, MAX_CARD_AGE_HOURS

MENU_VERSION = "3"   # поменяйте при изменении кнопок меню: слушатель разошлёт новую клавиатуру
CHECK_EVERY = 600    # как часто проверять напоминания, секунд
RESEND_LIMIT = 10    # сколько карточек присылать повторно за одно нажатие
PICK_PAGE = 10       # источников на одной странице выбора
_run = None          # прогон, запущенный кнопкой
_checked = 0


def on_callback(c, cb):
    """Нажатие кнопки под карточкой."""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")

    def answer(text):
        # Ответ на старое нажатие Telegram отклонит — это не ошибка, отметка уже записана
        tg.try_api("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    def set_keyboard(markup):
        if chat_id:
            tg.try_api("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"], reply_markup=markup)

    if OWNER_CHAT_ID not in (str(cb["from"]["id"]), str(chat_id)):
        return answer("Кнопки доступны только владельцу бота")
    action, _, key = (cb.get("data") or "").partition("|")
    if action == "pick":  # листание списка источников
        page = int(key) if key.isdigit() else 0
        if chat_id:
            text, markup = picker(c, page)
            tg.try_api("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=text,
                       parse_mode="HTML", reply_markup=markup)
        return answer("")
    if action == "src":  # выбран источник: запускаем прогон только по нему
        target = targets.find(c, key)
        if not target:
            set_keyboard({"inline_keyboard": []})
            return answer("Источник не найден: список изменился, нажмите кнопку ещё раз")
        kind, src, label = target
        if chat_id:
            tg.try_api("editMessageText", chat_id=chat_id, message_id=msg["message_id"], parse_mode="HTML",
                       text=f"🎯 Прогон по источнику: {targets.KINDS[kind]} {tg.esc(label)}")
        answer("Запускаю")
        tg.send(start_run(c, (kind, src)), reply_markup=tg.menu_keyboard())
        return
    ch, _, mid = key.rpartition("|")
    row = None
    if mid.isdigit() and action in {**tg.BUTTONS, **tg.REASONS, **tg.STAGES}:
        row = c.execute("SELECT * FROM posts WHERE channel=? AND msg_id=?", (ch, int(mid))).fetchone()
    if not row:
        # Кнопка прежнего бота или карточка, запись о которой уже удалена: убираем кнопки с сообщения
        set_keyboard({"inline_keyboard": []})
        return answer("Кнопка устарела, убрал её")
    feedback, reason, stage, stage_at = row["feedback"], row["reason"], row["stage"], row["stage_at"]
    if action in tg.REASONS:
        feedback, reason, label = "wrong", action, tg.REASONS[action]
    elif action in tg.STAGES:
        feedback, stage, stage_at, label = "take", action, db.now(), tg.STAGES[action]
    else:
        label = tg.BUTTONS[action]
        if action != feedback:
            feedback, reason, stage, stage_at = action, None, None, db.now()
    reason, stage = reason if feedback == "wrong" else None, stage if feedback == "take" else None
    # этап сменился — напоминание по нему ещё не уходило
    c.execute("UPDATE posts SET feedback=?, reason=?, stage=?, stage_at=?, reminded_at=?,"
              " tg_msg_id=COALESCE(?, tg_msg_id) WHERE channel=? AND msg_id=?",
              (feedback, reason, stage, stage_at, row["reminded_at"] if stage_at == row["stage_at"] else None,
               msg.get("message_id"), ch, int(mid)))
    c.commit()
    answer("Записано: " + label)
    set_keyboard(tg.card_keyboard(key, feedback, reason, stage))


def start_run(c, target=None):
    """target=(kind, source) — прогон только по одному источнику."""
    global _run
    if _run and _run.poll() is None:
        return "Прогон по кнопке уже идёт. Итог пришлю, когда закончится, потом можно запустить следующий."
    busy = db.active_run(c)
    args = [sys.executable, "-u", str(ROOT / "run.py"), "--notify"]
    if target:
        args += ["--source", f"{target[0]}:{target[1]}"]
    # run.py --notify сам дождётся окончания чужого прогона и обязательно пришлёт итог
    _run = subprocess.Popen(args, cwd=ROOT)
    if busy:
        return ("Сейчас идёт другой прогон (радар или по расписанию). "
                "Ваш начнётся сразу после него, итог пришлю.")
    if target:
        return "Прогон по источнику запущен. Обычно это меньше минуты, итог пришлю."
    return "Прогон запущен. Обычно он занимает несколько минут, итог пришлю."


def picker(c, page=0):
    """Список каналов и чатов кнопками, по PICK_PAGE на странице."""
    items = targets.all_targets(c)
    if not items:
        return "Источников нет: добавьте каналы в sources.txt или чаты в chats.txt.", None
    pages = (len(items) + PICK_PAGE - 1) // PICK_PAGE
    page = min(max(page, 0), pages - 1)
    rows = [[{"text": f"{targets.KINDS[kind]} {label}"[:60], "callback_data": f"src|{targets.key(kind, src)}"}]
            for kind, src, label in items[page * PICK_PAGE:(page + 1) * PICK_PAGE]]
    nav = []
    if page > 0:
        nav.append({"text": "◀️ Назад", "callback_data": f"pick|{page - 1}"})
    if page < pages - 1:
        nav.append({"text": "Дальше ▶️", "callback_data": f"pick|{page + 1}"})
    if nav:
        rows.append(nav)
    text = ("<b>Прогон по источнику</b>\nВыберите канал или чат — проверю только его."
            f"\n💬 чат · 📢 канал · страница {page + 1} из {pages}")
    return text, {"inline_keyboard": rows}


def pick(c):
    text, markup = picker(c)
    if markup:
        tg.send(text, reply_markup=markup)
        return None
    return text


def resend(c):
    """Ещё раз присылает свежие карточки, которые владелец не отметил. Новых не ищет — это делает прогон."""
    rows = c.execute("SELECT * FROM posts WHERE status='sent' AND feedback IS NULL AND card IS NOT NULL"
                     " AND posted_at > ? ORDER BY score DESC, posted_at DESC",
                     (db.ago(hours=MAX_CARD_AGE_HOURS),)).fetchall()
    if not rows:
        return (f"Свежих карточек без отметки нет (смотрю за последние {MAX_CARD_AGE_HOURS} ч)."
                " Новые ищет «▶️ Прогон».")
    for r in rows[:RESEND_LIMIT]:
        msg_id = tg.send_card(r, json.loads(r["card"]))
        c.execute("UPDATE posts SET tg_msg_id=? WHERE channel=? AND msg_id=?", (msg_id, r["channel"], r["msg_id"]))
        c.commit()
        time.sleep(1)  # лимит Bot API: не чаще одного сообщения в секунду в один чат
    rest = len(rows) - RESEND_LIMIT
    return (f"Прислал карточек без отметки: {min(len(rows), RESEND_LIMIT)}."
            + (f" Осталось ещё {rest} — отметьте эти и нажмите снова." if rest > 0 else ""))


def on_message(c, m):
    """Команда или кнопка меню. Чужим бот не отвечает."""
    if str(m["chat"]["id"]) != OWNER_CHAT_ID:
        return
    text = (m.get("text") or "").strip()
    cmd = tg.MENU.get(text) or tg.LEGACY_MENU.get(text) or (text.split()[0].split("@")[0][1:].lower() if text.startswith("/") else "")
    handlers = {"stats": reports.stats, "sources": reports.sources, "leads": reports.leads,
                "status": reports.status, "run": start_run, "cards": resend, "pick": pick}
    reply = handlers[cmd](c) if cmd in handlers else reports.HELP
    if reply:  # выбор источника уже отправил своё сообщение с кнопками
        tg.send(reply, reply_markup=tg.menu_keyboard())


def poll(c, wait=50):
    """Забирает обновления (long polling, ждёт до wait секунд). Номер последнего хранится в базе."""
    offset = c.execute("PRAGMA user_version").fetchone()[0]
    for u in tg.api("getUpdates", offset=offset + 1, timeout=wait):
        try:
            if "callback_query" in u:
                on_callback(c, u["callback_query"])
            elif "message" in u:
                on_message(c, u["message"])
        except sqlite3.OperationalError:
            raise  # база занята прогоном: это обновление заберём ещё раз
        except Exception as ex:
            # Одно непонятное обновление не должно останавливать остальные
            print("update error:", type(ex).__name__, ex, flush=True)
        c.execute(f"PRAGMA user_version={int(u['update_id'])}")
        c.commit()


def remind(c):
    """Напоминание о взятом лиде, этап которого давно не менялся. По каждому этапу — одно."""
    rows = c.execute(
        "SELECT rowid, * FROM posts WHERE feedback='take' AND (stage IS NULL OR stage IN ('wrote', 'replied'))"
        " AND reminded_at IS NULL AND stage_at < ?", (db.ago(hours=REMIND_AFTER_HOURS),)).fetchall()
    for r in rows:
        card = json.loads(r["card"] or "{}")
        tg.send(f"⏰ Лид в работе, этап «{tg.STAGES.get(r['stage'], 'без этапа')}» не менялся больше"
                f" {REMIND_AFTER_HOURS} ч.\n<b>{tg.esc(card.get('niche'))}</b> — {tg.esc(card.get('need'))}\n"
                f"<a href=\"{tg.esc(r['order_url'] or r['url'])}\">Открыть</a>",
                reply_markup=tg.row_keyboard(r),
                **({"reply_parameters": {"message_id": int(r["tg_msg_id"]), "allow_sending_without_reply": True}}
                   if r["tg_msg_id"] else {}))
        c.execute("UPDATE posts SET reminded_at=? WHERE rowid=?", (db.now(), r["rowid"]))
        c.commit()


def check(c):
    """Раз в CHECK_EVERY секунд, днём по Москве: напоминания и проверка, что прогоны идут."""
    global _checked
    hour = datetime.now(tg.MSK).hour
    if time.time() - _checked < CHECK_EVERY or not RUN_HOURS[0] <= hour <= RUN_HOURS[-1]:
        return
    _checked = time.time()
    remind(c)
    last = c.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
    if last and hour >= RUN_HOURS[0] + 3 and last < db.ago(hours=5):
        tg.alert(c, "norun", "Прогонов не было больше 5 часов. Проверьте на сервере: "
                             "systemctl list-timers leadbot.timer")


def setup(c):
    """Меню команд и кнопки под полем ввода. Клавиатуру рассылаем один раз на версию, а не при каждом перезапуске."""
    tg.api("setMyCommands", commands=[{"command": k, "description": v} for k, v in tg.COMMANDS.items()])
    if db.get_meta(c, "menu") != MENU_VERSION:
        tg.send("Бот обновлён: команды теперь на кнопках внизу.", reply_markup=tg.menu_keyboard())
        db.set_meta(c, "menu", MENU_VERSION)


if __name__ == "__main__":
    c = db.connect()
    ready = False
    while True:
        try:
            if not ready:
                setup(c)
                ready = True
            poll(c)
            check(c)
        except Exception as ex:
            # Обрыв сети или занятая база — ждём и продолжаем
            print("feedback error:", type(ex).__name__, ex, flush=True)
            time.sleep(5)
