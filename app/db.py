"""Слой хранения: SQLite + SQLAlchemy. Одна таблица напоминаний."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import String, Text, Date, DateTime, Boolean, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False)


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
    done: Mapped[bool] = mapped_column(Boolean, default=False)

    # Регулярные напоминания.
    chat_id: Mapped[int | None] = mapped_column(nullable=True)  # куда слать пинг в Telegram
    recurrence: Mapped[str | None] = mapped_column(String(50), nullable=True)  # daily|weekly:mon|monthly:15
    remind_time: Mapped[str | None] = mapped_column(String(20), nullable=True)  # HH:MM
    remind_active: Mapped[bool] = mapped_column(Boolean, default=True)  # пинги включены?

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
    }
    with engine.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reminders)")}
        for name, ddl in new_columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE reminders ADD COLUMN {name} {ddl}")


def add_reminder(**kwargs) -> Reminder:
    with Session(engine) as session:
        reminder = Reminder(**kwargs)
        session.add(reminder)
        session.commit()
        session.refresh(reminder)
        return reminder


def list_reminders(include_done: bool = False) -> list[Reminder]:
    with Session(engine) as session:
        stmt = select(Reminder)
        if not include_done:
            stmt = stmt.where(Reminder.done == False)  # noqa: E712
        # Сначала те, у кого есть дата (по возрастанию), затем всё остальное по дате создания.
        stmt = stmt.order_by(Reminder.event_date.is_(None), Reminder.event_date, Reminder.created_at.desc())
        return list(session.scalars(stmt))


def mark_done(reminder_id: int) -> None:
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.done = True
            session.commit()


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
            Reminder.done == False,  # noqa: E712
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
