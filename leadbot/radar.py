"""Server-only live observer. The existing worker remains the single card sender."""
import asyncio
import signal
import subprocess
import sys
import time

from telethon import events
from telethon.errors import FloodWaitError

from . import collector, db, watcher, telegram_out, classifier, kwork, flru, pchel, monitor
from .config import PCHEL_ENABLED, PCHEL_POLL_SECONDS, MONITOR_SECONDS, FLRU_ENABLED, FLRU_POLL_SECONDS, KWORK_ENABLED, KWORK_POLL_SECONDS, ROOT, SESSION, TG_API_ID, TG_API_HASH, RADAR_POLL_SECONDS, RADAR_WORKER_SECONDS, MAX_CARD_AGE_HOURS, MIN_SCORE
from .session_lock import SessionLock
from . import business_card


def telegram_busy(c):
    row = db.active_run(c)
    return row is not None and bool(row["session_required"])


def has_work(c):
    if c.execute("SELECT 1 FROM posts WHERE status='scored' AND score>=? LIMIT 1", (MIN_SCORE,)).fetchone() is not None:
        return True
    pending = c.execute("""SELECT 1 FROM llm_queue WHERE posted_at>=?
        AND (next_attempt_at IS NULL OR next_attempt_at<=?) LIMIT 1""",
        (db.ago(hours=MAX_CARD_AGE_HOURS), db.now())).fetchone() is not None
    providers = [p + ':' + m for p, m in classifier.provider_list()]
    return pending and any(classifier.remaining_cooldown(provider, c) <= 0 for provider in providers)


def ingest_event(c, peer_id, message, allowed_peers):
    if peer_id not in allowed_peers:
        return False
    if message.date.isoformat() < db.ago(hours=MAX_CARD_AGE_HOURS):
        return False
    chat = c.execute("SELECT * FROM chats WHERE peer_id=?", (peer_id,)).fetchone()
    if chat is None:
        return False
    try:
        post = watcher.store_message(c, chat, message)
        if post:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('radar:last_candidate',?)", (db.now(),))
        c.commit()
        return post is not None
    except BaseException:
        c.rollback()
        raise


