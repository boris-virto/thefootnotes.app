"""Ссылки — самое ценное в карточке (бронь, билет), поэтому достаём их кодом."""
import pytest
from sqlalchemy import text

from app import bot, db, ingest, links


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.com/page", "https://example.com/page"),
        ("текст до https://example.com/page и после", "https://example.com/page"),
        # хвостовая пунктуация не должна попадать в ссылку
        ("смотри тут: https://example.com/page.", "https://example.com/page"),
        ("(https://example.com/page)", "https://example.com/page"),
        ("«https://example.com/page»", "https://example.com/page"),
        # query-параметры сохраняем целиком
        ("бронь https://ex.com/b?a=1&b=2 ждёт", "https://ex.com/b?a=1&b=2"),
        ("http://plain.example", "http://plain.example"),
        ("нет ссылки вовсе", None),
        ("", None),
        (None, None),
    ],
)
def test_first_url(raw, expected):
    assert links.first_url(raw) == expected


def test_find_urls_returns_all_in_order():
    found = links.find_urls("раз https://a.example два https://b.example")
    assert found == ["https://a.example", "https://b.example"]


def test_pdf_link_branch_still_detected():
    """Ссылка на PDF по-прежнему уходит в отдельную ветку разбора."""
    urls = links.find_urls("вот билет https://ex.com/ticket.pdf смотри")
    assert [u for u in urls if links.is_pdf_url(u)] == ["https://ex.com/ticket.pdf"]


def test_is_pdf_url():
    assert links.is_pdf_url("https://ex.com/ticket.pdf")
    assert links.is_pdf_url("https://ex.com/T.PDF?x=1")
    assert not links.is_pdf_url("https://ex.com/page")


def test_saved_reminder_keeps_url_from_raw_text():
    """Регрессия: ссылка из сообщения должна попасть в карточку."""
    from app.llm import ExtractedReminder

    raw = ("Напомни забронировать это на один из выходных в августе\n\n"
           "https://www.belgradeturtle.com/service-page/belgrade-sunset-cruise")
    reminder = ingest.save_extracted(
        ExtractedReminder(title="Забронировать круиз на закате"),
        source="text", raw_text=raw, chat_id=1,
    )
    assert reminder.url == "https://www.belgradeturtle.com/service-page/belgrade-sunset-cruise"


def test_saved_reminder_without_link_has_no_url():
    from app.llm import ExtractedReminder

    reminder = ingest.save_extracted(
        ExtractedReminder(title="Купить хлеб"),
        source="text", raw_text="купить хлеб", chat_id=1,
    )
    assert reminder.url is None


def test_backfill_recovers_url_from_old_rows():
    """Карточки, сохранённые до появления поля url, восстанавливаем из raw_text."""
    with db.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO reminders (title, category, status, importance, done, "
                 "source, raw_text, url, remind_active, created_at, updated_at) "
                 "VALUES ('старая', 'task', 'todo', 2, 0, 'text', "
                 "'бронь https://old.example/x?a=1&b=2 тут', NULL, 1, "
                 "'2026-01-01 00:00:00', '2026-01-01 00:00:00')")
        )
    db._backfill_urls()
    saved = [r for r in db.list_board() if r.title == "старая"]
    assert saved and saved[0].url == "https://old.example/x?a=1&b=2"


def test_backfill_does_not_touch_existing_url():
    from app.llm import ExtractedReminder

    reminder = ingest.save_extracted(
        ExtractedReminder(title="есть ссылка"),
        source="text", raw_text="a https://first.example b https://second.example",
        chat_id=1,
    )
    assert reminder.url == "https://first.example"
    db._backfill_urls()
    assert db.get_reminder(reminder.id).url == "https://first.example"


def test_rendered_lines_include_url_and_escape_ampersand():
    """В HTML-сообщениях «&» обязан быть экранирован, иначе Telegram отвергнет текст."""
    from app.llm import ExtractedReminder
    from datetime import date

    reminder = ingest.save_extracted(
        ExtractedReminder(title="Круиз", event_date="2026-08-15"),
        source="text", raw_text="бронь https://ex.com/b?a=1&b=2", chat_id=1,
    )
    for rendered in (
        bot._confirmation(reminder),
        bot._list_line(reminder),
        bot._digest_line(reminder),
    ):
        assert "https://ex.com/b?a=1&amp;b=2" in rendered
        assert "?a=1&b=2" not in rendered  # «сырого» амперсанда быть не должно
