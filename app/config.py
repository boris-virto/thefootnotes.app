"""Конфигурация: читаем настройки из окружения (.env локально, переменные среды в облаке)."""
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

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "remember.db"

# Адрес базы. По умолчанию SQLite-файл на диске.
# Для переезда на Postgres: DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Создаём папки заранее, чтобы остальной код мог просто писать в них.
FILES_DIR.mkdir(parents=True, exist_ok=True)


def user_allowed(user_id: int) -> bool:
    """True, если пользователю разрешено пользоваться ботом."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS
