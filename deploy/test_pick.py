"""Кнопка «🎯 Прогон по источнику»: список, выбор и прогон только по одному каналу или чату."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leadbot import db, targets, feedback, telegram_out
import run


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.db")
        self.patch.start()
        self.c = db.connect()
        self.c.execute("INSERT INTO chats VALUES('@clinic',-100123,'clinic','Клиники',1)")
        self.c.commit()
        self.lists = [patch.object(targets, "load_chats", return_value=["@clinic"]),
                      patch.object(targets, "load_sources", return_value=[f"ch{i}" for i in range(12)])]
        for p in self.lists:
            p.start()

    def tearDown(self):
        for p in self.lists:
            p.stop()
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_menu_has_pick_instead_of_status(self):
        self.assertIn("pick", telegram_out.MENU.values())
        self.assertNotIn("status", telegram_out.MENU.values())
        self.assertIn("status", telegram_out.COMMANDS)  # /status остаётся командой

    def test_picker_pages_and_callback_size(self):
        text, markup = feedback.picker(self.c, 0)
        rows = markup["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "💬 Клиники")
        self.assertEqual(rows[-1][0]["callback_data"], "pick|1")
        for row in rows:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)
        _, last = feedback.picker(self.c, 99)  # несуществующая страница -> последняя
        self.assertEqual(last["inline_keyboard"][-1][0]["callback_data"], "pick|0")

    def test_choice_launches_single_source_run(self):
        k = targets.key("channel", "ch3")
        cb = {"id": "1", "from": {"id": int(feedback.OWNER_CHAT_ID or 0)}, "data": f"src|{k}",
              "message": {"chat": {"id": feedback.OWNER_CHAT_ID}, "message_id": 5}}
        with patch.object(feedback, "OWNER_CHAT_ID", "1"), patch.object(feedback.tg, "try_api"), \
                patch.object(feedback.tg, "send"), patch.object(feedback, "start_run", return_value="ok") as start:
            cb["from"]["id"] = 1
            feedback.on_callback(self.c, cb)
        start.assert_called_once_with(self.c, ("channel", "ch3"))

    def test_unknown_choice_is_reported(self):
        self.assertIsNone(targets.find(self.c, "deadbeef00"))

    def test_parse_source_argument(self):
        self.assertEqual(targets.parse("chat:@clinic"), ("chat", "@clinic"))
        self.assertEqual(targets.parse("channel:https://t.me/x"), ("channel", "https://t.me/x"))
        with self.assertRaises(ValueError):
            targets.parse("bad")

    def test_single_chat_run_touches_only_that_chat(self):
        calls = {}
        with patch.object(run.collector, "collect", side_effect=AssertionError("каналы не нужны")), \
                patch.object(run.kwork, "collect", side_effect=AssertionError("Kwork не нужен")), \
                patch.object(run.discovery, "run", side_effect=AssertionError("поиск не нужен")), \
                patch.object(run.watcher, "collect", side_effect=lambda c, sources=None: calls.setdefault("w", sources) and []), \
                patch.object(run.pending, "process", side_effect=lambda c, say, limit, channels=None: calls.setdefault("p", channels)):
            run.run(self.c, dry=False, say=lambda s: None, target=("chat", "@clinic"))
        self.assertEqual(calls["w"], ["@clinic"])
        self.assertEqual(calls["p"], {"clinic"})

    def test_single_source_run_shows_ready_cards(self):
        card = '{"score": 8, "niche": "n", "need": "x"}'
        rows = [("clinic", 1, "sent", 8, db.ago(hours=2)),     # уже прислал радар
                ("clinic", 2, "old", 7, db.ago(hours=10)),     # оценён, но устарел для автоотправки
                ("clinic", 3, "scored", 3, db.ago(hours=1)),   # низкая оценка
                ("clinic", 4, "sent", 9, db.ago(hours=30)),    # слишком давно
                ("other", 5, "sent", 9, db.ago(hours=1))]      # другой источник
        for ch, mid, status, score, posted in rows:
            self.c.execute("INSERT INTO posts(channel,msg_id,url,posted_at,status,created_at,score,card,sent_at) "
                           "VALUES(?,?,'u',?,?,?,?,?,?)", (ch, mid, posted, status, posted, score, card,
                                                          posted if status == "sent" else None))
        self.c.commit()
        sent = []
        with patch.object(run.telegram_out, "send_card", side_effect=lambda r, c: sent.append(r["msg_id"]) or 100), \
                patch.object(run.time, "sleep"):
            run.show_source_cards(self.c, ("chat", "@clinic"), db.now(), False, lambda s: None)
        self.assertEqual(sent, [1, 2])
        self.assertEqual(self.c.execute("SELECT status FROM posts WHERE msg_id=2").fetchone()[0], "sent")

    def test_source_recap(self):
        self.c.execute("INSERT INTO posts(channel,msg_id,url,posted_at,status,created_at,score) "
                       "VALUES('clinic',1,'u',?,'scored',?,3)", (db.now(), db.now()))
        self.c.commit()
        out = "\n".join(run.source_recap(self.c, ("chat", "@clinic"), db.now()))
        self.assertIn("сообщений: 1", out)
        self.assertIn("оценка ниже", out)


if __name__ == "__main__":
    unittest.main()
