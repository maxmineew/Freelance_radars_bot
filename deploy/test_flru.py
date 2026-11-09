"""FL.ru из RSS: разбор ленты, свежесть, запись в очередь, кнопка выбора источника."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from leadbot import db, flru, targets, telegram_out
from leadbot.config import FLRU_CHANNEL

NOW = datetime(2026, 11, 5, 12, 31, tzinfo=timezone.utc)
FEED = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>FL</title>
<item><title><![CDATA[Сделать сайт для стоматологии (Бюджет: 70 000  &#8381;)]]></title>
<link>https://www.fl.ru/projects/5528134/sdelat-sayt.html</link>
<description><![CDATA[Ищем подрядчика: нужен <b>сайт</b> клиники с онлайн-записью.]]></description>
<guid>https://www.fl.ru/projects/5528134/sdelat-sayt.html</guid>
<category><![CDATA[Сайты / Сайт под ключ]]></category>
<pubDate>Thu, 05 Nov 2026 12:25:12 GMT</pubDate></item>
<item><title><![CDATA[Настроить рекламу (для всех)]]></title>
<link>https://www.fl.ru/projects/5528100/reklama.html</link>
<description><![CDATA[Нужен таргетолог для салона красоты]]></description>
<pubDate>Thu, 05 Nov 2026 07:00:00 GMT</pubDate></item>
<item><title><![CDATA[Без номера]]></title><link>https://www.fl.ru/users/x/</link>
<pubDate>Thu, 05 Nov 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""


class FlruTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.db")
        self.patch.start()
        self.c = db.connect()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_parse_keeps_only_fresh_orders(self):
        posts = flru.parse(FEED.encode(), NOW)
        self.assertEqual([p["msg_id"] for p in posts], [5528134])  # второй старше 3 ч, третий не заказ
        p = posts[0]
        self.assertEqual(p["channel"], FLRU_CHANNEL)
        self.assertEqual(p["exchange"], "FL.ru")
        self.assertEqual(p["order_url"], "https://www.fl.ru/projects/5528134/sdelat-sayt.html")
        self.assertTrue(p["text"].startswith("Сделать сайт для стоматологии\nИщем подрядчика: нужен сайт клиники"))
        self.assertIn("Раздел: Сайты / Сайт под ключ", p["text"])
        self.assertIn("Бюджет: 70 000 ₽", p["text"])

    def test_for_all_suffix_removed(self):
        later = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
        posts = {p["msg_id"]: p for p in flru.parse(FEED.encode(), later)}
        self.assertTrue(posts[5528100]["text"].startswith("Настроить рекламу\n"))

    def test_collect_enqueues_once(self):
        class Http:
            def get(self, url):
                class R:
                    content = FEED.encode()

                    def raise_for_status(self):
                        pass
                return R()
        with patch.object(flru, "datetime") as dt:
            dt.now.return_value = NOW
            first = flru.store(self.c, flru.parse(Http().get("").content, NOW))
            again = flru.store(self.c, flru.parse(FEED.encode(), NOW))
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM llm_queue WHERE channel=?", (FLRU_CHANNEL,)).fetchone()[0], 1)
        row = self.c.execute("SELECT exchange, status FROM posts WHERE channel=?", (FLRU_CHANNEL,)).fetchone()
        self.assertEqual(tuple(row), ("FL.ru", "new"))

    def test_picker_and_card_link(self):
        with patch.object(targets, "load_chats", return_value=[]), patch.object(targets, "load_sources", return_value=[]):
            self.assertIn(("flru", FLRU_CHANNEL, "FL.ru"), targets.all_targets(self.c))
        self.assertEqual(targets.parse("flru:fl.ru"), ("flru", "fl.ru"))
        row = {"channel": FLRU_CHANNEL, "msg_id": 1, "url": "https://www.fl.ru/projects/1/x.html",
               "order_url": "https://www.fl.ru/projects/1/x.html", "exchange": "FL.ru", "posted_at": NOW.isoformat(),
               "hot": None, "feedback": None, "reason": None, "stage": None}
        card = {"score": 8, "service": "site", "niche": "клиника", "need": "сайт"}
        sent = {}
        with patch.object(telegram_out, "send", side_effect=lambda text, **kw: sent.setdefault("t", text) and {"message_id": 1}):
            telegram_out.send_card(row, card)
        self.assertIn("Заказ на FL.ru", sent["t"])
        self.assertNotIn("Открыть пост", sent["t"])

    def test_exchange_orders_go_first_in_queue(self):
        from leadbot import pending
        chat = {"channel": "clinic", "text": "Ищем подрядчика, срочно нужен сайт, звоните +7 900 123-45-67", "posted_at": NOW.isoformat(), "exchange": None}
        order = {"channel": FLRU_CHANNEL, "text": "Сайт для клиники", "posted_at": "2026-11-05T10:00:00+00:00", "exchange": "FL.ru"}

        class Row(dict):
            def keys(self):
                return super().keys()
        rows = sorted([Row(chat), Row(order)], key=pending.priority)
        self.assertEqual(rows[0]["channel"], FLRU_CHANNEL)


if __name__ == "__main__":
    unittest.main()
