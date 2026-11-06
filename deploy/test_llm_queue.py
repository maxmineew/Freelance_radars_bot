import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock

import httpx
from leadbot import db, pending, classifier
import run

LEAD = {"is_lead":True, "score":8, "niche":"Стоматология", "need":"Обзвон пациентов",
        "pain_signal":"Некому перезванивать", "service":"voice_agent", "budget_hint":"", "hook":"Помогу с обзвоном",
        "service_fit":"high", "fit_reason":"Нужен обзвон", "request_type":"pain", "urgency":"unspecified",
        "deadline_hint":"", "author_role":"unknown", "next_step":"Уточнить объём звонков"}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite"
        self.patch = patch.object(db, "DB_PATH", self.path)
        self.patch.start()
        self.c = db.connect()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, channel="clinic", mid=1, text="В нашей стоматологии некому обзванивать клиентов, нужна автоматизация", hours=0, enqueue=True):
        posted = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        self.c.execute("INSERT INTO posts(channel,msg_id,url,posted_at,status,created_at,text_hash,sketch) VALUES(?,?,?,?,?,?,?,?)",
                       (channel, mid, "https://t.me/clinic/1", posted, "new", db.now(), db.text_hash(text), db.sketch(text)))
        if enqueue:
            db.enqueue(self.c, {"channel":channel,"msg_id":mid,"text":text,"posted_at":posted})
        self.c.commit()

    def test_failure_survives_restart_then_becomes_card(self):
        self.post(text="Некому обзванивать клиентов стоматологии, телефон +7 999 123-45-67, @patient123")
        with patch.object(classifier, "classify", side_effect=classifier.ClassificationError("rate limit", 60, True)):
            pending.process(self.c, lambda _: None)
        row = self.c.execute("SELECT * FROM llm_queue").fetchone()
        self.assertNotIn("999", row["text"])
        self.assertNotIn("patient123", row["text"])
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0], "new")
        self.c.close()
        self.c = db.connect()
        self.c.execute("UPDATE llm_queue SET next_attempt_at=NULL")
        self.c.commit()
        with patch.object(classifier, "classify", return_value=dict(LEAD)):
            pending.process(self.c, lambda _: None)
        self.assertEqual(self.c.execute("SELECT status,score FROM posts").fetchone()[1], 8)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0], 0)

    def test_pending_duplicate_does_not_suppress_then_success_deduplicates(self):
        self.post("clinic1")
        self.post("clinic2")
        with patch.object(classifier, "classify", side_effect=classifier.ClassificationError("limit", 60, True)):
            pending.process(self.c, lambda _: None)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM posts WHERE status='dup'").fetchone()[0], 0)
        self.c.execute("UPDATE llm_queue SET next_attempt_at=NULL")
        self.c.commit()
        with patch.object(classifier, "classify", return_value=dict(LEAD)) as classify:
            pending.process(self.c, lambda _: None)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM posts WHERE status='dup'").fetchone()[0], 1)

    def test_invalid_card_stays_pending(self):
        self.post()
        bad = dict(LEAD, is_lead="false")
        with patch.object(classifier, "classify", return_value=bad):
            pending.process(self.c, lambda _:None)
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0], "new")
        self.assertIsNone(self.c.execute("SELECT score FROM posts").fetchone()[0])

    def test_expired_payload_removed(self):
        self.post(hours=3.1)
        with patch.object(classifier, "classify") as classify:
            pending.process(self.c, lambda _:None)
            classify.assert_not_called()
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0], "old")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0], 0)

    def test_voice_priority_under_budget(self):
        self.post("general", text="Нужен разработчик для интеграции интернет магазина с CRM", hours=2)
        self.post("voice", text="У нас некому обзванивать пациентов, нужен голосовой агент", hours=1)
        with patch.object(classifier, "classify", return_value=dict(LEAD)) as classify:
            pending.process(self.c, lambda _:None, limit=1)
        self.assertIn("обзванивать", classify.call_args.args[0])
        self.assertEqual(self.c.execute("SELECT status FROM posts WHERE channel='general'").fetchone()[0], "new")

    def test_atomic_run_claim(self):
        other = db.connect()
        try:
            self.assertIsNotNone(db.start_run(self.c))
            self.assertIsNone(db.start_run(other))
        finally:
            other.close()

    def test_recover_legacy_message(self):
        self.post(enqueue=False)
        self.c.execute("INSERT INTO chats(source,peer_id,name,title,last_id) VALUES('@clinic',-123,'clinic','Клиники',100)")
        self.c.commit()
        message=Mock(id=1, message="В нашей клинике некому обзванивать пациентов, нужна помощь",out=False)
        client=Mock()
        client.is_user_authorized.return_value=True
        client.get_messages.return_value=[message]
        with patch.object(pending,"TG_API_ID","1"),patch.object(pending,"TG_API_HASH","test"),patch.object(pending.watcher,"_client",return_value=client),patch.object(pending.watcher,"resolve",return_value=(Mock(),None)),patch.object(pending.time,"sleep"):
            pending.recover(self.c,lambda _:None)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0],1)

    def test_send_failure_is_retried_and_dry_never_marks_sent(self):
        self.post()
        db.complete(self.c,"clinic",1,"scored",8,json.dumps(LEAD,ensure_ascii=False))
        with patch.object(pending,"recover"),patch.object(pending,"process",return_value=None),patch.object(run.telegram_out,"send_card",side_effect=RuntimeError("network")):
            run.run(self.c,False,lambda _:None,retry_only=True)
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0],"scored")
        with patch.object(pending,"recover"),patch.object(pending,"process",return_value=None),patch("builtins.print"):
            run.run(self.c,True,lambda _:None,retry_only=True)
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0],"scored")
        with patch.object(pending,"recover"),patch.object(pending,"process",return_value=None),patch.object(run.telegram_out,"send_card",return_value=123) as send,patch.object(run.time,"sleep"):
            run.run(self.c,False,lambda _:None,retry_only=True)
            run.run(self.c,False,lambda _:None,retry_only=True)
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.c.execute("SELECT status FROM posts").fetchone()[0],"sent")


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        classifier._cooldowns.clear()
        classifier._last_request.clear()
        self.client=Mock()
        self.patches=[patch.object(classifier,"_client",self.client),patch.object(classifier,"GROQ_API_KEY","test"),
                      patch.object(classifier,"LLM_BACKEND","api"),patch.object(classifier,"GEMINI_API_KEY",""),patch.object(classifier,"ANTHROPIC_API_KEY",""),patch.object(classifier,"MODEL","primary"),
                      patch.object(classifier,"GROQ_FALLBACK_MODELS",["fallback"]),patch.object(classifier.time,"sleep")]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        classifier._cooldowns.clear()
        classifier._last_request.clear()

    def response(self,card=LEAD):
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(card,ensure_ascii=False)}}]})

    def test_daily_limit_uses_fallback_and_respects_cooldown(self):
        self.client.post.side_effect=[httpx.Response(429,headers={"retry-after":"3600"},text="tokens per day"),self.response(),self.response()]
        self.assertEqual(classifier.classify("Некому обзванивать пациентов")["score"],8)
        classifier.classify("Некому обзванивать клиентов")
        self.assertEqual([call.kwargs["json"]["model"] for call in self.client.post.call_args_list],["primary","fallback","fallback"])

    def test_malformed_json_and_string_boolean_are_retried(self):
        self.client.post.side_effect=[self.response(dict(LEAD,is_lead="false")),self.response()]
        self.assertEqual(classifier.classify("Некому обзванивать клиентов")["score"],8)
        self.assertEqual(self.client.post.call_count,2)

    def test_json_400_is_retried(self):
        self.client.post.side_effect=[httpx.Response(400,json={"error":{"code":"json_validate_failed"}}),self.response()]
        self.assertEqual(classifier.classify("Некому обзванивать клиентов")["score"],8)
        self.assertEqual(self.client.post.call_count,2)

    def test_invalid_answer_does_not_disable_provider_or_block_other_messages(self):
        self.client.post.side_effect=[self.response(dict(LEAD,is_lead="false"))]*4
        with self.assertRaises(classifier.ClassificationError) as result:
            classifier.classify("Некому обзванивать клиентов")
        self.assertFalse(result.exception.blocked)
        self.assertEqual(classifier.remaining_cooldown("groq:fallback"),0)

    def test_gpt_models_use_strict_card_schema(self):
        with patch.object(classifier,"MODEL","openai/gpt-oss-20b"):
            self.client.post.return_value=self.response()
            classifier.classify("Некому обзванивать клиентов")
        format=self.client.post.call_args.kwargs["json"]["response_format"]
        self.assertEqual(format["type"],"json_schema")
        self.assertTrue(format["json_schema"]["strict"])

    def test_network_error_does_not_log_input_or_keys(self):
        self.client.post.side_effect=httpx.ConnectError("secret_token some input")
        with self.assertRaises(classifier.ClassificationError) as result:
            classifier.classify("Некому обзванивать клиентов")
        self.assertNotIn("secret_token",str(result.exception))
        self.assertTrue(result.exception.blocked)

    def test_quota_cooldown_is_persistent(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,"DB_PATH",Path(tmp)/"db.sqlite"):
            c=db.connect()
            classifier.cooldown("groq:primary",3600,c)
            classifier._cooldowns.clear()
            self.assertGreater(classifier.remaining_cooldown("groq:primary",c),3500)
            c.close()

    def test_nonlead_has_zero_score(self):
        self.assertEqual(classifier.validate_card(dict(LEAD,is_lead=False))["score"],0)

    def test_full_text_hash_does_not_merge_shared_preamble(self):
        preamble="Объявление для всех участников сообщества "*20
        self.assertNotEqual(db.text_hash(preamble+"нужен обзвон"),db.text_hash(preamble+"предлагаю обучение"))


if __name__=="__main__":
    unittest.main()
