"""Recover legacy payloads and evaluate a durable queue without losing failed messages."""
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from telethon.errors import FloodWaitError

from concurrent.futures import ThreadPoolExecutor

from . import db, collector, watcher, classifier, hot, rules
from .config import CLAUDE_BATCH_SIZE, CLAUDE_WORKERS, LLM_BACKEND, REPLIES_COOL, EXCHANGE_CHANNELS, MAX_CARD_AGE_HOURS, MAX_CLASSIFICATIONS_PER_RUN, QUEUE_RECOVERY_LIMIT, TG_API_ID, TG_API_HASH

VOICE = re.compile(r"обзвон|прозвон|перезвон|перезван|некому|не хватает оператор|пропущенн.{0,15}звон|подтвержд.{0,20}запис|неявк|возврат пациент", re.I)
DIRECT = re.compile(r"(?:ищу|ищем|нужен|нужна|нужны|требуется|требуются|посоветуйте|порекомендуйте).{0,100}"
                    r"(?:исполнител|подрядчик|разработчик|специалист|бот|обзвон|агент|автоматизац|интеграци)", re.I | re.S)
URGENT = re.compile(r"срочно|сегодня|завтра|прямо сейчас", re.I)


def priority(row):
    direct = bool(DIRECT.search(row["text"]))
    # Fresh urgent/direct demand precedes general pain; voice remains a tie-breaker.
    timestamp = datetime.fromisoformat(row["posted_at"]).timestamp()
    # Заявки с телефоном и заказы из Яндекс Услуг идут первыми
    exchange = row["exchange"] if "exchange" in row.keys() else None
    # Заказы бирж (FL.ru, Kwork) — самыми первыми: на них отвечают в первые минуты
    return (("channel" not in row.keys() or row["channel"] not in EXCHANGE_CHANNELS), not hot.flags(row["text"], exchange), not (direct and bool(URGENT.search(row["text"]))), not direct,
            -timestamp, not bool(VOICE.search(row["text"])))


def save_recovered(c, row, text, chat=None):
    c.execute("UPDATE posts SET status='new' WHERE channel=? AND msg_id=? AND status IN ('new','dup')",
              (row["channel"], row["msg_id"]))
    db.enqueue(c, {"channel":row["channel"], "msg_id":row["msg_id"], "text":text,
                   "exchange":row["exchange"], "chat":chat, "posted_at":row["posted_at"]})
    c.commit()


def recover(c, say=print):
    deadline = time.monotonic() + 180
    cutoff = db.ago(hours=MAX_CARD_AGE_HOURS)
    rows = c.execute("""SELECT p.* FROM posts p WHERE p.status IN ('new','dup') AND p.card IS NULL
        AND p.posted_at>=? AND (p.recovery_at IS NULL OR p.recovery_at<=?)
        AND (p.status='new' OR p.recovery_at IS NULL)
        AND NOT EXISTS(SELECT 1 FROM llm_queue q WHERE q.channel=p.channel AND q.msg_id=p.msg_id)
        ORDER BY CASE p.status WHEN 'new' THEN 0 ELSE 1 END,p.posted_at DESC LIMIT ?""",
        (cutoff, db.ago(hours=2), QUEUE_RECOVERY_LIMIT)).fetchall()
    if not rows:
        return
    chats = {r["name"]:r for r in c.execute("SELECT * FROM chats")}
    by_chat, channels = defaultdict(list), []
    recovered = 0
    for row in rows:
        if row["channel"] in EXCHANGE_CHANNELS:
            continue  # заказ биржи попадает в очередь вместе с записью; t.me для него читать нечего
        if row["channel"] in chats:
            by_chat[row["channel"]].append(row)
        else:
            channels.append(row)
    c.commit()
    if by_chat and TG_API_ID and TG_API_HASH and not watcher.telegram_pause_remaining(c):
        client = watcher._client()
        client.connect()
        try:
            if client.is_user_authorized():
                for name, batch in by_chat.items():
                    if time.monotonic() >= deadline:
                        break
                    try:
                        c.executemany("UPDATE posts SET recovery_at=? WHERE channel=? AND msg_id=?",
                                      [(db.now(),r["channel"],r["msg_id"]) for r in batch])
                        c.commit()
                        ent, _ = watcher.resolve(client, c, chats[name]["source"])
                        if ent is None:
                            continue
                        messages = client.get_messages(ent, ids=[row["msg_id"] for row in batch])
                        lookup = {m.id:m for m in messages if m is not None}
                        for row in batch:
                            message = lookup.get(row["msg_id"])
                            if message and message.message and not message.out:
                                save_recovered(c, row, message.message, chats[name]["title"])
                                recovered += 1
                    except FloodWaitError as exc:
                        watcher.pause_telegram(c, exc.seconds)
                        say(f"Восстановление очереди: Telegram просит паузу {exc.seconds} с")
                        break
                    except Exception as exc:
                        say(f"Восстановление очереди чата: {type(exc).__name__}")
                    time.sleep(1)
            else:
                say("Восстановление очереди: наблюдателю требуется вход")
        finally:
            client.disconnect()
    with httpx.Client(headers=collector.HEADERS, timeout=20, follow_redirects=True) as http:
        for row in channels:
            if time.monotonic() >= deadline:
                break
            # Read the original post again; no inference from a hash or a URL alone.
            try:
                c.execute("UPDATE posts SET recovery_at=? WHERE channel=? AND msg_id=?", (db.now(),row["channel"],row["msg_id"]))
                c.commit()
                response = http.get(f"https://t.me/s/{row['channel']}", params={"before":row["msg_id"] + 1})
                response.raise_for_status()
                for post in collector.parse_page(response.text):
                    if post["msg_id"] == row["msg_id"]:
                        save_recovered(c, row, post["text"])
                        recovered += 1
                        break
            except httpx.HTTPError:
                pass
            time.sleep(1)
    say(f"Восстановлено сообщений в очередь: {recovered}/{len(rows)}")


