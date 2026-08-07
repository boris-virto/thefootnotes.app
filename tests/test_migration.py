"""Миграция существующей базы. На сервере init_db() запускается на живом файле с
реальными карточками, поэтому проверяем именно этот путь: колонки добавляются, данные
не теряются, а новые поля заполняются осмысленно."""
import sqlite3

import pytest
from sqlalchemy import create_engine

from app import db

# Схема самой первой версии: ни status/importance/url/updated_at, ни таблицы вложений.
LEGACY_SCHEMA = """
CREATE TABLE reminders (
  id INTEGER NOT NULL PRIMARY KEY,
  title VARCHAR(500) NOT NULL, category VARCHAR(50), event_date DATE, event_time VARCHAR(20),
  location VARCHAR(300), notes TEXT, source VARCHAR(20), raw_text TEXT,
  file_path VARCHAR(500), done BOOLEAN, created_at DATETIME
);
INSERT INTO reminders (title, category, source, raw_text, file_path, done, created_at) VALUES
 ('Старый билет','ticket','pdf','бронь https://old.example/x?a=1&b=2','/old/t.pdf',0,
  '2025-11-02 08:00:00'),
 ('Уже сделано','task','text','просто дело',NULL,1,'2025-11-03 09:30:00');
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """Отдельная база со старой схемой: подменяем engine, чтобы не задеть общую тестовую."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    return path


def test_migration_keeps_data_and_fills_new_columns(legacy_db):
    db.init_db()

    board = {r.title: r for r in db.list_board()}
    assert set(board) == {"Старый билет", "Уже сделано"}, "ни одна карточка не должна пропасть"

    # Старый флаг done -> статус доски.
    assert board["Уже сделано"].status == "done"
    assert board["Старый билет"].status == "todo"
    # Важность получает значение по умолчанию, а не None.
    assert board["Старый билет"].importance == db.IMPORTANCE_NORMAL
    # Ссылку восстанавливаем из сохранённого текста сообщения.
    assert board["Старый билет"].url == "https://old.example/x?a=1&b=2"
    # Одиночный file_path переносится в таблицу вложений.
    assert board["Старый билет"].file_paths == ["/old/t.pdf"]


def test_migration_leaves_no_empty_updated_at(legacy_db):
    """updated_at — курсор синхронизации для телефона: NULL сломал бы выборку изменений."""
    db.init_db()
    with db.engine.begin() as conn:
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM reminders WHERE updated_at IS NULL"
        ).scalar() == 0

    # Нетронутая карточка получает updated_at = created_at, а не «сейчас».
    untouched = [r for r in db.list_board() if r.title == "Уже сделано"][0]
    assert untouched.updated_at == untouched.created_at


def test_migration_creates_device_tables(legacy_db):
    db.init_db()
    with db.engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"attachments", "device_tokens", "pairing_codes"} <= tables


def test_migration_is_idempotent(legacy_db):
    """Рестарт сервиса запускает init_db() снова — второй проход не должен ничего портить."""
    db.init_db()
    first = {r.title: r.file_paths for r in db.list_board()}
    db.init_db()
    assert {r.title: r.file_paths for r in db.list_board()} == first
    assert len(db.list_board()) == 2, "повторный backfill не должен дублировать вложения"
