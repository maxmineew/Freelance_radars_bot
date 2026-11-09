"""Daily web discovery. Never joins groups or sends messages to communities."""
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, unquote

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel
from telethon.utils import get_peer_id

from . import db, watcher, collector, telegram_out, pending
from .config import (DISCOVERED_CHATS_FILE, DISCOVERED_SOURCES_FILE,
                     DISCOVERY_MAX_ADDITIONS, DISCOVERY_SAMPLE, DISCOVERY_MIN_DEMAND, TG_API_ID, TG_API_HASH)

MSK = timezone(timedelta(hours=3))
QUERIES = (
    "Telegram чат владельцев стоматологий",
    "Telegram чат собственников медицинских клиник",
    "Telegram сообщество руководителей контакт центров",
    "Telegram чат предпринимателей отдел продаж обзвон клиентов",
    "Telegram чат руководителей автосервисов",
    "Telegram чат amoCRM специалисты заказчики",
    "Telegram чат Bitrix24 внедрение автоматизация заказы",
    "Telegram чат владельцев салонов красоты администраторы",
)
CATALOGS = {"ru.intelegram.one", "tg-cat.com", "tgboard.com", "tg.me",
            "telegram-dialogs.ru", "tg.place", "telagon.io"}
LINK = re.compile(r"https?://(?:t\.me|telegram\.me|tg\.me)/(?:s/)?(?:\+[\w-]+|joinchat/[\w-]+|[A-Za-z][\w]{3,31})", re.I)
ROLES = re.compile(r"владел|собствен|управл|руковод|директор|предприним|бизнес|контакт.?центр|колл?.?центр", re.I)
NICHES = re.compile(r"стомат|клиник|медицин|контакт.?центр|колл?.?центр|продаж|автосервис|салон|предприним|бизнес", re.I)
EXCLUDE = re.compile(r"крипт|обнал|скам|казино|арбитраж|продажа баз|базы.*обнал", re.I)


def normalize(url):
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in {"t.me", "telegram.me", "tg.me"}:
        return None
    path = unquote(parts.path).strip("/")
    if path.startswith("s/"):
        path = path[2:]
    name = path.split("/")[0]
    if path.startswith("joinchat/"):
        name = "+" + path.split("/")[1]
    if name.startswith("+") and re.fullmatch(r"\+[\w-]+", name):
        return "https://t.me/" + name
    if not re.fullmatch(r"[A-Za-z][\w]{3,31}", name) or name.lower().endswith("bot"):
        return None
    if name.lower() in {"share", "proxy", "socks", "login", "addstickers", "addemoji", "joinchat"}:
        return None
    return "@" + name.lower()


def extract_links(text):
    return {src for url in LINK.findall(text) if (src := normalize(url))}


