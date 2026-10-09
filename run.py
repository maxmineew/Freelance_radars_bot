"""Прогон: сбор -> префильтр -> обезличивание -> LLM -> карточки -> метрики.
Запуск: python run.py [--dry] [--notify]
--dry: не отправлять в Telegram, печатать в консоль; --notify: прислать владельцу итог (так запускает кнопка «Прогон»)"""
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from leadbot import db, collector, classifier, telegram_out, watcher
from leadbot.config import MIN_SCORE, BOT_TOKEN, EXCHANGE_RULES, MAX_CARD_AGE_HOURS

MSK = timezone(timedelta(hours=3))


def age_hours(posted_at):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(posted_at)).total_seconds() / 3600


def set_status(c, p, status):
    c.execute("UPDATE posts SET status=? WHERE channel=? AND msg_id=?", (status, p["channel"], p["msg_id"]))
    c.commit()


def run(c, dry, say):
    """say печатает строку и кладёт её в итог прогона. Возвращает сбои, о которых нужно сообщить владельцу."""
    problems = {}
    db.purge_old(c)
    examples = classifier.examples(c)  # отметки владельца как примеры для модели

    new = collector.collect(c)
    say(f"Новых постов после префильтра: {len(new)}")
    sources = collector.load_sources()
    if sources and len(collector.failed) == len(sources):
        problems["tme"] = "Ни один канал не открылся: t.me недоступен с сервера."
    elif collector.failed:
        say(f"Каналы не открылись: {len(collector.failed)}")
    chat_new = watcher.collect(c)
    say(f"Новых сообщений из чатов после префильтра: {len(chat_new)}")
    if watcher.problems:
        problems["watcher"] = watcher.problems[0]
    if watcher.skipped:
        say(f"Чаты не прочитаны: {len(watcher.skipped)}")

    old = dups = errors = 0
    for p in new + chat_new:
        # Устаревшее не оцениваем и не шлём: при первом чтении источника в выборку попадает история за две недели
        if age_hours(p["posted_at"]) > MAX_CARD_AGE_HOURS:
            set_status(c, p, "old")
            old += 1
            continue
        # Дубли: один и тот же текст в нескольких источниках оцениваем и шлём один раз
        same = c.execute("SELECT channel, msg_id FROM posts WHERE text_hash=? ORDER BY rowid",
                         (db.text_hash(p["text"]),)).fetchall()
        if same and (same[0]["channel"], same[0]["msg_id"]) != (p["channel"], p["msg_id"]):
            set_status(c, p, "dup")
            dups += 1
            continue
        # Похожие дубли: тот же заказ с другой подписью или шапкой
        if db.has_similar(c, p["channel"], p["msg_id"], db.sketch(p["text"])):
            set_status(c, p, "dup")
            dups += 1
            continue
        try:
            card = classifier.classify(p["text"], p["exchange"], p.get("chat"), len(same) - 1, p["posted_at"],
                                       examples)
        except Exception as ex:
            print("classify error:", ex)
            errors += 1
            problems["llm"] = f"Модель не оценила постов: {errors}. Последняя ошибка: {str(ex)[:200]}"
            continue
        score = int(card.get("score", 0)) if card.get("is_lead") else 0
        c.execute("UPDATE posts SET status='scored', score=?, card=? WHERE channel=? AND msg_id=?",
                  (score, json.dumps(card, ensure_ascii=False), p["channel"], p["msg_id"]))
        c.commit()
    if dups:
        say(f"Дублей: {dups}")
    if errors:
        say(f"Ошибок оценки: {errors}")

    rows = c.execute("SELECT * FROM posts WHERE status='scored' AND score>=? ORDER BY score DESC", (MIN_SCORE,)).fetchall()
    # Правила бирж (порог оценки, свежесть, суточный лимит): сколько карточек уже ушло сегодня (сутки считаем по Москве)
    day_start = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    sent_today = {ex: c.execute("SELECT COUNT(*) FROM posts WHERE status='sent' AND exchange=? AND sent_at>=?",
                                (ex, day_start)).fetchone()[0] for ex in EXCHANGE_RULES}
    over = sent = unsent = 0
    for row in rows:
        ex = row["exchange"]
        age_h = age_hours(row["posted_at"])
        if age_h > MAX_CARD_AGE_HOURS:
            set_status(c, row, "old")
            old += 1
            continue
        if ex in EXCHANGE_RULES:
            rule = EXCHANGE_RULES[ex]
            if row["score"] < rule["min_score"] or age_h > rule["max_age_hours"] or sent_today[ex] >= rule["per_day"]:
                # Отсеянное не откладываем на завтра: заказы на бирже быстро устаревают
                set_status(c, row, "limit")
                over += 1
                continue
        card = json.loads(row["card"])
        tg_msg_id = None
        if dry:
            if row["order_url"]:
                print(f"\n  Заказ на {row['exchange']}: {row['order_url']}", end="")
            print(f"\n[{card['score']}] {row['url']}\n  {card['niche']} | {card['need']}\n  «{card['pain_signal']}»\n  -> {card['hook']}")
        else:
            try:
                tg_msg_id = telegram_out.send_card(row, card)
            except Exception as exc:
                # Карточка остаётся в статусе scored и уйдёт в следующий прогон; остальные шлём дальше
                print("send error:", exc)
                unsent += 1
                problems["send"] = f"Не отправлено карточек: {unsent}. Последняя ошибка: {str(exc)[:200]}"
                continue
            time.sleep(1)  # лимит Bot API: не чаще одного сообщения в секунду в один чат
        if ex in EXCHANGE_RULES:
            sent_today[ex] += 1
        sent += 1
        c.execute("UPDATE posts SET status='sent', sent_at=?, tg_msg_id=? WHERE channel=? AND msg_id=?",
                  (db.now(), tg_msg_id, row["channel"], row["msg_id"]))
        c.commit()
    say(f"Карточек отправлено: {sent}")
    if unsent:
        say(f"Карточек не отправлено из-за ошибки: {unsent}")
    if old:
        say(f"Не отправлено как устаревшее (старше {MAX_CARD_AGE_HOURS} ч): {old}")
    if over:
        say(f"Не отправлено по правилам биржи (оценка, свежесть, суточный лимит): {over}")

    # Метрики гипотезы
    m = c.execute("""SELECT COUNT(*) sent, SUM(feedback='take') took, SUM(feedback='wrong') wrong,
        SUM(feedback='skip') skip FROM posts WHERE status='sent'""").fetchone()
    took, skip, wrong = (m["took"] or 0), (m["skip"] or 0), (m["wrong"] or 0)
    print(f"\nМетрики: отправлено={m['sent']} беру={took} мимо={skip} неточно={wrong}")
    if took + skip + wrong:
        print(f"Precision = {took / (took + skip + wrong):.0%}")
    return problems


if __name__ == "__main__":
    dry = "--dry" in sys.argv or not BOT_TOKEN
    notify = "--notify" in sys.argv and not dry
    c = db.connect()
    if db.active_run(c):
        # Два прогона сразу делят одну сессию наблюдателя — второй не запускаем
        print("Прогон уже идёт, этот запуск пропущен")
        sys.exit(0)
    lines = []

    def say(line):
        print(line)
        lines.append(line)

    run_id = db.start_run(c)
    error = None
    try:
        for key, text in run(c, dry, say).items():
            if not dry:
                telegram_out.alert(c, key, text)
    except Exception as ex:
        error = f"{type(ex).__name__}: {str(ex)[:300]}"
        traceback.print_exc()
        if not dry:
            telegram_out.alert(c, "crash", f"Прогон упал: {error}")
        raise
    finally:
        db.finish_run(c, run_id, "\n".join(lines), error)
        if notify:
            telegram_out.try_api("sendMessage", chat_id=telegram_out.OWNER_CHAT_ID,
                                 text="Прогон завершён.\n" + ("\n".join(lines) if not error else "Ошибка: " + error))