def process(c, say=print, limit=MAX_CLASSIFICATIONS_PER_RUN, channels=None):
    """channels — оценить только сообщения этих источников (прогон по одному источнику)."""
    deadline = time.monotonic() + 600
    db.expire_queue(c)
    c.commit()
    examples = classifier.examples(c)
    rows = c.execute("""SELECT q.*,p.text_hash,p.sketch,p.author,p.replies_n FROM llm_queue q
        JOIN posts p ON p.channel=q.channel AND p.msg_id=q.msg_id
        WHERE p.status='new' AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=?)""", (db.now(),)).fetchall()
    if channels is not None:
        rows = [r for r in rows if r["channel"] in channels]
    # Phone/Yandex first, then urgent direct requests, then direct demand; newest first within each class.
    rows = sorted(rows, key=priority)
    evaluated = duplicates = errors = attempted = 0
    problem = None
    batch_mode = (LLM_BACKEND in {"claude_cli", "codex_cli"} and classifier.classify.__module__ == classifier.__name__
                  and classifier.batch_model() is not None)
    if batch_mode:
        limit = max(limit, CLAUDE_BATCH_SIZE * CLAUDE_WORKERS)
    def is_dup(row):
        same = c.execute("""SELECT 1 FROM posts WHERE text_hash=? AND card IS NOT NULL
            AND status IN ('scored','sent','limit') AND posted_at>=?
            AND NOT (channel=? AND msg_id=?) LIMIT 1""",
            (row["text_hash"], db.ago(hours=MAX_CARD_AGE_HOURS), row["channel"], row["msg_id"])).fetchone()
        if same or db.has_similar(c, row["channel"], row["msg_id"], row["sketch"] or ""):
            db.complete(c, row["channel"], row["msg_id"], "dup")
            return True
        return False

    todo = []
    for row in rows:
        if len(todo) >= limit or time.monotonic() >= deadline:
            break
        if row["posted_at"] < db.ago(hours=MAX_CARD_AGE_HOURS):
            db.complete(c, row["channel"], row["msg_id"], "old")
            continue
        if (row["replies_n"] or 0) >= REPLIES_COOL:
            db.complete(c, row["channel"], row["msg_id"], "old")  # под сообщением уже толпа исполнителей
            continue
        if rules.noise_reason(row["text"], row["exchange"]):
            db.complete(c, row["channel"], row["msg_id"], "skipped")
            continue
        if batch_mode and is_dup(row):
            duplicates += 1
            continue
        todo.append(row)

    def item(row):
        copies = c.execute("SELECT COUNT(*) FROM posts WHERE text_hash=?", (row["text_hash"],)).fetchone()[0] - 1
        text = row["text"]
        if row["author"] and not row["exchange"]:
            seen = c.execute("SELECT COUNT(*) FROM posts WHERE author=? AND posted_at>=?",
                             (row["author"], db.ago(hours=24 * 7))).fetchone()[0]
            if seen >= 3:
                text = f"[Автор написал {seen} сообщений за неделю: вероятно посредник или исполнитель, а не заказчик.]{chr(10)}{text}"
        return {"text": text, "exchange": row["exchange"], "chat": row["chat"], "copies": copies,
                "posted_at": row["posted_at"]}

    def fail(row, exc):
        nonlocal errors, problem
        errors += 1
        wait = getattr(exc, "retry_after", min(3600, 60 * 2 ** min(row["attempts"], 5)))
        safe_error = str(exc)[:250] if isinstance(exc, (classifier.ClassificationError, classifier.CliUnavailable)) else type(exc).__name__
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat()
        c.execute("UPDATE llm_queue SET next_attempt_at=?,last_error=? WHERE channel=? AND msg_id=?",
                  (retry_at, safe_error, row["channel"], row["msg_id"]))
        c.commit()
        problem = safe_error
        return wait

    def success(row, card):
        nonlocal evaluated
        db.complete(c, row["channel"], row["msg_id"], "scored", card["score"], json.dumps(card, ensure_ascii=False))
        evaluated += 1

    def single(row):
        """Оценка по одной; True — можно продолжать, False — LLM недоступна."""
        nonlocal attempted
        attempted += 1
        c.execute("UPDATE llm_queue SET attempts=attempts+1 WHERE channel=? AND msg_id=?", (row["channel"], row["msg_id"]))
        c.commit()
        try:
            it = item(row)
            card = classifier.classify(it["text"], row["exchange"], row["chat"], it["copies"],
                                       row["posted_at"], examples, state=c)
            # Validate even when a test/custom adapter replaces classifier.classify.
            card = classifier.validate_card(card)
        except Exception as exc:
            wait = fail(row, exc)
            if getattr(exc, "blocked", False):
                say(f"LLM недоступна; очередь сохранена, повтор не раньше чем через {int(wait)} с")
                return False
            return True
        success(row, card)
        return True

    if not batch_mode:
        for row in todo:
            if is_dup(row):  # проверяем перед оценкой: соседний дубль мог только что получить карточку
                duplicates += 1
                continue
            if not single(row):
                break
    else:
        key = LLM_BACKEND + ":" + classifier.batch_model()
        if classifier.remaining_cooldown(key, c) > 0:
            say("LLM недоступна: идёт пауза LLM CLI; очередь сохранена")
        else:
            for row in todo:
                c.execute("UPDATE llm_queue SET attempts=attempts+1 WHERE channel=? AND msg_id=?", (row["channel"], row["msg_id"]))
            c.commit()
            attempted += len(todo)
            groups = [todo[i:i + CLAUDE_BATCH_SIZE] for i in range(0, len(todo), CLAUDE_BATCH_SIZE)]
            items = [[item(r) for r in g] for g in groups]  # база читается только в главном потоке

            def call(i):
                try:
                    return classifier.classify_batch(items[i], examples)
                except Exception as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=max(1, CLAUDE_WORKERS)) as pool:
                results = list(pool.map(call, range(len(groups))))
            blocked = None
            retry = []
            for group, result in zip(groups, results):
                if isinstance(result, classifier.CliUnavailable):
                    blocked = result
                    for row in group:
                        fail(row, result)
                elif isinstance(result, Exception):
                    retry.extend(group)  # негодный ответ пакета: оценим по одному
                else:
                    for row, card in zip(group, result):
                        if card is None:
                            retry.append(row)
                        else:
                            success(row, card)
            if blocked is not None:
                classifier.cooldown(key, blocked.delay, c)
                say(f"LLM недоступна; очередь сохранена, повтор не раньше чем через {int(blocked.delay)} с")
            else:
                db.set_meta(c, "llm:last_model", key)
                for row in retry:
                    attempted -= 1  # single() посчитает попытку заново
                    if not single(row):
                        break
    left = c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
    say(f"Оценено из очереди: {evaluated}; дублей: {duplicates}; отложено после ошибок: {errors}; осталось: {left}")
    return problem
