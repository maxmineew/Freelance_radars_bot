import ast
import datetime
import json
from pathlib import Path
import shutil
import subprocess

root = Path('/opt/leadbot')
payload = Path(__file__).resolve().parent
radar = root / 'leadbot/radar.py'
text = radar.read_text()
needle = '            if now >= next_heartbeat:'
if 'business_card.tick(client, c)' not in text:
    assert text.count(needle) == 1
    imp = 'from .session_lock import SessionLock'
    assert text.count(imp) == 1
    text = text.replace(imp, imp + '\nfrom . import business_card')
    hook = '''            if client is not None and not telegram_busy(c) and not watcher.telegram_pause_remaining(c):
                try:
                    business_card.tick(client, c)
                except FloodWaitError as exc:
                    watcher.pause_telegram(c, exc.seconds)
                    disconnect()
                except Exception as exc:
                    print(f"Business card needs inspection ({type(exc).__name__})", flush=True)
'''
    text = text.replace(needle, hook + needle)
ast.parse(text)
ast.parse((payload / 'business_card.py').read_text(encoding='utf-8-sig'))
config = json.loads((payload / 'business_card.json').read_text(encoding='utf-8-sig'))
config['enabled'] = True
backup = root / ('backup-business-card-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir()
paths = ['leadbot/radar.py', 'leadbot/business_card.py', 'business_card.json', 'business_card.txt']
existed = {p: (root / p).exists() for p in paths}
for p in paths:
    if existed[p]:
        shutil.copy2(root / p, backup / p.replace('/', '_'))
subprocess.run(['systemctl', 'stop', 'leadbot-radar.service'], check=True)
try:
    radar.write_text(text)
    shutil.copy2(payload / 'business_card.py', root / 'leadbot/business_card.py')
    shutil.copy2(payload / 'business_card.txt', root / 'business_card.txt')
    (root / 'business_card.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    subprocess.run(['chown', 'leadbot:leadbot', *[str(root / p) for p in paths]], check=True)
    subprocess.run([str(root / 'venv/bin/python'), '-c', 'from leadbot import business_card, radar; print("Imports OK")'], cwd=root, check=True)
    subprocess.run(['systemctl', 'start', 'leadbot-radar.service'], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'leadbot-radar.service'], check=True)
except BaseException:
    for p in paths:
        if existed[p]:
            shutil.copy2(backup / p.replace('/', '_'), root / p)
        else:
            (root / p).unlink(missing_ok=True)
    subprocess.run(['systemctl', 'start', 'leadbot-radar.service'])
    raise
print('Installed and enabled. Backup:', backup)
print(json.dumps(config, ensure_ascii=False))
