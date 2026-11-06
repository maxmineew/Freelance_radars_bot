import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from telethon.errors import FloodWaitError
from telethon.tl.types import ChatInviteAlready

from leadbot import db, watcher, radar, classifier, pending, telegram_out
from leadbot.session_lock import SessionLock, LockedClient


def message(mid, text="Нужен разработчик голосового агента для обзвона клиентов, срочно сегодня"):
    return SimpleNamespace(id=mid, message=text, date=datetime.now(timezone.utc), out=False, entities=[])


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.db")
        self.patch.start()
        self.c = db.connect()
        self.c.execute("INSERT INTO chats VALUES('@clinic',-100123,'clinic','Клиники',1)")
        self.c.commit()
        self.chat = self.c.execute("SELECT * FROM chats").fetchone()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def collect(self, messages, interrupt=False):
        client = Mock()
        client.is_user_authorized.return_value = True
        def history(_ent, **kw):
            self.assertTrue(kw['reverse'])
            return iter([m for m in messages if m.id > kw['min_id']][:kw['limit']])
        client.iter_messages.side_effect = history
        with patch.object(watcher, "TG_API_ID", "1"), patch.object(watcher, "TG_API_HASH", "test"), \
             patch.object(watcher, "resolve", side_effect=lambda *_, **_kw: (Mock(), self.c.execute('SELECT * FROM chats').fetchone())):
            result = watcher.collect(self.c, client=client, sources=['@clinic'], pause_seconds=0,
                should_stop=lambda: interrupt and self.c.execute('SELECT last_id FROM chats').fetchone()[0] >= 51)
        return result, client

    def test_more_than_100_messages_are_drained_without_gaps(self):
        new, client = self.collect([message(i) for i in range(2, 252)])
        self.assertEqual(len(new), 250)
        self.assertEqual(client.iter_messages.call_count, 3)
        self.assertEqual(self.c.execute('SELECT last_id FROM chats').fetchone()[0], 251)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM llm_queue').fetchone()[0], 250)
        client.disconnect.assert_not_called()

    def test_interrupt_and_restart_continue_from_last_committed_message(self):
        messages = [message(i) for i in range(2, 252)]
        self.collect(messages, interrupt=True)
        self.assertEqual(self.c.execute('SELECT last_id FROM chats').fetchone()[0], 51)
        self.collect(messages)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 250)

    def test_live_event_does_not_jump_history_cursor_and_does_not_duplicate(self):
        self.assertTrue(radar.ingest_event(self.c, -100123, message(151), {-100123}))
        self.assertEqual(self.c.execute('SELECT last_id FROM chats').fetchone()[0], 1)
        self.collect([message(i) for i in range(2, 152)])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 150)
        self.assertFalse(radar.ingest_event(self.c, -100123, message(151), {-100123}))

    def test_unknown_source_is_ignored_and_no_contacts_are_saved(self):
        self.assertFalse(radar.ingest_event(self.c, -100999, message(2), {-100123}))
        radar.ingest_event(self.c, -100123, message(2, 'Нужен голосовой агент для обзвона, телефон +7 999 123-45-67, @client123'), {-100123})
        text = self.c.execute('SELECT text FROM llm_queue').fetchone()[0]
        self.assertNotIn('999', text)
        self.assertNotIn('client123', text)

    def test_exchange_link_keeps_exchange_limits(self):
        radar.ingest_event(self.c, -100123, message(2, 'Нужен разработчик бота для звонков, заказ https://kwork.ru/projects/123?ref=x'), {-100123})
        row = self.c.execute('SELECT * FROM posts').fetchone()
        self.assertEqual(row['exchange'], 'Kwork')
        self.assertEqual(row['order_url'], 'https://kwork.ru/projects/123')
        self.assertEqual(self.c.execute('SELECT exchange FROM llm_queue').fetchone()[0], 'Kwork')

    def test_database_failure_does_not_advance_cursor(self):
        with patch.object(db, 'enqueue', side_effect=RuntimeError('storage error')):
            self.collect([message(2)])
        self.assertEqual(self.c.execute('SELECT last_id FROM chats').fetchone()[0], 1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_worker_does_not_require_telegram_session(self):
        run_id = db.start_run(self.c, session_required=False)
        self.assertIsNotNone(run_id)
        self.assertFalse(radar.telegram_busy(self.c))
        db.finish_run(self.c, run_id, '')
        db.start_run(self.c)
        self.assertTrue(radar.telegram_busy(self.c))

    def test_manual_run_request_holds_radar_and_expires(self):
        self.assertFalse(db.manual_run_requested(self.c))
        db.request_manual_run(self.c)
        self.assertTrue(db.manual_run_requested(self.c))
        db.clear_manual_run(self.c)
        self.assertFalse(db.manual_run_requested(self.c))
        # Забытый флаг упавшего прогона не держит радар вечно
        db.set_meta(self.c, db.MANUAL_RUN_KEY, db.ago(minutes=db.MANUAL_RUN_HOLD_MINUTES + 1))
        self.assertFalse(db.manual_run_requested(self.c))

    def test_button_run_waits_for_worker_instead_of_exiting(self):
        import run
        worker = db.start_run(self.c, session_required=False)
        calls = []

        def finish_worker(_seconds):
            calls.append(1)
            db.finish_run(self.c, worker, '')

        with patch.object(run.time, 'sleep', side_effect=finish_worker):
            run_id = run.claim_for_button(self.c)
        self.assertIsNotNone(run_id)
        self.assertEqual(len(calls), 1)
        self.assertTrue(db.manual_run_requested(self.c))
        self.assertTrue(radar.telegram_busy(self.c))

    def test_expired_candidates_are_not_work(self):
        db.enqueue(self.c, dict(channel='clinic',msg_id=1,text='Нужен разработчик бота',
                               posted_at=(datetime.now(timezone.utc)-timedelta(hours=49)).isoformat()))
        self.c.commit()
        self.assertFalse(radar.has_work(self.c))

    def test_short_explicit_request_is_not_lost_by_prefilter(self):
        self.assertTrue(watcher.prefilter('Ищу разработчика бота'))
        self.assertFalse(watcher.prefilter('Всем привет!'))

    def test_urgent_direct_request_precedes_general_voice_pain(self):
        urgent = {'text':'Срочно сегодня нужен разработчик чат-бота', 'posted_at':db.now()}
        pain = {'text':'В клинике некому обзванивать клиентов', 'posted_at':db.ago(hours=1)}
        self.assertLess(pending.priority(urgent), pending.priority(pain))

    def test_fresh_direct_request_precedes_historical_direct_request(self):
        fresh = {'text':'Нужен разработчик бота для интеграции', 'posted_at':db.now()}
        older = dict(fresh, posted_at=db.ago(hours=20))
        self.assertLess(pending.priority(fresh), pending.priority(older))

    def test_fresh_chatbot_request_precedes_older_voice_request(self):
        fresh = {'text': 'Нужен разработчик бота', 'posted_at': db.now()}
        older = {'text': 'Нужен разработчик голосового агента для обзвона', 'posted_at': db.ago(hours=2)}
        self.assertLess(pending.priority(fresh), pending.priority(older))

    def test_event_older_than_three_hours_is_ignored(self):
        stale = message(2)
        stale.date = datetime.now(timezone.utc) - timedelta(hours=3, minutes=1)
        self.assertFalse(radar.ingest_event(self.c, -100123, stale, {-100123}))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM llm_queue').fetchone()[0], 0)

    def test_all_provider_cooldowns_prevent_useless_worker_launch(self):
        watcher.store_message(self.c, self.chat, message(2))
        self.c.commit()
        with patch.object(classifier, 'GROQ_API_KEY', 'test'), patch.object(classifier, 'LLM_BACKEND', 'api'), patch.object(classifier, 'GEMINI_API_KEY', ''), patch.object(classifier, 'ANTHROPIC_API_KEY', ''), \
             patch.object(classifier, 'remaining_cooldown', return_value=60):
            self.assertFalse(radar.has_work(self.c))

    def test_unjoined_invite_is_cached_between_polling_cycles(self):
        client = Mock()
        src = 'https://t.me/+TestPrivate'
        self.assertEqual(watcher.resolve(client, self.c, src), (None, None))
        self.assertEqual(watcher.resolve(client, self.c, src), (None, None))
        self.assertEqual(client.call_count, 1)
        # Manual/full runs can detect a newly joined group without waiting an hour.
        watcher.resolve(client, self.c, src, refresh_invites=True)
        self.assertEqual(client.call_count, 2)

    def test_expired_invite_cache_is_rechecked(self):
        client = Mock()
        src = 'https://t.me/+TestPrivate'
        db.set_meta(self.c, watcher.invite_cache_key(src), db.ago(hours=1))
        watcher.resolve(client, self.c, src)
        self.assertEqual(client.call_count, 1)

    def test_joined_invite_clears_negative_cache(self):
        src = 'https://t.me/+JoinedPrivate'
        db.set_meta(self.c, watcher.invite_cache_key(src), db.now())
        chat = SimpleNamespace(id=555, username='joined', title='Joined')
        client = Mock(return_value=ChatInviteAlready(chat))
        with patch.object(watcher, 'get_peer_id', return_value=-100555):
            watcher.resolve(client, self.c, src, refresh_invites=True)
        self.assertIsNone(db.get_meta(self.c, watcher.invite_cache_key(src)))
        self.assertEqual(self.c.execute("SELECT peer_id FROM chats WHERE source=?", (src,)).fetchone()[0], -100555)

    def test_long_flood_does_not_crash_full_run_and_preserves_queue(self):
        client = Mock()
        client.is_user_authorized.return_value = True
        with patch.object(watcher, 'TG_API_ID', '1'), patch.object(watcher, 'TG_API_HASH', 'test'), \
             patch.object(watcher, '_client', return_value=client), \
             patch.object(watcher, 'resolve', side_effect=FloodWaitError(None, capture=90)):
            result = watcher.collect(self.c, sources=['@clinic'], pause_seconds=0)
        self.assertEqual(result, [])
        self.assertGreater(watcher.telegram_pause_remaining(self.c), 85)
        self.assertEqual(self.c.execute('SELECT last_id FROM chats').fetchone()[0], 1)
        client.disconnect.assert_called_once()
        self.assertTrue(watcher.problems)

    def test_radar_flood_is_persisted_for_other_processes(self):
        client = Mock()
        client.is_user_authorized.return_value = True
        with patch.object(watcher, 'TG_API_ID', '1'), patch.object(watcher, 'TG_API_HASH', 'test'), \
             patch.object(watcher, 'resolve', side_effect=FloodWaitError(None, capture=90)):
            with self.assertRaises(FloodWaitError):
                watcher.collect(self.c, client=client, sources=['@clinic'], pause_seconds=0)
        self.assertGreater(watcher.telegram_pause_remaining(self.c), 85)
        client.disconnect.assert_not_called()

    def test_saved_telegram_pause_does_not_open_session(self):
        watcher.pause_telegram(self.c, 90)
        with patch.object(watcher, 'TG_API_ID', '1'), patch.object(watcher, 'TG_API_HASH', 'test'), \
             patch.object(watcher, '_client') as create:
            self.assertEqual(watcher.collect(self.c), [])
        create.assert_not_called()

    def test_short_flood_wait_auto_retry_is_enabled(self):
        with patch.object(watcher, 'TG_API_ID', '1'), patch.object(watcher, 'TG_API_HASH', 'test'), \
             patch.object(watcher, 'TelegramClient') as factory:
            watcher._client().factory()
        self.assertGreaterEqual(factory.call_args.kwargs['flood_sleep_threshold'], 13)
        self.assertLessEqual(factory.call_args.kwargs['flood_sleep_threshold'], 30)


class QualificationTests(unittest.TestCase):
    def test_invalid_enum_is_rejected(self):
        from deploy.test_llm_queue import LEAD
        with self.assertRaises(ValueError):
            classifier.validate_card(dict(LEAD, author_role='owner'), require_qualification=True)

    def test_new_model_answer_requires_qualification_but_old_card_is_compatible(self):
        from deploy.test_llm_queue import LEAD
        old = {k:v for k,v in LEAD.items() if k not in classifier.QUALIFICATION_ENUMS and k not in classifier.QUALIFICATION_TEXT}
        with self.assertRaises(ValueError):
            classifier.validate_card(dict(old), require_qualification=True)
        self.assertEqual(classifier.validate_card(old)['author_role'], 'unknown')

    def test_card_renders_unknowns_and_escapes_model_text(self):
        from deploy.test_llm_queue import LEAD
        card = dict(LEAD, fit_reason='<script>', next_step='Уточнить <объём>')
        text = telegram_out.qualification_text(card)
        self.assertIn('неизвестна', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)


class SessionTests(unittest.TestCase):
    def test_session_has_one_owner_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = SessionLock(Path(tmp)/'session.lock')
            second = SessionLock(Path(tmp)/'session.lock')
            first.acquire(timeout=0)
            try:
                with self.assertRaises(TimeoutError):
                    second.acquire(timeout=0)
            finally:
                first.release()
            second.acquire(timeout=0)
            second.release()

    def test_connection_failure_releases_session_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            underlying = Mock()
            underlying.connect.side_effect = RuntimeError('network')
            client = LockedClient(lambda: underlying, Path(tmp)/'session.lock')
            with self.assertRaises(RuntimeError):
                client.connect()
            other = SessionLock(Path(tmp)/'session.lock')
            other.acquire(timeout=0)
            other.release()


if __name__ == '__main__':
    unittest.main()
