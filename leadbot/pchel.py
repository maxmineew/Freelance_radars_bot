"""Проекты Pchel.net из открытой RSS-ленты (pchel.net/rss/). Логин не нужен."""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import db
from .config import MAX_CARD_AGE_HOURS, PCHEL_CHANNEL
from .flru import HEADERS, clean
from .kwork import store

URL = "https://pchel.net/rss/"
EXCHANGE = "Pchel.net"
PROJECT_ID = re.compile(r"-(\d+)/?$")


def to_post(item, now=None):
    now = now or datetime.now(timezone.utc)
    link = (item.findtext("link") or "").strip()
    m = PROJECT_ID.search(link)
    if not m:
        return None
    try:
        posted = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    if posted < now - timedelta(hours=MAX_CARD_AGE_HOURS):
        return None
    text = clean(item.findtext("title")) + "\n" + clean(item.findtext("description"))
    category = clean(item.findtext("category"))
    if category:
        text += f"\nРаздел: {category}"
    return {"channel": PCHEL_CHANNEL, "msg_id": int(m.group(1)), "text": text, "posted_at": posted.isoformat(),
            "order_url": link, "url": link, "exchange": EXCHANGE}


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
    new = store(c, fetch(http))
    db.set_meta(c, "pchel_ok_at", db.now())
    c.commit()
    return new
