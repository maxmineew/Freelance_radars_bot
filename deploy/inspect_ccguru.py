import re
from leadbot import watcher

client = watcher._client()
client.connect()
try:
    if not client.is_user_authorized():
        raise RuntimeError("Watcher login required")
    links = set()
    for msg in client.iter_messages("CCG_chatbot", limit=30):
        if msg.out:
            continue
        links.update(re.findall(r"https?://(?:t\.me|telegram\.me)/[\w+/-]+", msg.message or ""))
        for entity in msg.entities or []:
            url = getattr(entity, "url", "")
            if re.match(r"https?://(?:t\.me|telegram\.me)/", url):
                links.add(url)
        for row in getattr(msg.reply_markup, "rows", []) or []:
            for button in row.buttons:
                url = getattr(button, "url", "")
                if re.match(r"https?://(?:t\.me|telegram\.me)/", url):
                    links.add(url)
    for link in sorted(links):
        print(link)
    if not links:
        print("NO_LINKS_IN_WATCHER_DIALOG")
finally:
    client.disconnect()
