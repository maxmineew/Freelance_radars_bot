import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "leads.db"
SOURCES_FILE = ROOT / "sources.txt"
# Оценку делает Groq, если в .env есть GROQ_API_KEY, иначе Gemini
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = (os.getenv("GROQ_MODEL", "openai/gpt-oss-120b") if GROQ_API_KEY
         else os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")
RETENTION_DAYS = 30      # 152-ФЗ: храним минимум и недолго
MIN_SCORE = 6            # порог ICP-оценки для отправки карточки
# Правила для бирж. Kwork: лимит 30 откликов в месяц, поэтому шлём только лучшее и свежее —
# оценка от 7, заказ не старше 3 часов, не больше 3 карточек в сутки (по Москве)
EXCHANGE_RULES = {"Kwork": {"min_score": 7, "max_age_hours": 3, "per_day": 3}}
MAX_AGE_DAYS = 14       # старые посты не берём
MAX_CARD_AGE_HOURS = 48  # карточку шлём только по свежему посту: старше двух суток заказ обычно уже закрыт
PAGES_PER_CHANNEL = 3    # глубина пагинации t.me/s (~20 постов на страницу)
SIMILAR_DUP = 0.6        # доля общих четвёрок слов, с которой текст считается дублем более раннего
REMIND_AFTER_HOURS = 24  # через сколько напомнить о взятом лиде, если его этап не менялся
RUN_HOURS = [9, 11, 13, 15, 17, 19, 21]  # расписание прогонов по Москве, как в deploy/leadbot.timer

# Этап 2: наблюдатель за чатами (отдельный аккаунт, только чтение)
CHATS_FILE = ROOT / "chats.txt"
SESSION = ROOT / "watcher"           # файл сессии watcher.session
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
CHAT_MSG_LIMIT = 100     # максимум сообщений из чата за прогон

# Дешёвый префильтр перед LLM: сигналы потребности в автоматизации/ИИ/голосе
KEYWORDS = [
    "бот", "ассистент", "нейросет", "gpt", "автоматизац", "голосов", "звонк", "обзвон",
    "колл-центр", "ivr", "crm", "amocrm", "bitrix", "битрикс", "n8n", "интеграци",
    "заявк", "администратор", "не успева", "теряем", "ищу разработ", "нужен разработ",
    "нужен специалист", "ищем специалист", "автоворонк", "воронк", "рассылк", "парсинг",
    " ии ", " ai ", "искусственн",
]
