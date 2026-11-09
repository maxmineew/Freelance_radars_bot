"""Заказы Kwork напрямую с биржи проектов (kwork.ru/projects, JSON-ответ сайта).

Логин не нужен: это тот же запрос, которым страница биржи подгружает список.
Берём только свежие активные заказы; давно созданные и поднятые заново — пропускаем.
"""
import html
import re
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .collector import prefilter
from .config import KWORK_CHANNEL, KWORK_MAX_CREATED_DAYS, KWORK_PAGES, MAX_CARD_AGE_HOURS

URL = "https://kwork.ru/projects"
EXCHANGE = "Kwork"
MSK = timezone(timedelta(hours=3))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://kwork.ru/projects",
}
TAGS = re.compile(r"<[^>]+>")


def parse_time(value):
    """'2026-11-04 17:05:12' по Москве -> datetime UTC."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc)


def to_post(want, now=None):
    """Заказ Kwork -> запись в формате остальных источников; None, если заказ не подходит."""
    now = now or datetime.now(timezone.utc)
    if want.get("status", "active") != "active" or want.get("isWantActive") is False:
        return None
    try:
        active = parse_time(want.get("date_active") or want["date_create"])
        created = parse_time(want["date_create"])
    except (KeyError, TypeError, ValueError):
        return None
    if active < now - timedelta(hours=MAX_CARD_AGE_HOURS):
        return None
    if created < now - timedelta(days=KWORK_MAX_CREATED_DAYS):
        return None  # старый заказ, поднятый заново: обычно спам/рассылки
    name = html.unescape(TAGS.sub(" ", want.get("name") or "")).strip()
    description = html.unescape(TAGS.sub(" ", want.get("description") or "")).strip()
    price = want.get("priceLimit")
    text = f"{name}\n{description}" + (f"\nБюджет: до {int(float(price))} ₽" if price else "")
    url = f"https://kwork.ru/projects/{int(want['id'])}"
    return {"channel": KWORK_CHANNEL, "msg_id": int(want["id"]), "text": text,
            "posted_at": active.isoformat(), "order_url": url, "url": url, "exchange": EXCHANGE}


def fetch_page(http, page):
    r = http.post(URL, data={"c": "all", "page": page})
    r.raise_for_status()
    data = r.json().get("data") or {}
    return data.get("wants") or [], (data.get("pagination") or {}).get("last_page") or page


def fetch(http=None, now=None):
    """Свежие заказы со страниц биржи; листаем, пока страница не стала старше окна свежести."""
    now = now or datetime.now(timezone.utc)
    own = http is None
    http = http or httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)
    posts = []
    try:
        for page in range(1, KWORK_PAGES + 1):
            wants, last = fetch_page(http, page)
            posts += [p for p in (to_post(w, now) for w in wants) if p]
            oldest = min((parse_time(w["date_active"]) for w in wants if w.get("date_active")), default=None)
            if not wants or page >= last or (oldest and oldest < now - timedelta(hours=MAX_CARD_AGE_HOURS)):
                break
    finally:
        if own:
            http.close()
    return posts


def store(c, posts):
    new = []
    for p in posts:
        status = "new" if prefilter(p["text"]) else "skipped"
        cur = c.execute(
            "INSERT OR IGNORE INTO posts(channel,msg_id,url,posted_at,status,created_at,order_url,exchange,"
            "text_hash,sketch) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p["channel"], p["msg_id"], p["url"], p["posted_at"], status, db.now(), p["order_url"],
             p["exchange"], db.text_hash(p["text"]), db.sketch(p["text"])))
        if cur.rowcount and status == "new":
            db.enqueue(c, p)
            new.append(p)
    c.commit()
    return new


def collect(c, http=None):
    """Забирает свежие заказы Kwork и ставит подходящие в очередь классификации."""
    new = store(c, fetch(http))
    db.set_meta(c, "kwork_ok_at", db.now())
    c.commit()
    return new
