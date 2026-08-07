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
    """Свежие пустые таблицы перед каждым тестом (общая тестовая БД).

    Вложения чистим тоже: SQLite переиспользует id после опустошения таблицы, и
    оставшиеся строки attachments прилипли бы к новым карточкам."""
    db.init_db()
    with db.engine.begin() as conn:
        for table in ("attachments", "reminders", "device_tokens", "pairing_codes"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    yield


@pytest.fixture(autouse=True)
def no_scheduler():
    """Планировщик напоминаний живёт в боте; в тестах его нет — сбрасываем, чтобы
    хук, поставленный одним тестом, не протёк в остальные."""
    from app import ingest

    ingest.set_scheduler(None)
    yield
    ingest.set_scheduler(None)
