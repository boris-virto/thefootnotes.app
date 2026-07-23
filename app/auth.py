"""Проверка входа через Telegram Login Widget.

Telegram присылает данные пользователя (id, first_name, username, photo_url,
auth_date) и поле hash — HMAC-SHA256 от остальных полей на ключе sha256(bot_token).
Пересчитываем hash и сверяем: так убеждаемся, что данные действительно от Telegram,
а не подделаны. См. https://core.telegram.org/widgets/login#checking-authorization
"""
from __future__ import annotations

import hashlib
import hmac
import time

# Данные логина считаем протухшими через сутки — защита от повторного использования.
AUTH_MAX_AGE_SECONDS = 24 * 60 * 60


def verify_telegram_login(data: dict[str, str], bot_token: str) -> bool:
    """True, если подпись Telegram верна и данные не просрочены."""
    received_hash = data.get("hash")
    if not received_hash or not bot_token:
        return False

    # Строка проверки: все поля кроме hash, отсортированы, вида key=value через \n.
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    data_check_string = "\n".join(pairs)

    secret_key = hashlib.sha256(bot_token.encode()).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        return False

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return False
    if time.time() - auth_date > AUTH_MAX_AGE_SECONDS:
        return False

    return True
