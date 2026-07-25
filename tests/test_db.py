"""Тесты слоя БД: статусы, важность, фильтры списков, сортировка, миграция."""
import sqlite3
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine

from app import db


def _add(title, **kw):
    return db.add_reminder(title=title, category=kw.pop("category", "task"), **kw)


def test_set_status_syncs_done_flag():
    r = _add("задача")
    assert r.status == "todo" and r.done is False

    db.set_status(r.id, "doing")
    assert db.get_reminder(r.id).done is False

    db.set_status(r.id, "done")
    assert db.get_reminder(r.id).done is True

    db.set_status(r.id, "archived")
    assert db.get_reminder(r.id).done is True

    db.set_status(r.id, "todo")
    assert db.get_reminder(r.id).done is False


def test_set_status_rejects_bad_value():
    r = _add("задача")
    with pytest.raises(ValueError):
        db.set_status(r.id, "bogus")


def test_set_importance_clamps():
    r = _add("задача")
    db.set_importance(r.id, 9)
    assert db.get_reminder(r.id).importance == 3
    db.set_importance(r.id, -5)
    assert db.get_reminder(r.id).importance == 1
    db.set_importance(r.id, 2)
    assert db.get_reminder(r.id).importance == 2


def test_list_filters_by_status():
    _add("t", status="todo")
    _add("d", status="doing")
    _add("done", status="done")
    _add("arch", status="archived")

    active = {r.title for r in db.list_active()}
    board = {r.title for r in db.list_board()}
    archived = {r.title for r in db.list_archived()}

    assert active == {"t", "d"}
    assert board == {"t", "d", "done"}
    assert archived == {"arch"}
    # archived нигде, кроме архива
    assert "arch" not in board and "arch" not in active


def test_sort_by_date_then_importance():
    today = date.today()
    _add("через год", event_date=today + timedelta(days=365), importance=3)
    _add("завтра неважное", event_date=today + timedelta(days=1), importance=1)
    _add("завтра важное", event_date=today + timedelta(days=1), importance=3)
    _add("без даты", importance=2)

    order = [r.title for r in db.list_active()]
    # срок первичен: завтрашние выше годового; среди завтрашних — важное выше;
    # без даты — в самом низу.
    assert order.index("завтра важное") < order.index("завтра неважное")
    assert order.index("завтра неважное") < order.index("через год")
    assert order[-1] == "без даты"


def test_migration_backfills_status_from_done(tmp_path, monkeypatch):
    """Старый флаг done=1 должен превратиться в status='done', важность -> 2."""
    old_db = tmp_path / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript(
        """
        CREATE TABLE reminders (
          id INTEGER PRIMARY KEY, title VARCHAR(500), category VARCHAR(50),
          event_date DATE, event_time VARCHAR(20), location VARCHAR(300), notes TEXT,
          source VARCHAR(20), raw_text TEXT, file_path VARCHAR(500), done BOOLEAN,
          chat_id INTEGER, recurrence VARCHAR(50), remind_time VARCHAR(20),
          remind_active BOOLEAN, created_at DATETIME
        );
        """
    )
    con.execute("INSERT INTO reminders (title,done,created_at) VALUES ('старое сделано',1,'2026-01-01 10:00:00')")
    con.execute("INSERT INTO reminders (title,done,created_at) VALUES ('старое активное',0,'2026-01-02 10:00:00')")
    con.commit()
    con.close()

    test_engine = create_engine(f"sqlite:///{old_db}")
    monkeypatch.setattr(db, "engine", test_engine)
    db.init_db()

    rows = {r.title: r for r in db.list_board() + db.list_archived()}
    assert rows["старое сделано"].status == "done"
    assert rows["старое активное"].status == "todo"
    assert rows["старое сделано"].importance == 2  # дефолт для старых строк
