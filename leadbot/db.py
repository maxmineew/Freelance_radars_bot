import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from .config import DB_PATH, RETENTION_DAYS, SIMILAR_DUP

RUN_TIMEOUT_HOURS = 1  # как TimeoutStartSec в leadbot.service: прогон старше этого считается оборванным


def connect():
    c = sqlite3.connect(DB_PATH, timeout=60)  # прогон и слушатель кнопок пишут в одну базу
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS posts(
            channel TEXT, msg_id INTEGER, url TEXT, posted_at TEXT,
            status TEXT DEFAULT 'new',      -- new | skipped | scored | sent | dup | limit | old
            score INTEGER, card TEXT,       -- json
            feedback TEXT,                  -- take | skip | wrong
            created_at TEXT,
            PRIMARY KEY(channel, msg_id))"""
    )
    cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
    # order_url, exchange: ссылка на заказ на бирже (Kwork и т.п.); text_hash: поиск дублей
    # sent_at: когда карточка ушла владельцу (для суточных лимитов по биржам)
    # reason: причина отметки «Неточно»; stage, stage_at: этап работы со взятым лидом и когда он менялся
    # reminded_at: когда ушло напоминание по этому этапу; tg_msg_id: сообщение с карточкой
    # sketch: отпечаток для поиска похожих текстов
    for col in ("order_url", "exchange", "text_hash", "sent_at",
                "reason", "stage", "stage_at", "reminded_at", "tg_msg_id", "sketch"):
        if col not in cols:
            c.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT")
    c.execute(
        """CREATE TABLE IF NOT EXISTS chats(
            source TEXT PRIMARY KEY,        -- строка из chats.txt
            peer_id INTEGER, name TEXT, title TEXT,
            last_id INTEGER DEFAULT 0)      -- последнее прочитанное сообщение"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS runs(
            id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
            summary TEXT, error TEXT)"""
    )
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    return c


def text_hash(text):
    """Отпечаток текста для поиска дублей: начало текста без регистра, пробелов и знаков."""
    norm = re.sub(r"[\W_]+", "", text.lower())[:300]
    return hashlib.sha1(norm.encode()).hexdigest()


def sketch(text, k=24):
    """Отпечаток для поиска похожих текстов: k наименьших хешей четвёрок соседних слов.
    Сам текст по отпечатку не восстановить."""
    words = re.findall(r"\w+", text.lower())
    shingles = {" ".join(words[i:i + 4]) for i in range(max(1, len(words) - 3))}
    return " ".join(sorted({hashlib.sha1(s.encode()).hexdigest()[:8] for s in shingles})[:k])


def similarity(a, b, k=24):
    """Доля общих четвёрок слов у двух текстов (оценка по их отпечаткам), от 0 до 1."""
    a, b = set(a.split()), set(b.split())
    union = sorted(a | b)[:k]
    return sum(h in a and h in b for h in union) / len(union) if union else 0


def has_similar(c, channel, msg_id, sk):
    """Есть ли среди более ранних записей похожий текст: тот же заказ с другой подписью или шапкой."""
    rows = c.execute(
        "SELECT sketch FROM posts WHERE sketch IS NOT NULL AND status!='skipped'"
        " AND rowid < (SELECT rowid FROM posts WHERE channel=? AND msg_id=?)", (channel, msg_id))
    return any(similarity(sk, r["sketch"]) >= SIMILAR_DUP for r in rows)


def now():
    return datetime.now(timezone.utc).isoformat()


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def get_meta(c, key):
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row and row["value"]


def set_meta(c, key, value):
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
    c.commit()


def active_run(c):
    """Прогон, который сейчас идёт: два прогона одновременно делят одну сессию наблюдателя, так нельзя."""
    return c.execute("SELECT * FROM runs WHERE finished_at IS NULL AND started_at > ?",
                     (ago(hours=RUN_TIMEOUT_HOURS),)).fetchone()


def start_run(c):
    cur = c.execute("INSERT INTO runs(started_at) VALUES(?)", (now(),))
    c.commit()
    return cur.lastrowid


def finish_run(c, run_id, summary, error=None):
    c.execute("UPDATE runs SET finished_at=?, summary=?, error=? WHERE id=?", (now(), summary, error, run_id))
    c.commit()


def purge_old(c):
    """Автоудаление: записи старше RETENTION_DAYS удаляются, кроме отмеченных «Беру»."""
    limit = ago(days=RETENTION_DAYS)
    c.execute("DELETE FROM posts WHERE created_at < ? AND (feedback IS NULL OR feedback != 'take')", (limit,))
    c.execute("DELETE FROM runs WHERE started_at < ?", (limit,))
    c.commit()
