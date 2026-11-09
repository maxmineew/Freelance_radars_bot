import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from leadbot import db, hot, kwork, pending, telegram_out
from leadbot.config import KWORK_CHANNEL

MSK = timezone(timedelta(hours=3))


def msk(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")


def want(wid, name="Нужен чат-бот для записи клиентов", created=None, active=None, **extra):
    return dict({"id": wid, "name": name, "description": "Бот в Telegram &bull; интеграция с CRM",
                 "date_create": created or msk(minutes=10), "date_active": active or msk(minutes=10),
                 "priceLimit": "15000.00", "status": "active", "isWantActive": True}, **extra)


def transport(pages):
    def handler(request):
        page = int(dict(x.split("=") for x in request.content.decode().split("&"))["page"])
        return httpx.Response(200, json={"success": True, "data": {
            "wants": pages[page - 1], "pagination": {"current_page": page, "last_page": len(pages)}}})
    return httpx.Client(transport=httpx.MockTransport(handler))


class KworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "db.sqlite")
        self.patch.start()
        self.c = db.connect()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_fresh_order_is_queued_with_link_and_clean_text(self):
        new = kwork.collect(self.c, transport([[want(101)]]))
        self.assertEqual(len(new), 1)
        row = self.c.execute("SELECT * FROM posts").fetchone()
        self.assertEqual((row["channel"], row["exchange"], row["order_url"]),
                         (KWORK_CHANNEL, "Kwork", "https://kwork.ru/projects/101"))
        queued = self.c.execute("SELECT text FROM llm_queue").fetchone()["text"]
        self.assertIn("• интеграция", queued)
        self.assertIn("до 15000", queued)

    def test_stale_reraised_and_inactive_orders_are_skipped(self):
        wants = [want(1, active=msk(hours=4)),                   # старше окна свежести
                 want(2, created=msk(days=30)),                   # старый, поднятый заново
                 want(3, status="closed")]
        self.assertEqual(kwork.collect(self.c, transport([wants])), [])

    def test_repeat_poll_does_not_duplicate(self):
        client = transport([[want(5)]])
        kwork.collect(self.c, client)
        self.assertEqual(kwork.collect(self.c, client), [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0], 1)

    def test_paging_stops_at_old_page(self):
        pages = [[want(1)], [want(2, active=msk(hours=5))], [want(3)]]
        seen = []
        client = transport(pages)
        original = kwork.fetch_page
        with patch.object(kwork, "fetch_page", side_effect=lambda h, p: seen.append(p) or original(h, p)):
            kwork.fetch(client)
        self.assertEqual(seen, [1, 2])

    def test_kwork_rows_are_not_recovered_through_telegram(self):
        self.c.execute("INSERT INTO posts(channel,msg_id,url,posted_at,status,created_at) VALUES(?,?,?,?,?,?)",
                       (KWORK_CHANNEL, 9, "https://kwork.ru/projects/9", db.now(), "new", db.now()))
        self.c.commit()
        with patch.object(pending.httpx.Client, "get", side_effect=AssertionError("no t.me")) as get:
            pending.recover(self.c, say=lambda *_: None)
        get.assert_not_called()


class HotTests(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(hot.flags("Нужен бот, звоните +7 (999) 123-45-67"), "phone")
        self.assertEqual(hot.flags("Нужен бот, телефон [PHONE]"), "phone")
        self.assertEqual(hot.flags("Заказ с Яндекс Услуг: нужен сайт"), "yandex")
        self.assertEqual(hot.flags("Нужен сайт", "Яндекс Услуги"), "yandex")
        self.assertEqual(hot.flags("https://uslugi.yandex.ru/order/1 тел 89991234567"), "phone,yandex")
        self.assertIsNone(hot.flags("Нужен разработчик бота"))

    def test_phone_and_yandex_go_before_urgent_direct(self):
        urgent = {"text": "Срочно сегодня нужен разработчик чат-бота", "posted_at": db.now()}
        phone = {"text": "В клинике некому обзванивать, [PHONE]", "posted_at": db.ago(hours=2)}
        yandex = {"text": "Нужен сайт", "posted_at": db.ago(hours=2), "exchange": "Яндекс Услуги"}
        self.assertLess(pending.priority(phone), pending.priority(urgent))
        self.assertLess(pending.priority(yandex), pending.priority(urgent))

    def test_card_shows_priority_and_hides_tme_link_for_kwork(self):
        row = {"posted_at": db.now(), "hot": "phone,yandex", "order_url": "https://kwork.ru/projects/1",
               "exchange": "Kwork", "url": "https://kwork.ru/projects/1", "channel": KWORK_CHANNEL,
               "msg_id": 1, "feedback": None, "reason": None, "stage": None}
        with patch.object(telegram_out, "send", return_value={"message_id": 7}) as send:
            telegram_out.send_card(row, {"score": 9})
        text = send.call_args[0][0]
        self.assertTrue(text.startswith("🔥 <b>Приоритет:</b> 📞"))
        self.assertIn("Яндекс Услуг", text)
        self.assertNotIn("Открыть пост", text)


if __name__ == "__main__":
    unittest.main()
