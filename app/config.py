"""Конфигурация: читаем настройки из окружения (.env локально, переменные среды в облаке)."""
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Часовой пояс для времени напоминаний (IANA, напр. Europe/Belgrade, Europe/Moscow).
TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")

# Список разрешённых telegram user id. Пусто -> доступ всем.
_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x.strip()} if _allowed else set()

# Утренний дайджест: во сколько слать список напоминаний (HH:MM в TIMEZONE).
DIGEST_TIME = os.getenv("DIGEST_TIME", "09:00")
# Куда слать дайджест. Обычно не нужно: если пусто и chat_id один — берётся он.
# В личном чате Telegram chat_id совпадает с твоим user id.
_digest_chat = os.getenv("DIGEST_CHAT_ID", "").strip()
DIGEST_CHAT_ID = int(_digest_chat) if _digest_chat else None

# Секрет для подписи сессионной куки веб-дашборда. Если не задан — выводим из токена
# бота, чтобы всё работало без лишней настройки (сессии переживают рестарт, пока токен
# тот же). Хочешь независимый секрет — задай SESSION_SECRET в окружении.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip() or hashlib.sha256(
    f"session:{TELEGRAM_BOT_TOKEN}".encode()
).hexdigest()

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "thefootnotes.db"

# Адрес базы. По умолчанию SQLite-файл на диске.
# Для переезда на Postgres: DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Создаём папки заранее, чтобы остальной код мог просто писать в них.
FILES_DIR.mkdir(parents=True, exist_ok=True)


def user_allowed(user_id: int) -> bool:
    """True, если пользователю разрешено пользоваться ботом."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS
