"""Synthetic model check with quota diagnostics; never logs inputs, keys or response bodies."""
import re
import httpx
from leadbot import classifier, db


class Diagnostics:
    def __init__(self):
        self.http = httpx.Client(timeout=60)

    def post(self, *args, **kwargs):
        result = self.http.post(*args, **kwargs)
        if result.status_code != 200:
            print('MODEL_HTTP', result.status_code, 'model', kwargs['json'].get('model'))
            if result.status_code == 429:
                category = re.search(r'tokens per day|tokens per minute|requests per day|requests per minute', result.text, re.I)
                counts = re.search(r'Limit\s+(\d+).*?Used\s+(\d+).*?Requested\s+(\d+)', result.text, re.I)
                print('QUOTA_CATEGORY', category.group(0) if category else 'unknown')
                print('QUOTA_COUNTS', counts.groups() if counts else 'unknown')
                print('RETRY_SECONDS', classifier.retry_delay(result))
        return result


c = db.connect()
classifier._client = Diagnostics()
try:
    # A single diagnostic probe ignores persisted cooldowns without clearing them
    # in production, so a 429 can be classified without exposing its body.
    card = classifier.classify('Срочно сегодня ищу разработчика чат-бота для нашей клиники. Нужно подтверждать запись пациентов.',
                               posted_at=db.now(), _allow_wait=False)
    classifier.validate_card(card, require_qualification=True)
    print('LIVE_QUALIFICATION', {key:card[key] for key in ['is_lead','score','service','service_fit','request_type','urgency','author_role']})
    assert card['is_lead'] and card['score'] >= 6
    assert card['request_type'] == 'direct' and card['urgency'] == 'urgent'
    assert not card['budget_hint'], 'Budget was not specified'
    print('RADAR_MODEL_OK')
except classifier.ClassificationError as exc:
    print('MODEL_WAIT', str(exc), 'retry_seconds', round(exc.retry_after))
    raise SystemExit(75)
finally:
    c.close()
    classifier._client.http.close()
