"""Общая настройка тестов.

Важно: переменные окружения выставляем ДО импорта app.* — конфиг и engine БД
читаются на импорте. Используем временную SQLite-базу, чтобы ничего не задеть.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="thefootnotes-tests-")
os.environ.setdefault("DATA_DIR", _tmp)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-BOT-TOKEN")
os.environ.setdefault("ALLOWED_USER_IDS", "")  # доступ всем — упрощает тесты
os.environ.setdefault("ADMIN_TELEGRAM_ID", "999")

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Свежая пустая таблица перед каждым тестом (общая тестовая БД)."""
    db.init_db()
    with db.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM reminders")
    yield
