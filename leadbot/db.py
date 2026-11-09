import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from .config import DB_PATH, RETENTION_DAYS, SIMILAR_DUP, MAX_CARD_AGE_HOURS
from .anonymize import anonymize
from .hot import flags as hot_flags

RUN_TIMEOUT_HOURS = 1  # как TimeoutStartSec в leadbot.service: прогон старше этого считается оборванным


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # DATA_DIR может ещё не существовать
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
    # author: обезличенный отпечаток автора (не id); replies_n: сколько ответов уже получило сообщение в чате
    # hot: приоритет (phone — в заявке есть телефон, yandex — заказ из Яндекс Услуг); сам телефон не храним
    for col in ("order_url", "exchange", "text_hash", "sent_at",
                "reason", "stage", "stage_at", "reminded_at", "tg_msg_id", "sketch", "recovery_at", "hot", "author"):
        if col not in cols:
            c.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT")
    if "replies_n" not in cols:
        c.execute("ALTER TABLE posts ADD COLUMN replies_n INTEGER DEFAULT 0")
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
    if "session_required" not in {r["name"] for r in c.execute("PRAGMA table_info(runs)")}:
        c.execute("ALTER TABLE runs ADD COLUMN session_required INTEGER NOT NULL DEFAULT 1")
    c.execute("""CREATE TABLE IF NOT EXISTS llm_queue(
        channel TEXT, msg_id INTEGER, text TEXT NOT NULL, exchange TEXT, chat TEXT, posted_at TEXT,
        attempts INTEGER DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
        PRIMARY KEY(channel,msg_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS posts_pending_idx ON posts(status,posted_at)")
    c.commit()
    return c


def text_hash(text):
    """Отпечаток текста для поиска дублей: начало текста без регистра, пробелов и знаков."""
    norm = re.sub(r"[\W_]+", "", text.lower())
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
    """Only successfully evaluated fresh posts can suppress another message."""
    rows = c.execute(
        "SELECT sketch FROM posts WHERE sketch IS NOT NULL AND card IS NOT NULL"
        " AND status IN ('scored','sent','limit') AND posted_at>=?"
        " AND NOT (channel=? AND msg_id=?)", (ago(hours=MAX_CARD_AGE_HOURS), channel, msg_id))
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


MANUAL_RUN_KEY = "run:manual_requested"
MANUAL_RUN_HOLD_MINUTES = 20  # дольше этого флаг кнопки считается забытым (прогон по кнопке упал)


def request_manual_run(c):
    """Кнопка «Прогон» ждёт своей очереди: радар на это время не запускает новых обработчиков."""
    set_meta(c, MANUAL_RUN_KEY, now())


def clear_manual_run(c):
    c.execute("DELETE FROM meta WHERE key=?", (MANUAL_RUN_KEY,))
    c.commit()


def manual_run_requested(c):
    at = get_meta(c, MANUAL_RUN_KEY)
    return bool(at) and at > ago(minutes=MANUAL_RUN_HOLD_MINUTES)


def active_run(c):
    """Прогон, который сейчас идёт: два прогона одновременно делят одну сессию наблюдателя, так нельзя."""
    return c.execute("SELECT * FROM runs WHERE finished_at IS NULL AND started_at > ?",
                     (ago(hours=RUN_TIMEOUT_HOURS),)).fetchone()


def start_run(c, session_required=True):
    # Atomically claim the shared watcher session across regular and retry timers.
    c.execute("BEGIN IMMEDIATE")
    if active_run(c):
        c.rollback()
        return None
    cur = c.execute("INSERT INTO runs(started_at,session_required) VALUES(?,?)", (now(), int(session_required)))
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
    expire_queue(c)
    c.commit()


def enqueue(c, post):
    """Durable temporary payload, anonymized before storage; never overwrite retry timing."""
    c.execute("""INSERT OR IGNORE INTO llm_queue(channel,msg_id,text,exchange,chat,posted_at)
        VALUES(?,?,?,?,?,?)""", (post["channel"], post["msg_id"], anonymize(post["text"])[:3000],
                                post.get("exchange"), post.get("chat"), post["posted_at"]))
    c.execute("UPDATE posts SET text_hash=?,sketch=?,hot=? WHERE channel=? AND msg_id=?",
              (text_hash(post["text"]), sketch(post["text"]), hot_flags(post["text"], post.get("exchange")),
               post["channel"], post["msg_id"]))


def expire_queue(c):
    cutoff = ago(hours=MAX_CARD_AGE_HOURS)
    c.execute("UPDATE posts SET status='old' WHERE status='new' AND posted_at<?", (cutoff,))
    c.execute("""DELETE FROM llm_queue WHERE posted_at<? OR NOT EXISTS(
        SELECT 1 FROM posts p WHERE p.channel=llm_queue.channel AND p.msg_id=llm_queue.msg_id AND p.status='new')""", (cutoff,))


def complete(c, channel, msg_id, status, score=None, card=None):
    c.execute("UPDATE posts SET status=?,score=?,card=? WHERE channel=? AND msg_id=?",
              (status, score, card, channel, msg_id))
    if status == 'dup':
        c.execute("UPDATE posts SET recovery_at=? WHERE channel=? AND msg_id=?", (now(), channel, msg_id))
    c.execute("DELETE FROM llm_queue WHERE channel=? AND msg_id=?", (channel, msg_id))
    c.commit()
