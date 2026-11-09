"""Источники для прогона по одному каналу или чату: список для кнопок и разбор выбора."""
import hashlib
from .collector import load_sources
from .watcher import load_chats
from .config import KWORK_ENABLED, KWORK_CHANNEL, FLRU_ENABLED, FLRU_CHANNEL, PCHEL_ENABLED, PCHEL_CHANNEL

KINDS = {"chat": "💬", "channel": "📢", "kwork": "🧾", "flru": "🧾", "pchel": "🧾"}


def key(kind, source):
    """Короткий ключ для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1(f"{kind}:{source}".encode()).hexdigest()[:10]


def all_targets(c):
    """[(kind, source, label)]: сначала чаты, потом каналы, в конце биржи."""
    titles = {r["source"]: r["title"] for r in c.execute("SELECT source, title FROM chats")}
    out = [("chat", src, titles.get(src) or src) for src in load_chats()]
    out += [("channel", ch, "@" + ch) for ch in load_sources()]
    if KWORK_ENABLED:
        out.append(("kwork", KWORK_CHANNEL, "Kwork"))
    if FLRU_ENABLED:
        out.append(("flru", FLRU_CHANNEL, "FL.ru"))
    if PCHEL_ENABLED:
        out.append(("pchel", PCHEL_CHANNEL, "Pchel.net"))
    return out


def find(c, k):
    return next(((kind, src, label) for kind, src, label in all_targets(c) if key(kind, src) == k), None)


def parse(arg):
    """Аргумент --source вида kind:source."""
    kind, _, source = (arg or "").partition(":")
    if kind not in KINDS or not source:
        raise ValueError(f"Неверный источник: {arg}")
    return kind, source
