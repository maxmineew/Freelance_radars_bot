"""Самопроверка бота: диск, очередь, радар, вход Claude, плохие источники. Сообщения владельцу — через telegram_out.alert."""
import shutil
from datetime import datetime, timedelta, timezone

from . import db, telegram_out
from .config import (DB_PATH, DISK_ALERT_PERCENT, EXCHANGE_CHANNELS, MUTED_FILE, QUEUE_ALERT_SIZE,
                     SOURCE_MUTE_MIN_MARKS, SOURCE_MUTE_PRECISION, LLM_BACKEND, muted)


def disk_percent(path=None):
    usage = shutil.disk_usage(path or DB_PATH.parent)
    return usage.used * 100 / usage.total


def check_disk(c):
    pct = disk_percent()
    if pct >= DISK_ALERT_PERCENT:
        telegram_out.alert(c, "disk", f"Диск сервера заполнен на {pct:.0f}%. Освободите место, иначе бот встанет.")
        return True
    return False


def check_queue(c):
    size = c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
    prev = db.get_meta(c, "mon:queue")
    grew = False
    if prev:
        at, _, n = prev.partition("|")
        if at < db.ago(hours=1):
            grew = size - int(n) >= 300
            db.set_meta(c, "mon:queue", f"{db.now()}|{size}")
    else:
        db.set_meta(c, "mon:queue", f"{db.now()}|{size}")
    c.commit()
    if size >= QUEUE_ALERT_SIZE or grew:
        telegram_out.alert(c, "queue", f"Очередь оценки растёт: {size} сообщений. Проверьте вход LLM и радар.")
        return True
    return False


def check_radar(c):
    beat = db.get_meta(c, "radar:heartbeat")
    if beat and datetime.fromisoformat(beat) < datetime.now(timezone.utc) - timedelta(minutes=10):
        telegram_out.alert(c, "radar", "Радар не отвечает больше 10 минут: проверьте сервис leadbot-radar.")
        return True
    return False


def check_login(c):
    row = c.execute("SELECT 1 FROM llm_queue WHERE last_error LIKE '%not logged in%' LIMIT 1").fetchone()
    if row:
        codex = LLM_BACKEND == "codex_cli"
        name = "Codex" if codex else "Claude"
        command = "codex login --device-auth" if codex else "claude"
        telegram_out.alert(c, "codex_login" if codex else "claude_login",
                           f"Вход {name} истёк. На сервере выполните: "
                           f"runuser -u leadbot -- env HOME=/opt/leadbot {command} — и войдите заново.")
        return True
    return False


def source_precision(c):
    """[(channel, marks, takes)] по неслужебным источникам за срок хранения."""
    return [(r["channel"], r["marks"], r["take"] or 0) for r in c.execute(
        """SELECT channel, SUM(feedback IN ('take','skip','wrong')) marks, SUM(feedback='take') take
        FROM posts WHERE status='sent' GROUP BY channel""") if r["marks"]]


def mute_bad_sources(c):
    """Источник с точностью ниже порога при достаточном числе отметок перестаём читать и сообщаем владельцу."""
    bad = []
    off = muted()
    for channel, marks, take in source_precision(c):
        if channel in EXCHANGE_CHANNELS or marks < SOURCE_MUTE_MIN_MARKS or take / marks >= SOURCE_MUTE_PRECISION:
            continue
        chat = c.execute("SELECT source,title FROM chats WHERE name=?", (channel,)).fetchone()
        source = chat["source"] if chat else "@" + channel
        if source.lstrip("@").lower() in off:
            continue
        bad.append((source, chat["title"] if chat else source, marks, take))
    if not bad:
        return []
    with MUTED_FILE.open("a", encoding="utf-8") as f:
        for source, _, _, _ in bad:
            f.write(source + "\n")
    lines = [f"Отключил источник «{t}»: «Беру» {take} из {marks} отметок." for _, t, marks, take in bad]
    telegram_out.alert(c, "mute:" + ",".join(s for s, *_ in bad),
                       "\n".join(lines) + f"\nВернуть: удалите строку из {MUTED_FILE.name} на сервере.")
    return bad


def check(c):
    """Все проверки; ошибка одной не мешает остальным."""
    found = []
    for name, fn in (("disk", check_disk), ("queue", check_queue), ("radar", check_radar),
                     ("login", check_login), ("mute", mute_bad_sources)):
        try:
            if fn(c):
                found.append(name)
        except Exception as exc:
            print(f"Мониторинг {name}: {type(exc).__name__}", flush=True)
    return found
