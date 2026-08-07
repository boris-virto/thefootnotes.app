"""Доступ для устройств: код спаривания из бота -> долгий токен клиента.

Зачем отдельный механизм, если у дашборда уже есть вход по Telegram: Login Widget — это
редирект через oauth.telegram.org, то есть чужая веб-страница посреди входа. В нативном
клиенте так авторизоваться нечем: ни куки, ни редиректа туда не дотянуть. Поэтому
владелец просит у бота код (`/pair`), вводит его один раз при первом запуске, и клиент
обменивает код на токен, который дальше ходит в заголовке Authorization.

В базе лежат только sha256-хэши: утечка базы не даёт ни кода, ни токена.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from . import db
from .config import user_allowed

# Алфавит кода без похожих друг на друга символов (нет 0/O, 1/I/L) — код читают
# с экрана и набирают руками. 32^8 ≈ 1.1e12 вариантов при времени жизни 10 минут.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL = timedelta(minutes=10)

TOKEN_BYTES = 32


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def format_code(code: str) -> str:
    """ABCDEFGH -> ABCD-EFGH: так код проще прочитать и продиктовать."""
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_code(raw: str) -> str:
    """Приводит введённый код к каноническому виду: без дефисов и пробелов, в верхнем
    регистре. Всё, чего нет в алфавите, отбрасываем — иначе автозамена на телефоне
    (лишний пробел, «умный» дефис) ломала бы верный код."""
    return "".join(c for c in (raw or "").upper() if c in CODE_ALPHABET)


def issue_code(user_id: int, user_name: str | None = None) -> str:
    """Выдаёт новый код спаривания (прежние коды этого пользователя отменяются)."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    db.create_pairing_code(user_id, _sha256(code), db.utcnow() + CODE_TTL, user_name)
    return code


def consume_code(raw_code: str) -> tuple[int, str | None] | None:
    """Сжигает код и возвращает (user_id, имя) — или None, если он не подошёл.

    Код одноразовый: неудачная попытка с верным, но просроченным кодом тоже его сжигает.
    """
    code = normalize_code(raw_code)
    if len(code) != CODE_LENGTH:
        return None
    found = db.consume_pairing_code(_sha256(code))
    if found is None or not user_allowed(found[0]):
        return None
    return found


def redeem_code(raw_code: str, device_name: str) -> tuple[str, int] | None:
    """Обменивает код на долгий токен устройства. Возвращает (токен, user_id) или None."""
    found = consume_code(raw_code)
    if found is None:
        return None
    user_id, _ = found
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.create_device_token(user_id, _sha256(token), device_name or "устройство")
    return token, user_id


def verify_token(token: str) -> int | None:
    """user_id владельца токена, или None если токен неизвестен/доступ отозван."""
    if not token:
        return None
    user_id = db.touch_device_token(_sha256(token))
    if user_id is None or not user_allowed(user_id):
        return None
    return user_id