def main():
    if not (TG_API_ID and TG_API_HASH):
        raise RuntimeError("Радар: не настроен наблюдатель Telegram")
    singleton = SessionLock(str(SESSION) + ".radar.lock")
    singleton.acquire(timeout=0)
    c = db.connect()
    client = worker = None
    stopped = False
    peers = set()
    poll_sources = []
    poll_index = 0
    next_poll = next_worker = next_heartbeat = retry_connect = next_kwork = next_flru = next_pchel = next_monitor = 0

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    async def on_message(event):
        try:
            ingest_event(c, event.chat_id, event.message, peers)
        except Exception as exc:
            print(f"Радар: запись события не удалась ({type(exc).__name__}); восстановим из истории", flush=True)

    def disconnect():
        nonlocal client
        if client is not None:
            try:
                client.disconnect()
            finally:
                client = None

    try:
        print("Радар запущен: события + восстановление истории, только чтение", flush=True)
        while not stopped:
            now = time.monotonic()
            busy = telegram_busy(c)
            telegram_wait = watcher.telegram_pause_remaining(c)
            if busy or telegram_wait:
                disconnect()
            elif client is None and now >= retry_connect:
                candidate = watcher._client()
                try:
                    candidate.connect()
                    if not candidate.is_user_authorized():
                        raise RuntimeError("Радар: наблюдатель вышел из аккаунта")
                    candidate.get_me()  # Subscribe to updates without interactive login.
                    candidate.add_event_handler(on_message, events.NewMessage(incoming=True))
                    client = candidate
                    next_poll = 0
                except Exception as exc:
                    candidate.disconnect()
                    retry_connect = now + max(30, getattr(exc, "seconds", 0))
                    print(f"Радар: подключение отложено ({type(exc).__name__})", flush=True)
                    telegram_out.alert(c, "radar_connect", "Радар не подключился к Telegram; обычные прогоны продолжают работать.")
            if client is not None and now >= next_poll:
                if not poll_sources or poll_index >= len(poll_sources):
                    poll_sources = list(dict.fromkeys(watcher.load_chats() + ["@" + ch for ch in collector.load_sources()]))
                    poll_index = 0
                sources = poll_sources
                # Only configured sources may feed the event handler.
                peers.clear()
                peers.update(r["peer_id"] for r in c.execute("SELECT * FROM chats") if r["source"] in sources)
                try:
                    # Bound each slice so a large backlog cannot delay workers or
                    # starve other chats. The committed cursor resumes next cycle.
                    selected = sources[poll_index:poll_index + 3]
                    new = []
                    for source in selected:
                        deadline = time.monotonic() + 5
                        new.extend(watcher.collect(c, client=client, sources=[source], pause_seconds=0.2,
                            should_stop=lambda: stopped or telegram_busy(c) or time.monotonic() >= deadline))
                        if stopped or telegram_busy(c):
                            break
                        poll_index += 1
                    peers.update(r["peer_id"] for r in c.execute("SELECT * FROM chats") if r["source"] in sources)
                    db.set_meta(c, "radar:last_poll", db.now())
                    if new:
                        print(f"Радар: новых кандидатов из истории {len(new)}", flush=True)
                    next_poll = time.monotonic() + (RADAR_POLL_SECONDS if poll_index >= len(sources) else 1)
                except FloodWaitError as exc:
                    watcher.pause_telegram(c, exc.seconds)
                    retry_connect = time.monotonic() + exc.seconds
                    next_poll = retry_connect
                    disconnect()
                    print(f"Радар: пауза Telegram {exc.seconds} с", flush=True)
                except Exception as exc:
                    disconnect()
                    retry_connect = time.monotonic() + 30
                    print(f"Радар: восстановление соединения ({type(exc).__name__})", flush=True)
            if client is not None and not telegram_busy(c) and not watcher.telegram_pause_remaining(c):
                try:
                    business_card.tick(client, c)
                except FloodWaitError as exc:
                    watcher.pause_telegram(c, exc.seconds)
                    disconnect()
                except Exception as exc:
                    print(f"Business card needs inspection ({type(exc).__name__})", flush=True)
            if KWORK_ENABLED and now >= next_kwork:
                # Kwork не зависит от Telegram: опрашиваем биржу даже без подключения наблюдателя
                try:
                    found = kwork.collect(c)
                    if found:
                        print(f"Радар: новых заказов Kwork {len(found)}", flush=True)
                    next_kwork = time.monotonic() + KWORK_POLL_SECONDS
                except Exception as exc:
                    next_kwork = time.monotonic() + KWORK_POLL_SECONDS * 2
                    print(f"Радар: Kwork не ответил ({type(exc).__name__})", flush=True)
            if FLRU_ENABLED and now >= next_flru:
                try:
                    found = flru.collect(c)
                    if found:
                        print(f"Радар: новых заказов FL.ru {len(found)}", flush=True)
                        next_worker = 0  # оценить и прислать сразу, не дожидаясь очередного окна
                    next_flru = time.monotonic() + FLRU_POLL_SECONDS
                except Exception as exc:
                    next_flru = time.monotonic() + FLRU_POLL_SECONDS * 3
                    print(f"Радар: FL.ru не ответил ({type(exc).__name__})", flush=True)
            if PCHEL_ENABLED and now >= next_pchel:
                try:
                    found = pchel.collect(c)
                    if found:
                        print(f"Радар: новых проектов Pchel.net {len(found)}", flush=True)
                        next_worker = 0
                    next_pchel = time.monotonic() + PCHEL_POLL_SECONDS
                except Exception as exc:
                    next_pchel = time.monotonic() + PCHEL_POLL_SECONDS * 3
                    print(f"Радар: Pchel.net не ответил ({type(exc).__name__})", flush=True)
            if now >= next_monitor:
                try:
                    monitor.check(c)
                except Exception as exc:
                    print(f"Радар: мониторинг не выполнен ({type(exc).__name__})", flush=True)
                next_monitor = time.monotonic() + MONITOR_SECONDS
            if now >= next_heartbeat:
                db.set_meta(c, "radar:heartbeat", db.now())
                db.set_meta(c, "radar:state", "paused" if busy else "waiting" if telegram_wait else "connected" if client is not None else "reconnecting")
                next_heartbeat = now + 10
            if worker is not None and worker.poll() is not None:
                if worker.returncode:
                    print(f"Радар: обработчик завершился с кодом {worker.returncode}", flush=True)
                worker = None
            # Прогон по кнопке ждёт свободной сессии: не перехватываем её новым обработчиком
            if (now >= next_worker and worker is None and not db.active_run(c)
                    and not db.manual_run_requested(c) and has_work(c)):
                worker = subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), "--radar-worker"], cwd=ROOT)
                next_worker = time.monotonic() + RADAR_WORKER_SECONDS
            if client is not None:
                client.loop.run_until_complete(asyncio.sleep(1))
            else:
                time.sleep(1)
    finally:
        disconnect()
        # Finish an in-flight card transaction before systemd stops the process group.
        try:
            if worker is not None:
                worker.wait(timeout=300)
        finally:
            db.set_meta(c, "radar:state", "stopped")
            c.close()
            singleton.release()


if __name__ == "__main__":
    main()
