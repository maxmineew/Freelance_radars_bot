import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from leadbot import db, pending, classifier, rules, monitor, pchel, config
from deploy.test_llm_queue import LEAD


class RulesTests(unittest.TestCase):
    def test_noise(self):
        self.assertEqual(rules.noise_reason("Ищу работу, мое резюме в личке"), "seeker")
        self.assertEqual(rules.noise_reason("Вакансия: оклад 100к, график 5/2"), "vacancy")
        self.assertEqual(rules.noise_reason("Подписывайтесь на канал, скидка до 50%"), "advert")

    def test_lead_and_exchange_pass(self):
        self.assertIsNone(rules.noise_reason("Ищу разработчика бота, вакансия не нужна, оплата по договору"))
        self.assertIsNone(rules.noise_reason("Ищу работу над ошибками", exchange="Kwork"))


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "db.sqlite")
        self.patch.start()
        self.c = db.connect()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, mid, text, replies=0, author=None):
        posted = datetime.now(timezone.utc).isoformat()
        self.c.execute("INSERT INTO posts(channel,msg_id,url,posted_at,status,created_at,text_hash,sketch,author,replies_n)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("clinic", mid, "u", posted, "new", db.now(), db.text_hash(text), db.sketch(text), author, replies))
        db.enqueue(self.c, {"channel": "clinic", "msg_id": mid, "text": text, "posted_at": posted})
        self.c.commit()

    def status(self, mid):
        return self.c.execute("SELECT status FROM posts WHERE msg_id=?", (mid,)).fetchone()[0]


class ProcessTests(QueueBase):
    def test_replies_cool_and_noise_skip_llm(self):
        self.post(1, "Нужен бот для записи клиентов в стоматологию, кто сделает?", replies=config.REPLIES_COOL)
        self.post(2, "Ищу работу, мое резюме могу прислать")
        with patch.object(classifier, "classify") as classify:
            pending.process(self.c, lambda _: None)
        classify.assert_not_called()
        self.assertEqual(self.status(1), "old")
        self.assertEqual(self.status(2), "skipped")

    def test_batch_mode_scores_and_falls_back(self):
        for i in range(1, 5):
            self.post(i, f"Нужен разработчик бота для клиники номер {i}, обзвон пациентов {'абвгд'[i]}" * 2)
        calls = []

        def fake_batch(items, examples=""):
            calls.append(len(items))
            return [dict(LEAD) if "номер 2" not in it["text"] else None for it in items]

        with patch.object(config, "LLM_BACKEND", "claude_cli"), patch.object(pending, "LLM_BACKEND", "claude_cli"), \
                patch.object(pending, "CLAUDE_BATCH_SIZE", 2), \
                patch.object(classifier, "batch_model", return_value="m"), \
                patch.object(classifier, "classify_batch", side_effect=fake_batch), \
                patch.object(classifier, "classify", wraps=None) as single:
            single.__module__ = classifier.__name__
            single.return_value = dict(LEAD)
            pending.process(self.c, lambda _: None)
        self.assertEqual(sorted(calls), [2, 2])
        self.assertEqual(single.call_count, 1)  # негодная карточка переоценена по одной
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM posts WHERE status='scored'").fetchone()[0], 4)

    def test_batch_cli_unavailable_sets_cooldown(self):
        self.post(1, "Нужен разработчик бота для клиники, обзвон пациентов")
        err = classifier.CliUnavailable("not logged in", 3600)
        with patch.object(pending, "LLM_BACKEND", "claude_cli"), \
                patch.object(classifier, "batch_model", return_value="m"), \
                patch.object(classifier, "classify_batch", side_effect=err), \
                patch.object(classifier, "classify") as single:
            single.__module__ = classifier.__name__
            pending.process(self.c, lambda _: None)
        self.assertGreater(classifier.remaining_cooldown("claude_cli:m", self.c), 0)
        self.assertEqual(self.status(1), "new")
        classifier._cooldowns.clear()


class MonitorTests(QueueBase):
    def test_mute_bad_source(self):
        muted = Path(self.tmp.name) / "muted.txt"
        with patch.object(config, "MUTED_FILE", muted):
            self.assertEqual(config.muted(), set())
            muted.write_text("# comment\n@Bad_Chat\n", encoding="utf-8")
            self.assertEqual(config.muted(), {"bad_chat"})


class PchelTests(unittest.TestCase):
    def test_module_has_parser(self):
        self.assertTrue(callable(pchel.parse) and callable(pchel.collect))


if __name__ == "__main__":
    unittest.main()
