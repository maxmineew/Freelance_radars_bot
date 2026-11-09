from ddgs import DDGS

for backend in ("auto", "google", "yahoo", "duckduckgo"):
    try:
        results = DDGS(timeout=15).text('Telegram чат владельцев стоматологий',
                                       region="ru-ru", max_results=5, backend=backend)
        print(backend, len(results))
        for row in results[:3]:
            print(row.get("href"), row.get("title"))
    except Exception as exc:
        print(backend, type(exc).__name__, str(exc)[:180])
