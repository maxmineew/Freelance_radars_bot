"""Сбор постов из публичных каналов через t.me/s/<channel> (без аккаунта, только чтение)."""
import time
import re
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
import httpx
from bs4 import BeautifulSoup
from .config import muted, SOURCES_FILE, DISCOVERED_SOURCES_FILE, PAGES_PER_CHANNEL, MAX_CARD_AGE_HOURS, KEYWORDS
from . import db

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; lead-research/0.1)"}


def load_sources():
    out = []
    off = muted()
    for path in (SOURCES_FILE, DISCOVERED_SOURCES_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name = line.lstrip("@").lower()
                if name not in out and name not in off:
                    out.append(name)
    return out


# Биржи фриланса: если пост ведёт на заказ там, ссылку кладём в карточку
EXCHANGES = {
    "kwork.ru": "Kwork", "fl.ru": "FL.ru", "freelance.ru": "Freelance.ru",
    "freelance.habr.com": "Хабр Фриланс", "youdo.com": "YouDo", "workzilla.com": "Workzilla",
    "weblancer.net": "Weblancer", "pchel.net": "Пчёл.нет", "profi.ru": "Профи.ру",
    "uslugi.yandex.ru": "Яндекс Услуги",
}
_SKIP_PATHS = ("new_project", "/register", "/signup", "/login")


def find_order_link(widget):
    """Первая ссылка на заказ на бирже (из текста или кнопок поста). Реферальный хвост отрезается."""
    for a in widget.select(".tgme_widget_message_text a, a.tgme_widget_message_inline_button"):
        url = urlsplit(a.get("href") or "")
        host = url.netloc.lower().removeprefix("www.")
        name = EXCHANGES.get(host)
        if not name or url.path in ("", "/") or any(s in url.path for s in _SKIP_PATHS):
            continue
        return urlunsplit((url.scheme, url.netloc, url.path, "", "")), name
    return None, None


def parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    posts = []
    for w in soup.select("div.tgme_widget_message"):
        post = w.get("data-post")  # channel/123
        body = w.select_one(".tgme_widget_message_text")
        t = w.select_one("time")
        if not (post and body and t and t.get("datetime")):
            continue
        ch, mid = post.split("/")
        order_url, exchange = find_order_link(w)
        posts.append({
            "order_url": order_url, "exchange": exchange,
            "channel": ch, "msg_id": int(mid),
            "text": body.get_text("\n", strip=True),
            "posted_at": t["datetime"],
        })
    return posts


def prefilter(text):
    low = f" {text.lower()} "
    relevant = any(k in low for k in KEYWORDS)
    explicit = re.search(r"ищу|ищем|нужен|нужна|нужны|требуется|посоветуйте|порекомендуйте", low)
    return relevant and (len(text) > 40 or (len(text.strip()) >= 12 and explicit is not None))


failed = []  # каналы, страница которых в этом прогоне не открылась


def collect(c, sources=None):
    """sources — только эти каналы (прогон по одному источнику); по умолчанию все."""
    limit = datetime.now(timezone.utc) - timedelta(hours=MAX_CARD_AGE_HOURS)
    new = []
    failed.clear()
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as http:
        for ch in (load_sources() if sources is None else sources):
            before = None
            for _ in range(PAGES_PER_CHANNEL):
                url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
                try:
                    r = http.get(url)
                except httpx.HTTPError:
                    failed.append(ch)
                    break
                if r.status_code != 200:
                    failed.append(ch)
                posts = parse_page(r.text) if r.status_code == 200 else []
                if not posts:
                    break
                for p in posts:
                    if datetime.fromisoformat(p["posted_at"]) < limit:
                        continue
                    status = "new" if prefilter(p["text"]) else "skipped"
                    cur = c.execute(
                        "INSERT OR IGNORE INTO posts(channel,msg_id,url,posted_at,status,created_at,order_url,exchange,"
                        "text_hash,sketch) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (p["channel"], p["msg_id"], f"https://t.me/{p['channel']}/{p['msg_id']}",
                         p["posted_at"], status, db.now(), p["order_url"], p["exchange"],
                         db.text_hash(p["text"]), db.sketch(p["text"])),
                    )
                    if cur.rowcount and status == "new":
                        db.enqueue(c, p)
                        new.append(p)
                before = min(p["msg_id"] for p in posts)
                c.commit()  # до паузы: иначе база занята весь сбор и слушатель кнопок не может записать отметку
                time.sleep(2)  # вежливая пауза
            time.sleep(3)
    c.commit()
    return new
