"""Тесты проверки подписи входа через Telegram Login Widget."""
import hashlib
import hmac
import time

from app.auth import AUTH_MAX_AGE_SECONDS, verify_telegram_login

TOKEN = "123456:TEST-BOT-TOKEN"


def _sign(data: dict, token: str = TOKEN) -> dict:
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    secret = hashlib.sha256(token.encode()).digest()
    data = dict(data)
    data["hash"] = hmac.new(secret, "\n".join(pairs).encode(), hashlib.sha256).hexdigest()
    return data


def test_valid_signature_passes():
    data = _sign({"id": "130359870", "first_name": "Boris", "auth_date": str(int(time.time()))})
    assert verify_telegram_login(data, TOKEN) is True


def test_tampered_data_fails():
    data = _sign({"id": "130359870", "auth_date": str(int(time.time()))})
    data["id"] = "999"  # подменили после подписи
    assert verify_telegram_login(data, TOKEN) is False


def test_missing_hash_fails():
    assert verify_telegram_login({"id": "1", "auth_date": str(int(time.time()))}, TOKEN) is False


def test_wrong_token_fails():
    data = _sign({"id": "1", "auth_date": str(int(time.time()))}, token="OTHER-TOKEN")
    assert verify_telegram_login(data, TOKEN) is False


def test_expired_auth_date_fails():
    old = int(time.time()) - AUTH_MAX_AGE_SECONDS - 10
    data = _sign({"id": "1", "auth_date": str(old)})
    assert verify_telegram_login(data, TOKEN) is False


def test_empty_token_fails():
    data = _sign({"id": "1", "auth_date": str(int(time.time()))})
    assert verify_telegram_login(data, "") is False
