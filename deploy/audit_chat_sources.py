"""Read-only source audit; never reads or prints message bodies or credentials."""
import json
import sqlite3
import subprocess
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from leadbot.config import DB_PATH, RETENTION_DAYS
from leadbot.watcher import load_chats

c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
configured = load_chats()
cache = {r["source"]: dict(r) for r in c.execute("SELECT * FROM chats")}
groups = {}
for source in configured:
    row = cache.get(source)
    key = row["peer_id"] if row else source
    group = groups.setdefault(key, {"sources": [], "names": set(), "title": row["title"] if row else source, "last_id": 0})
    group["sources"].append(source)
    if row:
        group["names"].add(row["name"])
        group["last_id"] = max(group["last_id"], row["last_id"])
        for other in cache.values():
            if other["peer_id"] == row["peer_id"]:
                group["names"].add(other["name"])

log = subprocess.run(["journalctl", "-u", "leadbot.service", "--since", "2026-09-14", "--no-pager", "-o", "cat"],
                     capture_output=True, text=True, timeout=30).stdout
lines = log.splitlines()
result = []
for group in groups.values():
    names = sorted(group.pop("names"))
    posts = [r for name in names for r in c.execute("SELECT status,score,feedback,card,created_at,posted_at FROM posts WHERE channel=?", (name,))]
    statuses = Counter(r["status"] for r in posts)
    marks = Counter(r["feedback"] for r in posts if r["feedback"])
    services = Counter()
    voice_qualified = voice_sent = 0
    for r in posts:
        if r["card"]:
            card = json.loads(r["card"])
            if (r["score"] or 0) >= 6 and card.get("is_lead"):
                services[card.get("service", "other")] += 1
                if card.get("service") == "voice_agent":
                    voice_qualified += 1
                    voice_sent += int(r["status"] == "sent")
    failures = Counter()
    for line in lines:
        for src in group["sources"]:
            if "Наблюдатель: " + src + " " in line or "Наблюдатель: " + src + ":" in line:
                if "не состоит в чате" in line:
                    failures["not_joined"] += 1
                elif "No user has" in line or "Nobody is using" in line:
                    failures["username_missing"] += 1
                elif "expired" in line.lower() or "invalid" in line.lower():
                    failures["invalid_or_expired"] += 1
                else:
                    failures["other"] += 1
                break
    group.update(names=names, total=len(posts), statuses=dict(statuses), marks=dict(marks),
                 qualified=sum(services.values()), services=dict(services), voice_qualified=voice_qualified, voice_sent=voice_sent,
                 first_seen=min((r["created_at"] for r in posts), default=None),
                 last_seen=max((r["created_at"] for r in posts), default=None), failures=dict(failures))
    result.append(group)

runs = dict(c.execute("SELECT COUNT(*) runs,MIN(started_at) first,MAX(started_at) last,SUM(error IS NOT NULL) errors FROM runs").fetchone())
out = {"as_of": datetime.now(timezone.utc).isoformat(), "retention_days": RETENTION_DAYS,
       "configured_entries":len(configured), "unique_sources":len(groups), "runs":runs,
       "recent_runs":[dict(r) for r in c.execute("SELECT started_at,finished_at,summary,error FROM runs ORDER BY id DESC LIMIT 6")],
       "sources":result}
if "--summary" in sys.argv:
    zero = [r for r in result if not r["qualified"]]
    out = {"as_of":out["as_of"], "first_post":min(r["first_seen"] for r in result if r["first_seen"]),
           "sources":len(result), "zero":len(zero), "zero_with_posts":sum(bool(r["total"]) for r in zero),
           "zero_without_posts":sum(not r["total"] for r in zero),
           "voice_sources":sum(bool(r["voice_qualified"]) for r in result),
           "totals": {"posts":sum(r["total"] for r in result), "qualified":sum(r["qualified"] for r in result),
                      "take":sum(r["marks"].get("take",0) for r in result),
                      "unscored":sum(r["statuses"].get("new",0) for r in result),
                      "unscored_in_zero":sum(r["statuses"].get("new",0) for r in zero)},
           "classification_error_codes":dict(Counter(re.search(r"LLM (\d+)", line).group(1) if re.search(r"LLM (\d+)", line) else "other"
                                                    for line in lines if "classify error:" in line)),
           "zero_with_posts":[{"source":r["sources"][0],"title":r["title"],"posts":r["total"],
                               "scored":r["statuses"].get("scored",0),"unscored":r["statuses"].get("new",0),
                               "dups":r["statuses"].get("dup",0)} for r in sorted(zero,key=lambda r:-r["total"]) if r["total"]],
           "positive":[{"source":r["sources"][0],"qualified":r["qualified"],"take":r["marks"].get("take",0),"voice":r["voice_qualified"]} for r in result if r["qualified"]]}
print(json.dumps(out, ensure_ascii=False, indent=2))
c.close()
