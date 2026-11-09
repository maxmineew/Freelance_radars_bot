"""Прогон: сбор -> префильтр -> обезличивание -> LLM -> карточки -> метрики.
Запуск: python run.py [--dry] [--notify] [--source kind:name]
--dry: не отправлять в Telegram, печатать в консоль; --notify: прислать владельцу итог (так запускает кнопка «Прогон»)
--source: проверить только один источник — chat:<строка из chats.txt>, channel:<имя канала>, kwork:kwork.ru или flru:fl.ru или pchel:pchel.net"""
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from leadbot import db, collector, telegram_out, watcher, discovery, pending, kwork, flru, pchel, monitor, targets
from leadbot.config import (PCHEL_ENABLED, FLRU_ENABLED, KWORK_ENABLED, MIN_SCORE, BOT_TOKEN, EXCHANGE_RULES,
                            MAX_CARD_AGE_HOURS, MAX_CLASSIFICATIONS_PER_RUN, RADAR_BATCH_SIZE, REPLIES_COOL,
                            TG_API_ID, TG_API_HASH)

MSK = timezone(timedelta(hours=3))


def age_hours(posted_at):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(posted_at)).total_seconds() / 3600


def set_status(c, p, status):
    c.execute("UPDATE posts SET status=? WHERE channel=? AND msg_id=?", (status, p["channel"], p["msg_id"]))
    c.commit()


def target_channels(c, target):
    """Как источник называется в posts.channel: у чата это имя из таблицы chats."""
    kind, src = target
    if kind == "chat":
        row = c.execute("SELECT name FROM chats WHERE source=?", (src,)).fetchone()
        return {row["name"]} if row else set()
    return {src}


SOURCE_CARDS_HOURS = 24  # за сколько часов показывать готовые карточки при прогоне по источнику
SOURCE_CARDS_LIMIT = 10


def show_source_cards(c, target, started, dry, say):
    """Прогон по источнику присылает готовые карточки этого источника, даже уже отправленные радаром
    или не отправленные из-за возраста: владелец хочет увидеть, что источник даёт."""
    names = target_channels(c, target)
    if not names:
        say("Готовых карточек из этого источника нет.")
        return
    marks = ",".join("?" * len(names))
    rows = c.execute(f"""SELECT * FROM posts WHERE channel IN ({marks}) AND card IS NOT NULL AND score>=?
        AND status IN ('sent','scored','old','limit') AND posted_at>=?
        AND NOT (status='sent' AND sent_at>=?) ORDER BY posted_at DESC""",
                     (*names, MIN_SCORE, db.ago(hours=SOURCE_CARDS_HOURS), started)).fetchall()
    if not rows:
        say(f"Готовых карточек из этого источника за {SOURCE_CARDS_HOURS} ч нет.")
        return
    shown = 0
    for row in rows[:SOURCE_CARDS_LIMIT]:
        card = json.loads(row["card"])
        if dry:
            print(f"\n[{card['score']}] {row['url']}\n  {card['niche']} | {card['need']}")
            shown += 1
            continue
        try:
            msg_id = telegram_out.send_card(row, card)
        except Exception as exc:
            print("send error:", exc)
            break
        c.execute("UPDATE posts SET status='sent', sent_at=COALESCE(sent_at, ?), tg_msg_id=? WHERE channel=? AND msg_id=?",
                  (db.now(), msg_id, row["channel"], row["msg_id"]))
        c.commit()
        shown += 1
        time.sleep(1)
    say(f"Готовых карточек из источника за {SOURCE_CARDS_HOURS} ч: {len(rows)}, прислал: {shown}"
        + (" — самые свежие" if len(rows) > shown else ""))