def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS discovered_sources(
        source TEXT PRIMARY KEY, evidence TEXT, title TEXT, kind TEXT,
        peer_id INTEGER, status TEXT, detail TEXT, checked_at TEXT, notified INTEGER DEFAULT 0)""")
    c.commit()


def record(c, source, evidence, title, kind, peer, status, detail=""):
    c.execute("""INSERT INTO discovered_sources(source,evidence,title,kind,peer_id,status,detail,checked_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
        evidence=excluded.evidence,title=excluded.title,kind=excluded.kind,peer_id=excluded.peer_id,
        notified=CASE WHEN discovered_sources.status!=excluded.status THEN 0 ELSE discovered_sources.notified END,
        status=excluded.status,detail=excluded.detail,checked_at=excluded.checked_at""",
        (source, evidence, title, kind, peer, status, detail, db.now()))
    c.commit()


def write_sources(c):
    for kind, path in (("chat", DISCOVERED_CHATS_FILE), ("channel", DISCOVERED_SOURCES_FILE)):
        names = [r[0] for r in c.execute(
            "SELECT source FROM discovered_sources WHERE status='active' AND kind=? ORDER BY source", (kind,))]
        content = "# Автоматически найденные источники; управляются leadbot.discovery\n"
        content += "\n".join(names) + "\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)


def ccguru_links(client):
    links = set()
    for msg in client.iter_messages("CCG_chatbot", limit=30):
        if msg.out:
            continue
        links.update(extract_links(msg.message or ""))
        for ent in msg.entities or []:
            links.update(extract_links(getattr(ent, "url", "")))
        for row in getattr(msg.reply_markup, "rows", []) or []:
            for button in row.buttons:
                links.update(extract_links(getattr(button, "url", "")))
    return links


def web_candidates(http, say):
    found, success = {}, 0
    for query in QUERIES:
        try:
            rows = DDGS(timeout=12).text(query, region="ru-ru", max_results=8, backend="yahoo,brave,bing")
            rows = [r for r in rows if r.get("href") and r.get("title")]
            if not rows:
                raise RuntimeError("Empty search results")
            success += 1
            for row in rows:
                url = row["href"]
                for src in extract_links(url + " " + row.get("body", "")):
                    found.setdefault(src, url)
                if (urlsplit(url).hostname or "").lower() in CATALOGS:
                    try:
                        response = http.get(url)
                        response.raise_for_status()
                        soup = BeautifulSoup(response.text, "lxml")
                        # Only the description/main links, not unrelated catalog recommendations.
                        main = soup.select_one("main") or soup
                        links = [a.get("href", "") for a in main.select("a[href]")]
                        for src in extract_links(" ".join(links)):
                            found.setdefault(src, url)
                    except httpx.HTTPError:
                        pass
        except Exception as exc:
            say(f"Поиск сообществ: запрос не выполнен ({type(exc).__name__})")
        time.sleep(1)
    return found, success


def validate(client, c, src, evidence):
    ent, _ = watcher.resolve(client, c, src, refresh_invites=True)
    if ent is None:
        record(c, src, evidence, "Закрытое сообщество", "chat", None, "join_required", "Вступить под аккаунтом наблюдателя")
        return False
    if not isinstance(ent, Channel):
        record(c, src, evidence, getattr(ent, "title", ""), "other", get_peer_id(ent), "rejected", "Не канал или супергруппа")
        return False
    peer = get_peer_id(ent)
    known = c.execute("SELECT 1 FROM discovered_sources WHERE peer_id=? AND status='active'", (peer,)).fetchone()
    configured = set(watcher.load_chats()) | {"@" + s for s in collector.load_sources()}
    alias = c.execute("SELECT source FROM chats WHERE peer_id=?", (peer,)).fetchall()
    if known or any(r["source"] in configured and r["source"] != src for r in alias):
        record(c, src, evidence, ent.title, "chat", peer, "duplicate")
        return False
    full = client(GetFullChannelRequest(ent)).full_chat
    description = ent.title + " " + (getattr(full, "about", "") or "")
    kind = "chat" if getattr(ent, "megagroup", False) else "channel"
    from_ccguru = evidence == "https://t.me/CCG_chatbot"
    if EXCLUDE.search(description) or (not from_ccguru and not (ROLES.search(description) and NICHES.search(description))):
        record(c, src, evidence, ent.title, kind, peer, "rejected", "Тематика не соответствует целевому бизнесу")
        return False
    if not from_ccguru and (getattr(full, "participants_count", 0) or 0) < 500:
        record(c, src, evidence, ent.title, kind, peer, "rejected", "Меньше 500 участников")
        return False
    messages = list(client.iter_messages(ent, limit=DISCOVERY_SAMPLE))
    recent = [m for m in messages if m.message and m.date > datetime.now(timezone.utc) - timedelta(days=14)]
    if not recent:
        record(c, src, evidence, ent.title, kind, peer, "rejected", "Нет доступных текстовых сообщений за 14 дней")
        return False
    # Доля заявок: в живом чате за выборку встречаются явные запросы «ищу/нужен исполнитель»
    demand = sum(1 for m in recent if pending.DIRECT.search(m.message))
    if kind == "chat" and not from_ccguru and demand < DISCOVERY_MIN_DEMAND:
        record(c, src, evidence, ent.title, kind, peer, "rejected", f"Мало заявок: {demand} из {len(recent)}")
        return False
    # Expert broadcast channels do not expose customer discussion; only include channels with order signals.
    if kind == "channel" and not any(re.search(r"тендер|заказ|ищем|требуется|нужен|нужна", m.message, re.I) for m in recent):
        record(c, src, evidence, ent.title, kind, peer, "rejected", "Канал без признаков заказов")
        return False
    record(c, src, evidence, ent.title, kind, peer, "active")
    return True


def notify(c):
    rows = c.execute("SELECT * FROM discovered_sources WHERE notified=0 AND status IN ('active','join_required') LIMIT 10").fetchall()
    if not rows:
        return
    lines = ["🔎 Поиск новых сообществ"]
    for row in rows:
        url = row["source"] if row["source"].startswith("https:") else "https://t.me/" + row["source"].lstrip("@")
        label = "Подключено" if row["status"] == "active" else "Нужно вступить аккаунтом наблюдателя"
        lines.append(f"{label}: {telegram_out.esc(row['title'])}\n{telegram_out.esc(url)}")
    telegram_out.send("\n\n".join(lines))
    c.executemany("UPDATE discovered_sources SET notified=1 WHERE source=?", [(r["source"],) for r in rows])
    c.commit()


def run(c, say=print, force=False, notify_owner=True):
    init(c)
    remaining = watcher.telegram_pause_remaining(c)
    if remaining:
        say(f"Поиск сообществ отложен: Telegram просит паузу ещё {int(remaining) + 1} с")
        return
    today = datetime.now(MSK).date().isoformat()
    if not force and db.get_meta(c, "discovery:day") == today:
        return
    attempt = db.get_meta(c, "discovery:attempt")
    if not force and attempt and datetime.fromisoformat(attempt) > datetime.now(timezone.utc) - timedelta(hours=6):
        return
    db.set_meta(c, "discovery:attempt", db.now())
    if not (TG_API_ID and TG_API_HASH):
        raise RuntimeError("Поиск сообществ: не настроен наблюдатель Telegram")
    with httpx.Client(timeout=15, follow_redirects=False, headers=collector.HEADERS) as http:
        candidates, successful = web_candidates(http, say)
    # Recheck invitations after the owner has joined, even if search no longer returns them.
    for row in c.execute("SELECT source,evidence FROM discovered_sources WHERE status='join_required'"):
        candidates.setdefault(row["source"], row["evidence"])
    client = watcher._client()
    client.connect()
    added = 0
    try:
        if not client.is_user_authorized():
            raise RuntimeError("Поиск сообществ: требуется вход наблюдателя")
        try:
            cc_links = ccguru_links(client)
            candidates = {**{src: "https://t.me/CCG_chatbot" for src in cc_links}, **candidates}
            for src in cc_links:
                candidates[src] = "https://t.me/CCG_chatbot"
            if not cc_links:
                say("CCGuru: в диалоге наблюдателя нет ссылки на сообщество")
        except FloodWaitError as exc:
            watcher.pause_telegram(c, exc.seconds)
            raise
        except Exception as exc:
            say(f"CCGuru: проверка ссылок не выполнена ({type(exc).__name__})")
        configured = set(watcher.load_chats()) | {"@" + s for s in collector.load_sources()}
        for src, evidence in list(candidates.items())[:40]:
            if added >= DISCOVERY_MAX_ADDITIONS:
                break
            if src in configured:
                continue
            old = c.execute("SELECT * FROM discovered_sources WHERE source=?", (src,)).fetchone()
            if old and old["status"] == "active":
                continue
            if old and old["status"] != "join_required" and old["checked_at"] > db.ago(days=7):
                continue
            try:
                added += int(validate(client, c, src, evidence))
            except FloodWaitError as exc:
                watcher.pause_telegram(c, exc.seconds)
                raise RuntimeError(f"Поиск сообществ: Telegram просит паузу {exc.seconds} с") from None
            except Exception as exc:
                record(c, src, evidence, src, "unknown", None, "unavailable", type(exc).__name__)
            time.sleep(2)
    finally:
        client.disconnect()
    write_sources(c)
    say(f"Поиск сообществ: успешных запросов {successful}/{len(QUERIES)}, кандидатов {len(candidates)}, подключено {added}")
    if notify_owner:
        notify(c)
    if not successful:
        raise RuntimeError("Интернет-поиск не вернул результатов ни по одному запросу")
    db.set_meta(c, "discovery:day", today)


if __name__ == "__main__":
    import sys
    c = db.connect()
    if db.active_run(c):
        print("Прогон уже идёт; поиск отложен до следующего запуска")
        sys.exit(0)
    run_id = db.start_run(c)
    if run_id is None:
        print("Прогон уже идёт; поиск отложен")
        sys.exit(0)
    error = None
    try:
        run(c, force="--force" in sys.argv)
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        db.finish_run(c, run_id, "Поиск новых сообществ", error)
        c.close()
