"""Точка входа для Bothost: один постоянный процесс без наблюдателя Telegram.

Отличия от серверного радара (leadbot/radar.py): нет аккаунта-наблюдателя, поэтому
каналы читаем публичными страницами t.me/s (collector), а не через Telethon.
Биржи (Kwork, FL.ru, Pchel.net) опрашиваем так же, как радар. Кнопки и команды
владельца слушает отдельный поток (leadbot.feedback). Оценку и отправку карточек
выполняют те же прогоны run.py в подпроцессах: с маленькой партией (--radar-worker)
постоянно и полный прогон по расписанию RUN_HOURS (поиск сообществ там отключён
без наблюдателя — это не сбой).

Пересборка Bothost сохраняет только токен, env, домен и папку data/: база и найденные
источники лежат в DATA_DIR (main.py задаёт его до импорта leadbot, env имеет приоритет).
LLM: на Bothost нет CLI Claude/Codex — по умолчанию цепочка API (Groq -> Anthropic -> Gemini).
"""
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("LLM_BACKEND", "api")

from leadbot import classifier, collector, db, feedback, kwork, flru, monitor, pchel
from leadbot.config import (BOT_TOKEN, FLRU_ENABLED, FLRU_POLL_SECONDS, KWORK_ENABLED, KWORK_POLL_SECONDS,
                            MAX_CARD_AGE_HOURS, MIN_SCORE, MONITOR_SECONDS, PCHEL_ENABLED, PCHEL_POLL_SECONDS,
                            RADAR_WORKER_SECONDS, RUN_HOURS)

MSK = timezone(timedelta(hours=3))
CHANNEL_POLL_SECONDS = int(os.getenv("CHANNEL_POLL_SECONDS", "1800"))  # t.me/s: бережно, раз в 30 минут
FULL_RUN_RETRY_SECONDS = 300  # слот расписания занят — повторим попытку через 5 минут
HEARTBEAT_SECONDS = 10


def has_work(c):
    """Есть ли смысл запускать обработчик очереди (как radar.has_work, без импорта Telethon)."""
    if c.execute("SELECT 1 FROM posts WHERE status='scored' AND score>=? LIMIT 1", (MIN_SCORE,)).fetchone():
        return True
    pending = c.execute("""SELECT 1 FROM llm_queue WHERE posted_at>=?
        AND (next_attempt_at IS NULL OR next_attempt_at<=?) LIMIT 1""",
        (db.ago(hours=MAX_CARD_AGE_HOURS), db.now())).fetchone()
    providers = [p + ':' + m for p, m in classifier.provider_list()]
    return bool(pending) and any(classifier.remaining_cooldown(p, c) <= 0 for p in providers)


def spawn(args):
    return subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), *args], cwd=ROOT)


def feedback_loop():
    """Слушатель кнопок и команд — как служба leadbot-feedback, но потоком."""
    c = db.connect()
    ready = False
    while True:
        try:
            if not ready:
                feedback.setup(c)
                ready = True
            feedback.poll(c)
            feedback.check(c)
        except Exception as exc:
            # Обрыв сети или занятая база — ждём и продолжаем
            print(f"Кнопки: сбой {type(exc).__name__}; продолжаю", flush=True)
            time.sleep(5)


