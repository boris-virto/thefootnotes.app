"""Доступ с телефона: код из бота -> токен устройства. Единственная дверь в API,
поэтому проверяем её тщательно: одноразовость, срок, отзыв, чужой пользователь."""
from datetime import timedelta

import pytest

from app import config, db, tokens

OWNER = 130359870


def test_code_is_readable_and_normalizes_back():
    code = tokens.issue_code(OWNER)
    shown = tokens.format_code(code)
    assert "-" in shown and len(shown) == tokens.CODE_LENGTH + 1
    # Как бы код ни ввели — с дефисом, в нижнем регистре, с пробелами — он должен подойти.
    assert tokens.normalize_code(shown.lower()) == code
    assert tokens.normalize_code(f"  {shown}  ") == code


def test_code_alphabet_has_no_lookalikes():
    """Код читают с экрана: 0/O и 1/I/L спутать слишком легко."""
    assert not set("01OIL") & set(tokens.CODE_ALPHABET)


def test_code_exchanges_for_token():
    code = tokens.issue_code(OWNER, "Борис")
    token, user_id = tokens.redeem_code(tokens.format_code(code), "iPhone")
    assert user_id == OWNER
    assert tokens.verify_token(token) == OWNER
    assert [t.name for t in db.list_device_tokens(OWNER)] == ["iPhone"]


def test_code_works_only_once():
    code = tokens.issue_code(OWNER)
    assert tokens.redeem_code(code, "iPhone") is not None
    assert tokens.redeem_code(code, "iPad") is None, "код обязан быть одноразовым"


def test_new_code_cancels_the_previous_one():
    """Иначе забытый код продолжает жить и остаётся лишней дверью."""
    first = tokens.issue_code(OWNER)
    second = tokens.issue_code(OWNER)
    assert tokens.redeem_code(first, "iPhone") is None
    assert tokens.redeem_code(second, "iPhone") is not None


def test_expired_code_is_refused_and_burned(monkeypatch):
    monkeypatch.setattr(tokens, "CODE_TTL", timedelta(seconds=-1))
    code = tokens.issue_code(OWNER)
    assert tokens.redeem_code(code, "iPhone") is None
    # Просроченный код тоже сжигаем: он не должен «оживать» после правки часов.
    with db.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM pairing_codes").scalar() == 0


def test_wrong_code_gives_nothing():
    tokens.issue_code(OWNER)
    assert tokens.redeem_code("ZZZZ-ZZZZ", "iPhone") is None
    assert tokens.redeem_code("", "iPhone") is None
    assert tokens.redeem_code("ABC", "iPhone") is None  # короче нужной длины


def test_token_is_not_stored_in_plaintext():
    code = tokens.issue_code(OWNER)
    token, _ = tokens.redeem_code(code, "iPhone")
    with db.engine.begin() as conn:
        stored = conn.exec_driver_sql("SELECT token_hash FROM device_tokens").scalar()
    assert token not in stored and len(stored) == 64


def test_unknown_token_is_refused():
    assert tokens.verify_token("совсем-не-токен") is None
    assert tokens.verify_token("") is None


def test_revoked_token_stops_working():
    code = tokens.issue_code(OWNER)
    token, _ = tokens.redeem_code(code, "iPhone")
    device = db.list_device_tokens(OWNER)[0]
    assert db.revoke_device_token(OWNER, device.id) is True
    assert tokens.verify_token(token) is None
    assert db.revoke_device_token(OWNER, device.id) is False  # повторно нечего отзывать


def test_cannot_revoke_someone_elses_device():
    code = tokens.issue_code(OWNER)
    tokens.redeem_code(code, "iPhone")
    device = db.list_device_tokens(OWNER)[0]
    assert db.revoke_device_token(OWNER + 1, device.id) is False


def test_token_dies_when_owner_loses_access(monkeypatch):
    """Убрали id из ALLOWED_USER_IDS — выданные токены обязаны перестать работать."""
    code = tokens.issue_code(OWNER)
    token, _ = tokens.redeem_code(code, "iPhone")
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {OWNER + 1})
    assert tokens.verify_token(token) is None


def test_code_of_disallowed_user_is_refused(monkeypatch):
    code = tokens.issue_code(OWNER)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {OWNER + 1})
    assert tokens.redeem_code(code, "iPhone") is None


def test_consume_code_returns_name_for_session_login():
    code = tokens.issue_code(OWNER, "Борис")
    assert tokens.consume_code(tokens.format_code(code)) == (OWNER, "Борис")


@pytest.mark.parametrize("name", [None, ""])
def test_code_without_name_is_fine(name):
    code = tokens.issue_code(OWNER, name)
    assert tokens.consume_code(code) == (OWNER, None)
