import json
import sqlite3
from leadbot import config, db

c=sqlite3.connect(f"file:{config.DB_PATH}?mode=ro",uri=True)
c.row_factory=sqlite3.Row
out={"states":[dict(r) for r in c.execute("SELECT status,COUNT(*) count FROM posts GROUP BY status")],
     "queue":dict(c.execute("SELECT COUNT(*) pending,SUM(attempts>0) attempted,SUM(next_attempt_at IS NOT NULL) deferred FROM llm_queue").fetchone()),
     "recent_runs":[dict(r) for r in c.execute("SELECT started_at,finished_at,summary,error FROM runs ORDER BY id DESC LIMIT 3")],
     "last_model":db.get_meta(c,"llm:last_model"),
     "queue_errors":[dict(r) for r in c.execute("SELECT last_error,COUNT(*) count FROM llm_queue WHERE last_error IS NOT NULL GROUP BY last_error")],
     "fresh_unrestored":c.execute("SELECT COUNT(*) FROM posts p WHERE status='new' AND posted_at>=? AND NOT EXISTS(SELECT 1 FROM llm_queue q WHERE q.channel=p.channel AND q.msg_id=p.msg_id)",(db.ago(hours=config.MAX_CARD_AGE_HOURS),)).fetchone()[0]}
print(json.dumps(out,ensure_ascii=False,indent=2))
c.close()
