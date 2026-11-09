import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Контейнер (Bothost): пересборка сохраняет только токен, env, домен и папку data/,
# поэтому всё изменяемое состояние (база, найденные источники, отключённые источники,
# сессия наблюдателя) живёт в DATA_DIR. Локально env не задан — всё лежит в корне.
DATA_DIR = Path(os.getenv("DATA_DIR")) if os.getenv("DATA_DIR") else ROOT
DB_PATH = DATA_DIR / "leads.db"
SOURCES_FILE = ROOT / "sources.txt"
# Оценку делает Groq, если в .env есть GROQ_API_KEY, иначе Gemini
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = (os.getenv("GROQ_MODEL", "openai/gpt-oss-120b") if GROQ_API_KEY
         else os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
GROQ_FALLBACK_MODELS = [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m.strip()]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"
# Бэкенд оценки: "claude_cli" — только Anthropic через `claude -p` с логином подписки (без API-ключей),
# "codex_cli" — OpenAI через Codex CLI с входом ChatGPT; CODEX_MODEL задаёт модель (иначе выбор CLI).
# "api" — прежняя цепочка Groq/Anthropic API/Gemini
LLM_BACKEND = os.getenv("LLM_BACKEND", "claude_cli")
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "claude")
CLAUDE_CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "120"))
MAX_CLASSIFICATIONS_PER_RUN = 30
# Пакетная оценка через Claude CLI: несколько сообщений за один вызов и несколько вызовов параллельно
CLAUDE_BATCH_SIZE = int(os.getenv("CLAUDE_BATCH_SIZE", "5"))
CLAUDE_WORKERS = int(os.getenv("CLAUDE_WORKERS", "3"))
REPLIES_COOL = 3            # столько ответов в чате — лид остыл, карточку не шлём
SOURCE_MUTE_MIN_MARKS = 15  # отметок по источнику, после которых считаем точность
SOURCE_MUTE_PRECISION = 0.15  # ниже этой доли «Беру» источник отключаем
DISK_ALERT_PERCENT = 90
QUEUE_ALERT_SIZE = 1500
MONITOR_SECONDS = 600
DISCOVERY_SAMPLE = 100      # сообщений чата, по которым оцениваем долю заявок
DISCOVERY_MIN_DEMAND = 2    # минимум явных заявок в выборке
LLM_MIN_INTERVAL_SECONDS = 5
QUEUE_RECOVERY_LIMIT = 200
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")
RETENTION_DAYS = 30      # 152-ФЗ: храним минимум и недолго
MIN_SCORE = 6            # порог ICP-оценки для отправки карточки
# Правила для бирж. Kwork: лимит 30 откликов в месяц, поэтому шлём только лучшее и свежее —
# оценка от 9, заказ не старше 3 часов, не больше 2 карточек в сутки (по Москве)
EXCHANGE_RULES = {"Kwork": {"min_score": 9, "max_age_hours": 3, "per_day": 2}}
# Kwork напрямую: JSON биржи проектов kwork.ru/projects (без логина, только чтение)
KWORK_ENABLED = os.getenv("KWORK_ENABLED", "1") != "0"
KWORK_CHANNEL = "kwork.ru"     # «канал» для заказов Kwork в таблице posts
KWORK_POLL_SECONDS = 180       # как часто радар опрашивает Kwork
KWORK_PAGES = 5                # не глубже 5 страниц по 12 заказов
KWORK_MAX_CREATED_DAYS = 3     # старые заказы, поднятые заново, не берём
# FL.ru: открытая RSS-лента всех новых заказов, радар опрашивает её почти непрерывно
FLRU_ENABLED = os.getenv("FLRU_ENABLED", "1") != "0"
FLRU_CHANNEL = "fl.ru"         # «канал» для заказов FL.ru в таблице posts
FLRU_POLL_SECONDS = int(os.getenv("FLRU_POLL_SECONDS", "60"))
# Биржи, которые читаем напрямую: у их записей нет поста в Telegram
# Pchel.net: открытая RSS-лента проектов (Хабр Фриланс закрылся, Weblancer/YouDo закрыты защитой от ботов)
PCHEL_ENABLED = os.getenv("PCHEL_ENABLED", "1") != "0"
PCHEL_CHANNEL = "pchel.net"
PCHEL_POLL_SECONDS = int(os.getenv("PCHEL_POLL_SECONDS", "300"))
EXCHANGE_CHANNELS = {KWORK_CHANNEL, FLRU_CHANNEL, PCHEL_CHANNEL}
MAX_AGE_DAYS = 14       # старые посты не берём
MAX_CARD_AGE_HOURS = 3  # максимальный возраст заявки при сборе, оценке и отправке
PAGES_PER_CHANNEL = 3    # глубина пагинации t.me/s (~20 постов на страницу)
SIMILAR_DUP = 0.6        # доля общих четвёрок слов, с которой текст считается дублем более раннего
REMIND_AFTER_HOURS = 24  # через сколько напомнить о взятом лиде, если его этап не менялся
RUN_HOURS = [9, 11, 13, 15, 17, 19, 21]  # расписание прогонов по Москве, как в deploy/leadbot.timer

# Этап 2: наблюдатель за чатами (отдельный аккаунт, только чтение)
CHATS_FILE = ROOT / "chats.txt"
DISCOVERED_CHATS_FILE = DATA_DIR / "chats_discovered.txt"
DISCOVERED_SOURCES_FILE = DATA_DIR / "sources_discovered.txt"
DISCOVERY_MAX_ADDITIONS = 3
MUTED_FILE = DATA_DIR / "muted_sources.txt"  # источники, отключённые из-за низкой точности; удалите строку, чтобы вернуть


def muted():
    if not MUTED_FILE.exists():
        return set()
    return {l.strip().lstrip("@").lower() for l in MUTED_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")}
SESSION = DATA_DIR / "watcher"       # файл сессии watcher.session
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
CHAT_MSG_LIMIT = 100     # размер страницы; непрочитанное догоняем следующими страницами
RADAR_POLL_SECONDS = 60  # history fallback for sources without live updates
RADAR_WORKER_SECONDS = 15
RADAR_BATCH_SIZE = 3

# Дешёвый префильтр перед LLM: сигналы потребности в автоматизации/ИИ/голосе
KEYWORDS = [
    "перезвон", "перезван", "прозвон", "дозвон", "позвон", "звонить", "звонят",
    "колцентр", "коллцентр", "кол-центр", "контакт-центр", "контакт центр",
    "оператор", "телефони", "не хватает людей", "не хватает сотруд", "нехватка персонала",
    "некому", "подтвержд", "напоминан", "неявк", "не приходят", "возврат пациент",
    "реактивац", "холодн", "теплую баз", "тёплую баз",
    "бот", "ассистент", "нейросет", "gpt", "автоматизац", "голосов", "звонк", "обзвон",
    "колл-центр", "ivr", "crm", "amocrm", "bitrix", "битрикс", "n8n", "интеграци",
    "заявк", "администратор", "не успева", "теряем", "ищу разработ", "нужен разработ",
    "нужен специалист", "ищем специалист", "автоворонк", "воронк", "рассылк", "парсинг",
    " ии ", " ai ", "искусственн",
    # запись клиентов и квалификация заявок
    "записыва", "онлайн-запис", "онлайн запис", "запись клиент", "запись на приём", "запись на прием",
    "квалифик", "отсеив", "отсев", "входящие заявк", "входящих заявк", "обработк", "автоответ",
    "лиды", "лидов", "слив", "горячие клиент", "первичн", "обрабатыва",
]
