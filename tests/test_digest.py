"""Тесты логики дайджеста: корзины рубежей, отбор и формирование текста."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import bot

TODAY = date(2026, 7, 24)


def _rem(title="t", days=None, importance=2, milestone=None, category="ticket"):
    ed = TODAY + timedelta(days=days) if days is not None else None
    return SimpleNamespace(
        id=abs(hash(title)) % 100000, title=title, category=category,
        event_date=ed, event_time=None, location=None,
        importance=importance, digest_milestone=milestone,
    )


@pytest.mark.parametrize("days,expected", [
    (1, 30), (30, 30), (31, 60), (60, 60), (61, 90), (90, 90), (91, None), (365, None),
])
def test_digest_bucket(days, expected):
    assert bot._digest_bucket(days) == expected


def test_far_future_hidden():
    show, ms = bot._auto_digest_decision(_rem(days=319), TODAY)
    assert show is False and ms is None


def test_daily_window_always_shows():
    show, ms = bot._auto_digest_decision(_rem(days=10), TODAY)
    assert show is True and ms == 30


def test_milestone_shown_once():
    # первый заход в окно 90 дней — показать и запомнить рубеж
    show, ms = bot._auto_digest_decision(_rem(days=75, milestone=None), TODAY)
    assert show is True and ms == 90
    # рубеж уже отправлен — больше не показывать
    show, ms = bot._auto_digest_decision(_rem(days=74, milestone=90), TODAY)
    assert show is False and ms == 90


def test_overdue_today_undated_always_shown():
    for r in (_rem(days=-3), _rem(days=0), _rem(days=None)):
        show, _ = bot._auto_digest_decision(r, TODAY)
        assert show is True


def test_format_digest_sections_and_empty():
    assert bot._format_digest([], TODAY) is None
    text = bot._format_digest(
        [_rem("сегодня", days=0), _rem("просрочено", days=-2),
         _rem("скоро", days=5), _rem("без даты", days=None)],
        TODAY,
    )
    assert "📌 Сегодня" in text
    assert "⚠️ Просрочено" in text
    assert "🗓 Скоро" in text
    assert "📝 Без даты" in text


def test_missed_day_still_fires_milestone():
    """Если рассылка пропущена в день ровно-90, рубеж срабатывает на след. заходе."""
    r = _rem(days=89, milestone=None)  # день 90 пропущен, сегодня 89
    show, ms = bot._auto_digest_decision(r, TODAY)
    assert show is True and ms == 90
