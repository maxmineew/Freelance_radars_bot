"""Daily tick using the radar's already locked, authorized Telegram client."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent.parent


def due(config, now):
    now = now.astimezone(MSK)
    first = date.fromisoformat(config['first_date'])
    delta = (now.date() - first).days
    hour, minute = map(int, config['time'].split(':'))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (config.get('enabled') is True and delta >= 0
            and delta % config['interval_days'] == 0
            and 0 <= (now - scheduled).total_seconds() < 900)


def tick(client, connection, now=None):
    path = ROOT / 'business_card.json'
    if not path.exists():
        return
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    now = now or datetime.now(MSK)
    if not due(config, now):
        return
    target = config['target']
    day = now.astimezone(MSK).date().isoformat()
    connection.execute('CREATE TABLE IF NOT EXISTS business_card_posts ('
                       'target TEXT, day TEXT, status TEXT, message_id INTEGER, '
                       'PRIMARY KEY(target, day))')
    connection.commit()
    # Commit the claim BEFORE sending. An ambiguous network error must never
    # cause an automatic duplicate. Pending records require manual inspection.
    claim = connection.execute('INSERT OR IGNORE INTO business_card_posts '
                               '(target,day,status) VALUES(?,?,?)',
                               (target, day, 'pending'))
    connection.commit()
    if not claim.rowcount:
        return
    try:
        text = (ROOT / config['text_file']).read_text(encoding='utf-8-sig').strip()
        if not text or len(text) > 4096:
            raise ValueError('Визитка должна содержать 1–4096 символов')
        me = client.get_me()
        if getattr(me, 'bot', False):
            raise RuntimeError('Для публикации нужен личный аккаунт')
        entity = client.get_entity(target)
        permissions = client.get_permissions(entity, me)
        if not permissions.is_member or not permissions.send_messages:
            raise RuntimeError('Аккаунт не состоит в чате или не может писать')
        message = client.send_message(entity, text, parse_mode=None, link_preview=False)
        connection.execute('UPDATE business_card_posts SET status=?,message_id=? '
                           'WHERE target=? AND day=?', ('sent', message.id, target, day))
        connection.commit()
        print(f'Визитка опубликована: {target}, сообщение {message.id}', flush=True)
    except Exception as exc:
        print(f'Визитка: отправка не подтверждена ({type(exc).__name__}). '
              'Автоповтор отключён; проверьте чат и business_card_posts.', flush=True)
        # Keep pending even for errors before send: manual intervention is safer
        # than retrying an uncertain external side effect.
        raise
