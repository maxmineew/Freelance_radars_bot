import json
import re
import subprocess
import sqlite3
from collections import Counter
from leadbot import config

print(json.dumps({"model":config.MODEL,"groq_configured":bool(config.GROQ_API_KEY),
                  "gemini_configured":bool(config.GEMINI_API_KEY)},ensure_ascii=False))
log = subprocess.run(["journalctl","-u","leadbot.service","--since","2026-10-05","--no-pager","-o","cat"],
                     capture_output=True,text=True,timeout=30).stdout
errors=Counter()
for line in log.splitlines():
    if "classify error:" not in line:
        continue
    code=re.search(r"LLM (\d+)",line)
    limit=re.search(r"(tokens per day|tokens per minute|requests per day|requests per minute|json_validate_failed|json_schema|failed_generation)",line,re.I)
    kind=limit.group(1).lower() if limit else "other"
    errors[(code.group(1) if code else "unknown",kind)]+=1
print(json.dumps([{"code":k[0],"kind":k[1],"count":v} for k,v in errors.items()]))
c=sqlite3.connect(f"file:{config.DB_PATH}?mode=ro",uri=True)
print("queue",c.execute("SELECT status,count(*),sum(posted_at>=datetime('now','-2 days')) FROM posts WHERE status IN ('new','dup') GROUP BY status").fetchall())
c.close()
