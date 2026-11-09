"""Этап 2: чтение чатов под отдельным аккаунтом (Telethon).
Только чтение: сам никуда не вступает, ничего не пишет, авторов сообщений не сохраняет.
Вход (один раз, в своём терминале): python -m leadbot.watcher"""
import re
import time
import hashlib
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from telethon.utils import get_peer_id
from telethon.errors import FloodWaitError
from .config import muted, CHATS_FILE, DISCOVERED_CHATS_FILE, SESSION, TG_API_ID, TG_API_HASH, CHAT_MSG_LIMIT, MAX_CARD_AGE_HOURS
from .collector import prefilter, EXCHANGES, _SKIP_PATHS
from . import db
from .session_lock import LockedClient

_INVITE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/|^\+)([\w-]+)")
INVITE_RECHECK_SECONDS = 3600
SHORT_FLOOD_WAIT_SECONDS = 30


def telegram_pause_remaining(c):
    until = db.get_meta(c, "telegram:pause_until")
    if not until:
        return 0
    return max(0, (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds())


def pause_telegram(c, seconds):
    seconds = max(seconds, telegram_pause_remaining(c))
    db.set_meta(c, "telegram:pause_until", (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())


def invite_cache_key(src):
    return "telegram:invite:" + hashlib.sha256(src.encode()).hexdigest()


def load_chats():
    out = []
    off = muted()
    for path in (CHATS_FILE, DISCOVERED_CHATS_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in out and line.lstrip("@").lower() not in off:
                out.append(line)
    return out


def _client():
    return LockedClient(lambda: TelegramClient(str(SESSION), int(TG_API_ID), TG_API_HASH,
                                               flood_sleep_threshold=SHORT_FLOOD_WAIT_SECONDS), str(SESSION) + ".lock")


def resolve(client, c, src, refresh_invites=False):
    """Чат по строке из chats.txt. Результат кешируется в БД, чтобы не дёргать поиск по имени каждый прогон."""
    row = c.execute("SELECT * FROM chats WHERE source=?", (src,)).fetchone()
    if row:
        try:
            return client.get_input_entity(row["peer_id"]), row
        except ValueError:
            pass  # чата нет в кеше сессии (сессию заменили) — находим заново
    m = _INVITE.search(src)
    if m:
        key = invite_cache_key(src)
        cached = db.get_meta(c, key)
        if not refresh_invites and cached and datetime.fromisoformat(cached) > datetime.now(timezone.utc):
            return None, None
        inv = client(CheckChatInviteRequest(m.group(1)))
        if not isinstance(inv, ChatInviteAlready):
            db.set_meta(c, key, (datetime.now(timezone.utc) + timedelta(seconds=INVITE_RECHECK_SECONDS)).isoformat())
            return None, None  # в закрытый чат нужно вступить вручную
        c.execute("DELETE FROM meta WHERE key=?", (key,))
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


def author_hash(message):
    """Односторонний отпечаток автора: по нему считаем повторы, личность не храним."""
    sender = getattr(message, "sender_id", None)
    return hashlib.sha256(f"leadbot:{sender}".encode()).hexdigest()[:12] if sender else None


def count_reply(c, chat, message, author):
    """Ответ на сообщение другого автора остужает лид: считаем ответы под исходным постом."""
    reply = getattr(message, "reply_to", None)
    target = getattr(reply, "reply_to_msg_id", None)
    if target:
        c.execute("UPDATE posts SET replies_n=COALESCE(replies_n,0)+1 WHERE channel=? AND msg_id=?"
                  " AND (author IS NULL OR author!=?)", (chat["name"], target, author or ""))


def store_message(c, chat, message):
    """Events store candidates but NEVER advance the contiguous history cursor."""
    if message.out or not message.message:
        return None
    status = "new" if prefilter(message.message) else "skipped"
    author = author_hash(message)
    is_bot = chat["peer_id"] > 0
    order_url = exchange = None
    links = re.findall(r"https?://[^\s<>]+", message.message)
    links += [entity.url for entity in (getattr(message, "entities", None) or [])
              if isinstance(getattr(entity, "url", None), str)]
    for link in links:
        url = urlsplit(link.rstrip(".,)"))
        name = EXCHANGES.get(url.netloc.lower().removeprefix("www."))
        if name and url.path not in ("", "/") and not any(s in url.path for s in _SKIP_PATHS):
            order_url, exchange = urlunsplit((url.scheme, url.netloc, url.path, "", "")), name
            break
    cur = c.execute(
        "INSERT OR IGNORE INTO posts(channel,msg_id,url,posted_at,status,created_at,text_hash,sketch,order_url,exchange,author)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (chat["name"], message.id,
         f"https://t.me/{chat['name']}" if is_bot else f"https://t.me/{chat['name']}/{message.id}",
         message.date.isoformat(), status, db.now(), db.text_hash(message.message), db.sketch(message.message), order_url, exchange, author))
    if cur.rowcount:
        count_reply(c, chat, message, author)
    if cur.rowcount and status == "new":
        post = {"channel": chat["name"], "msg_id": message.id, "text": message.message,
                "exchange": exchange, "chat": chat["title"], "posted_at": message.date.isoformat()}
        db.enqueue(c, post)
        return post
    return None


def collect(c, client=None, sources=None, pause_seconds=3, should_stop=lambda: False):
    problems.clear()
    skipped.clear()
    if not (TG_API_ID and TG_API_HASH):
        return []
    remaining = telegram_pause_remaining(c)
    if remaining:
        problems.append(f"Telegram просит паузу ещё {int(remaining) + 1} с; чаты прочитаем позже, очередь сохранена.")
        return []
    limit = datetime.now(timezone.utc) - timedelta(hours=MAX_CARD_AGE_HOURS)
    new = []
    own_client = client is None
    client = client or _client()
    if own_client:
        client.connect()
    try:
        if not client.is_user_authorized():
            print("Наблюдатель: нет входа в аккаунт. Выполните: python -m leadbot.watcher")
            problems.append("Наблюдатель вышел из аккаунта, чаты не читаются. "
                            "Войдите заново на сервере: python -m leadbot.watcher")
            return []
        seen_peers = set()
        for src in (load_chats() if sources is None else sources):
            if should_stop():
                break
            try:
                ent, chat = resolve(client, c, src, refresh_invites=own_client)
                if not ent:
                    print(f"Наблюдатель: {src} пропущен, аккаунт не состоит в чате")
                    skipped.append(src)
                    continue
                if chat["peer_id"] in seen_peers:
                    continue  # публичное имя и приглашение могут вести в один чат
                seen_peers.add(chat["peer_id"])
                # Oldest unread first; a page limit must never jump over the backlog.
                # Commit each message WITH its cursor so interruption is resumable.
                last = chat["last_id"]
                while not should_stop():
                    count = 0
                    kwargs = {"min_id": last, "limit": CHAT_MSG_LIMIT, "reverse": True}
                    if not last:
                        kwargs["offset_date"] = limit
                    for m in client.iter_messages(ent, **kwargs):
                        if should_stop():
                            break
                        count += 1
                        try:
                            if m.date >= limit:
                                post = store_message(c, chat, m)
                                if post:
                                    new.append(post)
                            last = max(last, m.id)
                            c.execute("UPDATE chats SET last_id=? WHERE source=?", (last, src))
                            c.commit()
                        except BaseException:
                            c.rollback()
                            raise
                    if count < CHAT_MSG_LIMIT:
                        break
            except FloodWaitError as exc:
                pause_telegram(c, exc.seconds)
                problems.append(f"Telegram просит паузу {exc.seconds} с; сбор чатов отложен, очередь сохранена.")
                if not own_client:
                    raise  # Radar releases its lease and waits without reconnecting early.
                break  # A full run can still classify its saved queue and deliver cards.
            except Exception as ex:
                print(f"Наблюдатель: {src}: {ex}")
                skipped.append(src)
            time.sleep(pause_seconds)
    finally:
        if own_client:
            client.disconnect()
    return new


if __name__ == "__main__":
    with _client() as client:  # спросит номер телефона и код из Telegram
        print("Вход выполнен:", client.get_me().first_name)
