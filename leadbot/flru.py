"""Заказы FL.ru из открытой RSS-ленты (www.fl.ru/rss/all.xml).

Логин не нужен. Радар опрашивает ленту раз в FLRU_POLL_SECONDS, новые заказы сразу
уходят в очередь оценки, и карточка приходит через несколько секунд после публикации.
"""
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import db
from .config import FLRU_CHANNEL, MAX_CARD_AGE_HOURS
from .kwork import store

URL = "https://www.fl.ru/rss/all.xml"
EXCHANGE = "FL.ru"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36",
           "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}
TAGS = re.compile(r"<[^>]+>")
PROJECT_ID = re.compile(r"/projects/(\d+)")
BUDGET = re.compile(r"\s*\(Бюджет:\s*([^)]*)\)\s*$")
FOR_ALL = re.compile(r"\s*\(для всех\)\s*$")


def clean(value):
    return re.sub(r"[ \t\xa0]+", " ", html.unescape(TAGS.sub(" ", value or ""))).strip()


def to_post(item, now=None):
    """<item> ленты -> запись в формате остальных источников; None, если заказ не подходит."""
    now = now or datetime.now(timezone.utc)
    link = (item.findtext("link") or item.findtext("guid") or "").strip()
    m = PROJECT_ID.search(link)
    if not m:
        return None  # не заказ (вакансия, конкурс без номера и т. п.)
    try:
        posted = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    if posted < now - timedelta(hours=MAX_CARD_AGE_HOURS):
        return None
    title = clean(item.findtext("title"))
    budget = BUDGET.search(title)
    title = FOR_ALL.sub("", BUDGET.sub("", title)).strip()
    text = title + "\n" + clean(item.findtext("description"))
    category = clean(item.findtext("category"))
    if category:
        text += f"\nРаздел: {category}"
    if budget:
        text += f"\nБюджет: {budget.group(1).strip()}"
    url = link.split("?")[0]
    return {"channel": FLRU_CHANNEL, "msg_id": int(m.group(1)), "text": text,
            "posted_at": posted.isoformat(), "order_url": url, "url": url, "exchange": EXCHANGE}


def parse(xml_text, now=None):
    root = ET.fromstring(xml_text)
    return [p for p in (to_post(i, now) for i in root.iter("item")) if p]


def fetch(http=None, now=None):
    own = http is None
    http = http or httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)
    try:
        r = http.get(URL)
        r.raise_for_status()
        return parse(r.content, now)
    finally:
        if own:
            http.close()


def collect(c, http=None):
    """Забирает свежие заказы FL.ru и ставит подходящие в очередь классификации."""
    new = store(c, fetch(http))
    db.set_meta(c, "flru_ok_at", db.now())
    c.commit()
    return new
