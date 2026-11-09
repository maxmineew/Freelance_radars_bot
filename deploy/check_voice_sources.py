"""Read-only check of new sources using the server watcher session."""
from leadbot import watcher, db


def main():
    c = db.connect()
    client = watcher._client()
    client.connect()
    try:
        if not client.is_user_authorized():
            raise RuntimeError("Watcher login required")
        seen = set()
        for src in ("@denttalk", "@stomaonline"):
            try:
                ent, row = watcher.resolve(client, c, src)
                if ent is None:
                    print(src, "JOIN_REQUIRED")
                    continue
                duplicate = row["peer_id"] in seen or c.execute(
                    "SELECT 1 FROM chats WHERE peer_id=? AND source!=?",
                    (row["peer_id"], src),
                ).fetchone()
                seen.add(row["peer_id"])
                messages = list(client.iter_messages(ent, limit=1))
                print(src, "READ_OK" if messages else "NO_HISTORY", "ALIAS" if duplicate else "", row["title"])
            except Exception as exc:
                print(src, "CHECK_FAILED", type(exc).__name__)
    finally:
        client.disconnect()
        c.close()


if __name__ == "__main__":
    main()
