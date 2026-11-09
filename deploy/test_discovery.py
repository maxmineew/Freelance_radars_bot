import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from datetime import datetime, timezone
from telethon.tl.types import Channel
from leadbot import discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        self.c.execute("CREATE TABLE chats(source TEXT, peer_id INTEGER)")
        discovery.init(self.c)

    def tearDown(self):
        self.c.close()

    def test_normalization(self):
        self.assertEqual(discovery.normalize("https://t.me/s/DentTalk/123?x=1"), "@denttalk")
        self.assertEqual(discovery.normalize("https://telegram.me/joinchat/Ab_c-1"), "https://t.me/+Ab_c-1")
        for url in ("https://evil.example/denttalk", "https://t.me/CCG_chatbot", "https://t.me/share/url"):
            self.assertIsNone(discovery.normalize(url))

    def test_daily_cooldown_does_not_search(self):
        discovery.db.set_meta(self.c, "discovery:day", datetime.now(discovery.MSK).date().isoformat())
        with patch.object(discovery, "web_candidates") as search:
            discovery.run(self.c)
            search.assert_not_called()

    def test_atomic_files_only_include_active_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            chats, channels = Path(tmp) / "chats.txt", Path(tmp) / "sources.txt"
            discovery.record(self.c, "@clinic_chat", "web", "Клиники", "chat", -1, "active")
            discovery.record(self.c, "https://t.me/+Private", "web", "Закрытый", "chat", None, "join_required")
            discovery.record(self.c, "@orders", "web", "Заказы", "channel", -2, "active")
            with patch.object(discovery, "DISCOVERED_CHATS_FILE", chats), patch.object(discovery, "DISCOVERED_SOURCES_FILE", channels):
                discovery.write_sources(self.c)
            self.assertIn("@clinic_chat", chats.read_text(encoding="utf-8"))
            self.assertNotIn("Private", chats.read_text(encoding="utf-8"))
            self.assertIn("@orders", channels.read_text(encoding="utf-8"))

    def test_validate_active_and_off_topic(self):
        channel = Channel(id=123, title="Владельцы клиник", photo=None, date=datetime.now(timezone.utc), megagroup=True)
        client = Mock()
        client.return_value.full_chat.about = "Управление стоматологией"
        client.return_value.full_chat.participants_count = 1000
        client.iter_messages.return_value = [Mock(message="Нужен специалист: некому обзванивать пациентов", date=datetime.now(timezone.utc)),
                                             Mock(message="Посоветуйте исполнителя на обзвон базы", date=datetime.now(timezone.utc))]
        with patch.object(discovery.watcher, "resolve", return_value=(channel, None)), patch.object(discovery.watcher, "load_chats", return_value=[]), patch.object(discovery.collector, "load_sources", return_value=[]):
            self.assertTrue(discovery.validate(client, self.c, "@clinic", "web"))
            self.assertFalse(discovery.validate(client, self.c, "@clinic_alias", "web"))
        self.assertEqual(self.c.execute("SELECT status FROM discovered_sources WHERE source='@clinic_alias'").fetchone()[0], "duplicate")

    def test_unjoined_invite_is_pending(self):
        with patch.object(discovery.watcher, "resolve", return_value=(None, None)):
            self.assertFalse(discovery.validate(Mock(), self.c, "https://t.me/+Private", "web"))
        self.assertEqual(self.c.execute("SELECT status FROM discovered_sources").fetchone()[0], "join_required")


if __name__ == "__main__":
    unittest.main()