def run(c, dry, say, retry_only=False, radar_worker=False, target=None):
    """say печатает строку и кладёт её в итог прогона. Возвращает сбои, о которых нужно сообщить владельцу.
    target=(kind, source) — прогон только по одному источнику, без поиска новых сообществ."""
    problems = {}
    db.purge_old(c)
    started = db.now()
    kind = target[0] if target else None
    if not dry and not retry_only and not target:
        if TG_API_ID and TG_API_HASH:
            try:
                discovery.run(c, say)
            except Exception as exc:
                problems["discovery"] = f"Поиск новых сообществ: {str(exc)[:200]}"
                say(problems["discovery"])
        else:
            # Контейнер без наблюдателя (Bothost): поиск сообществ невозможен, это не сбой
            say("Поиск новых сообществ отключён: нет наблюдателя Telegram")
    if not retry_only and kind in (None, "channel"):
        sources = [target[1]] if target else collector.load_sources()
        new = collector.collect(c, sources=sources)
        say(f"Новых постов после префильтра: {len(new)}")
        if sources and len(collector.failed) == len(sources):
            problems["tme"] = ("Канал не открылся: t.me недоступен или канал закрыт." if target
                               else "Ни один канал не открылся: t.me недоступен с сервера.")
        elif collector.failed:
            say(f"Каналы не открылись: {len(collector.failed)}")
    if KWORK_ENABLED and not retry_only and kind in (None, "kwork"):
        try:
            say(f"Новых заказов Kwork после префильтра: {len(kwork.collect(c))}")
        except Exception as exc:
            problems["kwork"] = f"Kwork не ответил: {type(exc).__name__}. Заказы Kwork возьмём в следующий раз."
            say(problems["kwork"])
    if FLRU_ENABLED and not retry_only and kind in (None, "flru"):
        try:
            say(f"Новых заказов FL.ru после префильтра: {len(flru.collect(c))}")
        except Exception as exc:
            problems["flru"] = f"FL.ru не ответил: {type(exc).__name__}. Заказы FL.ru возьмём в следующий раз."
            say(problems["flru"])
    if PCHEL_ENABLED and not retry_only and kind in (None, "pchel"):
        try:
            say(f"Новых проектов Pchel.net после префильтра: {len(pchel.collect(c))}")
        except Exception as exc:
            say(f"Pchel.net не ответил: {type(exc).__name__}")
    if not radar_worker:
        monitor.check(c)
    if not retry_only and kind in (None, "chat"):
        chat_new = watcher.collect(c, sources=[target[1]] if target else None)
        say(f"Новых сообщений из {'чата' if target else 'чатов'} после префильтра: {len(chat_new)}")
        if watcher.problems:
            problems["watcher"] = watcher.problems[0]
        if watcher.skipped:
            say("Чат не прочитан: аккаунт не состоит в нём или чат недоступен" if target
                else f"Чаты не прочитаны: {len(watcher.skipped)}")

    if not radar_worker and not target:
        pending.recover(c, say)
    issue = pending.process(c, say, limit=RADAR_BATCH_SIZE if radar_worker else MAX_CLASSIFICATIONS_PER_RUN,
                            channels=target_channels(c, target) if target else None)
    if issue:
        problems["llm"] = issue
    old = 0

    rows = c.execute("SELECT * FROM posts WHERE status='scored' AND score>=? ORDER BY hot IS NOT NULL DESC, posted_at DESC, score DESC", (MIN_SCORE,)).fetchall()
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
        if (row["replies_n"] or 0) >= REPLIES_COOL:  # пока ждала отправки, под сообщением набралась толпа ответов
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
            sent += 1
            continue  # просмотр не помечает карточку отправленной
        else:
            try:
                tg_msg_id = telegram_out.send_card(row, card)
            except Exception as exc:
                # Карточка остаётся в статусе scored и уйдёт в следующий прогон; остальные шлём дальше
                print("send error:", exc)
                unsent += 1
                problems["send"] = f"Не отправлено карточек: {unsent}. Последняя ошибка: {str(exc)[:200]}"
                if unsent >= 3:
                    break  # leave the remaining scored cards for the next run
                continue
            time.sleep(1)  # лимит Bot API: не чаще одного сообщения в секунду в один чат
        if ex in EXCHANGE_RULES:
            sent_today[ex] += 1
        sent += 1
        c.execute("UPDATE posts SET status='sent', sent_at=?, tg_msg_id=? WHERE channel=? AND msg_id=?",
                  (db.now(), tg_msg_id, row["channel"], row["msg_id"]))
        c.commit()
    say(f"Карточек {'показано' if dry else 'отправлено'}: {sent}")
    if unsent:
        say(f"Карточек не отправлено из-за ошибки: {unsent}")
    if old:
        say(f"Не отправлено как устаревшее (старше {MAX_CARD_AGE_HOURS} ч): {old}")
    if over:
        say(f"Не отправлено по правилам биржи (оценка, свежесть, суточный лимит): {over}")
    if target:
        show_source_cards(c, target, started, dry, say)

    # Метрики гипотезы
    m = c.execute("""SELECT COUNT(*) sent, SUM(feedback='take') took, SUM(feedback='wrong') wrong,
        SUM(feedback='skip') skip FROM posts WHERE status='sent'""").fetchone()
    took, skip, wrong = (m["took"] or 0), (m["skip"] or 0), (m["wrong"] or 0)
    print(f"\nМетрики: отправлено={m['sent']} беру={took} мимо={skip} неточно={wrong}")
    if took + skip + wrong:
        print(f"Precision = {took / (took + skip + wrong):.0%}")
    return problems


MANUAL_WAIT_SECONDS = 15 * 60  # сколько прогон по кнопке ждёт окончания чужого прогона


def notify_owner(text):
    telegram_out.try_api("sendMessage", chat_id=telegram_out.OWNER_CHAT_ID, text=text)


