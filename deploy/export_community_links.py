import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
entries = {}

def add(raw, source, kind='', status='', note='', label=''):
    raw = raw.strip().rstrip('.,;')
    raw = re.sub(r'^https?://(?:t\.me|telegram\.me)/(?:s/|joinchat/)?', '', raw)
    raw = raw.lstrip('@')
    if raw.startswith('c/') or not re.fullmatch(r'\+?[A-Za-z0-9_-]+', raw):
        return
    key = raw if raw.startswith('+') else raw.lower()
    e = entries.setdefault(key, dict(raw=raw, sources=set(), kinds=set(), statuses=set(), notes=set(), label=''))
    e['sources'].add(source)
    if kind: e['kinds'].add(kind)
    if status: e['statuses'].add(status)
    if note: e['notes'].add(note)
    if label and not label.startswith('@'): e['label'] = label

for filename, kind in [('chats.txt', 'Чат'), ('sources.txt', 'Канал')]:
    for line in (ROOT / filename).read_text(encoding='utf-8-sig').splitlines():
        s = line.strip()
        disabled = s.startswith('#')
        s = s.lstrip('#').strip()
        if re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,}|https://t\.me/\+[A-Za-z0-9_-]+', s):
            add(s, filename, kind, 'Резерв / отключён' if disabled else 'Подключён в локальном списке')

section = ''
for line in (ROOT / 'chats.md').read_text(encoding='utf-8-sig').splitlines():
    if line.startswith('## '): section = line
    if section.startswith('## 4.') or section.startswith('## Идеи'): continue
    excluded = section.startswith('## 3.')
    kind = 'Канал' if section.startswith('## 1.') else ('Чат' if section.startswith('## 2.') else '')
    cells = [s.strip() for s in line.strip('|').split('|')] if line.startswith('|') else []
    note = cells[1] if excluded and len(cells)>1 else ''
    if kind and len(cells)>2:
        note = '; '.join(cells[1:])
    labels = dict((url, label) for label,url in re.findall(r'\[([^\]]+)\]\((https?://t\.me/[^)]+)\)', line))
    values = re.findall(r'https?://t\.me/(?:s/)?\+?[A-Za-z0-9_-]+|(?<![\w])@[A-Za-z][A-Za-z0-9_]{3,}', line)
    values += ['https://t.me/'+x for x in re.findall(r'`(\+[A-Za-z0-9_-]+)`', line)]
    for value in values:
        add(value, 'chats.md', kind, 'Исключён из отбора' if excluded else '', note, labels.get(value,''))

db = sqlite3.connect((ROOT / 'leads.db').as_uri()+'?mode=ro', uri=True)
for source, name, title in db.execute('SELECT source,name,title FROM chats'):
    add(source, 'leads.db:chats', note='Название в базе: '+title if title else '')
for (channel,) in db.execute('SELECT DISTINCT channel FROM posts'):
    add(channel, 'leads.db:posts')
db.close()
add('CCG_chatbot', 'leadbot/discovery.py', note='Бот входа в сообщество CCGuru')

groups = {'Чаты и сообщества': [], 'Каналы': [], 'Исключённые и прочие источники': [], 'Боты-посредники': []}
for e in entries.values():
    if e['raw'].lower().endswith('bot') or e['raw'].lower().endswith('_bot'):
        group = 'Боты-посредники'
    elif 'Чат' in e['kinds']: group = 'Чаты и сообщества'
    elif 'Канал' in e['kinds']: group = 'Каналы'
    else: group = 'Исключённые и прочие источники'
    groups[group].append(e)

lines = ['# Telegram-чаты, сообщества и каналы бота', '', 'Дата извлечения: 20.10.2026.', '',
    f'Всего уникальных ссылок: **{len(entries)}**, включая ботов-посредников.', '',
    'Источник: локальная папка `'+str(ROOT)+'`. Проверены списки, справочник, код и база `leads.db`. Тестовые ссылки из тестов исключены. Данные с сервера не запрашивались.', '',
    'Статус подключения указан по локальным файлам. Причины исключения, размеры, активность и приоритеты перенесены из справочника и не проверялись заново в Telegram. Публичные имена приведены к ссылкам `https://t.me/...`; дубли ссылок удалены. Приглашение и публичное имя одного сообщества могут оставаться отдельными ссылками.', '',
    'По справочнику приглашение `+7ezqiJCm7sM1ODQy` и публичное имя `denttalk` относятся к одному чату владельцев стоматологий.', '']
def safe(value): return value.replace('|',' / ').replace('\n',' ')
for group, items in groups.items():
    lines += [f'## {group} ({len(items)})', '', '| Ссылка | Статус | Сведения из бота | Где найдена |', '|---|---|---|---|']
    for e in sorted(items,key=lambda x:x['raw'].lower()):
        url = 'https://t.me/'+e['raw']
        label = e['label'] or e['raw']
        status = '; '.join(sorted(e['statuses'])) or 'Упомянут в справочнике / базе'
        lines.append('| ['+safe(label)+']('+url+') | '+safe(status)+' | '+safe('; '.join(sorted(e['notes'])))+' | '+', '.join(sorted(e['sources']))+' |')
    lines.append('')
output = ROOT / 'telegram_communities.md'
output.write_text('\n'.join(lines),encoding='utf-8-sig')
print(output)
print({k:len(v) for k,v in groups.items()})
print('TOTAL',len(entries))
