"""Этап 2: чтение чатов под отдельным аккаунтом (Telethon).
Только чтение: сам никуда не вступает, ничего не пишет, авторов сообщений не сохраняет.
Вход (один раз, в своём терминале): python -m leadbot.watcher"""
import re
import time
from datetime import datetime, timedelta, timezone
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from telethon.utils import get_peer_id
from .config import CHATS_FILE, SESSION, TG_API_ID, TG_API_HASH, CHAT_MSG_LIMIT, MAX_AGE_DAYS
from .collector import prefilter
from . import db

_INVITE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/|^\+)([\w-]+)")


def load_chats():
    if not CHATS_FILE.exists():
        return []
    lines = (line.strip() for line in CHATS_FILE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _client():
    return TelegramClient(str(SESSION), int(TG_API_ID), TG_API_HASH)


def resolve(client, c, src):
    """Чат по строке из chats.txt. Результат кешируется в БД, чтобы не дёргать поиск по имени каждый прогон."""
    row = c.execute("SELECT * FROM chats WHERE source=?", (src,)).fetchone()
    if row:
        try:
            return client.get_input_entity(row["peer_id"]), row
        except ValueError:
            pass  # чата нет в кеше сессии (сессию заменили) — находим заново
    m = _INVITE.search(src)
    if m:
        inv = client(CheckChatInviteRequest(m.group(1)))
        if not isinstance(inv, ChatInviteAlready):
            return None, None  # в закрытый чат нужно вступить вручную
        ent = inv.chat
    else:
        ent = client.get_entity(src.rsplit("/", 1)[-1].lstrip("@"))
    name = getattr(ent, "username", None) or f"c/{ent.id}"
    if not row:
        c.execute("INSERT INTO chats(source,peer_id,name,title,last_id) VALUES(?,?,?,?,0)",
                  (src, get_peer_id(ent), name, getattr(ent, "title", None) or ent.first_name))  # у бота нет title
    c.commit()
    return ent, c.execute("SELECT * FROM chats WHERE source=?", (src,)).fetchone()


problems = []  # сбои этого прогона, о которых нужно сообщить владельцу
skipped = []   # чаты, которые в этом прогоне прочитать не удалось


def collect(c):
    problems.clear()
    skipped.clear()
    if not (TG_API_ID and TG_API_HASH):
        return []
    limit = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    new = []
    client = _client()
    client.connect()
    try:
        if not client.is_user_authorized():
            print("Наблюдатель: нет входа в аккаунт. Выполните: python -m leadbot.watcher")
            problems.append("Наблюдатель вышел из аккаунта, чаты не читаются. "
                            "Войдите заново на сервере: python -m leadbot.watcher")
            return []
        for src in load_chats():
            try:
                ent, chat = resolve(client, c, src)
                if not ent:
                    print(f"Наблюдатель: {src} пропущен, аккаунт не состоит в чате")
                    skipped.append(src)
                    continue
                last = chat["last_id"]
                is_bot = chat["peer_id"] > 0  # личный диалог с ботом: ссылок на отдельные сообщения нет
                for m in client.iter_messages(ent, min_id=chat["last_id"], limit=CHAT_MSG_LIMIT):
                    last = max(last, m.id)
                    if m.date < limit:
                        break
                    if m.out or not m.message:
                        continue
                    status = "new" if prefilter(m.message) else "skipped"
                    cur = c.execute(
                        "INSERT OR IGNORE INTO posts(channel,msg_id,url,posted_at,status,created_at,text_hash,sketch)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (chat["name"], m.id,
                         f"https://t.me/{chat['name']}" if is_bot else f"https://t.me/{chat['name']}/{m.id}",
                         m.date.isoformat(), status, db.now(), db.text_hash(m.message), db.sketch(m.message)),
                    )
                    if cur.rowcount and status == "new":
                        new.append({"channel": chat["name"], "msg_id": m.id, "text": m.message,
                                    "exchange": None, "chat": chat["title"],
                                    "posted_at": m.date.isoformat()})
                c.execute("UPDATE chats SET last_id=? WHERE source=?", (last, src))
                c.commit()
            except Exception as ex:
                print(f"Наблюдатель: {src}: {ex}")
                skipped.append(src)
            time.sleep(3)  # вежливая пауза
    finally:
        client.disconnect()
    return new


if __name__ == "__main__":
    with _client() as client:  # спросит номер телефона и код из Telegram
        print("Вход выполнен:", client.get_me().first_name)