def claim_for_button(c):
    """Прогон по кнопке не пропускается молча: ждёт, пока закончится радар или прогон по расписанию."""
    db.request_manual_run(c)  # радар не запускает новых обработчиков, пока мы ждём
    deadline = time.monotonic() + MANUAL_WAIT_SECONDS
    while True:
        run_id = db.start_run(c, session_required=True)
        if run_id is not None or time.monotonic() >= deadline:
            return run_id
        time.sleep(2)


def recap(c, run_started):
    """Что уже нашлось до этого прогона: радар отправляет карточки сразу, кнопке часто нечего добавить."""
    window = db.ago(hours=MAX_CARD_AGE_HOURS)
    before = c.execute("SELECT COUNT(*) FROM posts WHERE status='sent' AND sent_at>=? AND sent_at<?",
                       (window, run_started)).fetchone()[0]
    candidates = c.execute("SELECT COUNT(*) FROM posts WHERE created_at>=? AND status NOT IN ('skipped')",
                           (window,)).fetchone()[0]
    queued = c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
    out = [f"\nЗа последние {MAX_CARD_AGE_HOURS} ч (до этого прогона):",
           f"• прошло префильтр сообщений: {candidates}",
           f"• отправлено карточек радаром и прогонами по расписанию: {before} — они уже в чате выше"]
    if queued:
        out.append(f"• ждут оценки LLM: {queued}")
    return out


def source_recap(c, target, run_started):
    """Что этот источник дал за последние часы — чтобы «0 карточек» было понятно."""
    names = target_channels(c, target)
    if not names:
        return ["\nПо этому источнику записей пока нет."]
    marks = ",".join("?" * len(names))
    window = db.ago(hours=MAX_CARD_AGE_HOURS)
    r = c.execute(f"""SELECT COUNT(*) total, SUM(status!='skipped') passed,
        SUM(status='sent' AND sent_at<?) before, SUM(status='sent' AND sent_at>=?) now_sent,
        SUM(status IN ('scored','old','limit') AND score<?) low,
        SUM(status='dup') dup FROM posts WHERE channel IN ({marks}) AND posted_at>=?""",
                  (run_started, run_started, MIN_SCORE, *names, window)).fetchone()
    queued = c.execute(f"SELECT COUNT(*) FROM llm_queue WHERE channel IN ({marks})", tuple(names)).fetchone()[0]
    out = [f"\nЭтот источник за последние {MAX_CARD_AGE_HOURS} ч:",
           f"• сообщений: {r['total'] or 0}, прошло префильтр: {r['passed'] or 0}",
           f"• карточек отправлено раньше (радаром): {r['before'] or 0}, в этом прогоне: {r['now_sent'] or 0}",
           f"• оценка ниже {MIN_SCORE}: {r['low'] or 0}"]
    if r["dup"]:
        out.append(f"• дубли уже присланных: {r['dup']}")
    if queued:
        out.append(f"• ждут оценки LLM: {queued}")
    return out


if __name__ == "__main__":
    dry = "--dry" in sys.argv or not BOT_TOKEN
    notify = "--notify" in sys.argv and not dry
    radar_worker = "--radar-worker" in sys.argv
    target = targets.parse(sys.argv[sys.argv.index("--source") + 1]) if "--source" in sys.argv else None
    c = db.connect()
    if notify:
        run_id = claim_for_button(c)
        if run_id is None:
            db.clear_manual_run(c)
            notify_owner(f"Прогон не запущен: другой прогон идёт дольше {MANUAL_WAIT_SECONDS // 60} мин. "
                         "Нажмите кнопку прогона позже или посмотрите /status.")
            sys.exit(0)
    else:
        if db.active_run(c):
            # Два прогона сразу делят одну сессию наблюдателя — второй не запускаем
            print("Прогон уже идёт, этот запуск пропущен")
            sys.exit(0)
        run_id = db.start_run(c, session_required=not radar_worker)
        if run_id is None:
            print("Прогон уже идёт, этот запуск пропущен")
            sys.exit(0)
    run_started = c.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    lines = []

    def say(line):
        print(line)
        lines.append(line)

    error = None
    try:
        for key, text in run(c, dry, say, retry_only="--retry-only" in sys.argv or radar_worker,
                            radar_worker=radar_worker, target=target).items():
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
            db.clear_manual_run(c)
            extra = []
            if not error:
                try:
                    extra = source_recap(c, target, run_started) if target else recap(c, run_started)
                except Exception as ex:  # сводка — подсказка, итог без неё всё равно уходит
                    print("recap error:", ex)
            title = f"Прогон по источнику {target[1]} завершён." if target else "Прогон завершён."
            notify_owner(title + "\n" + ("\n".join(lines + extra) if not error else "Ошибка: " + error))