def main():
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    c = db.connect()
    print(f"Bothost-цикл запущен: каналы t.me/s + биржи + очередь LLM"
          f" + кнопки; DATA_DIR={os.environ['DATA_DIR']}", flush=True)
    if BOT_TOKEN:
        threading.Thread(target=feedback_loop, daemon=True).start()
    else:
        print("BOT_TOKEN не задан: кнопки не слушаю (сухой режим)", flush=True)

    worker = full = None
    full_hour = None  # час RUN_HOURS, который уже отработан сегодня
    next_channels = next_kwork = next_flru = next_pchel = next_monitor = 0.0
    next_worker = next_heartbeat = next_full_retry = 0.0
    try:
        while not stopped:
            now = time.monotonic()
            # 1. Сбор: публичные каналы t.me/s и биржи
            if now >= next_channels:
                try:
                    new = collector.collect(c)
                    if new:
                        print(f"Цикл: новых постов каналов после префильтра {len(new)}", flush=True)
                except Exception as exc:
                    print(f"Цикл: каналы не прочитаны ({type(exc).__name__})", flush=True)
                next_channels = now + CHANNEL_POLL_SECONDS
            if KWORK_ENABLED and now >= next_kwork:
                try:
                    found = kwork.collect(c)
                    if found:
                        print(f"Цикл: новых заказов Kwork {len(found)}", flush=True)
                except Exception as exc:
                    print(f"Цикл: Kwork не ответил ({type(exc).__name__})", flush=True)
                next_kwork = now + KWORK_POLL_SECONDS
            if FLRU_ENABLED and now >= next_flru:
                try:
                    found = flru.collect(c)
                    if found:
                        print(f"Цикл: новых заказов FL.ru {len(found)}", flush=True)
                        next_worker = 0  # оценить и прислать сразу, не дожидаясь окна
                except Exception as exc:
                    print(f"Цикл: FL.ru не ответил ({type(exc).__name__})", flush=True)
                next_flru = now + FLRU_POLL_SECONDS
            if PCHEL_ENABLED and now >= next_pchel:
                try:
                    found = pchel.collect(c)
                    if found:
                        print(f"Цикл: новых проектов Pchel.net {len(found)}", flush=True)
                        next_worker = 0
                except Exception as exc:
                    print(f"Цикл: Pchel.net не ответил ({type(exc).__name__})", flush=True)
                next_pchel = now + PCHEL_POLL_SECONDS
            # 2. Самопроверка и пульс (мониторинг смотрит на radar:heartbeat)
            if now >= next_monitor:
                try:
                    monitor.check(c)
                except Exception as exc:
                    print(f"Цикл: мониторинг не выполнен ({type(exc).__name__})", flush=True)
                next_monitor = now + MONITOR_SECONDS
            if now >= next_heartbeat:
                db.set_meta(c, "radar:heartbeat", db.now())
                db.set_meta(c, "radar:state", "bothost")
                next_heartbeat = now + HEARTBEAT_SECONDS
            # 3. Обработчик очереди: маленькая партия, как у радара. Слот полного
            #    прогона важнее: пока он открыт, работника не поднимаем, иначе он
            #    займёт блокировку и прогон стартует впустую («Прогон уже идёт»)
            hour = datetime.now(MSK).hour
            full_pending = hour in RUN_HOURS and hour != full_hour
            if worker is not None and worker.poll() is not None:
                if worker.returncode:
                    print(f"Цикл: обработчик завершился с кодом {worker.returncode}", flush=True)
                worker = None
            if (full is None and worker is None and now >= next_worker and not full_pending
                    and not db.active_run(c) and not db.manual_run_requested(c) and has_work(c)):
                worker = spawn(["--radar-worker"])
                next_worker = now + RADAR_WORKER_SECONDS
            # 4. Полный прогон по расписанию: поиск сообществ отключён, но большая
            #    партия оценки, восстановление очереди и сводка метрик — как по таймеру.
            #    Ждём, пока работчик завершится и освободит блокировку прогонов
            if full_pending and full is None and worker is None and now >= next_full_retry:
                if db.active_run(c) or db.manual_run_requested(c):
                    next_full_retry = now + FULL_RUN_RETRY_SECONDS
                else:
                    full = spawn([])
                    full_hour = hour
                    print(f"Цикл: прогон по расписанию ({hour}:00 МСК) запущен", flush=True)
            if full is not None and full.poll() is not None:
                if full.returncode:
                    print(f"Цикл: прогон по расписанию завершился с кодом {full.returncode}", flush=True)
                full = None
            time.sleep(1)
    finally:
        # Дать идущей отправке карточек закончиться, прежде чем контейнер умрёт
        for proc in (worker, full):
            if proc is not None:
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
        db.set_meta(c, "radar:state", "stopped")
        c.close()


if __name__ == "__main__":
    main()
