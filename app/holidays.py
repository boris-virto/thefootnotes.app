"""Детерминированный расчёт дат «плавающих» праздников.

LLM только ОПОЗНАЁТ, какой это праздник, и возвращает его ключ (см. ``RESOLVERS``).
Точную григорианскую дату ближайшего будущего вычисляет код — потому что лунно-
солнечные праздники модель «помнит» с ошибкой в неделю-полторы (проверено: Рош-а-Шана
и Йом-Кипур она датировала на ~11 дней и год мимо).

- Еврейские — pyluach (точно, чистый Python).
- Исламские — hijridate (таблица Umm al-Qura). Реальная дата может сдвинуться на
  ±1 день из-за наблюдения луны — такие праздники помечены как приблизительные.
- Христианская Пасха — арифметика (западная: алгоритм Мьюса/Батчера; православная:
  юлианская с поправкой +13 дней, верно для 1900–2099).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def _nearest_future(candidates: list[date], today: date) -> date | None:
    future = sorted(d for d in candidates if d >= today)
    return future[0] if future else None


# --- Еврейский календарь (pyluach) ---------------------------------------
# Нумерация месяцев pyluach: религиозный год с Нисана=1 … Тишрей=7 … Адар=12
# (в високосный год добавляется Адар II = месяц 13).

def _hebrew(month: int, day: int, *, leap_month: int | None = None):
    """Резолвер еврейского праздника. ``leap_month`` — номер месяца в високосный
    год (для Пурима: 14 Адара переезжает из месяца 12 в Адар II = 13)."""

    def resolve(today: date) -> date | None:
        from pyluach import dates, hebrewcal

        base = dates.GregorianDate(today.year, today.month, today.day).to_heb().year
        candidates = []
        for hy in (base - 1, base, base + 1, base + 2):
            m = month
            if leap_month is not None and hebrewcal.Year(hy).leap:
                m = leap_month
            candidates.append(dates.HebrewDate(hy, m, day).to_pydate())
        return _nearest_future(candidates, today)

    return resolve


# --- Исламский календарь (hijridate) -------------------------------------

def _islamic(month: int, day: int):
    def resolve(today: date) -> date | None:
        from hijridate import Gregorian, Hijri

        base = Gregorian(today.year, today.month, today.day).to_hijri().year
        candidates = []
        for hy in (base - 1, base, base + 1):
            g = Hijri(hy, month, day).to_gregorian()
            candidates.append(date(g.year, g.month, g.day))
        return _nearest_future(candidates, today)

    return resolve


# --- Христианская Пасха (арифметика, без зависимостей) --------------------

def _easter_western_year(y: int) -> date:
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _easter_orthodox_year(y: int) -> date:
    a = y % 19
    b = y % 7
    c = y % 4
    d = (19 * a + 15) % 30
    e = (2 * c + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    julian = date(y, month, day)
    return julian + timedelta(days=13)  # юлианский → григорианский сдвиг (1900–2099)


def _easter(calc):
    def resolve(today: date) -> date | None:
        return _nearest_future([calc(today.year), calc(today.year + 1)], today)

    return resolve


# Ключи, которые понимает система. Их же перечисляем модели в промпте.
RESOLVERS = {
    # еврейские
    "rosh_hashanah": _hebrew(7, 1),    # 1 Тишрея
    "yom_kippur": _hebrew(7, 10),      # 10 Тишрея
    "sukkot": _hebrew(7, 15),          # 15 Тишрея
    "hanukkah": _hebrew(9, 25),        # 25 Кислева
    "tu_bishvat": _hebrew(11, 15),     # 15 Швата
    "purim": _hebrew(12, 14, leap_month=13),  # 14 Адара (Адар II в високосный)
    "passover": _hebrew(1, 15),        # 15 Нисана (Песах)
    "lag_baomer": _hebrew(2, 18),      # 18 Ияра
    "shavuot": _hebrew(3, 6),          # 6 Сивана
    # исламские (Umm al-Qura; ±1 день)
    "islamic_new_year": _islamic(1, 1),   # 1 Мухаррама
    "ashura": _islamic(1, 10),            # 10 Мухаррама
    "mawlid": _islamic(3, 12),            # 12 Раби аль-авваль
    "ramadan_start": _islamic(9, 1),      # 1 Рамадана
    "eid_al_fitr": _islamic(10, 1),       # 1 Шавваля (Ураза-байрам)
    "eid_al_adha": _islamic(12, 10),      # 10 Зуль-хиджа (Курбан-байрам)
    # христианские
    "easter_western": _easter(_easter_western_year),
    "easter_orthodox": _easter(_easter_orthodox_year),
}

# Праздники, чья вычисленная дата может разойтись с фактической на ±1 день
# (исламские зависят от реального наблюдения луны).
APPROXIMATE = {
    "islamic_new_year", "ashura", "mawlid",
    "ramadan_start", "eid_al_fitr", "eid_al_adha",
}


def resolve(key: str, today: date) -> date | None:
    """Ближайшая будущая (или сегодняшняя) григорианская дата праздника.

    Возвращает None для неизвестного ключа (модель могла выдумать) — тогда вызывающий
    код оставляет дату как есть."""
    fn = RESOLVERS.get(key)
    if fn is None:
        logger.warning("Неизвестный ключ праздника от модели: %r", key)
        return None
    try:
        return fn(today)
    except Exception:
        logger.exception("Не смог вычислить дату праздника %r", key)
        return None


def is_approximate(key: str) -> bool:
    return key in APPROXIMATE
