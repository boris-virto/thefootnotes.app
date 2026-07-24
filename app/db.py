"""Слой хранения: SQLite + SQLAlchemy. Одна таблица напоминаний."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import String, Text, Date, DateTime, Boolean, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False)

# Статусы карточки на доске задач. archived не показывается на доске и в дайджесте.
ACTIVE_STATUSES = ("todo", "doing")          # то, что идёт в дайджест
BOARD_STATUSES = ("todo", "doing", "done")   # три колонки доски
VALID_STATUSES = ("todo", "doing", "done", "archived")

# Уровни важности: 1 — низкая, 2 — обычная, 3 — высокая.
IMPORTANCE_LOW, IMPORTANCE_NORMAL, IMPORTANCE_HIGH = 1, 2, 3


class Base(DeclarativeBase):
    pass


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Структурированные поля, которые извлекает LLM.
    title: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(50), default="note")
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Служебные поля.
    source: Mapped[str] = mapped_column(String(20), default="text")  # text|voice|photo|pdf
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # исходный текст/транскрипт
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # сохранённый файл

    # Статус на доске задач и приоритет. done оставлен для обратной совместимости и
    # держится синхронным со status (done := status in {done, archived}).
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo|doing|done|archived
    importance: Mapped[int] = mapped_column(default=IMPORTANCE_NORMAL)  # 1..3
    done: Mapped[bool] = mapped_column(Boolean, default=False)

    # Регулярные напоминания.
    chat_id: Mapped[int | None] = mapped_column(nullable=True)  # куда слать пинг в Telegram
    recurrence: Mapped[str | None] = mapped_column(String(50), nullable=True)  # daily|weekly:mon|monthly:15
    remind_time: Mapped[str | None] = mapped_column(String(20), nullable=True)  # HH:MM
    remind_active: Mapped[bool] = mapped_column(Boolean, default=True)  # пинги включены?

    # Последний отправленный рубеж дайджеста для будущего события: 90/60/30 дней
    # (None — ещё ни одного). Нужно, чтобы «разовые» напоминания за 3 и 2 месяца
    # не терялись при пропущенной рассылке и не повторялись каждый день.
    digest_milestone: Mapped[int | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Простая миграция: добавляем недостающие колонки в существующую таблицу,
    не теряя уже сохранённые напоминания. Только для SQLite — на Postgres
    для этого подключим Alembic."""
    if engine.dialect.name != "sqlite":
        return
    new_columns = {
        "chat_id": "INTEGER",
        "recurrence": "VARCHAR(50)",
        "remind_time": "VARCHAR(20)",
        "remind_active": "BOOLEAN DEFAULT 1",
        "digest_milestone": "INTEGER",
        "status": "VARCHAR(20) DEFAULT 'todo'",
        "importance": "INTEGER DEFAULT 2",
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reminders)")}
        added = []
        for name, ddl in new_columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE reminders ADD COLUMN {name} {ddl}")
                added.append(name)
        # Первичное заполнение status из старого флага done: выполненные -> 'done'.
        if "status" in added:
            conn.exec_driver_sql("UPDATE reminders SET status='done' WHERE done=1")


def add_reminder(**kwargs) -> Reminder:
    with Session(engine) as session:
        reminder = Reminder(**kwargs)
        session.add(reminder)
        session.commit()
        session.refresh(reminder)
        return reminder


def sort_reminders(items: list[Reminder]) -> list[Reminder]:
    """Порядок для доски и списков: сначала по сроку (ближайшие/просроченные сверху,
    без даты — вниз), при равном сроке — по важности (выше сверху), затем новые выше.
    Сортировка стабильная, поэтому применяем ключи по нарастанию приоритета."""
    items = sorted(items, key=lambda r: r.created_at, reverse=True)
    items.sort(key=lambda r: (r.event_date is None, r.event_date or date.max, -r.importance))
    return items


def _list_by_status(statuses: tuple[str, ...]) -> list[Reminder]:
    with Session(engine) as session:
        stmt = select(Reminder).where(Reminder.status.in_(statuses))
        return sort_reminders(list(session.scalars(stmt)))


def list_reminders(include_done: bool = False) -> list[Reminder]:
    """Активные напоминания (todo + в процессе); include_done добавляет колонку «сделано»."""
    return _list_by_status(BOARD_STATUSES if include_done else ACTIVE_STATUSES)


def list_active() -> list[Reminder]:
    """Только незавершённые (todo + в процессе) — для дайджеста и списка в боте."""
    return _list_by_status(ACTIVE_STATUSES)


def list_board() -> list[Reminder]:
    """Все три колонки доски (без архива)."""
    return _list_by_status(BOARD_STATUSES)


def list_archived() -> list[Reminder]:
    with Session(engine) as session:
        stmt = (
            select(Reminder)
            .where(Reminder.status == "archived")
            .order_by(Reminder.created_at.desc())
        )
        return list(session.scalars(stmt))


def set_status(reminder_id: int, status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Недопустимый статус: {status}")
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.status = status
            reminder.done = status in ("done", "archived")
            session.commit()


def set_importance(reminder_id: int, importance: int) -> None:
    importance = max(IMPORTANCE_LOW, min(IMPORTANCE_HIGH, int(importance)))
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.importance = importance
            session.commit()


def mark_done(reminder_id: int) -> None:
    set_status(reminder_id, "done")


def get_reminder(reminder_id: int) -> Reminder | None:
    with Session(engine) as session:
        return session.get(Reminder, reminder_id)


def list_active_recurring() -> list[Reminder]:
    """Все включённые регулярные напоминания — их надо (пере)ставить в расписание."""
    with Session(engine) as session:
        stmt = select(Reminder).where(
            Reminder.recurrence.is_not(None),
            Reminder.remind_active == True,  # noqa: E712
        )
        return list(session.scalars(stmt))


def list_upcoming_events() -> list[Reminder]:
    """Невыполненные напоминания с будущей датой события и известным чатом —
    для них надо (пере)ставить пинг «за день до» после перезапуска."""
    today = date.today()
    with Session(engine) as session:
        stmt = select(Reminder).where(
            Reminder.event_date.is_not(None),
            Reminder.status.in_(ACTIVE_STATUSES),
            Reminder.chat_id.is_not(None),
            Reminder.event_date >= today,
        )
        return list(session.scalars(stmt))


def list_with_files() -> list[Reminder]:
    """Напоминания, у которых есть сохранённый файл (фото/PDF) — их можно скачать."""
    with Session(engine) as session:
        stmt = (
            select(Reminder)
            .where(Reminder.file_path.is_not(None))
            .order_by(Reminder.created_at.desc())
        )
        return list(session.scalars(stmt))


def set_remind_active(reminder_id: int, active: bool) -> None:
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.remind_active = active
            session.commit()


def set_digest_milestone(reminder_id: int, milestone: int | None) -> None:
    """Запоминает последний отправленный рубеж дайджеста для события."""
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.digest_milestone = milestone
            session.commit()
