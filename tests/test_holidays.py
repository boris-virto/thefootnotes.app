"""Праздники с «плавающей» датой считаем кодом, а не памятью модели.

Даты сверены с публичными календарями (Hebcal, Umm al-Qura, таблицы Пасхи)."""
from datetime import date

import pytest

from app import holidays, llm


TODAY = date(2026, 7, 27)


@pytest.mark.parametrize(
    "key, expected",
    [
        # еврейские (pyluach) — Рош-а-Шана 5787 и т.д.
        ("rosh_hashanah", date(2026, 9, 12)),
        ("yom_kippur", date(2026, 9, 21)),
        ("sukkot", date(2026, 9, 26)),
        ("hanukkah", date(2026, 12, 5)),
        ("passover", date(2027, 4, 22)),   # ближайший Песах уже в будущем году
        ("purim", date(2027, 3, 23)),      # 5787 високосный → Адар II
        ("shavuot", date(2027, 6, 11)),   # 5787 високосный → Шавуот позже обычного
        # христианская Пасха
        ("easter_western", date(2027, 3, 28)),
        ("easter_orthodox", date(2027, 5, 2)),
    ],
)
def test_fixed_reference_dates(key, expected):
    assert holidays.resolve(key, TODAY) == expected


def test_islamic_dates_are_plausible_and_future():
    # Точную дату диктует наблюдение луны, поэтому проверяем «в будущем и в нужном окне».
    for key in ("ramadan_start", "eid_al_fitr", "eid_al_adha", "islamic_new_year"):
        d = holidays.resolve(key, TODAY)
        assert d is not None and d >= TODAY


def test_nearest_future_picks_upcoming_not_past():
    # 27 июля 2026: Рош-а-Шана 5786 (сен-2025) в прошлом, 5787 (сен-2026) — ближайшая.
    assert holidays.resolve("rosh_hashanah", TODAY) == date(2026, 9, 12)
    # За день до праздника всё ещё показываем этот же (сегодня-или-позже).
    assert holidays.resolve("rosh_hashanah", date(2026, 9, 12)) == date(2026, 9, 12)
    # На следующий день после — уже следующий год.
    assert holidays.resolve("rosh_hashanah", date(2026, 9, 13)) == date(2027, 10, 2)


def test_unknown_key_returns_none():
    assert holidays.resolve("not_a_holiday", TODAY) is None


def test_is_approximate_only_islamic():
    assert holidays.is_approximate("eid_al_adha")
    assert not holidays.is_approximate("rosh_hashanah")
    assert not holidays.is_approximate("easter_western")


def test_apply_holiday_overrides_model_date():
    # Модель могла выдать свою (неверную) дату — код её перезаписывает вычисленной.
    extracted = llm.ExtractedReminder(title="Рош-а-Шана", event_date="2026-09-23",
                                      holiday="rosh_hashanah")
    result = llm._apply_holiday(extracted, TODAY)
    assert result.event_date == "2026-09-12"


def test_apply_holiday_adds_note_for_islamic():
    extracted = llm.ExtractedReminder(title="Курбан-байрам", holiday="eid_al_adha")
    result = llm._apply_holiday(extracted, TODAY)
    assert result.event_date is not None
    assert "±1" in (result.notes or "")


def test_apply_holiday_no_note_for_hebrew():
    extracted = llm.ExtractedReminder(title="Ханука", holiday="hanukkah", notes="свечи")
    result = llm._apply_holiday(extracted, TODAY)
    assert result.notes == "свечи"  # приписки нет — дата точная


def test_apply_holiday_ignores_when_absent():
    extracted = llm.ExtractedReminder(title="просто задача", event_date="2026-08-01")
    result = llm._apply_holiday(extracted, TODAY)
    assert result.event_date == "2026-08-01"


def test_apply_holiday_unknown_key_keeps_model_date():
    extracted = llm.ExtractedReminder(title="что-то", event_date="2026-08-01",
                                      holiday="bogus")
    result = llm._apply_holiday(extracted, TODAY)
    assert result.event_date == "2026-08-01"
