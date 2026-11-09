"""Read-only server health check; no messages or Telegram client connections."""
import json
from datetime import datetime, timezone

from leadbot import db, classifier

c = db.connect()
heartbeat = db.get_meta(c, 'radar:heartbeat')
assert heartbeat, 'No radar heartbeat'
age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
assert age < 180, f'Stale heartbeat: {age:.0f}s'
print('RADAR_STATE', db.get_meta(c, 'radar:state'), 'heartbeat_age_seconds', round(age))
print('HISTORY_LAST_POLL', db.get_meta(c, 'radar:last_poll'))
print('QUEUE_SIZE', c.execute('SELECT COUNT(*) FROM llm_queue').fetchone()[0])
print('SESSION_RUN_ACTIVE', bool(db.active_run(c)))
checked = 0
for row in c.execute("SELECT card FROM posts WHERE card LIKE '%\"request_type\"%' ORDER BY COALESCE(sent_at,created_at) DESC LIMIT 100"):
    card = json.loads(row['card'])
    if 'request_type' in card:
        classifier.validate_card(card, require_qualification=True)
        checked += 1
print('VALID_NEW_QUALIFICATIONS', checked)
print('LAST_MODEL', db.get_meta(c, 'llm:last_model'))
print('QUEUE_ERRORS', [dict(row) for row in c.execute('SELECT last_error,COUNT(*) count FROM llm_queue WHERE last_error IS NOT NULL GROUP BY last_error LIMIT 5')])
print('MODEL_COOLDOWNS', {key: round(classifier.remaining_cooldown(key, c)) for key in
                         ['groq:' + classifier.MODEL, *['groq:' + model for model in classifier.GROQ_FALLBACK_MODELS]]})
print('RECENT_RUNS', [dict(row) for row in c.execute('SELECT id,session_required,error,finished_at FROM runs ORDER BY id DESC LIMIT 3')])
c.close()
print('RADAR_HEALTH_OK')
